"""Endpoints de pipelines (template de fases)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from ..models import Pipeline, PipelineStep, Robot
from ..schemas import PipelineCreate, PipelineOut
from .deps import get_session

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


@router.get("", response_model=list[PipelineOut])
def list_pipelines(session: Session = Depends(get_session)):
    return (
        session.query(Pipeline)
        .options(joinedload(Pipeline.steps).joinedload(PipelineStep.robot))
        .order_by(Pipeline.id.desc())
        .all()
    )


@router.post("", response_model=PipelineOut, status_code=201)
def create_pipeline(data: PipelineCreate, session: Session = Depends(get_session)):
    if session.query(Pipeline).filter(Pipeline.name == data.name).first():
        raise HTTPException(409, f"pipeline '{data.name}' já existe")

    robot_ids = {r.id for r in session.query(Robot).all()}
    positions = [step.position for step in data.steps]
    if len(positions) != len(set(positions)):
        raise HTTPException(400, "posições das fases não podem repetir")

    pipeline = Pipeline(name=data.name)
    for step in sorted(data.steps, key=lambda x: x.position):
        if step.robot_id not in robot_ids:
            raise HTTPException(400, f"robô {step.robot_id} não existe")
        pipeline.steps.append(
            PipelineStep(
                position=step.position, robot_id=step.robot_id, post_merge=step.post_merge
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
