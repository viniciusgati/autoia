"""Endpoints de robôs (configuração de agentes)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import Robot
from ..schemas import RobotCreate, RobotOut, RobotUpdate
from .deps import get_repository_or_404, get_robot_or_404, get_session

router = APIRouter(prefix="/api/robots", tags=["robots"])


def _scope_filter(repository_id: int | None, model):
    """Filtro por escopo: sem filtro → globais; com filtro → globais + do projeto."""
    col = model.repository_id
    if repository_id is None:
        return col.is_(None)
    return or_(col.is_(None), col == repository_id)


@router.get("", response_model=list[RobotOut])
def list_robots(
    repository_id: int | None = None,
    archived: bool | None = None,
    session: Session = Depends(get_session),
):
    q = session.query(Robot)
    if repository_id is not None:
        get_repository_or_404(session, repository_id)
        q = q.filter(_scope_filter(repository_id, Robot))
    else:
        # página global: apenas robôs do sistema (sem repository_id)
        q = q.filter(Robot.repository_id.is_(None))
    if archived is not None:
        q = q.filter(Robot.archived.is_(archived))
    else:
        q = q.filter(Robot.archived.is_(False))
    return q.order_by(Robot.name).all()


@router.post("", response_model=RobotOut, status_code=201)
def create_robot(data: RobotCreate, session: Session = Depends(get_session)):
    if data.repository_id is not None:
        get_repository_or_404(session, data.repository_id)
    # Unicidade por escopo: mesmo nome dentro do mesmo projeto (ou global).
    q = session.query(Robot).filter(
        Robot.name == data.name,
        _scope_filter(data.repository_id, Robot),
    )
    if q.first():
        escopo = "globais" if data.repository_id is None else "deste projeto"
        raise HTTPException(409, f"robô '{data.name}' já existe nos robôs {escopo}")
    robot = Robot(
        name=data.name,
        mission=data.mission,
        role=data.role,
        model=data.model,
        repository_id=data.repository_id,
    )
    session.add(robot)
    session.commit()
    session.refresh(robot)
    return robot


@router.put("/{robot_id}", response_model=RobotOut)
def update_robot(
    robot_id: int, data: RobotUpdate, session: Session = Depends(get_session)
):
    robot = get_robot_or_404(session, robot_id)
    if data.mission is not None:
        robot.mission = data.mission
    if data.model is not None:
        robot.model = data.model
    if data.active is not None:
        robot.active = data.active
    if data.archived is not None:
        robot.archived = data.archived
    session.commit()
    session.refresh(robot)
    return robot


@router.delete("/{robot_id}", status_code=204)
def delete_robot(robot_id: int, session: Session = Depends(get_session)):
    robot = get_robot_or_404(session, robot_id)
    session.delete(robot)
    session.commit()
