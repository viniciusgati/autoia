"""Endpoints de tarefas (criar, iniciar, revisar, retry, PM)."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import (
    CHAT_DISPATCH,
    CHAT_MERGE,
    CHAT_STATUS_IDLE,
    CHAT_STATUS_QUEUED,
    STEP_BLOCKED,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_MODE_MANUAL,
    STEP_PENDING,
    STEP_RUNNING,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_MODE_AUTO,
    TASK_MODE_MANUAL,
    TASK_NEEDS_REVIEW,
    TASK_OPEN,
    TASK_PAUSED,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Epic,
    Pipeline,
    Project,
    Repository,
    RepositoryUser,
    Robot,
    RunEvent,
    StepMission,
    StepSummary,
    SubTask,
    Task,
    TaskMessage,
    TaskProposal,
    TaskRun,
    TaskStep,
    User,
)
from ..schemas import (
    ApproveStepRequest,
    BlockedContinueRequest,
    BouncebackRequest,
    ChatMessageResponse,
    ChatSendRequest,
    DescriptionFromFileOut,
    FeedbackCreate,
    InstructionRequest,
    ResponsibleUpdate,
    RetryRequest,
    ReviewRequest,
    StepDiffOut,
    StepFileDiffOut,
    TaskCreate,
    TaskListItem,
    TaskMessageOut,
    TaskOut,
    TaskProposalOut,
    TaskRunOut,
    TaskStepListOut,
    TaskSummaryOut,
    TaskUpdateRequest,
    TaskChangePipelineRequest,
    TaskProposalUpdate,
    TimelineEventOut,
    WorkspaceOccurrenceOut,
    WorkspaceOut,
)
from ..timeline import derive_task_occurrences, derive_task_timeline, fallback_mission
from ..worker import gitops, exec_common
from ..worker.runner import (
    _effective,
    _effective_step_mode,
    _pm_decide,
    _system_event,
    _task_workspace,
    create_child_task,
)
from ..worker.summarizer import summarize_task
from .deps import get_repository_or_404, get_session, get_settings, is_repo_admin, require_auth
from .etag import conditional

log = logging.getLogger("autoia.api")

TASK_CREATED = "created"

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_query(session: Session):
    return session.query(Task).options(
        joinedload(Task.steps).joinedload(TaskStep.robot),
        joinedload(Task.steps).joinedload(TaskStep.artifacts),
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
        project_id=task.project_id,
        epic_id=task.epic_id,
        mode=task.mode,
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
                execution_mode=st.execution_mode,
            )
            for st in sorted((s for s in task.steps if not s.archived), key=lambda x: x.position)
        ],
    )


def _get_task_or_404(session: Session, task_id: int) -> Task:
    task = _task_query(session).filter(Task.id == task_id).first()
    if task is None:
        raise HTTPException(404, "tarefa não encontrada")
    return task


def _project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "projeto não encontrado")
    return project


def _epic_or_404(session: Session, epic_id: int) -> Epic:
    epic = session.get(Epic, epic_id)
    if epic is None:
        raise HTTPException(404, "épico não encontrado")
    return epic


def _apply_task_association(session: Session, task: Task, data: TaskUpdateRequest) -> None:
    """Aplica a associação Projeto > Épico de um PATCH (editável em qualquer status).

    Espelha `update_chamado` (chamados.py:553-569): projeto de outro repositório
    → 400; épico atual que não pertence ao novo projeto → limpo (sem erro); épico
    inexistente → 404; épico de outro repositório → 400; `epic_id` prevalece e
    deriva o `project_id`. Distingue **campo ausente** (`model_fields_set`) de
    **`null` explícito**: ausente = não altera; `project_id: null` remove projeto e
    épico (épico exige projeto); `epic_id: null` remove apenas o épico.
    """
    fields = data.model_fields_set
    if "project_id" in fields:
        if data.project_id is None:
            task.project_id = None
            task.epic_id = None
        else:
            project = _project_or_404(session, data.project_id)
            if project.repository_id != task.repository_id:
                raise HTTPException(400, "projeto não pertence a este repositório")
            task.project_id = project.id
            if task.epic_id is not None:
                epic = session.get(Epic, task.epic_id)
                if epic is not None and epic.project_id != project.id:
                    task.epic_id = None
    if "epic_id" in fields:
        if data.epic_id is None:
            task.epic_id = None
        else:
            epic = _epic_or_404(session, data.epic_id)
            if epic.project.repository_id != task.repository_id:
                raise HTTPException(400, "épico não pertence a este repositório")
            task.epic_id = epic.id
            task.project_id = epic.project_id


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

    # Associação organizacional Projeto > Épico (0..1, opcional — metadados).
    # Regras espelhadas no fluxo de chamados: o épico prevalece e deriva o projeto.
    project_id = data.project_id
    epic = None
    if data.epic_id is not None:
        epic = _epic_or_404(session, data.epic_id)
        if epic.project.repository_id != data.repository_id:
            raise HTTPException(400, "épico não pertence a este repositório")
        project_id = epic.project_id
    if project_id is not None:
        project = _project_or_404(session, project_id)
        if project.repository_id != data.repository_id:
            raise HTTPException(400, "projeto não pertence a este repositório")

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
        project_id=project_id,
        epic_id=epic.id if epic else data.epic_id,
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


# ---------- Import de descrição a partir de arquivo (txt/md) ----------

# Extensões aceitas (case-insensitive) e limite de tamanho: exatamente 100 KB é
# válido; acima disso é erro. O arquivo NÃO é armazenado — só o conteúdo é lido.
DESCRIPTION_FILE_EXTENSIONS = {".txt", ".md", ".markdown"}
MAX_DESCRIPTION_FILE_BYTES = 100 * 1024


@router.post("/description-from-file", response_model=DescriptionFromFileOut)
def description_from_file(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Extrai o conteúdo de um `.txt`/`.md`/`.markdown` para preencher a
    descrição da tarefa (o arquivo não é armazenado no servidor).

    Validações (qualquer violação → 400 com mensagem específica):
    extensão permitida (case-insensitive), tamanho ≤ 100 KB (arquivo vazio →
    erro, para nunca limpar o campo por acidente) e decodificação UTF-8
    (BOM inicial tolerado via `utf-8-sig`).
    """
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in DESCRIPTION_FILE_EXTENSIONS:
        raise HTTPException(
            400,
            "extensão não permitida — use .txt, .md ou .markdown",
        )
    raw = file.file.read(MAX_DESCRIPTION_FILE_BYTES + 1)
    if len(raw) == 0:
        raise HTTPException(400, "arquivo vazio — selecione um arquivo com conteúdo")
    if len(raw) > MAX_DESCRIPTION_FILE_BYTES:
        raise HTTPException(400, "arquivo muito grande (máx. 100 KB)")
    try:
        description = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "arquivo não é texto UTF-8 válido") from exc
    return DescriptionFromFileOut(description=description)


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


