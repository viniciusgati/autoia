"""Endpoints de repositórios (registro + clone + config + membros + skills)."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import skills as skills_mod
from ..config import Settings
from ..models import Repository, RepositorySkill, RepositoryUser, User
from ..schemas import (
    RepositoryCreate,
    RepositoryMemberCreate,
    RepositoryOut,
    RepositorySkillOut,
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


def _ensure_repo_admin(
    session: Session,
    user: User | None,
    repo: Repository,
    message: str = "apenas admin global ou admin do projeto gerencia membros",
) -> None:
    """Ação restrita exige admin global ou admin do projeto.

    Com auth OFF (`user is None`) preserva o comportamento legado (permitido).
    """
    if user is None:
        return
    if user.is_admin or is_repo_admin(session, user, repo.id):
        return
    raise HTTPException(403, message)


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


# ---------- Skills do projeto ----------

_SKILL_ADMIN_MESSAGE = "apenas admin global ou admin do projeto gerencia skills"


def _get_repo_skill_or_404(
    session: Session, repo_id: int, skill_id: int
) -> RepositorySkill:
    """Skill do repositório, 404 se não existir ou pertencer a outro projeto."""
    skill = session.get(RepositorySkill, skill_id)
    if skill is None or skill.repository_id != repo_id:
        raise HTTPException(404, "skill não encontrada")
    return skill


@router.get("/{repo_id}/skills", response_model=list[RepositorySkillOut])
def list_skills(
    repo_id: int,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    """Lista as skills do projeto (admin global ou admin do projeto; auth OFF → None)."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo, _SKILL_ADMIN_MESSAGE)
    return (
        session.query(RepositorySkill)
        .filter(RepositorySkill.repository_id == repo_id)
        .order_by(RepositorySkill.id)
        .all()
    )


@router.post("/{repo_id}/skills", response_model=RepositorySkillOut, status_code=201)
def upload_skill(
    repo_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Envia uma skill de projeto: `.zip` com `SKILL.md` na raiz.

    Validação (via `app.skills.validate_and_extract`): ≤ 5 MB, ≤ 50 entradas, sem
    path traversal/absoluto; qualquer violação → 400 com a mensagem específica e
    nada é extraído. Arquivos gravados em `data/skills/<repo_id>/<skill_id>/`;
    nome duplicado no mesmo projeto → 409.
    """
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo, _SKILL_ADMIN_MESSAGE)

    raw = file.file.read(skills_mod.MAX_SKILL_ZIP_BYTES + 1)
    if len(raw) > skills_mod.MAX_SKILL_ZIP_BYTES:
        raise HTTPException(400, "arquivo muito grande (máx. 5 MB)")
    zip_filename = file.filename or "skill.zip"

    # Extrai num diretório temporário (o id da skill ainda não existe) e só move
    # para o destino final após o registro no banco; validação falhou → nada sobra.
    tmp_dir = os.path.join(
        settings.skills_dir, str(repo_id), f".upload-{uuid.uuid4().hex}"
    )
    try:
        meta = skills_mod.validate_and_extract(raw, tmp_dir, zip_filename=zip_filename)
    except skills_mod.SkillZipError as exc:
        skills_mod.remove_skill_dir(tmp_dir)
        raise HTTPException(400, str(exc)) from exc

    existing = (
        session.query(RepositorySkill)
        .filter(
            RepositorySkill.repository_id == repo_id,
            RepositorySkill.name == meta["name"],
        )
        .first()
    )
    if existing is not None:
        skills_mod.remove_skill_dir(tmp_dir)
        raise HTTPException(409, f"skill '{meta['name']}' já existe neste projeto")

    skill = RepositorySkill(repository_id=repo_id, **meta)
    session.add(skill)
    session.commit()
    session.refresh(skill)
    final_dir = os.path.join(settings.skills_dir, str(repo_id), str(skill.id))
    os.makedirs(os.path.dirname(final_dir), exist_ok=True)
    os.replace(tmp_dir, final_dir)  # mesmo filesystem → rename atômico
    return skill


@router.get("/{repo_id}/skills/{skill_id}/file")
def get_skill_file(
    repo_id: int,
    skill_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Conteúdo do `SKILL.md` da skill (texto UTF-8) para o preview na UI."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo, _SKILL_ADMIN_MESSAGE)
    _get_repo_skill_or_404(session, repo_id, skill_id)
    skill_md = os.path.join(
        settings.skills_dir, str(repo_id), str(skill_id), skills_mod.SKILL_MD
    )
    if not os.path.isfile(skill_md):
        raise HTTPException(404, "SKILL.md não encontrado no disco")
    return Response(
        content=Path(skill_md).read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


@router.delete("/{repo_id}/skills/{skill_id}", status_code=204)
def delete_skill(
    repo_id: int,
    skill_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Exclui uma skill: remove o registro do banco e o diretório do disco."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo, _SKILL_ADMIN_MESSAGE)
    skill = _get_repo_skill_or_404(session, repo_id, skill_id)
    skill_dir = os.path.join(settings.skills_dir, str(repo_id), str(skill_id))
    session.delete(skill)
    session.commit()
    skills_mod.remove_skill_dir(skill_dir)
