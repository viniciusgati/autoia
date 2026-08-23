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
# Pipeline aberto (human-in-the-loop): a task fica parada aguardando o humano
# dirigir os agentes via chat (dispatcher). Não é terminal nem erro.
TASK_OPEN = "open"

# Modos de execução da task (alternáveis em runtime, nunca fixos).
TASK_MODE_AUTO = "auto"       # pipeline clássico: fases avançam sozinhas
TASK_MODE_MANUAL = "manual"   # tudo dirigido pelo humano via chat (dispatcher)

# Modo de execução de UMA fase (None = herda o modo da task).
STEP_MODE_AUTO = "auto"
STEP_MODE_MANUAL = "manual"

# Estado da ação de chat de uma task (análogo a ChamadoStage: ativa/aguardando/executando).
CHAT_STATUS_IDLE = "idle"           # aguardando o humano
CHAT_STATUS_QUEUED = "queued"       # ação encaminhada, aguardando o chat-worker
CHAT_STATUS_RUNNING = "running"     # ação em execução no chat-worker

# Ações de chat pendentes (Task.pending_action).
CHAT_DISPATCH = "dispatch"
CHAT_MERGE = "merge"

# Ações decididas pelo dispatcher (autoia_dispatch.json).
DISPATCH_RUN_AGENT = "run_agent"
DISPATCH_MERGE = "merge"
DISPATCH_CHAT = "chat"
DISPATCH_ASK = "ask"

# Status de UMA rodada de agente (TaskRun) no modo human-in-the-loop.
TASKRUN_RUNNING = "executando"
TASKRUN_DONE = "concluida"
TASKRUN_FAILED = "falhou"
TASKRUN_BLOCKED = "bloqueada"

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

# ── Chamados (fluxo de atendimento, independente da pipeline de tasks) ──────

# Status de vida de um Projeto/Épico/Chamado (nível de organização, não pipeline).
PROJECT_ABERTO = "aberto"
PROJECT_EM_ANDAMENTO = "em_andamento"
PROJECT_FECHADO = "fechado"

EPIC_ABERTO = "aberto"
EPIC_EM_ANDAMENTO = "em_andamento"
EPIC_FECHADO = "fechado"

CHAMADO_ABERTO = "aberto"
CHAMADO_EM_ANDAMENTO = "em_andamento"
CHAMADO_RESPONDIDO = "respondido"
CHAMADO_CANCELADO = "cancelado"
CHAMADO_CONCLUIDO = "concluido"
CHAMADO_FALHOU = "falhou"

# Status de UMA etapa (estágio) vivida por um chamado.
CHAMADO_STAGE_PENDENTE = "pendente"
CHAMADO_STAGE_ATIVA = "ativa"           # aguardando ações do usuário (ferramentas/fechamento)
CHAMADO_STAGE_AGUARDANDO = "aguardando" # ação encaminhada (ferramenta ou avaliação) aguardando worker
CHAMADO_STAGE_EXECUTANDO = "executando" # ação em execução no worker
CHAMADO_STAGE_FECHADA = "fechada"

# Resultados possíveis ao fechar uma etapa (decisão da avaliação do robô).
STAGE_DECISION_NEXT = "next_stage"
STAGE_DECISION_RESPOSTA = "resposta"
STAGE_DECISION_CANCELAR = "cancelar"
STAGE_DECISION_CONCLUIR = "concluir"


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
    # Modo de sandbox de execução do projeto: None = herda o global (AUTOIA_SANDBOX);
    # "off" | "fs" | "full" sobrescreve para este repositório.
    sandbox: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Controle de features
    allow_auto_tasks: Mapped[bool] = mapped_column(default=False)
    allow_external_tasks: Mapped[bool] = mapped_column(default=False)
    default_pipeline_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=True)
    # Gera o resumo do desenvolvimento (LLM) automaticamente a cada avanço de fase
    # e ao parar em estado terminal/decisão, sem precisar clicar em "regenerar".
    auto_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    # Repositórios onde este projeto PODE criar tarefas (nomes exatos, allowlist de
    # saída). Vazio = restritivo: o robô NÃO pode propor tasks para outros projetos
    # (só para o próprio). Fica visível no prompt/handoff dos robôs.
    task_targets: Mapped[list] = mapped_column(JSON, default=list)
    # Informações úteis injetadas no contexto dos robôs (ex.: DNS do deploy, URLs de
    # staging, env vars, serviços do host) — texto livre, incluído no AGENTS.md e no prompt.
    external_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="repository")
    skills: Mapped[list["RepositorySkill"]] = relationship(
        back_populates="repository",
        order_by="RepositorySkill.id",
        cascade="all, delete-orphan",
    )


