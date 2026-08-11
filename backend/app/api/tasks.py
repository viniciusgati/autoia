"""Endpoints de tarefas (criar, iniciar, revisar, retry, PM)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import (
    STEP_BLOCKED,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_PENDING,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_PAUSED,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Pipeline,
    Repository,
    RunEvent,
    SubTask,
    Task,
    TaskProposal,
    TaskStep,
)
from ..schemas import (
    ApproveStepRequest,
    BlockedContinueRequest,
    BouncebackRequest,
    FeedbackCreate,
    RetryRequest,
    ReviewRequest,
    TaskCreate,
    TaskOut,
    TaskProposalOut,
    TaskSummaryOut,
    TaskUpdateRequest,
    TimelineEventOut,
)
from ..timeline import derive_task_timeline
from ..worker.runner import _pm_decide, _system_event, create_child_task
from ..worker.summarizer import summarize_task
from .deps import get_repository_or_404, get_session, get_settings

log = logging.getLogger("autoia.api")

TASK_CREATED = "created"

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_query(session: Session):
    return session.query(Task).options(
        joinedload(Task.steps).joinedload(TaskStep.robot),
        joinedload(Task.repository),
        joinedload(Task.subtasks),
        joinedload(Task.proposals),
        selectinload(Task.summaries),
    )


def _get_task_or_404(session: Session, task_id: int) -> Task:
    task = _task_query(session).filter(Task.id == task_id).first()
    if task is None:
        raise HTTPException(404, "tarefa não encontrada")
    return task


@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    data: TaskCreate,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    get_repository_or_404(session, data.repository_id)
    pipeline = session.get(Pipeline, data.pipeline_id)
    if pipeline is None:
        raise HTTPException(404, "pipeline não encontrado")
    if pipeline.repository_id not in (None, data.repository_id):
        raise HTTPException(400, "pipeline não pertence a este projeto")
    if not pipeline.steps:
        raise HTTPException(400, "pipeline sem fases")

    task = Task(
        repository_id=data.repository_id,
        pipeline_id=pipeline.id,
        title=data.title,
        description=data.description,
        kind=data.kind,
        executor=data.executor,
        budget_limit=data.budget_limit if data.budget_limit is not None else settings.task_budget,
    )
    for step in sorted(pipeline.steps, key=lambda x: x.position):
        task.steps.append(
            TaskStep(
                position=step.position,
                robot_id=step.robot_id,
                post_merge=step.post_merge,
                pause_before=step.pause_before,
            )
        )
    for i, st_data in enumerate(data.subtasks):
        task.subtasks.append(
            SubTask(
                position=i,
                title=st_data.title,
                description=st_data.description,
                acceptance_criteria=st_data.acceptance_criteria,
            )
        )
    session.add(task)
    session.commit()
    return _get_task_or_404(session, task.id)


@router.get("", response_model=list[TaskOut])
def list_tasks(
    repository_id: int | None = None,
    session: Session = Depends(get_session),
):
    q = _task_query(session)
    if repository_id is not None:
        q = q.filter(Task.repository_id == repository_id)
    return q.order_by(Task.id.desc()).limit(100).all()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)):
    return _get_task_or_404(session, task_id)


# ---------- Propostas de tasks filhas (aprovação humana) ----------


def _get_proposal_or_404(task: Task, proposal_id: int) -> TaskProposal:
    proposal = next((p for p in task.proposals if p.id == proposal_id), None)
    if proposal is None:
        raise HTTPException(404, "proposta não encontrada")
    return proposal


@router.get("/{task_id}/proposals", response_model=list[TaskProposalOut])
def list_proposals(task_id: int, session: Session = Depends(get_session)):
    task = _get_task_or_404(session, task_id)
    return sorted(task.proposals, key=lambda p: p.position)


@router.post("/{task_id}/proposals/{proposal_id}/accept", response_model=TaskOut)
def accept_proposal(
    task_id: int,
    proposal_id: int,
    session: Session = Depends(get_session),
):
    """Aprova a proposta e cria a task filha real (valida `allow_external_tasks`
    quando a proposta mira outro repositório)."""
    task = _get_task_or_404(session, task_id)
    proposal = _get_proposal_or_404(task, proposal_id)
    if proposal.status != "pending":
        raise HTTPException(400, f"proposta já foi {proposal.status}")

    if proposal.target_repository_id is not None:
        target_repo = session.get(Repository, proposal.target_repository_id)
        if target_repo is None:
            raise HTTPException(404, "repositório alvo não encontrado")
        if not target_repo.allow_external_tasks:
            raise HTTPException(
                400,
                f"repositório '{target_repo.name}' não aceita tasks externas",
            )

    child = create_child_task(
        session,
        task,
        title=proposal.title,
        description=proposal.description,
        kind=proposal.kind,
        target_repository_id=proposal.target_repository_id,
    )
    proposal.status = "accepted"
    proposal.accepted_task_id = child.id
    _system_event(
        session, _anchor_step(task), "proposal_accepted",
        {"proposal_id": proposal.id, "title": proposal.title, "child_task_id": child.id},
    )
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/proposals/{proposal_id}/reject", response_model=TaskOut)
def reject_proposal(
    task_id: int,
    proposal_id: int,
    session: Session = Depends(get_session),
):
    task = _get_task_or_404(session, task_id)
    proposal = _get_proposal_or_404(task, proposal_id)
    if proposal.status != "pending":
        raise HTTPException(400, f"proposta já foi {proposal.status}")
    proposal.status = "rejected"
    _system_event(
        session, _anchor_step(task), "proposal_rejected",
        {"proposal_id": proposal.id, "title": proposal.title},
    )
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/start", response_model=TaskOut)
def start_task(
    task_id: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_CREATED:
        raise HTTPException(400, f"tarefa não está em 'created' (status atual: {task.status})")
    task.branch = f"{settings.branch_prefix}/task-{task.id}"
    task.status = TASK_QUEUED
    min(task.steps, key=lambda st: st.position).status = STEP_PENDING
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/pause", response_model=TaskOut)
def pause_task(task_id: int, session: Session = Depends(get_session)):
    """Pausa uma tarefa em andamento (worker para de reclamar fases)."""
    task = _get_task_or_404(session, task_id)
    if task.status not in (TASK_QUEUED, TASK_IN_PROGRESS):
        raise HTTPException(
            400, f"só dá para pausar tarefa em andamento (status atual: {task.status})"
        )
    task.status = TASK_PAUSED
    anchor = _anchor_step(task)
    _system_event(session, anchor, "task_paused", {"status_anterior": "in_progress"})
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/resume", response_model=TaskOut)
def resume_task(task_id: int, session: Session = Depends(get_session)):
    """Retoma uma tarefa pausada (volta para a fila; fases pendentes re-executam)."""
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_PAUSED:
        raise HTTPException(400, f"tarefa não está pausada (status atual: {task.status})")
    task.status = TASK_QUEUED
    anchor = _anchor_step(task)
    _system_event(session, anchor, "task_resumed", {})
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: int, session: Session = Depends(get_session)):
    """Cancela uma tarefa: terminal, o pipeline não avança nem integra mais."""
    task = _get_task_or_404(session, task_id)
    if task.status in (TASK_DONE, TASK_FAILED, TASK_CANCELLED):
        raise HTTPException(
            400, f"tarefa em estado terminal '{task.status}' não pode ser cancelada"
        )
    task.status = TASK_CANCELLED
    task.error = "cancelada pelo usuário"
    anchor = _anchor_step(task)
    _system_event(session, anchor, "task_cancelled", {})
    session.commit()
    return _get_task_or_404(session, task_id)


def _anchor_step(task: Task) -> TaskStep:
    """Step usado como âncora para eventos de nível de task (corrente ou o primeiro)."""
    steps = sorted(task.steps, key=lambda st: st.position)
    return next((st for st in steps if st.position == task.current_step), steps[0] if steps else None)


@router.post("/{task_id}/review", response_model=TaskOut)
def review_task(
    task_id: int, data: ReviewRequest, session: Session = Depends(get_session)
):
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_NEEDS_REVIEW:
        raise HTTPException(400, f"tarefa não está aguardando revisão (status: {task.status})")

    if data.action == "approve":
        task.budget_limit = (task.budget_limit or 0.0) + data.extra_budget
        task.status = TASK_IN_PROGRESS
        task.error = None
        for st in task.steps:
            if st.status == STEP_PENDING:
                st.error = None
    else:
        task.status = "failed"
        task.error = data.note or "cancelada na revisão humana"
        for st in task.steps:
            if st.status == STEP_PENDING:
                st.status = STEP_FAILED
                st.error = task.error
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/bounceback", response_model=TaskOut)
def bounceback_task(
    task_id: int,
    data: BouncebackRequest,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    """Retorna o pipeline para uma fase anterior a partir de ``needs_review``.

    Diferente do retry simples (que reabre uma fase só), o bounceback reseta
    o step alvo **e todos os steps seguintes**, limpando veredictos e sumários.
    A tarefa volta para ``queued`` e o worker retoma do step alvo.

    Útil quando uma falha pós-merge ou um problema detectado exige reexecutar
    a partir de uma fase anterior (ex.: voltar ao developer após deploy-tester
    detectar que a feature não está deployada).
    """
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_NEEDS_REVIEW:
        raise HTTPException(400, f"tarefa não está aguardando revisão (status: {task.status})")

    target = next((st for st in task.steps if st.position == data.target_position), None)
    if target is None:
        raise HTTPException(404, f"fase {data.target_position} não encontrada")

    # Valida que o alvo é anterior ao último step executado
    max_executed = max(
        (st.position for st in task.steps if st.status not in (STEP_PENDING,)),
        default=None,
    )
    if max_executed is not None and data.target_position >= max_executed:
        raise HTTPException(
            400,
            f"alvo (posição {data.target_position}) deve ser anterior à última fase "
            f"executada (posição {max_executed})",
        )

    if target.attempt >= settings.max_attempts:
        raise HTTPException(
            400, f"tentativas máximas atingidas para a fase {data.target_position} "
            f"({settings.max_attempts})"
        )

    # Salva nota como feedback da task
    if data.note:
        task.feedback = data.note

    # Reseta o step alvo
    target.attempt += 1
    target.status = STEP_PENDING
    target.error = None
    target.summary = None
    target.verdict = None
    target.finished_at = None
    target.started_at = None

    # Reseta todos os steps seguintes
    for st in task.steps:
        if st.position > data.target_position:
            st.status = STEP_PENDING
            st.error = None
            st.summary = None
            st.verdict = None
            st.finished_at = None
            st.started_at = None

    task.status = TASK_QUEUED
    task.error = None

    # Evento de auditoria
    max_seq = (
        session.query(func.max(RunEvent.seq))
        .filter(RunEvent.step_id == target.id)
        .scalar() or 0
    )
    session.add(RunEvent(
        step_id=target.id,
        seq=max_seq + 1,
        kind="human_bounceback",
        payload={
            "target_position": data.target_position,
            "reviewed_by": data.reviewed_by,
            "note": data.note,
            "from_status": TASK_NEEDS_REVIEW,
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    ))

    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/steps/{position}/retry", response_model=TaskOut)
def retry_step(
    task_id: int,
    position: int,
    data: RetryRequest | None = None,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    """Reabre uma fase para re-execução, opcionalmente com uma nota (feedback externo).

    Fases `failed`/`guardrail_blocked` voltam a pending; fases já `done` também podem
    voltar (ex.: "voltar para o developer" com a nota do erro externo) — as fases
    seguintes são reabertas naturalmente conforme o fluxo avança.
    """
    task = _get_task_or_404(session, task_id)
    if task.status == "created":
        raise HTTPException(400, "tarefa ainda não foi iniciada")
    step = next((st for st in task.steps if st.position == position), None)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    if step.status in (STEP_PENDING, "running"):
        raise HTTPException(400, f"fase em andamento (status: {step.status})")
    if (
        step.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)
        and step.attempt >= settings.max_attempts
        and task.status != TASK_BLOCKED
    ):
        # Em task bloqueada (ex.: conflito de merge) o retry fica liberado mesmo com
        # tentativas máximas: o usuário precisa poder instruir o robô (feedback) e
        # re-executar a fase para resolver o bloqueio.
        raise HTTPException(400, f"tentativas máximas atingidas ({settings.max_attempts})")

    if data and data.note:
        task.feedback = data.note
    step.attempt += 1
    step.status = STEP_PENDING
    step.error = None
    step.summary = None
    step.finished_at = None
    step.started_at = None
    task.status = TASK_QUEUED
    task.error = None
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/approve-step", response_model=TaskOut)
def approve_step(
    task_id: int,
    data: ApproveStepRequest,
    session: Session = Depends(get_session),
):
    """Aprovação humana de uma fase com `pause_before` (gate no pipeline).

    A task está em `waiting_approval` com a fase pendente; aprovar libera a fase
    para o worker executar. A `note` opcional vira feedback externo da task e
    entra no handoff da fase aprovada.
    """
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_WAITING_APPROVAL:
        raise HTTPException(
            400,
            f"tarefa não está aguardando aprovação (status atual: {task.status})",
        )
    step = next((st for st in task.steps if st.position == data.position), None)
    if step is None:
        raise HTTPException(404, f"fase {data.position} não encontrada")
    if not step.pause_before:
        raise HTTPException(400, f"fase {data.position} não tem gate de aprovação")
    if step.status != STEP_PENDING:
        raise HTTPException(400, f"fase {data.position} não está pendente (status: {step.status})")

    if data.note:
        task.feedback = data.note
    # O gate é one-shot: consumido na aprovação, senão o claim re-pausaria a task.
    step.pause_before = False
    task.status = TASK_QUEUED
    task.error = None

    max_seq = (
        session.query(func.max(RunEvent.seq))
        .filter(RunEvent.step_id == step.id)
        .scalar() or 0
    )
    session.add(RunEvent(
        step_id=step.id,
        seq=max_seq + 1,
        kind="human_gate_approved",
        payload={
            "position": step.position,
            "note": data.note,
            "from_status": TASK_WAITING_APPROVAL,
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    ))
    session.commit()
    return _get_task_or_404(session, task_id)


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int,
    data: TaskUpdateRequest,
    session: Session = Depends(get_session),
):
    """Edição humana da história (descrição + critérios de aceite).

    Permitida apenas antes do fluxo (created) ou durante uma parada por
    aprovação humana (waiting_approval) — nunca no meio da execução, para não
    divergir do que o PO refinou. O campo `details` (detalhes da implementação)
    pode ser editado a qualquer momento: complementa o contexto e entra no
    handoff das próximas fases, diferenciado da solicitação original.
    """
    if data.details is not None:
        task = _get_task_or_404(session, task_id)
        task.details = data.details
        session.commit()
        return _get_task_or_404(session, task_id)
    task = _get_task_or_404(session, task_id)
    if task.status not in ("created", TASK_WAITING_APPROVAL):
        raise HTTPException(
            400,
            f"editar história só é permitido em 'created' ou 'waiting_approval' "
            f"(status atual: {task.status})",
        )
    if data.description is not None:
        task.description = data.description
    if data.acceptance_criteria is not None:
        task.acceptance_criteria = data.acceptance_criteria
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/feedback", response_model=TaskOut)
def set_feedback(
    task_id: int, data: FeedbackCreate, session: Session = Depends(get_session)
):
    """Anexa/sobrescreve uma nota externa (erro de deploy, pedido de ajuste...) que as
    próximas fases recebem no handoff e no prompt."""
    task = _get_task_or_404(session, task_id)
    task.feedback = data.text
    session.commit()
    return _get_task_or_404(session, task_id)


@router.delete("/{task_id}/feedback", response_model=TaskOut)
def clear_feedback(task_id: int, session: Session = Depends(get_session)):
    task = _get_task_or_404(session, task_id)
    task.feedback = None
    session.commit()
    return _get_task_or_404(session, task_id)


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int, session: Session = Depends(get_session)):
    task = _get_task_or_404(session, task_id)
    if task.status not in ("created",):
        raise HTTPException(400, f"só é possível excluir tasks com status 'created' (atual: {task.status})")
    session.delete(task)
    session.commit()


@router.post("/{task_id}/pm/decide", response_model=TaskOut)
def pm_decide(
    task_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Dispara o robô PM para decidir o rumo de uma tarefa travada (em background)."""
    task = _get_task_or_404(session, task_id)
    if task.status not in (TASK_FAILED, TASK_NEEDS_REVIEW, TASK_BLOCKED):
        raise HTTPException(
            400, f"PM só decide em tarefas travadas (status atual: {task.status})"
        )

    def _pm_thread() -> None:
        try:
            _pm_decide(
                request.app.state.Session, request.app.state.settings, task_id, "manual"
            )
        except Exception:
            log.exception("PM falhou para a task %s", task_id)

    background_tasks.add_task(_pm_thread)
    return task


