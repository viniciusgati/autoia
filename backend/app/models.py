"""Modelos do banco de dados."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# Estados possíveis
TASK_QUEUED = "queued"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_BLOCKED = "blocked"
TASK_NEEDS_REVIEW = "needs_review"
TASK_WAITING_APPROVAL = "waiting_approval"
TASK_PAUSED = "paused"
TASK_CANCELLED = "cancelled"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_GUARDRAIL_BLOCKED = "guardrail_blocked"
# Fase que o agente declarou que não consegue continuar sozinha (aguarda instrução
# do usuário para retomar — ver `autoia_blocked.json`).
STEP_BLOCKED = "blocked"

SUB_PENDING = "pending"
SUB_IMPLEMENTING = "implementing"
SUB_IMPLEMENTED = "implemented"
SUB_VERIFYING = "verifying"
SUB_DONE = "done"
SUB_FAILED = "failed"


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    url: Mapped[str] = mapped_column(String(500))
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Configurações que sobrescrevem os defaults globais (AUTOIA_*)
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_pm_decisions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_per_interaction: Mapped[float | None] = mapped_column(Float, nullable=True)
    risky_patterns_extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    db_rule: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Controle de features
    allow_auto_tasks: Mapped[bool] = mapped_column(default=False)
    allow_external_tasks: Mapped[bool] = mapped_column(default=False)
    default_pipeline_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=True)
    # Gera o resumo do desenvolvimento (LLM) automaticamente a cada avanço de fase
    # e ao parar em estado terminal/decisão, sem precisar clicar em "regenerar".
    auto_summary: Mapped[bool] = mapped_column(Boolean, default=False)

    tasks: Mapped[list["Task"]] = relationship(back_populates="repository")


class User(Base):
    """Usuário humano da plataforma (autenticação por sessão de cookie).

    O primeiro registro (bootstrap via `POST /api/auth/register` com `users`
    vazio) vira admin global; os demais são criados por admin via API.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def is_admin(self) -> bool:
        """Admin global: gerencia usuários e atua em qualquer tarefa."""
        return self.role == "admin"


