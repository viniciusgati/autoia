"""Endpoints de pipelines (template de fases)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ..models import Pipeline, PipelineStep, Robot
from ..schemas import PipelineCreate, PipelineOut
from .deps import get_repository_or_404, get_session

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


def _scope_filter(repository_id: int | None, model):
    """Filtro por escopo: global (NULL) ou globais + do projeto."""
    col = model.repository_id
    if repository_id is None:
        return col.is_(None)
    return or_(col.is_(None), col == repository_id)


@router.get("", response_model=list[PipelineOut])
def list_pipelines(
    repository_id: int | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(Pipeline)
    if repository_id is not None:
        get_repository_or_404(session, repository_id)
        q = q.filter(_scope_filter(repository_id, Pipeline))
    else:
        # página global: apenas pipelines do sistema (sem repository_id)
        q = q.filter(Pipeline.repository_id.is_(None))
    return (
        q.options(joinedload(Pipeline.steps).joinedload(PipelineStep.robot))
        .order_by(Pipeline.id.desc())
        .all()
    )


@router.post("", response_model=PipelineOut, status_code=201)
def create_pipeline(data: PipelineCreate, session: Session = Depends(get_session)):
    if data.repository_id is not None:
        get_repository_or_404(session, data.repository_id)
    q = session.query(Pipeline).filter(
        Pipeline.name == data.name,
        _scope_filter(data.repository_id, Pipeline),
    )
    if q.first():
        escopo = "globais" if data.repository_id is None else "deste projeto"
        raise HTTPException(409, f"pipeline '{data.name}' já existe nos pipelines {escopo}")

    positions = [step.position for step in data.steps]
    if len(positions) != len(set(positions)):
        raise HTTPException(400, "posições das fases não podem repetir")

    # Robôs válidos: globais + os do mesmo projeto do pipeline (não arquivados).
    robot_q = session.query(Robot).filter(
        _scope_filter(data.repository_id, Robot),
        Robot.archived.is_(False),
    )
    robot_ids = {r.id for r in robot_q.all()}

    pipeline = Pipeline(name=data.name, repository_id=data.repository_id)
    for step in sorted(data.steps, key=lambda x: x.position):
        if step.robot_id not in robot_ids:
            raise HTTPException(400, f"robô {step.robot_id} não existe neste escopo")
        pipeline.steps.append(
            PipelineStep(
                position=step.position,
                robot_id=step.robot_id,
                post_merge=step.post_merge,
                pause_before=step.pause_before,
            )
        )

    session.add(pipeline)
    session.commit()
    return (
        session.query(Pipeline)
        .options(joinedload(Pipeline.steps).joinedload(PipelineStep.robot))
        .filter(Pipeline.id == pipeline.id)
        .one()
    )


@router.delete("/{pipeline_id}", status_code=204)
def delete_pipeline(pipeline_id: int, session: Session = Depends(get_session)):
    pipeline = session.get(Pipeline, pipeline_id)
    if pipeline is None:
        raise HTTPException(404, "pipeline não encontrado")
    session.delete(pipeline)
    session.commit()
