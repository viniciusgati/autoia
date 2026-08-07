"""Dependências compartilhadas dos routers."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Repository, Robot


def get_session(request: Request):
    with request.app.state.Session() as s:
        yield s


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repository_or_404(session: Session, repo_id: int) -> Repository:
    repo = session.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(404, "repositório não encontrado")
    return repo


def get_robot_or_404(session: Session, robot_id: int) -> Robot:
    robot = session.get(Robot, robot_id)
    if robot is None:
        raise HTTPException(404, "robô não encontrado")
    return robot