@router.get("/chat-worker/status")
def chat_worker_status(
    settings=Depends(get_settings),
    _user: User | None = Depends(require_auth),
):
    from ..worker.chat_runner import CHAT_HEARTBEAT_FILE

    hb = os.path.join(settings.workspace_dir, CHAT_HEARTBEAT_FILE)
    try:
        mtime = os.path.getmtime(hb)
        age = time.time() - mtime
    except OSError:
        return {"alive": False, "last_heartbeat_sec": None}
    return {"alive": age < 15, "last_heartbeat_sec": round(age, 1)}


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
        pipeline_id=proposal.pipeline_id,
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


@router.patch("/{task_id}/proposals/{proposal_id}", response_model=TaskProposalOut)
def update_proposal(
    task_id: int,
    proposal_id: int,
    data: TaskProposalUpdate,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Edita uma proposta PENDENTE antes de aceitar: o usuário ajusta título,
    descrição, kind e a pipeline da task filha — a task nasce com os valores
    editados. A proposta aceita/rejeitada é imutável (histórico de decisão
    preservado)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    proposal = _get_proposal_or_404(task, proposal_id)
    if proposal.status != "pending":
        raise HTTPException(
            400, f"proposta já foi {proposal.status} — não é mais editável"
        )
    payload = data.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(400, "nada para alterar")
    proposal.title = payload.get("title") or proposal.title
    if "description" in payload:
        proposal.description = payload["description"] or ""
    proposal.kind = payload.get("kind") or proposal.kind
    if "pipeline_id" in payload:
        pipeline_id = payload.get("pipeline_id")
        if pipeline_id is not None:
            pipeline = session.get(Pipeline, pipeline_id)
            if pipeline is None:
                raise HTTPException(404, "pipeline não encontrado")
            if pipeline.repository_id not in (None, proposal.target_repository_id or task.repository_id):
                raise HTTPException(
                    400, "pipeline não pertence ao projeto da task filha"
                )
        proposal.pipeline_id = pipeline_id
    _system_event(
        session, _anchor_step(task), "proposal_edited",
        {"proposal_id": proposal.id, **payload},
    )
    session.commit()
    return proposal


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
    first = min(_active_steps(task), key=lambda st: st.position)
    # Modo manual (ou primeira fase manual): abre o chat em vez de enfileirar.
    if task.mode == TASK_MODE_MANUAL or _effective_step_mode(first, task) == STEP_MODE_MANUAL:
        task.status = TASK_OPEN
        task.chat_status = CHAT_STATUS_IDLE
        task.pending_action = None
    else:
        task.status = TASK_QUEUED
        first.status = STEP_PENDING
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/change-pipeline", response_model=TaskOut)
def change_task_pipeline(
    task_id: int,
    data: TaskChangePipelineRequest,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Troca a pipeline de uma task e recria as fases do zero — reiniciar o
    trabalho (correção) com outra pipeline, mesmo que a task já tenha rodado.

    Funciona em QUALQUER status:
    - fase em execução → é interrompida (stop file; o worker entrega o controle);
    - histórico (RunEvent/ocorrências) NÃO é apagado — a nova execução vira novas
      ocorrências na timeline;
    - a task volta para `created` (sem branch) e o usuário dá start quando quiser.
    """
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    pipeline = session.get(Pipeline, data.pipeline_id)
    if pipeline is None:
        raise HTTPException(404, "pipeline não encontrado")
    if pipeline.repository_id not in (None, task.repository_id):
        raise HTTPException(400, "pipeline não pertence a este projeto")
    if not pipeline.steps:
        raise HTTPException(400, "pipeline sem fases")

    # Interrompe fase em execução (a task sai de running; o worker não decide mais).
    _force_stop_running(session, task, settings)

    # Arquiva as fases antigas (histórico preservado — RunEvent/summaries/missions
    # continuam no banco) e recria as fases do zero com a nova pipeline.
    for st in task.steps:
        st.archived = True
        st.status = "created"
        st.started_at = None
        st.finished_at = None
        st.error = None
    task.pipeline_id = pipeline.id
    task.current_step = 0
    task.acceptance_criteria = None
    task.branch = None
    task.error = None
    task.feedback = None
    task.status = TASK_CREATED
    for step in sorted(pipeline.steps, key=lambda x: x.position):
        task.steps.append(
            TaskStep(
                position=step.position,
                robot_id=step.robot_id,
                post_merge=step.post_merge,
                pause_before=step.pause_before,
                status="created",
            )
        )
    _system_event(
        session, _anchor_step(task), "pipeline_changed",
        {
            "pipeline_id": pipeline.id,
            "fases": len(pipeline.steps),
            "arquivadas": len([st for st in task.steps if st.archived]),
        },
    )
    session.commit()
    return _get_task_or_404(session, task_id)


@router.post("/{task_id}/pause", response_model=TaskOut)
def pause_task(
    task_id: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Pausa uma tarefa em andamento: interrompe a fase em execução (se houver) e
    o worker para de reclamar fases."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status not in (TASK_QUEUED, TASK_IN_PROGRESS):
        raise HTTPException(
            400, f"só dá para pausar tarefa em andamento (status atual: {task.status})"
        )
    _force_stop_running(session, task, settings)
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


def _active_steps(task: Task) -> list[TaskStep]:
    """Steps ativos da task (ordena por posição), ignorando os arquivados por
    mudança de pipeline (`change-pipeline`)."""
    return sorted(
        (st for st in task.steps if not st.archived),
        key=lambda st: st.position,
    )


def _anchor_step(task: Task) -> TaskStep:
    """Step usado como âncora para eventos de nível de task (corrente ou o primeiro)."""
    steps = _active_steps(task)
    return next((st for st in steps if st.position == task.current_step), steps[0] if steps else None)


def _force_stop_running(session: Session, task: Task, settings) -> TaskStep | None:
    """Interrompe a fase em execução da task (se houver) e ENTREGA o controle ao
    usuário: grava o stop file da task (o executor mata o subprocesso), reseta a
    fase para `pending` e registra o evento. O worker, ao final da execução morta,
    vê a fase fora de `running` e NÃO decide por ela (não avança o pipeline).

    Chamado pelo pause e por instrução/rewind/retry — antes disso eles eram
    recusados com "fase em execução", deixando o usuário sem conseguir parar nem
    injetar nada.
    """
    running = next((st for st in task.steps if st.status == STEP_RUNNING), None)
    if running is None:
        return None
    try:
        os.makedirs(settings.workspace_dir, exist_ok=True)
        with open(exec_common.task_stop_path(settings.workspace_dir, task.id), "w", encoding="utf-8") as f:
            f.write(str(datetime.now(timezone.utc).timestamp()))
    except OSError:
        log.warning("não foi possível gravar o stop da task %s", task.id, exc_info=True)
    running.status = STEP_PENDING
    running.started_at = None
    running.error = None
    _system_event(
        session, running, "execution_stopped",
        {
            "reason": "execução interrompida pelo usuário (pause/instrução/rewind)",
            "position": running.position,
        },
    )
    return running


def _apply_task_mode(session: Session, task: Task, mode: str, settings) -> None:
    """Alterna o modo de execução da task (auto | manual) em runtime.

    - Para `manual`: interrompe a fase em execução (se houver) e abre o chat
      (status `open`); fases `pending` não são mais reclamadas pelo auto-worker.
    - Para `auto`: reenfileira as fases auto pendentes (volta a `queued`) ou,
      sem fase pendente, conclui.
    """
    if mode == TASK_MODE_MANUAL:
        if task.mode != TASK_MODE_MANUAL:
            task.mode = TASK_MODE_MANUAL
            if task.status in (TASK_QUEUED, TASK_IN_PROGRESS):
                _force_stop_running(session, task, settings)
                task.status = TASK_OPEN
                task.chat_status = CHAT_STATUS_IDLE
                task.pending_action = None
    else:  # auto
        if task.chat_status != CHAT_STATUS_IDLE:
            # Uma ação de chat em voo não pode coexistir com o auto-worker na mesma
            # task (concorrência real); espera concluir.
            raise HTTPException(
                400, "ação de chat em andamento — aguarde concluir para voltar ao modo pipeline"
            )
        if task.mode != TASK_MODE_AUTO:
            task.mode = TASK_MODE_AUTO
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            if task.status == TASK_OPEN:
                pending = next(
                    (st for st in _active_steps(task) if st.status == STEP_PENDING), None
                )
                task.status = TASK_QUEUED if pending is not None else TASK_DONE


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
        for st in _active_steps(task):
            if st.status == STEP_PENDING:
                st.error = None
    else:
        task.status = "failed"
        task.error = data.note or "cancelada na revisão humana"
        for st in _active_steps(task):
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

    target = next((st for st in _active_steps(task) if st.position == data.target_position), None)
    if target is None:
        raise HTTPException(404, f"fase {data.target_position} não encontrada")

    # Valida que o alvo é anterior ao último step executado
    max_executed = max(
        (st.position for st in _active_steps(task) if st.status not in (STEP_PENDING,)),
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
    for st in _active_steps(task):
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
    step = next((st for st in _active_steps(task) if st.position == position), None)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    if step.status in (STEP_PENDING, "running"):
        raise HTTPException(400, f"fase em andamento (status: {step.status})")
    # Se outra fase está executando, interrompe ANTES de reexecutar: o usuário
    # mandou parar e seguir o comando — o robô é morto e a fase atual é entregue
    # ao usuário (não avança). Antes isso era recusado com 409.
    _force_stop_running(session, task, settings)

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
    step = next((st for st in _active_steps(task) if st.position == data.position), None)
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
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Edição humana da história (descrição + critérios de aceite).

    Permitida apenas antes do fluxo (created) ou durante uma parada por
    aprovação humana (waiting_approval) — nunca no meio da execução, para não
    divergir do que o PO refinou. O campo `details` (detalhes da implementação)
    pode ser editado a qualquer momento: complementa o contexto e entra no
    handoff das próximas fases, diferenciado da solicitação original. A associação
    Projeto > Épico (`project_id`/`epic_id`) também é editável em qualquer status:
    são metadados organizacionais que não alteram a história nem a execução. O
    `executor` (kimi/opencode) também é editável em qualquer status, exceto com
    uma fase em execução real: o runner lê `task.executor` a cada fase e na
    decisão do PM, então a troca vale para as próximas execuções (fases já
    concluídas não são re-executadas).
    """
    # Executor das fases: o runner já lê `task.executor` por fase e no PM, então a
    # troca entre execuções é segura. Proibida apenas com uma fase em execução real
    # (seria ignorada até o fim da fase e confundiria o operador) — vale para
    # qualquer status, antes da restrição de edição da história.
    if "executor" in data.model_fields_set:
        task = _get_task_or_404(session, task_id)
        _ensure_can_act(session, task, user)
        if any(st.status == STEP_RUNNING for st in _active_steps(task)):
            raise HTTPException(
                400,
                "não é possível alterar o executor enquanto uma fase está em "
                "execução; aguarde a fase atual terminar",
            )
        if data.executor is not None:
            task.executor = data.executor
            session.commit()
        return _get_task_or_404(session, task_id)
    # Modo de execução (auto | manual): alternável em runtime, em qualquer status.
    if "mode" in data.model_fields_set and data.mode is not None:
        task = _get_task_or_404(session, task_id)
        _ensure_can_act(session, task, user)
        _apply_task_mode(session, task, data.mode, settings)
        session.commit()
        return _get_task_or_404(session, task_id)
    if data.details is not None:
        task = _get_task_or_404(session, task_id)
        _ensure_can_act(session, task, user)
        task.details = data.details
        session.commit()
        return _get_task_or_404(session, task_id)
    # Associação Projeto > Épico: metadados organizacionais editáveis em qualquer
    # status (não alteram a história nem o contexto de execução — padrão `details`).
    if "project_id" in data.model_fields_set or "epic_id" in data.model_fields_set:
        task = _get_task_or_404(session, task_id)
        _ensure_can_act(session, task, user)
        _apply_task_association(session, task, data)
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
    blocked_steps = [st for st in _active_steps(task) if st.status == STEP_BLOCKED]
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
    for st in _active_steps(task):
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
    return {st.id: st for st in _active_steps(task)}


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
    missions = {
        (m.step_id, m.run): m
        for m in session.query(StepMission)
        .filter(StepMission.task_id == task_id)
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
            for st in _active_steps(task):
                if st.status in (STEP_PENDING, STEP_RUNNING):
                    continue
                try:
                    info = _phase_diff(checkout, st, repo.default_branch)
                except Exception:
                    continue
                files_by_position[st.position] = info.get("files") or []

    out_occurrences: list[WorkspaceOccurrenceOut] = []
    last_by_step: dict[int, dict] = {}
    for occ in occurrences:
        sid = occ["step_id"]
        step = steps_by_id.get(sid)
        delivered = step_summaries.get((sid, occ["attempt"]))
        mission = missions.get((sid, occ["run"]))
        prev_occ = last_by_step.get(sid)
        # Branch da alteração: pré-merge = branch da task; pós-merge = default do
        # repositório (a fase roda no estado integrado).
        occ_branch = task.branch
        if repo and step is not None and step.post_merge:
            occ_branch = repo.default_branch
        out_occurrences.append(WorkspaceOccurrenceOut(
            step_id=sid,
            position=occ["position"],
            robot=occ["robot"],
            attempt=occ["attempt"],
            run=occ["run"],
            is_rerun=occ["is_rerun"],
            status=occ["status"],
            goal=occ["goal"],
            mission=mission.mission if mission is not None else fallback_mission(step, task, occ, prev_occ),
            mission_source=mission.source if mission is not None else "fallback",
            started_at=occ["started_at"],
            finished_at=occ["finished_at"],
            duration_ms=occ.get("duration_ms"),
            cost=occ.get("cost", 0.0),
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
            branch=occ_branch,
        ))
        last_by_step[sid] = occ

    decisions: list[dict] = []
    if task.status == TASK_BLOCKED and task.block_reason_type == "decision_request":
        decisions.append({
            "question": task.block_reason,
            "options": task.block_options or [],
            "context": task.block_question or "",
        })

    # Chat human-in-the-loop (modo manual): mensagens + rodadas de agente + agentes.
    messages = (
        session.query(TaskMessage)
        .filter(TaskMessage.task_id == task_id)
        .order_by(TaskMessage.seq)
        .all()
    )
    runs = (
        session.query(TaskRun)
        .filter(TaskRun.task_id == task_id)
        .order_by(TaskRun.id)
        .all()
    )
    agents = _available_agents(session, task.repository_id)

    return WorkspaceOut(
        task=task,
        summary=task.summary,
        occurrences=out_occurrences,
        decisions=decisions,
        messages=messages,
        runs=runs,
        agents=agents,
    )


def _phase_diff(checkout: str, step, base: str) -> dict:
    """Diff (git) da alteração de UMA fase.

    Fonte preferida: `step.commit_sha` — o commit REAL que a execução produziu
    (registrado pelo worker), sem depender de mensagem. Na ausência (fases antigas
    ou em execução sob código antigo): a fase `implement` usa o diff acumulado da
    branch da task (`origin/<base>...HEAD`, que cobre commits de subtarefa); as
    demais fases buscam o commit da fase no segmento da branch (nunca na história
    toda, que misturaria tasks já mescladas).
    """
    sha = getattr(step, "commit_sha", None)
    if sha:
        return gitops.diff_for_commit(checkout, sha)
    role = step.robot.role if step.robot else ""
    if role == "implement":
        return gitops.diff_ahead(checkout, base)
    return gitops.diff_for_step(checkout, step.position, base)


def _phase_file_diff(checkout: str, step, base: str, file_path: str) -> dict:
    """Diff de UM arquivo dentro da alteração de uma fase (espelha `_phase_diff`)."""
    sha = getattr(step, "commit_sha", None)
    if sha:
        return gitops.diff_file_for_commit(checkout, sha, file_path)
    role = step.robot.role if step.robot else ""
    if role == "implement":
        return gitops.diff_ahead_file(checkout, base, file_path)
    return gitops.diff_step_file(checkout, step.position, file_path, base)


@router.get("/{task_id}/steps/{position}/diff", response_model=StepDiffOut)
def step_diff(
    task_id: int,
    position: int,
    settings=Depends(get_settings),
    session: Session = Depends(get_session),
):
    """Diff real (git) do commit da fase — o git é a fonte de verdade da alteração."""
    task = _get_task_or_404(session, task_id)
    step = next((st for st in _active_steps(task) if st.position == position), None)
    if step is None:
        raise HTTPException(404, f"fase {position} não encontrada")
    repo = task.repository
    eff = _effective(settings, repo)
    checkout = _task_workspace(eff, repo.id, task.id)
    if not os.path.isdir(os.path.join(checkout, ".git")):
        return StepDiffOut()
    try:
        info = _phase_diff(checkout, step, repo.default_branch)
    except Exception:
        log.warning("diff da fase %s (task %s) falhou", position, task_id, exc_info=True)
        info = {"stat": "", "diff": "", "files": [], "commit": None}
    return StepDiffOut(
        stat=info["stat"],
        diff=info["diff"],
        files=info["files"],
        commit=info["commit"],
    )


@router.get("/{task_id}/steps/{position}/diff-file/{file_path:path}", response_model=StepFileDiffOut)
def step_file_diff(
    task_id: int,
    position: int,
    file_path: str,
    settings=Depends(get_settings),
    session: Session = Depends(get_session),
):
    """Diff real (git) de UM arquivo dentro do commit da fase.

    O workspace lista os arquivos alterados de cada fase; clicar num arquivo abre
    o diff só dele — o git é a fonte de verdade da alteração.
    """
    task = _get_task_or_404(session, task_id)
    step = next((st for st in _active_steps(task) if st.position == position), None)
    if step is None:
        raise HTTPException(404, f"fase {position} não encontrada")
    repo = task.repository
    eff = _effective(settings, repo)
    checkout = _task_workspace(eff, repo.id, task.id)
    if not os.path.isdir(os.path.join(checkout, ".git")):
        return StepFileDiffOut(path=file_path)
    try:
        info = _phase_file_diff(checkout, step, repo.default_branch, file_path)
    except Exception:
        log.warning("diff do arquivo %s (fase %s, task %s) falhou",
                    file_path, position, task_id, exc_info=True)
        info = {"stat": "", "diff": "", "files": [], "commit": None}
    return StepFileDiffOut(
        path=file_path,
        stat=info["stat"],
        diff=info["diff"],
        commit=info["commit"],
    )


@router.post("/{task_id}/instruction", response_model=TaskOut)
def send_instruction(
    task_id: int,
    data: InstructionRequest,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
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
        target = next((st for st in _active_steps(task) if st.position == data.position), None)
        if target is None:
            raise HTTPException(404, f"fase {data.position} não encontrada")
        max_executed = max(
            (st.position for st in _active_steps(task) if st.status != STEP_PENDING),
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

    # Se uma fase está em execução e o usuário está reenviando instrução (rewind,
    # retomada), INTERROMPE a execução e segue o comando — o robô é morto pelo
    # stop file da task e a fase atual é entregue ao usuário (não avança). Antes
    # isso era recusado com 409 ("fase em execução") e o usuário nunca conseguia
    # injetar nada.
    if data.position is not None or task.status in (
        TASK_BLOCKED, TASK_NEEDS_REVIEW, TASK_FAILED, TASK_DONE,
    ):
        _force_stop_running(session, task, settings)

    anchor: TaskStep | None = None
    blocked_step: int | None = None

    if target is not None:
        # Reexecuta a partir da fase escolhida (voltar para etapa anterior).
        if task.status == TASK_WAITING_APPROVAL:
            gated = next((st for st in _active_steps(task) if st.pause_before), None)
            if gated is not None:
                gated.pause_before = False
        _rewind_pipeline(session, task, target)
        anchor = target
    elif task.status == TASK_BLOCKED:
        blocked_steps = [st for st in _active_steps(task) if st.status == STEP_BLOCKED]
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
        gated = next((st for st in _active_steps(task) if st.pause_before), None)
        if gated is not None:
            gated.pause_before = False
        anchor = gated or _pick_anchor(task)
        task.status = TASK_QUEUED
    elif task.status in (TASK_NEEDS_REVIEW, TASK_FAILED):
        pending = next((st for st in _active_steps(task) if st.status == STEP_PENDING), None)
        if pending is not None:
            anchor = pending
        else:
            failed = next(
                (st for st in _active_steps(task) if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
                None,
            )
            if failed is None:
                raise HTTPException(400, "nenhuma fase pendente ou falha para continuar")
            _reopen_step(failed)
            anchor = failed
        task.status = TASK_QUEUED
    elif task.status == TASK_DONE:
        last = max(_active_steps(task), key=lambda st: st.position)
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
    active = _active_steps(task)
    if not active:
        return None
    running = next((st for st in active if st.status == STEP_RUNNING), None)
    if running:
        return running
    pending_failed = next(
        (st for st in active if st.status in (STEP_PENDING, STEP_FAILED, STEP_BLOCKED)),
        None,
    )
    return pending_failed or active[-1]


# ---------- Chat human-in-the-loop (modo manual) ----------


def _available_agents(session: Session, repo_id: int) -> list[Robot]:
    """Agentes (robôs) disponíveis para o dispatcher/menu no modo manual: os globais
    (seed) + os do repositório, ativos e não arquivados."""
    from sqlalchemy import or_

    return (
        session.query(Robot)
        .filter(
            or_(Robot.repository_id.is_(None), Robot.repository_id == repo_id),
            Robot.active.is_(True),
            Robot.archived.is_(False),
        )
        .order_by(Robot.name)
        .all()
    )


@router.post("/{task_id}/chat", response_model=ChatMessageResponse)
def send_chat(
    task_id: int,
    data: ChatSendRequest,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Envia uma mensagem no chat human-in-the-loop (modo manual).

    A mensagem vira `pending_action=dispatch`: o chat-worker roda o dispatcher,
    que decide o próximo agente/ação. Requer a task em `open` (modo manual)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status != TASK_OPEN:
        raise HTTPException(
            400, f"chat disponível apenas em tarefas abertas (status atual: {task.status})"
        )
    if task.chat_status != CHAT_STATUS_IDLE:
        raise HTTPException(400, "uma ação já está em andamento nesta tarefa")
    text = (data.text or "").strip()
    if not text:
        raise HTTPException(400, "mensagem vazia")
    max_seq = (
        session.query(func.max(TaskMessage.seq)).filter(TaskMessage.task_id == task.id).scalar()
        or 0
    )
    session.add(TaskMessage(task_id=task.id, seq=max_seq + 1, kind="user", payload={"text": text}))
    task.pending_action = CHAT_DISPATCH
    task.chat_status = CHAT_STATUS_QUEUED
    session.commit()
    return ChatMessageResponse(ok=True, message="mensagem encaminhada ao dispatcher")


@router.get("/{task_id}/chat", response_model=list[TaskMessageOut])
def list_chat(task_id: int, session: Session = Depends(get_session)):
    """Transcript do chat human-in-the-loop da task (mensagens em ordem)."""
    task = _get_task_or_404(session, task_id)
    return (
        session.query(TaskMessage)
        .filter(TaskMessage.task_id == task.id)
        .order_by(TaskMessage.seq)
        .all()
    )


@router.post("/{task_id}/merge", response_model=ChatMessageResponse)
def request_merge(
    task_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Atalho determinístico: dispara a integração (merge+push) via chat-worker.

    Equivalente ao dispatcher decidir `merge`. Só em modo manual (task `open`)."""
    task = _get_task_or_404(session, task_id)
    _ensure_can_act(session, task, user)
    if task.status != TASK_OPEN:
        raise HTTPException(400, f"merge manual só em tarefas abertas (status atual: {task.status})")
    if task.chat_status != CHAT_STATUS_IDLE:
        raise HTTPException(400, "uma ação já está em andamento nesta tarefa")
    task.pending_action = CHAT_MERGE
    task.chat_status = CHAT_STATUS_QUEUED
    session.commit()
    return ChatMessageResponse(ok=True, message="integração encaminhada ao chat-worker")
