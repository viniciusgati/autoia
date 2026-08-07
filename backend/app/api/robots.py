"""Endpoints de robôs (configuração de agentes)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Robot
from ..schemas import RobotCreate, RobotOut, RobotUpdate
from .deps import get_robot_or_404, get_session

router = APIRouter(prefix="/api/robots", tags=["robots"])


@router.get("", response_model=list[RobotOut])
def list_robots(session: Session = Depends(get_session)):
    return session.query(Robot).order_by(Robot.name).all()


@router.post("", response_model=RobotOut, status_code=201)
def create_robot(data: RobotCreate, session: Session = Depends(get_session)):
    if session.query(Robot).filter(Robot.name == data.name).first():
        raise HTTPException(409, f"robô '{data.name}' já existe")
    robot = Robot(name=data.name, mission=data.mission, role=data.role, model=data.model)
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
    session.commit()
    session.refresh(robot)
    return robot


@router.delete("/{robot_id}", status_code=204)
def delete_robot(robot_id: int, session: Session = Depends(get_session)):
    robot = get_robot_or_404(session, robot_id)
    session.delete(robot)
    session.commit()