class Session(Base):
    """Sessão de autenticação (cookie `autoia_session`)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship()


class RepositoryUser(Base):
    """Participação de um usuário em um projeto (papel `member` | `admin`).

    Upsertado automaticamente ao reatribuir uma tarefa (role `member`) e
    gerenciável por admin do projeto via `/api/repositories/{id}/members`.
    """

    __tablename__ = "repository_users"
    __table_args__ = (
        UniqueConstraint("repository_id", "user_id", name="uq_repo_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(20), default="member")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship()
    repository: Mapped[Repository] = relationship()


class Robot(Base):
    __tablename__ = "robots"
    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_robot_scope_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("repositories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    mission: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default="implement")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"
    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_pipeline_scope_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("repositories.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    steps: Mapped[list["PipelineStep"]] = relationship(
        back_populates="pipeline",
        order_by="PipelineStep.position",
        cascade="all, delete-orphan",
    )


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"))
    position: Mapped[int] = mapped_column(Integer)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"))
    post_merge: Mapped[bool] = mapped_column(default=False)
    pause_before: Mapped[bool] = mapped_column(default=False)

    pipeline: Mapped[Pipeline] = relationship(back_populates="steps")
    robot: Mapped[Robot] = relationship()


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(50), default="issue")
    status: Mapped[str] = mapped_column(String(30), default="created")
    # Executor das fases: "kimi" (kimi-code CLI) ou "opencode" (opencode CLI).
    executor: Mapped[str] = mapped_column(String(20), default="kimi")
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    branch: Mapped[str | None] = mapped_column(String(300), nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget_limit: Mapped[float] = mapped_column(Float, default=10.0)
    cost_spent: Mapped[float] = mapped_column(Float, default=0.0)
    pm_decisions: Mapped[int] = mapped_column(Integer, default=0)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    parent_task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    # Responsável explícito pela tarefa (default = criador). NULL em tasks
    # pré-existentes até reatribuição: "sem responsável = qualquer autenticado atua".
    responsible_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    # Detalhes da implementação adicionados MANUALMENTE pelo usuário durante o
    # fluxo (complementam o contexto original, diferenciados de description).
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Instrução fornecida pelo usuário ao retomar uma fase bloqueada (separada do
    # contexto original — entra no handoff/prompt da retomada).
    resume_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Motivo estruturado do bloqueio declarado pelo agente (autoia_blocked.json).
    block_reason_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Opções de uma decisão solicitada pelo agente ao usuário (autoia_decision.json).
    block_options: Mapped[list] = mapped_column(JSON, default=list)

    repository: Mapped[Repository] = relationship(back_populates="tasks")
    pipeline: Mapped[Pipeline] = relationship()
    parent: Mapped["Task | None"] = relationship(remote_side="Task.id", back_populates="children")
    children: Mapped[list["Task"]] = relationship(back_populates="parent")
    responsible: Mapped["User | None"] = relationship(foreign_keys=[responsible_id])
    steps: Mapped[list["TaskStep"]] = relationship(
        back_populates="task",
        order_by="TaskStep.position",
        cascade="all, delete-orphan",
    )
    subtasks: Mapped[list["SubTask"]] = relationship(
        back_populates="task",
        order_by="SubTask.position",
        cascade="all, delete-orphan",
    )
    proposals: Mapped[list["TaskProposal"]] = relationship(
        back_populates="task",
        order_by="TaskProposal.position",
        cascade="all, delete-orphan",
        foreign_keys="TaskProposal.task_id",
    )
    # Versões do resumo gerado por LLM (mais recente primeiro).
    summaries: Mapped[list["TaskSummary"]] = relationship(
        back_populates="task",
        order_by="TaskSummary.id.desc()",
        cascade="all, delete-orphan",
    )
    # Resumos por execução de fase (StepSummary), mais recente primeiro.
    step_summaries: Mapped[list["StepSummary"]] = relationship(
        back_populates="task",
        order_by="StepSummary.id.desc()",
        cascade="all, delete-orphan",
    )

    @property
    def summary(self) -> "TaskSummary | None":
        """Resumo mais recente do desenvolvimento (se houver)."""
        return self.summaries[0] if self.summaries else None


class TaskSummary(Base):
    """Resumo estruturado do desenvolvimento, gerado por LLM dedicada e persistido.

    A LLM NUNCA é fonte de verdade: é uma representação resumida dos dados reais da
    execução (fases, eventos/timeline, arquivos, testes). Regenerar cria uma nova
    versão; a API sempre retorna a mais recente. Falha na geração não afeta o pipeline.
    """

    __tablename__ = "task_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    summary: Mapped[str] = mapped_column(Text, default="")
    request: Mapped[str | None] = mapped_column(Text, nullable=True)
    implementation: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    files: Mapped[list] = mapped_column(JSON, default=list)
    tasks_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    task: Mapped[Task] = relationship(back_populates="summaries")


class StepSummary(Base):
    """Resumo de UMA execução de fase ("O que foi entregue"), gerado por LLM dedicada.

    O resumo da fase é chaveado por (step, attempt): cada re-execução tem o seu,
    preservando o histórico imutável da timeline. A LLM nunca é fonte de verdade —
    eventos/arquivos/diff são. Regenerar um passo cria uma nova linha apenas se o
    attempt mudar; falha na geração não afeta o pipeline.
    """

    __tablename__ = "step_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    step_id: Mapped[int] = mapped_column(ForeignKey("task_steps.id"))
    position: Mapped[int] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    changes: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    files: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    task: Mapped[Task] = relationship(back_populates="step_summaries")
    step: Mapped["TaskStep"] = relationship(back_populates="step_summaries")


class TaskProposal(Base):
    """Proposta de task filha gerada por um robô (autoia_tasks.json) que fica
    PENDENTE de aprovação humana antes de virar uma task real.

    O worker NUNCA cria a task automaticamente: grava a proposta (dedup por
    `task_id + title`) e o humano decide aceitar/rejeitar via API."""

    __tablename__ = "task_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    step_id: Mapped[int | None] = mapped_column(ForeignKey("task_steps.id"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(50), default="feature")
    target_repository_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    accepted_task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tasks.id"), nullable=True
    )

    task: Mapped[Task] = relationship(back_populates="proposals", foreign_keys=[task_id])

    @property
    def repository_id(self) -> int | None:
        """Repositório da task pai (para exibir o projeto de origem da proposta)."""
        return self.task.repository_id if self.task else None
    accepted_task: Mapped["Task | None"] = relationship(foreign_keys=[accepted_task_id])


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    position: Mapped[int] = mapped_column(Integer)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"))
    status: Mapped[str] = mapped_column(String(30), default=STEP_PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    post_merge: Mapped[bool] = mapped_column(default=False)
    pause_before: Mapped[bool] = mapped_column(default=False)
    log_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Objetivo legível da fase ("O que será feito") derivado deterministicamente
    # da mission do robô + título da task no momento da execução.
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sessão do executor (kimi `-S`) desta fase: permite RETOMAR a mesma conversa
    # numa re-execução (timeout/stall), preservando o contexto do LLM. Limpo
    # (na prática) quando a fase conclui com sucesso.
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    diff_stat: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Snapshot do responsável da task no momento do claim + quem concluiu a fase.
    responsible_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    finished_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    task: Mapped[Task] = relationship(back_populates="steps")
    robot: Mapped[Robot] = relationship()
    responsible: Mapped["User | None"] = relationship(foreign_keys=[responsible_id])
    finished_by: Mapped["User | None"] = relationship(foreign_keys=[finished_by_id])
    step_summaries: Mapped[list["StepSummary"]] = relationship(
        back_populates="step",
        order_by="StepSummary.id.desc()",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="step",
        order_by="RunEvent.seq",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["StepArtifact"]] = relationship(
        back_populates="step",
        order_by="StepArtifact.created_at",
        cascade="all, delete-orphan",
    )


class RunEvent(Base):
    """Uma interação registrada com o kimi (ou decisão do worker/guardrail)."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("task_steps.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    step: Mapped[TaskStep] = relationship(back_populates="events")


class SubTask(Base):
    """Unidade de implementação de uma tarefa. Cada subtarefa tem seu próprio ciclo
    implement → verify com bounce-back independente, na mesma branch da task."""

    __tablename__ = "subtasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default=SUB_PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    task: Mapped[Task] = relationship(back_populates="subtasks")


class StepArtifact(Base):
    """Arquivo gerado por um robô durante a execução de uma fase (ex.: screenshot de
    smoke test). Os arquivos ficam no checkout (não versionados); esta tabela rastreia
    os metadados para exibição na UI."""

    __tablename__ = "step_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("task_steps.id"))
    filename: Mapped[str] = mapped_column(String(300))
    filepath: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    step: Mapped[TaskStep] = relationship(back_populates="artifacts")