class RepositorySkill(Base):
    """Skill de projeto: conhecimento de domínio enviado pelo usuário (upload de
    `.zip` com `SKILL.md` na raiz), materializado no checkout dos robôs nas fases
    (`.autoia/skills/`/`.opencode/skills/`) sem poluir o git do repositório.

    Os arquivos ficam em `data/skills/<repository_id>/<skill_id>/`; o banco guarda
    apenas os metadados. Excluir a skill remove o diretório do disco + a linha.
    """

    __tablename__ = "repository_skills"
    __table_args__ = (
        # Nome único por projeto — mantém a materialização no checkout determinística.
        UniqueConstraint("repository_id", "name", name="uq_repository_skill_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    file_count: Mapped[int] = mapped_column(Integer, default=1)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    repository: Mapped[Repository] = relationship(back_populates="skills")


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
    archived: Mapped[bool] = mapped_column(default=False)
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
    # Modo de execução da task: "auto" (pipeline clássico) | "manual"
    # (human-in-the-loop: o humano dirige os agentes via chat). Alternável.
    mode: Mapped[str] = mapped_column(String(20), default=TASK_MODE_AUTO)
    # Ação de chat pendente (modo manual): "dispatch" | "merge" | "run_agent:<id>".
    pending_action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Estado da ação de chat: idle | queued | running (espelho do ChamadoStage).
    chat_status: Mapped[str] = mapped_column(String(20), default=CHAT_STATUS_IDLE)
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
    # Associação organizacional Projeto > Épico (0..1 cada, opcional). Metadados:
    # não entram no handoff/prompt dos robôs nem afetam a execução (padrão Chamado).
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True)
    epic_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("epics.id"), nullable=True)

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
    project: Mapped["Project | None"] = relationship(back_populates="tasks")
    epic: Mapped["Epic | None"] = relationship(back_populates="tasks")
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
    # Missão por execução de fase (StepMission) — texto humano "por que esta execução".
    step_missions: Mapped[list["StepMission"]] = relationship(
        back_populates="task",
        order_by="StepMission.run",
        cascade="all, delete-orphan",
    )
    # Chat human-in-the-loop (TaskMessage) — transcript da task em modo manual.
    messages: Mapped[list["TaskMessage"]] = relationship(
        back_populates="task",
        order_by="TaskMessage.seq",
        cascade="all, delete-orphan",
    )
    # Histórico de rodadas de agente no modo manual (TaskRun).
    runs: Mapped[list["TaskRun"]] = relationship(
        back_populates="task",
        order_by="TaskRun.id",
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


class StepMission(Base):
    """Missão humana de UMA execução de fase ("por que esta execução existe").

    Chaveada por (step_id, run) — cada execução real da fase tem a sua (a `run` é a
    numeração de `attempt_started`, única mesmo quando `attempt` se repete após
    bounce-back). Gerada por LLM dedicada (via executor da task, custo contábil zero)
    a partir do contexto que originou a execução; a UI usa a missão LLM e, enquanto
    não está pronta (ou se falhou), um fallback determinístico derivado dos eventos.
    A LLM NUNCA é fonte de verdade — eventos/timeline são.
    """

    __tablename__ = "step_missions"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    step_id: Mapped[int] = mapped_column(ForeignKey("task_steps.id"))
    run: Mapped[int] = mapped_column(Integer, default=1)
    mission: Mapped[str] = mapped_column(Text, default="")
    # "llm" | "fallback" — quem originou o texto persistido.
    source: Mapped[str] = mapped_column(String(20), default="llm")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    task: Mapped[Task] = relationship(back_populates="step_missions")
    step: Mapped["TaskStep"] = relationship(back_populates="step_missions")


class TaskProposal(Base):
    """Proposta de task filha gerada por um robô (autoia_tasks.json) que fica
    PENDENTE de aprovação humana antes de virar uma task real.

    O worker NUNCA cria a task automaticamente: grava a proposta (dedup por
    `task_id + title`) e o humano decide aceitar/rejeitar via API. `pipeline_id`
    é opcional: quando definido (escolhido na UI antes de aceitar), a task filha
    usa esse pipeline em vez do default do repo/pai."""

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
    # Pipeline que a task filha usará se aceita (NULL = default do repo/pai).
    pipeline_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=True
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
    # Modo de execução desta fase: None = herda o modo da task; "auto" | "manual".
    execution_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    # Fase substituída por uma mudança de pipeline (`change-pipeline`): fica ARQUIVADA
    # (histórico/RunEvent preservado), mas é ignorada pelo worker e pela UI atual.
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
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
    step_missions: Mapped[list["StepMission"]] = relationship(
        back_populates="step",
        order_by="StepMission.run",
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


class TaskMessage(Base):
    """Uma interação do chat human-in-the-loop de uma task (transcript da task).

    Espelho do `ChamadoMessage`, porém atrelado à Task (modo `manual`): `user`
    (mensagem do humano), `assistant_text` (resposta do dispatcher/texto do agente),
    `tool_call`/`tool_result` (atividade do executor), `dispatch` (decisão do
    dispatcher) e `system` (eventos do chat-worker). Payload SEMPRE completo."""

    __tablename__ = "task_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    task: Mapped[Task] = relationship(back_populates="messages")


class TaskRun(Base):
    """Uma rodada de agente no modo human-in-the-loop (histórico de invocações).

    Registra qual agente (robô) o humano mandou rodar, com qual instrução, e o
    resultado: texto final, veredicto, diff e custo. É a fonte do histórico que o
    handoff e o chat mostram. `merge` também vira uma TaskRun (role `merge`)."""

    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    robot_id: Mapped[int | None] = mapped_column(ForeignKey("robots.id"), nullable=True)
    # Snapshot do agente (nome/role) no momento da rodada.
    robot_name: Mapped[str] = mapped_column(String(100), default="")
    robot_role: Mapped[str] = mapped_column(String(30), default="implement")
    instruction: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default=TASKRUN_RUNNING)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    diff_stat: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    task: Mapped[Task] = relationship(back_populates="runs")
    robot: Mapped["Robot | None"] = relationship()


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


class Project(Base):
    """Projeto (nível organizacional) de um repositório: contém épicos e chamados.

    Pertence a um Repository (mesmo escopo das tasks) e, além dos metadados, pode
    carregar um `summary` gerado por LLM (recursos de conteúdo — fase 2+)."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default=PROJECT_ABERTO)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    repository: Mapped[Repository] = relationship()
    epics: Mapped[list["Epic"]] = relationship(
        back_populates="project",
        order_by="Epic.id",
        cascade="all, delete-orphan",
    )
    chamados: Mapped[list["Chamado"]] = relationship(back_populates="project")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")


class Epic(Base):
    """Épico de um projeto: agrupa chamados. Pode carregar `scope` (objetivos/escopo
    gerados por LLM) e `summary` (resumo/métricas)."""

    __tablename__ = "epics"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default=EPIC_ABERTO)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="epics")
    chamados: Mapped[list["Chamado"]] = relationship(back_populates="epic")
    tasks: Mapped[list["Task"]] = relationship(back_populates="epic")


class ChamadoStageType(Base):
    """Catálogo de tipos de etapa do fluxo de chamados.

    Configurável por repositório (`repository_id` NULL = global/seed). Define quais
    ferramentas de apoio estão disponíveis na etapa (`allowed_tools`), quais
    fechamentos são possíveis (`close_options`, ex.: `next:<tipo>`, `resposta`,
    `cancelar`, `concluir`) e a configuração de entrega (`delivery_config`,
    ex.: `{"mode": "branch_mr", "url": "https://..."}` — usada na fase 2).
    """

    __tablename__ = "chamado_stage_types"
    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_chamado_stage_type_scope_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    is_initial: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    close_options: Mapped[list] = mapped_column(JSON, default=list)
    delivery_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Chamado(Base):
    """Um chamado (ticket) de atendimento — entidade NOVA, paralela à Task.

    Percorre um fluxo dinâmico de etapas (`ChamadoStage`), cada uma com transcript
    próprio de LLM (`ChamadoMessage`) e decisão de fechamento. `workflow_status` é o
    nome da etapa atual (status principal, independente da pipeline). `status` é o
    estado de vida do chamado. Quando uma etapa precisa de desenvolvimento, o fluxo
    pode (fase 2) disparar uma entrega configurável ou criar/relacionar uma Task."""

    __tablename__ = "chamados"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True)
    epic_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("epics.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    # Nome da etapa atual (ex.: "entrada", "analise", "desenvolvimento").
    workflow_status: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default=CHAMADO_ABERTO)
    executor: Mapped[str] = mapped_column(String(20), default="kimi")
    budget_limit: Mapped[float] = mapped_column(Float, default=10.0)
    cost_spent: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    repository: Mapped[Repository] = relationship()
    project: Mapped["Project | None"] = relationship(back_populates="chamados")
    epic: Mapped["Epic | None"] = relationship(back_populates="chamados")
    responsible: Mapped["User | None"] = relationship(foreign_keys=[responsible_id])
    stages: Mapped[list["ChamadoStage"]] = relationship(
        back_populates="chamado",
        order_by="ChamadoStage.position",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list["ChamadoMessage"]] = relationship(
        back_populates="chamado",
        order_by="ChamadoMessage.seq",
        cascade="all, delete-orphan",
    )

    @property
    def current_stage(self) -> "ChamadoStage | None":
        """Etapa atual: a mais recente que ainda não foi fechada."""
        open_stages = [st for st in self.stages if st.status != CHAMADO_STAGE_FECHADA]
        if not open_stages:
            return None
        return max(open_stages, key=lambda st: st.position)


class ChamadoStage(Base):
    """Uma etapa vivida por um chamado.

    `pending_action` sinaliza o que o worker deve processar: `tool:<chave>` (rodar a
    ferramenta com a última mensagem `user`) ou `evaluate` (avaliar o fechamento da
    etapa). Ao fechar, `decision` guarda o resultado (ex.: `next_stage:analise`,
    `resposta`, `cancelar`, `concluir`) e `result` o texto (resposta ao cliente,
    justificativa)."""

    __tablename__ = "chamado_stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chamado_id: Mapped[int] = mapped_column(ForeignKey("chamados.id"))
    stage_type_id: Mapped[int] = mapped_column(ForeignKey("chamado_stage_types.id"))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default=CHAMADO_STAGE_PENDENTE)
    pending_action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    chamado: Mapped[Chamado] = relationship(back_populates="stages")
    stage_type: Mapped[ChamadoStageType] = relationship()
    messages: Mapped[list["ChamadoMessage"]] = relationship(
        back_populates="stage",
        order_by="ChamadoMessage.seq",
        cascade="all, delete-orphan",
    )


class ChamadoMessage(Base):
    """Uma interação do fluxo de um chamado (transcript por etapa).

    Espelho do `RunEvent` das tasks, porém **task-independente**: os eventos são
    atrelados a `(chamado_id, stage_id)` e o payload é SEMPRE completo (nunca truncar).
    `kind`: `user` (pedido da pessoa, com `tool`), `assistant_text`, `tool_call`,
    `tool_result`, `system` (decisões/erros/avaliações do worker)."""

    __tablename__ = "chamado_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chamado_id: Mapped[int] = mapped_column(ForeignKey("chamados.id"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("chamado_stages.id"))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    chamado: Mapped[Chamado] = relationship(back_populates="messages")
    stage: Mapped[ChamadoStage] = relationship(back_populates="messages")
