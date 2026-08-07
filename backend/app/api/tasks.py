"""Endpoints de tarefas (criar, iniciar, revisar, retry, PM)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from ..models import (
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_PENDING,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_QUEUED,
    Pipeline,
    Task,
    TaskStep,
)
from ..schemas import ReviewRequest, TaskCreate, TaskOut
from ..worker.runner import _pm_decide
from .deps import get_repository_or_404, get_session, get_settings

log = logging.getLogger("autoia.api")

TASK_CREATED = "created"

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_query(session: Session):
    return session.query(Task).options(
        joinedload(Task.steps).joinedload(TaskStep.robot),
        joinedload(Task.repository),
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
    if not pipeline.steps:
        raise HTTPException(400, "pipeline sem fases")

    task = Task(
        repository_id=data.repository_id,
        pipeline_id=pipeline.id,
        title=data.title,
        description=data.description,
        kind=data.kind,
        budget_limit=data.budget_limit if data.budget_limit is not None else settings.task_budget,
    )
    for step in sorted(pipeline.steps, key=lambda x: x.position):
        task.steps.append(
            TaskStep(
                position=step.position,
                robot_id=step.robot_id,
                post_merge=step.post_merge,
            )
        )
    session.add(task)
    session.commit()
    return _get_task_or_404(session, task.id)


@router.get("", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)):
    return _task_query(session).order_by(Task.id.desc()).limit(100).all()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)):
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


@router.post("/{task_id}/steps/{position}/retry", response_model=TaskOut)
def retry_step(
    task_id: int,
    position: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    task = _get_task_or_404(session, task_id)
    step = next((st for st in task.steps if st.position == position), None)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    if step.status not in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED):
        raise HTTPException(400, f"fase não pode ser repetida (status: {step.status})")
    if step.attempt >= settings.max_attempts:
        raise HTTPException(400, f"tentativas máximas atingidas ({settings.max_attempts})")
    step.attempt += 1
    step.status = STEP_PENDING
    step.error = None
    step.summary = None
    step.finished_at = None
    task.status = TASK_QUEUED
    task.error = None
    session.commit()
    return _get_task_or_404(session, task_id)


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
