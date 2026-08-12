"""Endpoints de repositórios (registro + clone + config + membros)."""

from __future__ import annotations

import logging
import os
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import (
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_PAUSED,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Pipeline,
    PipelineStep,
    Repository,
    RepositoryUser,
    Robot,
    RunEvent,
    StepArtifact,
    StepSummary,
    SubTask,
    Task,
    TaskProposal,
    TaskStep,
    TaskSummary,
    User,
)
from ..schemas import (
    RepositoryCreate,
    RepositoryDeleteInfo,
    RepositoryMemberCreate,
    RepositoryOut,
    RepositoryUpdate,
    RepositoryUserOut,
    RepositoryUserUpdate,
)
from ..worker import exec_common, gitops
from ..worker.runner import _system_event
from .deps import (
    get_repository_or_404,
    get_session,
    get_settings,
    is_repo_admin,
    require_auth,
)

log = logging.getLogger("autoia.api")

router = APIRouter(prefix="/api/repositories", tags=["repositories"])

# Estados de task que contam como "ativa" (não terminal) para a exclusão do
# projeto: são canceladas ANTES da remoção dos registros (o runner respeita o
# status `cancelled` via `_handle_cancelled` — não avança nem faz merge).
ACTIVE_TASK_STATUSES = (
    TASK_QUEUED,
    TASK_IN_PROGRESS,
    TASK_BLOCKED,
    TASK_NEEDS_REVIEW,
    TASK_WAITING_APPROVAL,
    TASK_PAUSED,
)


def _ensure_repo_admin(session: Session, user: User | None, repo: Repository) -> None:
    """POST/PATCH/DELETE de membros exige admin global ou admin do projeto."""
    if user is None:
        return
    if user.is_admin or is_repo_admin(session, user, repo.id):
        return
    raise HTTPException(403, "apenas admin global ou admin do projeto gerencia membros")


def _task_anchor_step(task: Task) -> TaskStep | None:
    """Step usado como âncora para eventos de nível de task (corrente ou o primeiro)."""
    steps = sorted(task.steps, key=lambda st: st.position)
    return next((st for st in steps if st.position == task.current_step), steps[0] if steps else None)


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


