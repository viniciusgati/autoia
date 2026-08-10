"""Endpoints de steps: eventos gravados (observabilidade), log bruto e artifacts."""

from __future__ import annotations

import mimetypes
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import RunEvent, StepArtifact, TaskStep
from ..schemas import ArtifactOut, RunEventOut
from .deps import get_session, get_settings

router = APIRouter(prefix="/api/steps", tags=["steps"])


def _get_step_or_404(session: Session, step_id: int) -> TaskStep:
    step = session.get(TaskStep, step_id)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    return step


def _artifact_checkout(step: TaskStep, settings: Settings) -> str:
    """Diretório de checkout isolado onde os artifacts do step residem."""
    return os.path.join(
        settings.workspace_dir,
        str(step.task.repository_id),
        f"task_{step.task_id}",
    )


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


# ── Artifacts ──


@router.get("/{step_id}/artifacts", response_model=list[ArtifactOut])
def list_artifacts(step_id: int, session: Session = Depends(get_session)):
    """Lista os arquivos gerados pelo robô nesta fase (ex.: screenshots)."""
    _get_step_or_404(session, step_id)
    return (
        session.query(StepArtifact)
        .filter(StepArtifact.step_id == step_id)
        .order_by(StepArtifact.created_at)
        .all()
    )


@router.get("/artifacts/{artifact_id}/file")
def get_artifact_file(
    artifact_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """Serve o binário do artifact (imagem)."""
    artifact = session.get(StepArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "artifact não encontrado")
    step = session.get(TaskStep, artifact.step_id)
    if step is None:
        raise HTTPException(404, "fase não encontrada")
    checkout = _artifact_checkout(step, settings)
    full_path = os.path.join(checkout, artifact.filepath)
    if not os.path.isfile(full_path):
        raise HTTPException(404, "arquivo não encontrado no disco")
    media_type, _ = mimetypes.guess_type(full_path)
    return FileResponse(full_path, media_type=media_type or "application/octet-stream")


@router.delete("/{step_id}/artifacts")
def delete_artifacts(
    step_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """Remove todos os artifacts do step (arquivos do disco + registros no banco)."""
    step = _get_step_or_404(session, step_id)
    checkout = _artifact_checkout(step, settings)
    artifacts = (
        session.query(StepArtifact)
        .filter(StepArtifact.step_id == step_id)
        .all()
    )
    for a in artifacts:
        full_path = os.path.join(checkout, a.filepath)
        try:
            os.remove(full_path)
        except OSError:
            pass
        session.delete(a)
    session.commit()
    return {"deleted": len(artifacts)}
