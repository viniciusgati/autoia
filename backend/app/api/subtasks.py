"""Endpoints de subtarefas (listar, criar, editar, retry)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import (
    SUB_FAILED,
    SUB_PENDING,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    TASK_IN_PROGRESS,
    TASK_QUEUED,
    SubTask,
)
from ..schemas import SubTaskIn, SubTaskOut, SubTaskUpdate
from ..worker.runner import _pm_decide
from .deps import get_session, get_settings
from .tasks import _get_task_or_404

log = logging.getLogger("autoia.api")

router = APIRouter(prefix="/api/tasks/{task_id}/subtasks", tags=["subtasks"])


def _serialize_subtask(st: SubTask) -> dict:
    return SubTaskOut.model_validate(st).model_dump()


@router.get("", response_model=list[SubTaskOut])
def list_subtasks(task_id: int, session: Session = Depends(get_session)):
    task = _get_task_or_404(session, task_id)
    return sorted(task.subtasks, key=lambda s: s.position)


@router.post("", response_model=SubTaskOut, status_code=201)
def create_subtask(
    task_id: int,
    data: SubTaskIn,
    session: Session = Depends(get_session),
):
    task = _get_task_or_404(session, task_id)
    if task.status == "failed":
        raise HTTPException(400, "tarefa falhou — crie uma nova task")
    position = max((st.position for st in task.subtasks), default=-1) + 1
    st = SubTask(
        task_id=task.id,
        position=position,
        title=data.title,
        description=data.description,
        acceptance_criteria=data.acceptance_criteria,
    )
    session.add(st)
    session.commit()
    return _serialize_subtask(st)


@router.patch("/{position}", response_model=SubTaskOut)
def update_subtask(
    task_id: int,
    position: int,
    data: SubTaskUpdate,
    session: Session = Depends(get_session),
):
    task = _get_task_or_404(session, task_id)
    st = next((s for s in task.subtasks if s.position == position), None)
    if st is None:
        raise HTTPException(404, "subtarefa não encontrada")
    if data.title is not None:
        st.title = data.title
    if data.description is not None:
        st.description = data.description
    if data.acceptance_criteria is not None:
        st.acceptance_criteria = data.acceptance_criteria
    session.commit()
    return _serialize_subtask(st)


@router.post("/{position}/retry", response_model=SubTaskOut)
def retry_subtask(
    task_id: int,
    position: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    """Reabre uma subtarefa específica para re-execução.

    Se a subtarefa falhou ou precisa ser refeita, volta a `pending` e
    reencaminha a task para execução (implement → verify para esta subtarefa).
    """
    task = _get_task_or_404(session, task_id)
    st = next((s for s in task.subtasks if s.position == position), None)
    if st is None:
        raise HTTPException(404, "subtarefa não encontrada")
    if st.status == SUB_PENDING and task.status in (TASK_QUEUED, TASK_IN_PROGRESS):
        # Subtarefa `pending` só é "em andamento" enquanto o worker pode processá-la
        # (task na fila/rodando). Se a task morreu (failed/needs_review/blocked/…),
        # o worker nunca mais vai reclamar a subtarefa — retry é a única saída.
        raise HTTPException(400, f"subtarefa em andamento (status: {st.status})")
    if st.attempt >= settings.max_attempts:
        raise HTTPException(400, f"tentativas máximas atingidas ({settings.max_attempts})")

    st.status = SUB_PENDING
    st.attempt += 1
    st.verdict = None
    st.summary = None
    st.error = None
    st.started_at = None
    st.finished_at = None

    # Reabre a fase implement se a task estiver travada (verify rejeitou).
    implement_step = next(
        (s for s in task.steps if s.position == position_implement(task)),
        None,
    )
    if implement_step and implement_step.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED, "done"):
        implement_step.status = "pending"
        implement_step.error = None
        implement_step.summary = None
        implement_step.finished_at = None
        implement_step.started_at = None

    # Evento de auditoria: quem fez o retry e de qual subtarefa
    if implement_step:
        from sqlalchemy import func
        from ..models import RunEvent
        max_seq = (
            session.query(func.max(RunEvent.seq))
            .filter(RunEvent.step_id == implement_step.id)
            .scalar() or 0
        )
        session.add(RunEvent(
            step_id=implement_step.id,
            seq=max_seq + 1,
            kind="human_subtask_retry",
            payload={
                "position": st.position,
                "title": st.title,
                "attempt": st.attempt + 1,
            },
        ))

    task.status = TASK_QUEUED
    task.error = None
    session.commit()
    return _serialize_subtask(st)


def position_implement(task) -> int:
    """Posição do step `implement` no pipeline da task (a que executa as subtarefas)."""
    for st in task.steps:
        if st.robot and st.robot.role == "implement":
            return st.position
    return -1
