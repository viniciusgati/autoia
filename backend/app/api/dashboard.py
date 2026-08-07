"""Dashboard: métricas agregadas."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import RunEvent, Task
from ..schemas import DashboardOut, RunEventOut
from .deps import get_session

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOut)
def dashboard(session: Session = Depends(get_session)):
    rows = (
        session.query(Task.status, func.count(Task.id))
        .group_by(Task.status)
        .all()
    )
    tasks_by_status = {status: count for status, count in rows}
    total_cost = (
        session.query(func.sum(RunEvent.cost)).scalar() or 0.0
    )
    guardrail_events = (
        session.query(func.count(RunEvent.id))
        .filter(RunEvent.kind == "guardrail_blocked")
        .scalar()
        or 0
    )
    recent_guardrails = (
        session.query(RunEvent)
        .filter(RunEvent.kind == "guardrail_blocked")
        .order_by(RunEvent.id.desc())
        .limit(10)
        .all()
    )
    total_tasks = sum(tasks_by_status.values())
    return DashboardOut(
        tasks_by_status=tasks_by_status,
        total_cost=round(total_cost, 4),
        total_tasks=total_tasks,
        guardrail_events=guardrail_events,
        recent_guardrails=[RunEventOut.model_validate(e) for e in recent_guardrails],
    )