@router.get("/{repo_id}/delete-info", response_model=RepositoryDeleteInfo)
def repository_delete_info(
    repo_id: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Contagem de tasks ativas (serão interrompidas) + checkout do projeto —
    alimenta o diálogo de confirmação de exclusão no frontend."""
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo)
    active = (
        session.query(Task)
        .filter(Task.repository_id == repo_id, Task.status.in_(ACTIVE_TASK_STATUSES))
        .count()
    )
    return RepositoryDeleteInfo(
        active_tasks=active,
        checkout_path=os.path.abspath(os.path.join(settings.workspace_dir, str(repo_id))),
    )


@router.delete("/{repo_id}", status_code=204)
def delete_repository(
    repo_id: int,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
    user: User | None = Depends(require_auth),
):
    """Exclusão CASCATA completa do projeto (irreversível).

    Cancela tasks não terminais, sinaliza o worker para matar os subprocessos
    ativos do projeto, remove TODOS os registros dependentes (events, steps,
    summaries, subtasks, proposals, membros, robôs/pipelines escopados) e apaga o
    checkout local (`workspace_dir/<repo_id>`) do disco. O remote não é tocado.
    """
    repo = get_repository_or_404(session, repo_id)
    _ensure_repo_admin(session, user, repo)

    # 1) Canal de parada cooperativa (API → worker): o worker mata os subprocessos
    #    ativos do projeto ao ver `workspace_dir/.stop-<repo_id>` (API e worker são
    #    processos separados; kill seletivo por projeto).
    stop_path = exec_common.repo_stop_path(settings.workspace_dir, repo_id)
    try:
        os.makedirs(settings.workspace_dir, exist_ok=True)
        with open(stop_path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        log.warning(
            "não foi possível gravar o sinal de parada do projeto %s", repo_id, exc_info=True
        )

    # 2) Cancela tasks NÃO terminais ANTES da remoção (com evento `task_cancelled`).
    tasks = session.query(Task).filter(Task.repository_id == repo_id).all()
    task_ids = [t.id for t in tasks]
    for task in tasks:
        if task.status in ACTIVE_TASK_STATUSES:
            task.status = TASK_CANCELLED
            task.error = "projeto excluído — execução cancelada"
            _system_event(
                session, _task_anchor_step(task), "task_cancelled",
                {"reason": "projeto excluído"},
            )
    session.flush()

    # 3) Remove TODOS os registros dependentes, em ordem (FK-safe p/ Postgres, sem
    #    órfãos). Deletes em massa não disparam cascade do ORM — são explícitos.
    step_ids: list[int] = []
    if task_ids:
        step_ids = [
            sid
            for (sid,) in session.query(TaskStep.id)
            .filter(TaskStep.task_id.in_(task_ids))
            .all()
        ]
    if step_ids:
        session.query(RunEvent).filter(RunEvent.step_id.in_(step_ids)).delete(synchronize_session=False)
        session.query(StepArtifact).filter(StepArtifact.step_id.in_(step_ids)).delete(synchronize_session=False)
        session.query(StepSummary).filter(StepSummary.step_id.in_(step_ids)).delete(synchronize_session=False)
        session.query(TaskProposal).filter(TaskProposal.step_id.in_(step_ids)).delete(synchronize_session=False)
        session.query(TaskStep).filter(TaskStep.id.in_(step_ids)).delete(synchronize_session=False)
    if task_ids:
        session.query(TaskSummary).filter(TaskSummary.task_id.in_(task_ids)).delete(synchronize_session=False)
        session.query(StepSummary).filter(StepSummary.task_id.in_(task_ids)).delete(synchronize_session=False)
        session.query(SubTask).filter(SubTask.task_id.in_(task_ids)).delete(synchronize_session=False)
        session.query(TaskProposal).filter(TaskProposal.task_id.in_(task_ids)).delete(synchronize_session=False)
        # Propostas de OUTROS projetos aceitas apontando para tasks deste projeto:
        # a task some, então o vínculo é desfeito (evita FK órfã em Postgres).
        session.query(TaskProposal).filter(TaskProposal.accepted_task_id.in_(task_ids)).update(
            {"accepted_task_id": None}, synchronize_session=False
        )
        # Tasks filhas de outros projetos com parent neste projeto: desfaz o vínculo.
        session.query(Task).filter(
            Task.parent_task_id.in_(task_ids), Task.repository_id != repo_id
        ).update({"parent_task_id": None}, synchronize_session=False)
        session.query(Task).filter(Task.repository_id == repo_id).delete(synchronize_session=False)

    # Propostas de outros projetos que MIRAM este projeto como alvo: desfaz o alvo.
    session.query(TaskProposal).filter(TaskProposal.target_repository_id == repo_id).update(
        {"target_repository_id": None}, synchronize_session=False
    )

    session.query(RepositoryUser).filter(RepositoryUser.repository_id == repo_id).delete(synchronize_session=False)

    # Robôs/pipelines escopados ao projeto (e seus steps).
    robot_ids = [
        rid for (rid,) in session.query(Robot.id).filter(Robot.repository_id == repo_id).all()
    ]
    pipeline_ids = [
        pid
        for (pid,) in session.query(Pipeline.id).filter(Pipeline.repository_id == repo_id).all()
    ]
    if pipeline_ids:
        # Outros projetos podem apontar para um pipeline escopado deste projeto.
        session.query(Repository).filter(Repository.default_pipeline_id.in_(pipeline_ids)).update(
            {"default_pipeline_id": None}, synchronize_session=False
        )
        session.query(PipelineStep).filter(PipelineStep.pipeline_id.in_(pipeline_ids)).delete(synchronize_session=False)
        session.query(Pipeline).filter(Pipeline.id.in_(pipeline_ids)).delete(synchronize_session=False)
    if robot_ids:
        # Steps de pipelines (de qualquer projeto) referenciando robôs escopados:
        # sem o robô, o step não faz sentido — remove junto (cascata).
        session.query(PipelineStep).filter(PipelineStep.robot_id.in_(robot_ids)).delete(synchronize_session=False)
        session.query(Robot).filter(Robot.id.in_(robot_ids)).delete(synchronize_session=False)

    # 4) Checkout local do projeto some do disco (o remote é intocado).
    checkout = os.path.abspath(os.path.join(settings.workspace_dir, str(repo_id)))
    shutil.rmtree(checkout, ignore_errors=True)

    # 5) Por fim, a linha do projeto.
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
