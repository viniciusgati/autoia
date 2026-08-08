"""Endpoints de repositórios (registro + clone)."""

from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Repository
from ..schemas import RepositoryCreate, RepositoryOut
from ..worker import gitops
from .deps import get_repository_or_404, get_session, get_settings

log = logging.getLogger("autoia.api")

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


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
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)

    dest = os.path.abspath(os.path.join(settings.workspace_dir, str(repo.id)))
    # descarta checkout órfão de tentativa anterior (falha no meio do clone)
    shutil.rmtree(dest, ignore_errors=True)
    try:
        gitops.clone(data.url, dest)
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


@router.delete("/{repo_id}", status_code=204)
def delete_repository(repo_id: int, session: Session = Depends(get_session)):
    repo = get_repository_or_404(session, repo_id)
    session.delete(repo)
    session.commit()
