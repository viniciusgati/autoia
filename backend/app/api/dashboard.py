"""Dashboard: métricas agregadas + avisos de tarefas que requerem atenção."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    STEP_GUARDRAIL_BLOCKED,
    TASK_BLOCKED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    RunEvent,
    Task,
    TaskStep,
)
from ..schemas import DashboardOut, NoticeOut, RunEventOut
from .deps import get_session

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Níveis da métrica de arquitetura (worker/arch_metric.py) que viram aviso.
_ARCH_NOTICE_LEVELS = {"alto": "critical", "médio": "warning"}


def _build_notices(session: Session) -> list[NoticeOut]:
    """Avisos de tarefas que precisam de atenção, mais críticos primeiro."""
    notices: list[NoticeOut] = []

    # steps bloqueados por guardrail (a task segue, mas merece olhar)
    blocked_steps = (
        session.query(TaskStep)
        .join(Task)
        .filter(TaskStep.status == STEP_GUARDRAIL_BLOCKED)
        .order_by(TaskStep.id.desc())
        .limit(10)
        .all()
    )
    for step in blocked_steps:
        notices.append(
            NoticeOut(
                task_id=step.task.id,
                task_title=step.task.title,
                task_status=step.task.status,
                repository_id=step.task.repository_id,
                level="critical",
                kind="guardrail",
                message=step.error or "guardrail bloqueou a execução",
                ts=step.finished_at or step.started_at or step.task.updated_at,
            )
        )

    # tasks aguardando revisão humana/PM
    review_tasks = (
        session.query(Task)
        .filter(Task.status == TASK_NEEDS_REVIEW)
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in review_tasks:
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="needs_review",
                message=task.error or "aguardando revisão",
                ts=task.updated_at,
            )
        )

    # tasks paradas em gate de aprovação humana (pause_before no pipeline)
    gate_tasks = (
        session.query(Task)
        .filter(Task.status == TASK_WAITING_APPROVAL)
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in gate_tasks:
        gated = next(
            (st for st in sorted(task.steps, key=lambda x: x.position)
             if st.status == "pending" and st.pause_before),
            None,
        )
        robot = gated.robot.name if gated and gated.robot else "?"
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="human_gate",
                message=f"aguardando aprovação para executar a fase "
                        f"F{gated.position if gated else '?'} · {robot}",
                ts=task.updated_at,
            )
        )

    # tasks bloqueadas (ex.: conflito de merge)
    blocked_tasks = (
        session.query(Task)
        .filter(Task.status == TASK_BLOCKED)
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in blocked_tasks:
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="critical",
                kind="blocked",
                message=task.error or "tarefa bloqueada",
                ts=task.updated_at,
            )
        )

    # tasks ativas com custo perto do limite (>= 80% do orçamento)
    costly_tasks = (
        session.query(Task)
        .filter(
            Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
            Task.cost_spent >= 0.8 * Task.budget_limit,
        )
        .order_by(Task.cost_spent.desc())
        .limit(10)
        .all()
    )
    for task in costly_tasks:
        pct = 100.0 * task.cost_spent / task.budget_limit if task.budget_limit else 0.0
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="budget_high",
                message=f"custo alto: {pct:.0f}% do orçamento ({task.cost_spent:.2f} US$)",
                ts=task.updated_at,
            )
        )

    # métrica de arquitetura: mudança drástica de deploy/arquitetura
    arch_events = (
        session.query(RunEvent, TaskStep, Task)
        .join(TaskStep, RunEvent.step_id == TaskStep.id)
        .join(Task, TaskStep.task_id == Task.id)
        .filter(RunEvent.kind == "arch_metric")
        .order_by(RunEvent.id.desc())
        .limit(30)
        .all()
    )
    for event, _step, task in arch_events:
        level = (event.payload or {}).get("level")
        if level not in _ARCH_NOTICE_LEVELS:
            continue
        reasons = (event.payload or {}).get("reasons") or []
        message = "arquitetura/deploy alterados: " + ", ".join(str(r) for r in reasons)
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level=_ARCH_NOTICE_LEVELS[level],
                kind="arch",
                message=message[:160],
                ts=event.ts,
            )
        )

    # mais críticos primeiro; dentro do mesmo nível, mais recentes primeiro
    notices.sort(
        key=lambda n: (0 if n.level == "critical" else 1, -n.ts.timestamp())
    )
    return notices[:10]


@router.get("", response_model=DashboardOut)
def dashboard(
    repository_id: int | None = None,
    session: Session = Depends(get_session),
):
    # base query para Task, opcionalmente filtrada por repositório
    task_q = session.query(Task)
    if repository_id is not None:
        task_q = task_q.filter(Task.repository_id == repository_id)

    rows = (
        task_q.with_entities(Task.status, func.count(Task.id))
        .group_by(Task.status)
        .all()
    )
    tasks_by_status = {status: count for status, count in rows}

    # custo total: filtra eventos cuja task pertence ao repositório
    cost_q = session.query(func.sum(RunEvent.cost))
    if repository_id is not None:
        cost_q = (
            cost_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
            .filter(Task.repository_id == repository_id)
        )
    total_cost = cost_q.scalar() or 0.0

    # guardrail events
    guard_q = session.query(func.count(RunEvent.id)).filter(
        RunEvent.kind == "guardrail_blocked"
    )
    if repository_id is not None:
        guard_q = (
            guard_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
            .filter(Task.repository_id == repository_id)
        )
    guardrail_events = guard_q.scalar() or 0

    recent_guardrails_q = (
        session.query(RunEvent)
        .filter(RunEvent.kind == "guardrail_blocked")
    )
    if repository_id is not None:
        recent_guardrails_q = (
            recent_guardrails_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
            .filter(Task.repository_id == repository_id)
        )
    recent_guardrails = (
        recent_guardrails_q.order_by(RunEvent.id.desc()).limit(10).all()
    )

    total_tasks = sum(tasks_by_status.values())
    return DashboardOut(
        tasks_by_status=tasks_by_status,
        total_cost=round(total_cost, 4),
        total_tasks=total_tasks,
        guardrail_events=guardrail_events,
        recent_guardrails=[RunEventOut.model_validate(e) for e in recent_guardrails],
        notices=_build_notices(session),
    )
