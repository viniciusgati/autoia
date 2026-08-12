"""Dependências compartilhadas dos routers."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import Repository, RepositoryUser, Robot
from ..models import Session as AuthSession
from ..models import User


def get_session(request: Request):
    with request.app.state.Session() as s:
        yield s


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def is_repo_admin(session: Session, user: User, repo_id: int) -> bool:
    """Admin do projeto: participação `repository_users` com papel `admin`."""
    return (
        session.query(RepositoryUser)
        .filter(
            RepositoryUser.repository_id == repo_id,
            RepositoryUser.user_id == user.id,
            RepositoryUser.role == "admin",
        )
        .first()
        is not None
    )


def require_auth(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User | None:
    """Usuário autenticado pela sessão do cookie `autoia_session`.

    Com `Settings.auth_enabled=False` (comportamento legado) retorna `None` e
    nenhuma rota exige sessão — a suíte antiga permanece inalterada. Com auth ON,
    qualquer rota `/api/*` protegida responde 401 sem cookie/sessão inválida.
    """
    if not settings.auth_enabled:
        return None
    token = request.cookies.get("autoia_session")
    if not token:
        raise HTTPException(401, "não autenticado — faça login")
    auth_session = (
        session.query(AuthSession).filter(AuthSession.token == token).first()
    )
    if auth_session is None or auth_session.expires_at < utcnow():
        raise HTTPException(401, "sessão expirada ou inválida — entre novamente")
    user = session.get(User, auth_session.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "usuário inativo ou removido")
    return user


def require_admin(
    user: User | None = Depends(require_auth),
) -> User | None:
    """Admin global (ou None com auth OFF — guardas de admin checam `is_admin`)."""
    if user is not None and not user.is_admin:
        raise HTTPException(403, "apenas admin global pode fazer esta operação")
    return user


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
