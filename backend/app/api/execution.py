"""Página global "Execução": tasks ativas + eventos ao vivo + propostas + avisos.

Um único GET entrega tudo que a página precisa por poll (5s): tasks ativas,
últimos ~30 eventos das fases running, propostas pendentes de aprovação humana,
notices (reuso de `dashboard._build_notices`) e o status do worker.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    TASK_BLOCKED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_PAUSED,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    RunEvent,
    Task,
    TaskProposal,
    TaskStep,
    User,
)
from ..schemas import ExecutionOut, RunEventOut, TaskProposalOut, WorkerStatusOut
from .dashboard import _build_notices, _user_repo_ids
from .deps import get_session, get_settings, require_auth
from .etag import conditional
from .scope import make_scoped
from .tasks import _task_list_item

router = APIRouter(prefix="/api/execution", tags=["execution"])

ACTIVE_STATUSES = [
    TASK_QUEUED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_WAITING_APPROVAL,
    TASK_BLOCKED,
    TASK_PAUSED,
]


def _worker_status(settings: Settings) -> WorkerStatusOut:
    hb = os.path.join(settings.workspace_dir, "worker.heartbeat")
    try:
        mtime = os.path.getmtime(hb)
        age = time.time() - mtime
    except OSError:
        return WorkerStatusOut(alive=False, last_heartbeat_sec=None)
    return WorkerStatusOut(alive=age < 15, last_heartbeat_sec=round(age, 1))


@router.get("", response_model=ExecutionOut)
def execution(
    repository_id: int | None = None,
    request: Request = None,
    response: Response = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    # Com auth ON, a página global fica restrita ao escopo de visibilidade do
    # usuário: participa do projeto da task OU é o responsável dela OU a task
    # não tem responsável (espelha `_ensure_can_act`). O escopo vale também
    # para propostas, avisos, eventos ao vivo e o token de cache.
    repo_ids: list[int] | None = None
    user_id: int | None = None
    if user is not None:
        repo_ids = _user_repo_ids(session, user.id)
        user_id = user.id
    scoped = make_scoped(repo_ids, user_id, repository_id)

    max_task_ts = scoped(session.query(func.max(Task.updated_at))).scalar()
    event_q = (
        session.query(func.max(RunEvent.id))
        .join(TaskStep, RunEvent.step_id == TaskStep.id)
        .join(Task, TaskStep.task_id == Task.id)
    )
    max_event_id = scoped(event_q).scalar()
    max_proposal_id, pending_proposals = scoped(
        session.query(
            func.max(TaskProposal.id),
            func.count(TaskProposal.id).filter(TaskProposal.status == "pending"),
        ).join(Task, TaskProposal.task_id == Task.id)
    ).first()
    hb = os.path.join(settings.workspace_dir, "worker.heartbeat")
    try:
        hb_mtime = os.path.getmtime(hb)
    except OSError:
        hb_mtime = None
    token = "|".join(
        str(x) for x in (max_task_ts, max_event_id, max_proposal_id, pending_proposals, hb_mtime)
    )
    not_modified = conditional(request, response, token)
    if not_modified is not None:
        return not_modified

    # Tasks ativas (escopo de visibilidade + filtro opcional por projeto)
    tasks = (
        scoped(session.query(Task))
        .filter(Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.updated_at.desc())
        .limit(50)
        .all()
    )

    # Eventos ao vivo das fases running (últimos ~30 por fase)
    running_q = scoped(session.query(TaskStep).join(Task)).filter(TaskStep.status == "running")
    current_events: dict[str, list[RunEventOut]] = {}
    for step in running_q.order_by(TaskStep.id.desc()).limit(10).all():
        events = (
            session.query(RunEvent)
            .filter(RunEvent.step_id == step.id)
            .order_by(RunEvent.seq.desc())
            .limit(30)
            .all()
        )
        current_events[str(step.id)] = [RunEventOut.model_validate(e) for e in events]

    # Propostas pendentes de aprovação humana (mesmo escopo das tasks)
    proposal_q = scoped(
        session.query(TaskProposal).join(Task, TaskProposal.task_id == Task.id)
    ).filter(TaskProposal.status == "pending")
    proposals = proposal_q.order_by(TaskProposal.created_at.desc()).limit(50).all()

    return ExecutionOut(
        tasks=[_task_list_item(t) for t in tasks],
        current_events=current_events,
        proposals=[TaskProposalOut.model_validate(p) for p in proposals],
        notices=_build_notices(session, scoped),
        worker=_worker_status(settings),
    )
