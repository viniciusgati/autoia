"""Endpoints de tarefas (criar, iniciar, revisar, retry, PM)."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import (
    STEP_BLOCKED,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_PENDING,
    STEP_RUNNING,
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
    RepositoryUser,
    RunEvent,
    StepSummary,
    SubTask,
    Task,
    TaskProposal,
    TaskStep,
    User,
)
from ..schemas import (
    ApproveStepRequest,
    BlockedContinueRequest,
    BouncebackRequest,
    FeedbackCreate,
    InstructionRequest,
    ResponsibleUpdate,
    RetryRequest,
    ReviewRequest,
    StepDiffOut,
    TaskCreate,
    TaskListItem,
    TaskOut,
    TaskProposalOut,
    TaskStepListOut,
    TaskSummaryOut,
    TaskUpdateRequest,
    TimelineEventOut,
    WorkspaceOccurrenceOut,
    WorkspaceOut,
)
from ..timeline import derive_task_occurrences, derive_task_timeline
from ..worker import gitops
from ..worker.runner import _effective, _pm_decide, _system_event, _task_workspace, create_child_task
from ..worker.summarizer import summarize_task
from .deps import get_repository_or_404, get_session, get_settings, is_repo_admin, require_auth
from .etag import conditional

log = logging.getLogger("autoia.api")

TASK_CREATED = "created"

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_query(session: Session):
    return session.query(Task).options(
        joinedload(Task.steps).joinedload(TaskStep.robot),
        joinedload(Task.repository),
        joinedload(Task.subtasks),
        joinedload(Task.proposals),
        joinedload(Task.responsible),
        selectinload(Task.summaries),
    )


def _task_list_query(session: Session):
    """Query leve p/ listagens: sem resumos LLM, propostas e subtarefas."""
    return session.query(Task).options(
        joinedload(Task.steps).joinedload(TaskStep.robot),
        joinedload(Task.responsible),
    )


def _summary_preview(text: str | None, limit: int = 220) -> str | None:
    """Preview de exibição do resumo de uma fase (o texto integral fica no detalhe)."""
    if not text:
        return None
    cleaned = text.strip().replace("\n", " ")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _task_list_item(task: Task) -> TaskListItem:
    return TaskListItem(
        id=task.id,
        repository_id=task.repository_id,
        pipeline_id=task.pipeline_id,
        title=task.title,
        kind=task.kind,
        status=task.status,
        executor=task.executor,
        current_step=task.current_step,
        budget_limit=task.budget_limit,
        cost_spent=task.cost_spent,
        pm_decisions=task.pm_decisions,
        error=task.error,
        created_at=task.created_at,
        updated_at=task.updated_at,
        parent_task_id=task.parent_task_id,
        responsible_id=task.responsible_id,
        responsible=task.responsible,
        steps=[
            TaskStepListOut(
                id=st.id,
                position=st.position,
                robot=st.robot,
                status=st.status,
                attempt=st.attempt,
                verdict=st.verdict,
                post_merge=st.post_merge,
                pause_before=st.pause_before,
                diff_stat=st.diff_stat,
                summary_preview=_summary_preview(st.summary),
                error=st.error,
                started_at=st.started_at,
                finished_at=st.finished_at,
            )
            for st in sorted(task.steps, key=lambda x: x.position)
        ],
    )


def _get_task_or_404(session: Session, task_id: int) -> Task:
    task = _task_query(session).filter(Task.id == task_id).first()
    if task is None:
        raise HTTPException(404, "tarefa não encontrada")
    return task


def _upsert_repo_member(session: Session, repo_id: int, user_id: int, role: str = "member") -> RepositoryUser:
    """Upsert idempotente de `repository_users(repo, user, role)` — não duplica.

    Membro já existente mantém o papel atual: reatribuir a tarefa para um admin
    do projeto nunca rebaixa o papel dele para `member` (idempotência real —
    a 2ª chamada não altera o estado).
    """
    member = (
        session.query(RepositoryUser)
        .filter(
            RepositoryUser.repository_id == repo_id,
            RepositoryUser.user_id == user_id,
        )
        .first()
    )
    if member is None:
        member = RepositoryUser(repository_id=repo_id, user_id=user_id, role=role)
        session.add(member)
    return member


def _ensure_can_act(session: Session, task: Task, user: User | None) -> None:
    """Permissão de atuação numa tarefa (TODAS as mutações da lista fechada).

    - `user=None` (auth OFF): permite — comportamento legado preservado.
    - Sem responsável definido: qualquer autenticado atua.
    - Com responsável: só ele, admin do projeto ou admin global (403 caso contrário).
    """
    if user is None:
        return
    if task.responsible_id is None:
        return
    if user.id == task.responsible_id or user.is_admin:
        return
    if is_repo_admin(session, user, task.repository_id):
        return
    raise HTTPException(
        403, "somente o responsável ou admin do projeto pode atuar nesta tarefa"
    )


@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    data: TaskCreate,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
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
        # Default = criador; com auth OFF (user=None) fica NULL até reatribuição.
        responsible_id=user.id if user else None,
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


@router.get("", response_model=list[TaskListItem])
def list_tasks(
    repository_id: int | None = None,
    request: Request = None,
    response: Response = None,
    session: Session = Depends(get_session),
):
    token_parts: list[str] = []
    q_count = session.query(func.max(Task.updated_at), func.count(Task.id))
    q_count = q_count.filter(Task.repository_id == repository_id) if repository_id is not None else q_count
    max_ts, count = q_count.first()
    token_parts.extend([str(max_ts) if max_ts is not None else "", str(count)])
    not_modified = conditional(request, response, "|".join(token_parts))
    if not_modified is not None:
        return not_modified

    q = _task_list_query(session)
    if repository_id is not None:
        q = q.filter(Task.repository_id == repository_id)
    tasks = q.order_by(Task.id.desc()).limit(100).all()
    return [_task_list_item(t) for t in tasks]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)):
    return _get_task_or_404(session, task_id)


@router.put("/{task_id}/responsible", response_model=TaskOut)
def assign_responsible(
    task_id: int,
    data: ResponsibleUpdate,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Reatribui o responsável por uma tarefa.

    Permissão: admin global, admin do projeto ou o próprio responsável atual
    (mesma regra de `_ensure_can_act`). O usuário alvo vira membro do projeto
    (upsert idempotente de `repository_users`, role `member`).
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    target = session.get(User, data.user_id)
    if target is None:
        raise HTTPException(404, "usuário não encontrado")
    task.responsible_id = target.id
    _upsert_repo_member(session, task.repository_id, target.id)
    session.commit()
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
    user: User | None = Depends(require_auth),
):
    """Aprova a proposta e cria a task filha real (valida `allow_external_tasks`
    quando a proposta mira outro repositório)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status != TASK_CREATED:
        raise HTTPException(400, f"tarefa não está em 'created' (status atual: {task.status})")
    task.branch = f"{settings.branch_prefix}/task-{task.id}"
    task.status = TASK_QUEUED
    min(task.steps, key=lambda st: st.position).status = STEP_PENDING
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/pause", response_model=TaskOut)
def pause_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Pausa uma tarefa em andamento (worker para de reclamar fases)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
def resume_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Retoma uma tarefa pausada (volta para a fila; fases pendentes re-executam)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status != TASK_PAUSED:
        raise HTTPException(400, f"tarefa não está pausada (status atual: {task.status})")
    task.status = TASK_QUEUED
    anchor = _anchor_step(task)
    _system_event(session, anchor, "task_resumed", {})
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Cancela uma tarefa: terminal, o pipeline não avança nem integra mais."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    task_id: int,
    data: ReviewRequest,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    """Retorna o pipeline para uma fase anterior a partir de ``needs_review``.

    Diferente do retry simples (que reabre uma fase só), o bounceback reseta
    o step alvo **e todos os steps seguintes**, limpando veredictos e sumários.
    A tarefa volta para ``queued`` e o worker retoma do step alvo.

    Útil quando uma falha pós-merge ou um problema detectado exige reexecutar
    a partir de uma fase anterior (ex.: voltar ao developer após deploy-tester
    detectar que a feature não está deployada).

    Ação sempre MANUAL (humano): não fica presa ao `max_attempts` (limite só do
    bounce-back automático do worker).
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    """Reabre uma fase para re-execução, opcionalmente com uma nota (feedback externo).

    Fases `failed`/`guardrail_blocked` voltam a pending; fases já `done` também podem
    voltar (ex.: "voltar para o developer" com a nota do erro externo) — as fases
    seguintes são reabertas naturalmente conforme o fluxo avança.

    O retry é sempre uma ação MANUAL (humano): não fica limitado a `max_attempts`,
    que vale apenas para o bounce-back automático do worker.
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status == "created":
        raise HTTPException(400, "tarefa ainda não foi iniciada")
    step = next((st for st in task.steps if st.position == position), None)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    if step.status in (STEP_PENDING, "running"):
        raise HTTPException(400, f"fase em andamento (status: {step.status})")

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
    user: User | None = Depends(require_auth),
):
    """Aprovação humana de uma fase com `pause_before` (gate no pipeline).

    A task está em `waiting_approval` com a fase pendente; aprovar libera a fase
    para o worker executar. A `note` opcional vira feedback externo da task e
    entra no handoff da fase aprovada.
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
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
        _ensure_can_act(session, task, user)
        task.details = data.details
        session.commit()
        return _get_task_or_404(session, task_id)
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    task_id: int,
    data: FeedbackCreate,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Anexa/sobrescreve uma nota externa (erro de deploy, pedido de ajuste...) que as
    próximas fases recebem no handoff e no prompt."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    task.feedback = data.text
    session.commit()
    return _get_task_or_404(session, task_id)


@router.delete("/{task_id}/feedback", response_model=TaskOut)
def clear_feedback(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    task.feedback = None
    session.commit()
    return _get_task_or_404(session, task_id)


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    """Dispara o robô PM para decidir o rumo de uma tarefa travada (em background)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    user: User | None = Depends(require_auth),
):
    """Retoma uma fase bloqueada com a instrução do usuário.

    O desenvolvimento volta a partir do ponto em que foi interrompido (mesma task,
    mesma fase/etapa, mesmo contexto e histórico): a instrução é armazenada
    separadamente do contexto original, entra no handoff/prompt da retomada e
    aparece na timeline como intervenção do usuário.
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
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
    task.block_reason_type = None
    task.block_reason = None
    task.block_question = None
    task.block_options = []

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


# ---------- Workspace (tela de trabalho) e interação por instrução ----------


def _rewind_pipeline(session: Session, task: Task, target: TaskStep) -> None:
    """Reexecuta a partir de `target`: reabre o alvo e reseta os steps seguintes.

    O histórico (RunEvent) nunca é apagado — novas execuções viram novas ocorrências
    na timeline. Compartilhado pelo bounceback manual e pelo envio de instrução.
    """
    target.attempt += 1
    target.status = STEP_PENDING
    target.error = None
    target.summary = None
    target.verdict = None
    target.finished_at = None
    target.started_at = None
    for st in task.steps:
        if st.position > target.position:
            st.status = STEP_PENDING
            st.error = None
            st.summary = None
            st.verdict = None
            st.finished_at = None
            st.started_at = None
    task.status = TASK_QUEUED
    task.error = None


def _reopen_step(step: TaskStep) -> None:
    """Reabre UMA fase para re-execução (mantém a próxima inalterada)."""
    step.attempt += 1
    step.status = STEP_PENDING
    step.error = None
    step.started_at = None
    step.finished_at = None


def _emit_user_event(session: Session, step: TaskStep, instruction: str, blocked_step: int | None = None) -> None:
    max_seq = (
        session.query(func.max(RunEvent.seq))
        .filter(RunEvent.step_id == step.id)
        .scalar() or 0
    )
    session.add(RunEvent(
        step_id=step.id,
        seq=max_seq + 1,
        kind="user_intervention",
        payload={
            "instruction": instruction,
            "blocked_step": blocked_step,
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    ))
    max_seq = (
        session.query(func.max(RunEvent.seq))
        .filter(RunEvent.step_id == step.id)
        .scalar() or 0
    )
    session.add(RunEvent(
        step_id=step.id,
        seq=max_seq + 1,
        kind="execution_resumed",
        payload={
            "step": step.position,
            "instruction": instruction,
            "ts": datetime.now(timezone.utc).isoformat(),
        },
    ))


def _step_by_id(task: Task) -> dict[int, TaskStep]:
    return {st.id: st for st in task.steps}


def _count_tests(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _derive_tests(step: TaskStep, occ: dict) -> dict | None:
    """Resultado de testes de fases verify (best-effort do texto real do robô)."""
    if step is None or not step.robot or step.robot.role != "verify":
        return None
    text = f"{occ.get('delivered_text') or ''}\n{step.summary or ''}"
    passed = _count_tests(r"(\d+)\s*testes?\s+passaram", text) or _count_tests(r"(\d+)\s+passed", text)
    failed = _count_tests(r"(\d+)\s*testes?\s+f[áa]lharam", text) or _count_tests(r"(\d+)\s+failed", text)
    return {
        "passed": passed,
        "failed": failed,
        "verdict": step.verdict or occ.get("status"),
    }


@router.get("/{task_id}/workspace", response_model=WorkspaceOut)
def task_workspace(
    task_id: int,
    settings=Depends(get_settings),
    session: Session = Depends(get_session),
):
    """Tela de trabalho: task + timeline cronológica de execuções de fase.

    Cada execução (tentativa) vira uma ocorrência — o histórico é imutável e
    re-execuções aparecem como novas ocorrências, não substituem as anteriores.
    """
    task = _get_task_or_404(session, task_id)
    occurrences = derive_task_occurrences(session, task)
    step_summaries = {
        (ss.step_id, ss.attempt): ss
        for ss in session.query(StepSummary)
        .filter(StepSummary.task_id == task_id)
        .all()
    }
    proposals_by_step: dict[int | None, list[TaskProposal]] = {}
    for p in task.proposals:
        proposals_by_step.setdefault(p.step_id, []).append(p)
    steps_by_id = _step_by_id(task)

    # Diff real (git) por fase: computa UMA vez por posição (commit da fase é imutável).
    files_by_position: dict[int, list[str]] = {}
    checkout: str | None = None
    repo = task.repository
    if repo:
        eff = _effective(settings, repo)
        checkout = _task_workspace(eff, repo.id, task.id)
        if os.path.isdir(os.path.join(checkout, ".git")):
            for st in task.steps:
                if st.status in (STEP_PENDING, STEP_RUNNING):
                    continue
                try:
                    info = gitops.diff_for_step(checkout, st.position)
                except Exception:
                    continue
                files_by_position[st.position] = info.get("files") or []

    out_occurrences: list[WorkspaceOccurrenceOut] = []
    for occ in occurrences:
        sid = occ["step_id"]
        step = steps_by_id.get(sid)
        delivered = step_summaries.get((sid, occ["attempt"]))
        out_occurrences.append(WorkspaceOccurrenceOut(
            step_id=sid,
            position=occ["position"],
            robot=occ["robot"],
            attempt=occ["attempt"],
            status=occ["status"],
            goal=occ["goal"],
            started_at=occ["started_at"],
            finished_at=occ["finished_at"],
            last_activity=occ["last_activity"],
            delivered_text=occ["delivered_text"],
            delivered=delivered,
            stop=occ["stop"],
            proposals=proposals_by_step.get(sid, []),
            files=files_by_position.get(occ["position"], []),
            file_count=len(files_by_position.get(occ["position"], [])),
            tests=_derive_tests(step, occ),
            system_activity=occ["system_activity"],
            events=occ["events"],
        ))

    decisions: list[dict] = []
    if task.status == TASK_BLOCKED and task.block_reason_type == "decision_request":
        decisions.append({
            "question": task.block_reason,
            "options": task.block_options or [],
            "context": task.block_question or "",
        })

    return WorkspaceOut(
        task=task,
        summary=task.summary,
        occurrences=out_occurrences,
        decisions=decisions,
    )


@router.get("/{task_id}/steps/{position}/diff", response_model=StepDiffOut)
def step_diff(
    task_id: int,
    position: int,
    settings=Depends(get_settings),
    session: Session = Depends(get_session),
):
    """Diff real (git) do commit da fase — o git é a fonte de verdade da alteração."""
    task = _get_task_or_404(session, task_id)
    step = next((st for st in task.steps if st.position == position), None)
    if step is None:
        raise HTTPException(404, f"fase {position} não encontrada")
    repo = task.repository
    eff = _effective(settings, repo)
    checkout = _task_workspace(eff, repo.id, task.id)
    if not os.path.isdir(os.path.join(checkout, ".git")):
        return StepDiffOut()
    try:
        info = gitops.diff_for_step(checkout, position)
    except Exception:
        log.warning("diff da fase %s (task %s) falhou", position, task_id, exc_info=True)
        info = {"stat": "", "diff": "", "files": [], "commit": None}
    return StepDiffOut(
        stat=info["stat"],
        diff=info["diff"],
        files=info["files"],
        commit=info["commit"],
    )


@router.post("/{task_id}/instruction", response_model=TaskOut)
def send_instruction(
    task_id: int,
    data: InstructionRequest,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Canal de trabalho do workspace: envia uma instrução ao agente e continua.

    Sem `position`: retoma o ponto de parada natural do status atual (bloqueio,
    pausa, revisão, falha). Com `position`: reexecuta a partir daquela fase
    (nova execução — o histórico anterior permanece intacto).
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status == "created":
        raise HTTPException(400, "tarefa ainda não foi iniciada")
    instruction = data.instruction.strip()
    target = None
    if data.position is not None:
        target = next((st for st in task.steps if st.position == data.position), None)
        if target is None:
            raise HTTPException(404, f"fase {data.position} não encontrada")
        max_executed = max(
            (st.position for st in task.steps if st.status != STEP_PENDING),
            default=-1,
        )
        if data.position > max_executed and task.status != TASK_DONE:
            raise HTTPException(
                400,
                f"não é possível continuar da fase {data.position} "
                f"(última fase executada: {max_executed})",
            )

    task.resume_instruction = instruction
    task.error = None
    task.block_reason_type = None
    task.block_reason = None
    task.block_question = None
    task.block_options = []
    anchor: TaskStep | None = None
    blocked_step: int | None = None

    if target is not None:
        # Reexecuta a partir da fase escolhida (voltar para etapa anterior).
        if task.status == TASK_WAITING_APPROVAL:
            gated = next((st for st in task.steps if st.pause_before), None)
            if gated is not None:
                gated.pause_before = False
        _rewind_pipeline(session, task, target)
        anchor = target
    elif task.status == TASK_BLOCKED:
        blocked_steps = [st for st in task.steps if st.status == STEP_BLOCKED]
        if not blocked_steps:
            raise HTTPException(400, "nenhuma fase bloqueada aguardando instrução")
        step = max(blocked_steps, key=lambda st: st.position)
        _reopen_step(step)
        blocked_step = step.position
        anchor = step
        task.status = TASK_QUEUED
    elif task.status == TASK_PAUSED:
        task.status = TASK_QUEUED
        anchor = _pick_anchor(task)
    elif task.status == TASK_WAITING_APPROVAL:
        gated = next((st for st in task.steps if st.pause_before), None)
        if gated is not None:
            gated.pause_before = False
        anchor = gated or _pick_anchor(task)
        task.status = TASK_QUEUED
    elif task.status in (TASK_NEEDS_REVIEW, TASK_FAILED):
        pending = next((st for st in task.steps if st.status == STEP_PENDING), None)
        if pending is not None:
            anchor = pending
        else:
            failed = next(
                (st for st in task.steps if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
                None,
            )
            if failed is None:
                raise HTTPException(400, "nenhuma fase pendente ou falha para continuar")
            _reopen_step(failed)
            anchor = failed
        task.status = TASK_QUEUED
    elif task.status == TASK_DONE:
        last = max(task.steps, key=lambda st: st.position)
        _reopen_step(last)
        anchor = last
        task.status = TASK_QUEUED
    elif task.status in (TASK_QUEUED, TASK_IN_PROGRESS):
        anchor = _pick_anchor(task)
        # instrução entra no handoff das próximas fases (sem rewind).
    else:
        raise HTTPException(
            400, f"não é possível enviar instrução no status {task.status}"
        )

    if anchor is not None:
        _emit_user_event(session, anchor, instruction, blocked_step)
    session.commit()
    return _get_task_or_404(session, task_id)


def _pick_anchor(task: Task) -> TaskStep | None:
    """Fase âncora para eventos (a mais relevante no estado atual)."""
    if not task.steps:
        return None
    running = next((st for st in task.steps if st.status == STEP_RUNNING), None)
    if running:
        return running
    active = next(
        (st for st in task.steps if st.status in (STEP_PENDING, STEP_FAILED, STEP_BLOCKED)),
        None,
    )
    return active or sorted(task.steps, key=lambda st: st.position)[-1]
