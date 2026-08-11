"""Página global "Execução": tasks ativas + eventos ao vivo + propostas + avisos.

Um único GET entrega tudo que a página precisa por poll (5s): tasks ativas,
últimos ~30 eventos das fases running, propostas pendentes de aprovação humana,
notices (reuso de `dashboard._build_notices`) e o status do worker.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends
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
)
from ..schemas import ExecutionOut, RunEventOut, TaskOut, TaskProposalOut, WorkerStatusOut
from .dashboard import _build_notices
from .deps import get_session, get_settings

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
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    # Tasks ativas (filtro opcional por projeto)
    task_q = session.query(Task)
    if repository_id is not None:
        task_q = task_q.filter(Task.repository_id == repository_id)
    tasks = (
        task_q.filter(Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.updated_at.desc())
        .limit(50)
        .all()
    )

    # Eventos ao vivo das fases running (últimos ~30 por fase)
    running_q = session.query(TaskStep).join(Task).filter(TaskStep.status == "running")
    if repository_id is not None:
        running_q = running_q.filter(Task.repository_id == repository_id)
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

    # Propostas pendentes de aprovação humana
    proposal_q = session.query(TaskProposal).filter(TaskProposal.status == "pending")
    if repository_id is not None:
        proposal_q = proposal_q.join(Task, TaskProposal.task_id == Task.id).filter(
            Task.repository_id == repository_id
        )
    proposals = proposal_q.order_by(TaskProposal.created_at.desc()).limit(50).all()

    return ExecutionOut(
        tasks=[TaskOut.model_validate(t) for t in tasks],
        current_events=current_events,
        proposals=[TaskProposalOut.model_validate(p) for p in proposals],
        notices=_build_notices(session),
        worker=_worker_status(settings),
    )