# ---------- Resumo do desenvolvimento (LLM dedicada) ----------


@router.get("/{task_id}/summary", response_model=TaskSummaryOut | None)
def get_task_summary(task_id: int, session: Session = Depends(get_session)):
    """Resumo estruturado mais recente do desenvolvimento (None se nunca gerado)."""
    task = _get_task_or_404(session, task_id)
    return task.summary


@router.post("/{task_id}/summary/regenerate", response_model=TaskSummaryOut | None)
def regenerate_summary(
    task_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Regenera o resumo do desenvolvimento (em background, via executor da task).

    Útil após novas fases, retorno de etapa, intervenções ou mudança de modelo.
    A falha na geração NUNCA impede o desenvolvimento nem altera dados originais.
    """
    task = _get_task_or_404(session, task_id)

    def _summarize() -> None:
        try:
            summarize_task(request.app.state.settings, request.app.state.Session, task_id)
        except Exception:
            log.exception("resumo falhou para a task %s", task_id)

    background_tasks.add_task(_summarize)
    return task.summary


# ---------- Timeline cronológica da execução ----------


@router.get("/{task_id}/timeline", response_model=list[TimelineEventOut])
def task_timeline(task_id: int, session: Session = Depends(get_session)):
    """Timeline cronológica do desenvolvimento (eventos determinísticos dos RunEvent)."""
    task = _get_task_or_404(session, task_id)
    return [TimelineEventOut(**ev) for ev in derive_task_timeline(session, task)]


# ---------- Bloqueio + retomada por instrução ----------


@router.post("/{task_id}/blocked/continue", response_model=TaskOut)
def continue_blocked(
    task_id: int,
    data: BlockedContinueRequest,
    session: Session = Depends(get_session),
):
    """Retoma uma fase bloqueada com a instrução do usuário.

    O desenvolvimento volta a partir do ponto em que foi interrompido (mesma task,
    mesma fase/etapa, mesmo contexto e histórico): a instrução é armazenada
    separadamente do contexto original, entra no handoff/prompt da retomada e
    aparece na timeline como intervenção do usuário.
    """
    task = _get_task_or_404(session, task_id)
    if task.status != TASK_BLOCKED:
        raise HTTPException(
            400, f"tarefa não está bloqueada aguardando instrução (status: {task.status})"
        )
    blocked_steps = [st for st in task.steps if st.status == STEP_BLOCKED]
    if not blocked_steps:
        raise HTTPException(400, "nenhuma fase bloqueada aguardando instrução")
    step = max(blocked_steps, key=lambda st: st.position)

    # A instrução é persistida separadamente (não sobrescreve description/details).
    task.resume_instruction = data.instruction
    task.error = None
    task.status = TASK_QUEUED

    # Reabre a fase exata onde o desenvolvimento parou, preservando o histórico.
    step.status = STEP_PENDING
    step.attempt += 1
    step.error = None
    step.started_at = None
    step.finished_at = None

    def _event(kind: str, payload: dict) -> None:
        max_seq = (
            session.query(func.max(RunEvent.seq))
            .filter(RunEvent.step_id == step.id)
            .scalar() or 0
        )
        session.add(RunEvent(
            step_id=step.id,
            seq=max_seq + 1,
            kind=kind,
            payload=payload,
        ))

    _event("user_intervention", {
        "instruction": data.instruction,
        "blocked_step": step.position,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _event("execution_resumed", {
        "step": step.position,
        "instruction": data.instruction,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    session.commit()
    return _get_task_or_404(session, task_id)
