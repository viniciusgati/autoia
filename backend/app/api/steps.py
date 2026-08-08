"""Endpoints de steps: eventos gravados (observabilidade) e log bruto."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..models import RunEvent, TaskStep
from ..schemas import RunEventOut
from .deps import get_session

router = APIRouter(prefix="/api/steps", tags=["steps"])


def _get_step_or_404(session: Session, step_id: int) -> TaskStep:
    step = session.get(TaskStep, step_id)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    return step


@router.get("/{step_id}/events", response_model=list[RunEventOut])
def list_events(
    step_id: int,
    kind: str | None = None,
    offset: int = 0,
    limit: int = 200,
    order: Literal["asc", "desc"] = "asc",
    session: Session = Depends(get_session),
):
    _get_step_or_404(session, step_id)
    query = session.query(RunEvent).filter(RunEvent.step_id == step_id)
    if kind:
        query = query.filter(RunEvent.kind == kind)
    ordering = RunEvent.seq.desc() if order == "desc" else RunEvent.seq
    return (
        query.order_by(ordering)
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 1000))
        .all()
    )


@router.get("/{step_id}/log")
def get_log(step_id: int, session: Session = Depends(get_session)):
    """Log bruto do transcript (últimas ~2000 linhas)."""
    step = _get_step_or_404(session, step_id)
    if not step.log_path or not os.path.isfile(step.log_path):
        return Response(content="", media_type="text/plain; charset=utf-8")
    with open(step.log_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    tail = lines[-5000:]
    return Response(content="".join(tail), media_type="text/plain; charset=utf-8")
