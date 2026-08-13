"""Schemas Pydantic da API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------- Repository ----------

class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)
    default_branch: str = Field(default="main", max_length=100)
    # Configurações opcionais
    max_attempts: int | None = None
    max_pm_decisions: int | None = None
    run_timeout: int | None = None
    task_budget: float | None = None
    cost_per_interaction: float | None = None
    risky_patterns_extra: str | None = None
    db_rule: str | None = None
    allow_auto_tasks: bool = False
    allow_external_tasks: bool = False
    default_pipeline_id: int | None = None
    auto_summary: bool = False
    # Modo de sandbox de execução do projeto: None herda o global; "off"|"fs"|"full".
    sandbox: str | None = None


class RepositoryUpdate(BaseModel):
    """Edição de configurações de um repositório existente (todos opcionais)."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    default_branch: str | None = Field(default=None, max_length=100)
    max_attempts: int | None = None
    max_pm_decisions: int | None = None
    run_timeout: int | None = None
    task_budget: float | None = None
    cost_per_interaction: float | None = None
    risky_patterns_extra: str | None = None
    db_rule: str | None = None
    allow_auto_tasks: bool | None = None
    allow_external_tasks: bool | None = None
    default_pipeline_id: int | None = None
    auto_summary: bool | None = None
    sandbox: str | None = None


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    default_branch: str
    local_path: str | None
    created_at: datetime
    # Configurações
    max_attempts: int | None = None
    max_pm_decisions: int | None = None
    run_timeout: int | None = None
    task_budget: float | None = None
    cost_per_interaction: float | None = None
    risky_patterns_extra: str | None = None
    db_rule: str | None = None
    allow_auto_tasks: bool = False
    allow_external_tasks: bool = False
    default_pipeline_id: int | None = None
    auto_summary: bool = False
    sandbox: str | None = None


class RepositoryDeleteInfo(BaseModel):
    """Informações exibidas no diálogo de confirmação de exclusão do projeto
    (`GET /api/repositories/{id}/delete-info`)."""

    active_tasks: int
    checkout_path: str | None


# ---------- Usuários / Auth ----------

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: str
    active: bool
    created_at: datetime


class UserCreate(BaseModel):
    """Criação de usuário por admin global (senha em texto puro, hasheada no backend)."""

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=255)
    role: str = Field(default="member", pattern="^(member|admin)$")


class UserUpdate(BaseModel):
    """Edição de usuário por admin global (todos os campos opcionais)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    password: str | None = Field(default=None, min_length=6, max_length=255)
    role: str | None = Field(default=None, pattern="^(member|admin)$")
    active: bool | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    """Bootstrap: só aceito com `users` vazio (primeiro registro vira admin global)."""

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=255)


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    user_id: int
    expires_at: datetime


class RepositoryUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    user_id: int
    role: str
    created_at: datetime
    user: UserOut | None = None


class RepositoryUserUpdate(BaseModel):
    role: Literal["member", "admin"]


class RepositoryMemberCreate(BaseModel):
    user_id: int
    role: Literal["member", "admin"] = "member"


# ---------- Skills de projeto ----------

class RepositorySkillOut(BaseModel):
    """Skill de projeto: metadados do upload de `.zip` com `SKILL.md` na raiz.

    Os arquivos ficam em `data/skills/<repository_id>/<skill_id>/` no disco; o
    payload alimenta a lista/feedback da UI (nome, descrição do frontmatter,
    nº de arquivos e tamanho total).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    name: str
    description: str
    file_count: int
    size_bytes: int
    created_at: datetime


# ---------- Robot ----------

class RobotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mission: str = Field(min_length=1)
    role: str = Field(default="implement", max_length=30)
    model: str | None = None
    repository_id: int | None = None


class RobotUpdate(BaseModel):
    mission: str | None = None
    role: str | None = None
    model: str | None = None
    active: bool | None = None


class RobotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int | None = None
    name: str
    mission: str
    role: str
    model: str | None
    active: bool
    created_at: datetime


# ---------- Pipeline ----------

class PipelineStepIn(BaseModel):
    position: int = Field(ge=0)
    robot_id: int
    post_merge: bool = False
    pause_before: bool = False


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    steps: list[PipelineStepIn] = Field(min_length=1)
    repository_id: int | None = None


class PipelineStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    robot_id: int
    post_merge: bool
    pause_before: bool = False
    robot: RobotOut | None = None


class PipelineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int | None = None
    name: str
    steps: list[PipelineStepOut]
    created_at: datetime


# ---------- Task ----------

class SubTaskIn(BaseModel):
    """Subtarefa definida na criação da task (opcional — o PO também pode gerar)."""

    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    acceptance_criteria: str | None = None


class SubTaskUpdate(BaseModel):
    """Edição de subtarefa durante a execução (injeta contexto)."""

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None


class SubTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    title: str
    description: str
    acceptance_criteria: str | None
    status: str
    attempt: int
    summary: str | None
    verdict: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class TaskCreate(BaseModel):
    repository_id: int
    pipeline_id: int
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    kind: Literal["issue", "bug", "feature", "chore"] = "issue"
    executor: Literal["kimi", "opencode"] = "kimi"
    budget_limit: float | None = Field(default=None, gt=0)
    subtasks: list[SubTaskIn] = []


class TaskStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    robot: RobotOut | None = None
    status: str
    attempt: int
    verdict: str | None
    post_merge: bool
    pause_before: bool = False
    log_path: str | None
    summary: str | None
    diff_stat: str | None = None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    responsible_id: int | None = None
    finished_by_id: int | None = None
    responsible: UserOut | None = None
    finished_by: UserOut | None = None
    artifacts: list[ArtifactOut] = []


class TaskProposalOut(BaseModel):
    """Proposta de task filha aguardando (ou não) aprovação humana."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    step_id: int | None
    position: int
    title: str
    description: str
    kind: str
    repository_id: int | None = None
    target_repository_id: int | None
    status: str
    created_at: datetime
    accepted_task_id: int | None = None


class TaskStepListOut(BaseModel):
    """Fase em payload "lean" de listas: sem o texto integral do resumo,
    apenas um preview truncado para exibição (o completo fica no detalhe)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    robot: RobotOut | None = None
    status: str
    attempt: int
    verdict: str | None
    post_merge: bool
    pause_before: bool = False
    diff_stat: str | None = None
    summary_preview: str | None = None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    responsible_id: int | None = None
    finished_by_id: int | None = None
    responsible: UserOut | None = None
    finished_by: UserOut | None = None


class TaskListItem(BaseModel):
    """Listagem leve de tasks (polling): sem resumo LLM, children, propostas e
    subtarefas — só o que as telas de grid/dashboard precisam renderizar."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    pipeline_id: int
    title: str
    kind: str
    status: str
    executor: str = "kimi"
    current_step: int
    budget_limit: float
    cost_spent: float
    pm_decisions: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    parent_task_id: int | None = None
    responsible_id: int | None = None
    responsible: UserOut | None = None
    steps: list[TaskStepListOut] = []


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    pipeline_id: int
    title: str
    description: str
    kind: str
    status: str
    executor: str = "kimi"
    current_step: int
    branch: str | None
    acceptance_criteria: str | None
    budget_limit: float
    cost_spent: float
    pm_decisions: int
    feedback: str | None = None
    error: str | None
    details: str | None = None
    resume_instruction: str | None = None
    block_reason_type: str | None = None
    block_reason: str | None = None
    block_question: str | None = None
    summary: "TaskSummaryOut | None" = None
    created_at: datetime
    updated_at: datetime
    parent_task_id: int | None = None
    responsible_id: int | None = None
    responsible: UserOut | None = None
    steps: list[TaskStepOut] = []
    subtasks: list[SubTaskOut] = []
    proposals: list[TaskProposalOut] = []
    children: list["TaskOut"] = []


class TaskSummaryOut(BaseModel):
    """Resumo estruturado do desenvolvimento gerado por LLM (persistido no banco)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    summary: str
    request: str | None = None
    implementation: str | None = None
    changes: list[str] = []
    result: str | None = None
    issues: list[str] = []
    files: list[str] = []
    tasks_summary: str | None = None
    model: str | None = None
    created_at: datetime


class TimelineEventOut(BaseModel):
    """Evento da timeline cronológica de execução (resumo determinístico, sem LLM)."""

    seq: int
    ts: datetime
    type: str
    name: str
    summary: str
    status: str | None = None
    duration_ms: int | None = None
    input: dict | None = None
    output: dict | None = None
    raw: dict
    step_id: int | None = None
    step_position: int | None = None
    step_robot: str | None = None
    step_role: str | None = None


