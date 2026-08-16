"""Dashboard: métricas agregadas + avisos de tarefas que requerem atenção.

Com autenticação ligada, `GET /api/dashboard` (e os endpoints `/api/me/*`)
filtram métricas e avisos aos projetos do usuário; com auth OFF o dashboard
permanece global (comportamento atual).
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from ..models import (
    STEP_GUARDRAIL_BLOCKED,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Repository,
    RepositoryUser,
    RunEvent,
    Task,
    TaskProposal,
    TaskStep,
    User,
)
from ..schemas import (
    DashboardOut,
    MyProjectOut,
    MyTaskOut,
    NoticeOut,
    RunEventOut,
    TaskProposalOut,
)
from .deps import get_session, get_settings, require_auth
from .etag import conditional

# Estados terminais: um aviso sobre essas tasks não pede mais ação humana.
_TASK_TERMINAL = (TASK_DONE, TASK_FAILED, TASK_CANCELLED)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
# Endpoints do dashboard pessoal (precisam de sessão — 401 com auth OFF).
me_router = APIRouter(prefix="/api/me", tags=["me"])

# Níveis da métrica de arquitetura (worker/arch_metric.py) que viram aviso.
_ARCH_NOTICE_LEVELS = {"alto": "critical", "médio": "warning"}

# Status que pedem ação humana (selo "aguardando você" na Home).
_PENDING_ACTION = (TASK_NEEDS_REVIEW, TASK_WAITING_APPROVAL, TASK_BLOCKED)
_ACTIVE_STATUSES = (TASK_QUEUED, TASK_IN_PROGRESS)


def _user_repo_ids(session: Session, user_id: int) -> list[int]:
    """Projetos visíveis ao usuário: participação em `repository_users` — exceto
    o admin global, que enxerga TODOS os projetos (coerente com `_ensure_can_act`
    e com a edição de projetos em `repositories.py`)."""
    user = session.get(User, user_id)
    if user is not None and user.is_admin:
        return [rid for (rid,) in session.query(Repository.id).all()]
    return [
        r.repository_id
        for r in session.query(RepositoryUser)
        .filter(RepositoryUser.user_id == user_id)
        .all()
    ]


def _my_tasks(session: Session, user_id: int) -> list[MyTaskOut]:
    """Tarefas com `responsible_id == eu`, com o nome do projeto."""
    rows = (
        session.query(Task, Repository)
        .join(Repository, Task.repository_id == Repository.id)
        .filter(Task.responsible_id == user_id)
        .order_by(Task.updated_at.desc())
        .all()
    )
    return [
        MyTaskOut(
            id=task.id,
            repository_id=task.repository_id,
            repository_name=repo.name,
            title=task.title,
            status=task.status,
            cost_spent=task.cost_spent,
            budget_limit=task.budget_limit,
            updated_at=task.updated_at,
        )
        for task, repo in rows
    ]


def _my_projects(session: Session, user_id: int) -> list[MyProjectOut]:
    """Participações do usuário com papel e contagem de tarefas minhas."""
    participations = (
        session.query(RepositoryUser, Repository)
        .join(Repository, RepositoryUser.repository_id == Repository.id)
        .filter(RepositoryUser.user_id == user_id)
        .order_by(Repository.id)
        .all()
    )
    projects: list[MyProjectOut] = []
    for ru, repo in participations:
        counts = (
            session.query(Task.status, func.count(Task.id))
            .filter(
                Task.repository_id == repo.id,
                Task.responsible_id == user_id,
            )
            .group_by(Task.status)
            .all()
        )
        by_status = dict(counts)
        total = sum(by_status.values())
        active = sum(by_status.get(s, 0) for s in _ACTIVE_STATUSES)
        pending = sum(by_status.get(s, 0) for s in _PENDING_ACTION)
        projects.append(
            MyProjectOut(
                id=repo.id,
                name=repo.name,
                role=ru.role,
                my_tasks_total=total,
                my_tasks_active=active,
                my_tasks_pending=pending,
            )
        )
    return projects


@me_router.get("/tasks", response_model=list[MyTaskOut])
def my_tasks(
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    if user is None:
        raise HTTPException(401, "autenticação desabilitada")
    return _my_tasks(session, user.id)


@me_router.get("/projects", response_model=list[MyProjectOut])
def my_projects(
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    if user is None:
        raise HTTPException(401, "autenticação desabilitada")
    return _my_projects(session, user.id)


def _build_notices(
    session: Session,
    scoped: Callable[[Query], Query] | None = None,
) -> list[NoticeOut]:
    """Avisos de tarefas que precisam de atenção, mais críticos primeiro.

    `scoped` aplica o escopo de visibilidade a uma query de Task (ex.: projetos do
    usuário no dashboard, ou o escopo unificado do /api/execution); `None` = global
    (auth OFF).
    """
    notices: list[NoticeOut] = []

    def _apply(q):
        return scoped(q) if scoped is not None else q

    # steps bloqueados por guardrail (a task segue, mas merece olhar)
    blocked_steps = (
        _apply(
            session.query(TaskStep)
            .join(Task)
            .filter(
                TaskStep.status == STEP_GUARDRAIL_BLOCKED,
                Task.status.not_in(_TASK_TERMINAL),
            )
        )
        .order_by(TaskStep.id.desc())
        .limit(10)
        .all()
    )
    for step in blocked_steps:
        notices.append(
            NoticeOut(
                task_id=step.task.id,
                task_title=step.task.title,
                task_status=step.task.status,
                repository_id=step.task.repository_id,
                level="critical",
                kind="guardrail",
                message=step.error or "guardrail bloqueou a execução",
                ts=step.finished_at or step.started_at or step.task.updated_at,
            )
        )

    # tasks aguardando revisão humana/PM
    review_tasks = (
        _apply(session.query(Task).filter(Task.status == TASK_NEEDS_REVIEW))
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in review_tasks:
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="needs_review",
                message=task.error or "aguardando revisão",
                ts=task.updated_at,
            )
        )

    # tasks paradas em gate de aprovação humana (pause_before no pipeline)
    gate_tasks = (
        _apply(session.query(Task).filter(Task.status == TASK_WAITING_APPROVAL))
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in gate_tasks:
        gated = next(
            (st for st in sorted(task.steps, key=lambda x: x.position)
             if st.status == "pending" and st.pause_before),
            None,
        )
        robot = gated.robot.name if gated and gated.robot else "?"
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="human_gate",
                message=f"aguardando aprovação para executar a fase "
                        f"F{gated.position if gated else '?'} · {robot}",
                ts=task.updated_at,
            )
        )

    # tasks bloqueadas (ex.: conflito de merge)
    blocked_tasks = (
        _apply(session.query(Task).filter(Task.status == TASK_BLOCKED))
        .order_by(Task.updated_at.desc())
        .limit(10)
        .all()
    )
    for task in blocked_tasks:
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="critical",
                kind="blocked",
                message=task.error or "tarefa bloqueada",
                ts=task.updated_at,
            )
        )

    # tasks ativas com custo perto do limite (>= 80% do orçamento)
    costly_tasks = (
        _apply(
            session.query(Task).filter(
                Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
                Task.cost_spent >= 0.8 * Task.budget_limit,
            )
        )
        .order_by(Task.cost_spent.desc())
        .limit(10)
        .all()
    )
    for task in costly_tasks:
        pct = 100.0 * task.cost_spent / task.budget_limit if task.budget_limit else 0.0
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level="warning",
                kind="budget_high",
                message=f"custo alto: {pct:.0f}% do orçamento ({task.cost_spent:.2f} US$)",
                ts=task.updated_at,
            )
        )

    # métrica de arquitetura: mudança drástica de deploy/arquitetura
    arch_events = (
        _apply(
            session.query(RunEvent, TaskStep, Task)
            .join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
            .filter(
                RunEvent.kind == "arch_metric",
                Task.status.not_in(_TASK_TERMINAL),
            )
        )
        .order_by(RunEvent.id.desc())
        .limit(30)
        .all()
    )
    for event, _step, task in arch_events:
        level = (event.payload or {}).get("level")
        if level not in _ARCH_NOTICE_LEVELS:
            continue
        reasons = (event.payload or {}).get("reasons") or []
        message = "arquitetura/deploy alterados: " + ", ".join(str(r) for r in reasons)
        notices.append(
            NoticeOut(
                task_id=task.id,
                task_title=task.title,
                task_status=task.status,
                repository_id=task.repository_id,
                level=_ARCH_NOTICE_LEVELS[level],
                kind="arch",
                message=message[:160],
                ts=event.ts,
            )
        )

    # mais críticos primeiro; dentro do mesmo nível, mais recentes primeiro
    notices.sort(
        key=lambda n: (0 if n.level == "critical" else 1, -n.ts.timestamp())
    )
    return notices[:10]


@router.get("", response_model=DashboardOut)
def dashboard(
    repository_id: int | None = None,
    request: Request = None,
    response: Response = None,
    session: Session = Depends(get_session),
    user: User | None = Depends(require_auth),
):
    # Com auth ON, métricas e avisos ficam restritos aos projetos do usuário.
    repo_ids: list[int] | None = None
    if user is not None:
        repo_ids = _user_repo_ids(session, user.id)

    def _scoped_task(q):
        if repository_id is not None:
            q = q.filter(Task.repository_id == repository_id)
        elif repo_ids is not None:
            q = q.filter(Task.repository_id.in_(repo_ids))
        return q

    # Avisos preservam o escopo atual do dashboard: só projetos do usuário
    # (o filtro explícito por projeto não restringe os avisos neste endpoint).
    def _scoped_notices(q):
        if repo_ids is not None:
            q = q.filter(Task.repository_id.in_(repo_ids))
        return q

    # Token barato (304): tasks + eventos (guardrail/arch/PM mudam junto).
    max_task_ts, token_total = _scoped_task(
        session.query(Task)
    ).with_entities(func.max(Task.updated_at), func.count(Task.id)).first()
    event_q = (
        session.query(func.max(RunEvent.id))
        .join(TaskStep, RunEvent.step_id == TaskStep.id)
        .join(Task, TaskStep.task_id == Task.id)
    )
    if repository_id is not None:
        event_q = event_q.filter(Task.repository_id == repository_id)
    elif repo_ids is not None:
        event_q = event_q.filter(Task.repository_id.in_(repo_ids))
    max_event_id = event_q.scalar()
    # Propostas: max id + total não-rejeitadas (aceita continua na lista até o usuário
    # agir; rejeitada sai). Entram no token p/ o ETag invalidar quando mudar.
    proposal_q = session.query(TaskProposal).join(Task, TaskProposal.task_id == Task.id)
    if repository_id is not None:
        proposal_q = proposal_q.filter(Task.repository_id == repository_id)
    elif repo_ids is not None:
        proposal_q = proposal_q.filter(Task.repository_id.in_(repo_ids))
    max_proposal_id, proposal_count = proposal_q.with_entities(
        func.max(TaskProposal.id),
        func.count(TaskProposal.id).filter(TaskProposal.status != "rejected"),
    ).first()
    not_modified = conditional(
        request, response,
        "|".join(str(x) for x in (max_task_ts, token_total, max_event_id, max_proposal_id, proposal_count))
    )
    if not_modified is not None:
        return not_modified

    rows = (
        _scoped_task(session.query(Task))
        .with_entities(Task.status, func.count(Task.id))
        .group_by(Task.status)
        .all()
    )
    tasks_by_status = {status: count for status, count in rows}

    # custo total: filtra eventos cuja task pertence ao escopo
    cost_q = session.query(func.sum(RunEvent.cost))
    if repository_id is not None or repo_ids is not None:
        cost_q = (
            cost_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
        )
        if repository_id is not None:
            cost_q = cost_q.filter(Task.repository_id == repository_id)
        elif repo_ids is not None:
            cost_q = cost_q.filter(Task.repository_id.in_(repo_ids))
    total_cost = cost_q.scalar() or 0.0

    # guardrail events
    guard_q = session.query(func.count(RunEvent.id)).filter(
        RunEvent.kind == "guardrail_blocked"
    )
    if repository_id is not None or repo_ids is not None:
        guard_q = (
            guard_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
        )
        if repository_id is not None:
            guard_q = guard_q.filter(Task.repository_id == repository_id)
        elif repo_ids is not None:
            guard_q = guard_q.filter(Task.repository_id.in_(repo_ids))
    guardrail_events = guard_q.scalar() or 0

    recent_guardrails_q = (
        session.query(RunEvent).filter(RunEvent.kind == "guardrail_blocked")
    )
    if repository_id is not None or repo_ids is not None:
        recent_guardrails_q = (
            recent_guardrails_q.join(TaskStep, RunEvent.step_id == TaskStep.id)
            .join(Task, TaskStep.task_id == Task.id)
        )
        if repository_id is not None:
            recent_guardrails_q = recent_guardrails_q.filter(Task.repository_id == repository_id)
        elif repo_ids is not None:
            recent_guardrails_q = recent_guardrails_q.filter(Task.repository_id.in_(repo_ids))
    recent_guardrails = (
        recent_guardrails_q.order_by(RunEvent.id.desc()).limit(10).all()
    )

    total_tasks = sum(tasks_by_status.values())
    proposals = (
        proposal_q.filter(TaskProposal.status != "rejected")
        .order_by(TaskProposal.id.desc())
        .limit(50)
        .all()
    )
    return DashboardOut(
        tasks_by_status=tasks_by_status,
        total_cost=round(total_cost, 4),
        total_tasks=total_tasks,
        guardrail_events=guardrail_events,
        recent_guardrails=[RunEventOut.model_validate(e) for e in recent_guardrails],
        notices=_build_notices(session, _scoped_notices),
        proposals=[TaskProposalOut.model_validate(p) for p in proposals],
        user=user,
        my_tasks=_my_tasks(session, user.id) if user is not None else [],
        projects=_my_projects(session, user.id) if user is not None else [],
    )
