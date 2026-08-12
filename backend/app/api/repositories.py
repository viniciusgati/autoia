"""Endpoints de repositórios (registro + clone + config + membros)."""

from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Repository, RepositoryUser, User
from ..schemas import (
    RepositoryCreate,
    RepositoryMemberCreate,
    RepositoryOut,
    RepositoryUpdate,
    RepositoryUserOut,
    RepositoryUserUpdate,
)
from ..worker import gitops
from .deps import (
    get_repository_or_404,
    get_session,
    get_settings,
    is_repo_admin,
    require_auth,
)

log = logging.getLogger("autoia.api")

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


def _ensure_repo_admin(session: Session, user: User | None, repo: Repository) -> None:
    """POST/PATCH/DELETE de membros exige admin global ou admin do projeto."""
    if user is None:
        return
    if user.is_admin or is_repo_admin(session, user, repo.id):
        return
    raise HTTPException(403, "apenas admin global ou admin do projeto gerencia membros")


@router.post("", response_model=RepositoryOut, status_code=201)
def create_repository(
    data: RepositoryCreate,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
):
    if session.query(Repository).filter(Repository.name == data.name).first():
        raise HTTPException(409, f"repositório '{data.name}' já existe")

    repo = Repository(
        name=data.name,
        url=data.url,
        default_branch=data.default_branch,
        local_path=None,
        max_attempts=data.max_attempts,
        max_pm_decisions=data.max_pm_decisions,
        run_timeout=data.run_timeout,
        task_budget=data.task_budget,
        cost_per_interaction=data.cost_per_interaction,
        risky_patterns_extra=data.risky_patterns_extra,
        db_rule=data.db_rule,
        allow_auto_tasks=data.allow_auto_tasks,
        allow_external_tasks=data.allow_external_tasks,
        default_pipeline_id=data.default_pipeline_id,
        auto_summary=data.auto_summary,
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)

    dest = os.path.abspath(os.path.join(settings.workspace_dir, str(repo.id)))
    # descarta checkout órfão de tentativa anterior (falha no meio do clone)
    shutil.rmtree(dest, ignore_errors=True)
    try:
        gitops.clone(data.url, dest)
        if gitops.repo_is_empty(dest):
            # remote recém-criado sem branch (ex.: repo novo no GitHub): cria
            # branch default com README básico e commit inicial, depois segue.
            gitops.bootstrap_empty_repo(
                dest,
                data.default_branch or "main",
                data.name,
                settings.git_user_name,
                settings.git_user_email,
            )
        default_branch = gitops.resolve_default_branch(dest, data.default_branch)
    except (gitops.GitError, OSError) as exc:
        session.delete(repo)
        session.commit()
        # não deixa checkout parcial para trás (senão o retry falha com "already exists")
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(400, f"falha ao clonar: {exc}") from exc

    repo.local_path = dest
    repo.default_branch = default_branch
    session.commit()
    session.refresh(repo)
    return repo


@router.get("", response_model=list[RepositoryOut])
def list_repositories(session: Session = Depends(get_session)):
    return session.query(Repository).order_by(Repository.id.desc()).all()


@router.put("/{repo_id}", response_model=RepositoryOut)
def update_repository(
    repo_id: int,
    data: RepositoryUpdate,
    session: Session = Depends(get_session),
):
    repo = get_repository_or_404(session, repo_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(repo, field, value)
    session.commit()
    session.refresh(repo)
    return repo


@router.delete("/{repo_id}", status_code=204)
def delete_repository(repo_id: int, session: Session = Depends(get_session)):
    repo = get_repository_or_404(session, repo_id)
    session.delete(repo)
    session.commit()


# ---------- Membros do projeto ----------


@router.get("/{repo_id}/members", response_model=list[RepositoryUserOut])
def list_members(
    repo_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Participações do projeto (qualquer autenticado pode listar — alimenta o
    controle de atribuição de tarefas; gestão exige admin do projeto)."""
    get_repository_or_404(session, repo_id)
    return (
        session.query(RepositoryUser)
        .filter(RepositoryUser.repository_id == repo_id)
        .order_by(RepositoryUser.id)
        .all()
    )


@router.post("/{repo_id}/members", response_model=RepositoryUserOut, status_code=201)
def add_member(
    repo_id: int,
    data: RepositoryMemberCreate,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Adiciona um usuário como membro do projeto (admin global ou do projeto)."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo)
    target = session.get(User, data.user_id)
    if target is None:
        raise HTTPException(404, "usuário não encontrado")
    member = (
        session.query(RepositoryUser)
        .filter(
            RepositoryUser.repository_id == repo_id,
            RepositoryUser.user_id == data.user_id,
        )
        .first()
    )
    if member is not None:
        raise HTTPException(409, "usuário já é membro do projeto")
    member = RepositoryUser(
        repository_id=repo_id, user_id=data.user_id, role=data.role
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


@router.patch("/{repo_id}/members/{user_id}", response_model=RepositoryUserOut)
def update_member(
    repo_id: int,
    user_id: int,
    data: RepositoryUserUpdate,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Altera o papel de um membro (admin global ou do projeto)."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo)
    member = (
        session.query(RepositoryUser)
        .filter(
            RepositoryUser.repository_id == repo_id,
            RepositoryUser.user_id == user_id,
        )
        .first()
    )
    if member is None:
        raise HTTPException(404, "usuário não é membro do projeto")
    member.role = data.role
    session.commit()
    session.refresh(member)
    return member


@router.delete("/{repo_id}/members/{user_id}", status_code=204)
def remove_member(
    repo_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Remove um usuário do projeto (admin global ou do projeto)."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo)
    member = (
        session.query(RepositoryUser)
        .filter(
            RepositoryUser.repository_id == repo_id,
            RepositoryUser.user_id == user_id,
        )
        .first()
    )
    if member is None:
        raise HTTPException(404, "usuário não é membro do projeto")
    session.delete(member)
    session.commit()