class StepSummaryOut(BaseModel):
    """Resumo de UMA execução de fase ("O que foi entregue") gerado por LLM dedicada."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    position: int
    attempt: int
    summary: str
    changes: list[str] = []
    result: str | None = None
    issues: list[str] = []
    files: list[str] = []
    created_at: datetime


class WorkspaceOccurrenceOut(BaseModel):
    """Uma execução de fase na timeline do workspace (histórico imutável)."""

    step_id: int
    position: int
    robot: dict | None = None
    attempt: int
    run: int = 1
    is_rerun: bool = False
    status: str
    goal: str | None = None
    # Missão desta execução ("por que esta execução existe"): texto humano. Vem da
    # LLM dedicada (StepMission) ou, enquanto não está pronta, de um fallback
    # determinístico derivado dos eventos. `mission_source`: "llm" | "fallback".
    mission: str | None = None
    mission_source: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Duração total desta execução em ms (dos timestamps dos eventos; None se incompleta).
    duration_ms: int | None = None
    last_activity: str | None = None
    delivered_text: str | None = None
    delivered: StepSummaryOut | None = None
    stop: dict | None = None
    proposals: list[TaskProposalOut] = []
    files: list[str] = []
    file_count: int = 0
    tests: dict | None = None
    system_activity: list[dict] = []
    events: list[TimelineEventOut] = []


class WorkspaceOut(BaseModel):
    """Payload da tela de trabalho (workspace): task + timeline de execuções."""

    task: TaskOut
    summary: TaskSummaryOut | None = None
    occurrences: list[WorkspaceOccurrenceOut] = []
    decisions: list[dict] = []


class StepDiffOut(BaseModel):
    """Diff real (git) do commit de uma fase — o git é a fonte de verdade."""

    stat: str = ""
    diff: str = ""
    files: list[str] = []
    commit: str | None = None


class InstructionRequest(BaseModel):
    """Instrução do usuário ao agente + (opcional) a partir de qual fase continuar."""

    instruction: str = Field(min_length=1, max_length=10000)
    position: int | None = None


class BlockedContinueRequest(BaseModel):
    """Instrução do usuário para retomar uma fase bloqueada (continuar de onde parou)."""

    instruction: str = Field(min_length=1, max_length=10000)


class FeedbackCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class RetryRequest(BaseModel):
    """Retry manual de fase: `note` opcional vira feedback externo da task."""

    note: str | None = Field(default=None, max_length=10000)


class ResponsibleUpdate(BaseModel):
    """Reatribuição do responsável por uma tarefa (upsert de repository_users)."""

    user_id: int


class ReviewRequest(BaseModel):
    action: Literal["approve", "cancel"]
    extra_budget: float = Field(default=5.0, ge=0)
    note: str | None = None


class BouncebackRequest(BaseModel):
    target_position: int  # posição do step para onde voltar (ex.: 2 = implement)
    note: str | None = Field(default=None, max_length=2000)
    reviewed_by: str = "humano"  # identificação de quem confirmou


class ApproveStepRequest(BaseModel):
    """Aprovação humana de uma fase com `pause_before` (gate)."""

    position: int  # posição do step aguardando aprovação
    note: str | None = Field(default=None, max_length=10000)


class TaskUpdateRequest(BaseModel):
    """Edição humana da história (descrição/critérios) — permitida em `created` e
    `waiting_approval`. `details` (detalhes da implementação) é permitido em qualquer
    status: complementa o contexto e entra no handoff das próximas fases."""

    description: str | None = None
    acceptance_criteria: str | None = None
    details: str | None = None


# ---------- Eventos ----------

class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    seq: int
    ts: datetime
    kind: str
    payload: dict
    cost: float


class ArtifactOut(BaseModel):
    """Metadados de um arquivo gerado por um robô (ex.: screenshot de smoke test)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    filename: str
    description: str | None
    created_at: datetime


# ---------- Dashboard ----------

class NoticeOut(BaseModel):
    """Aviso de uma tarefa que requer atenção (guardrail, orçamento, arquitetura...)."""

    task_id: int
    task_title: str
    task_status: str
    repository_id: int
    level: Literal["critical", "warning"]
    kind: str
    message: str
    ts: datetime


class MyTaskOut(BaseModel):
    """Tarefa do usuário no dashboard pessoal (responsable == eu)."""

    id: int
    repository_id: int
    repository_name: str
    title: str
    status: str
    cost_spent: float = 0.0
    budget_limit: float = 0.0
    updated_at: datetime


class MyProjectOut(BaseModel):
    """Participação do usuário em um projeto (papel + contagem de tarefas minhas)."""

    id: int
    name: str
    role: str
    my_tasks_total: int = 0
    my_tasks_active: int = 0
    my_tasks_pending: int = 0


class DashboardOut(BaseModel):
    tasks_by_status: dict[str, int]
    total_cost: float
    total_tasks: int
    guardrail_events: int
    recent_guardrails: list[RunEventOut]
    notices: list[NoticeOut] = []
    # Dashboard pessoal (auth ON): usuário logado, tarefas dele e participações.
    user: UserOut | None = None
    my_tasks: list[MyTaskOut] = []
    projects: list[MyProjectOut] = []


# ---------- Execução (página global) ----------

class WorkerStatusOut(BaseModel):
    alive: bool
    last_heartbeat_sec: float | None = None


class ExecutionOut(BaseModel):
    """Payload da página global "Execução": tasks ativas, eventos ao vivo das fases
    running, propostas pendentes, avisos e status do worker (1 request/poll)."""

    tasks: list[TaskListItem] = []
    current_events: dict[str, list[RunEventOut]] = {}
    proposals: list[TaskProposalOut] = []
    notices: list[NoticeOut] = []
    worker: WorkerStatusOut = WorkerStatusOut(alive=False)
