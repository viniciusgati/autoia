"""Schemas Pydantic da API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _serialize_utc(value: datetime) -> str:
    """Serializa datetime NAIVE (armazenado em UTC) com marcador de fuso UTC.

    O banco guarda UTC sem tzinfo (`db.utcnow()` remove); sem o marcador o
    navegador interpreta o valor como hora local e mostra a hora UTC crua.
    Com `+00:00`, o `new Date(...)` do frontend converte para o fuso do usuário
    (ex.: GMT-3)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class ApiModel(BaseModel):
    """Base comum da API.

    Todos os datetimes saem com marcador de fuso UTC (`+00:00`) no JSON, para o
    frontend converter para o fuso local do usuário corretamente (ex.: GMT-3).
    """

    model_config = ConfigDict(json_encoders={datetime: _serialize_utc})


# ---------- Repository ----------

class RepositoryCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)
    default_branch: str = Field(default="main", max_length=100)
    # Configurações opcionais
    max_attempts: int | None = None
    max_pm_decisions: int | None = None
    run_timeout: int | None = None
    no_progress_timeout: int | None = None
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
    # Perfil de toolchain seguro e allowlisted (ex.: android-compose-37).
    sandbox_profile: str | None = None
    # Imagem pinada pelo administrador para este projeto (opcional).
    sandbox_image: str | None = None
    # Repositórios onde este projeto pode criar tarefas (allowlist de saída;
    # vazio = restritivo, só o próprio projeto).
    task_targets: list[str] = []
    # Informações úteis injetadas no contexto dos robôs (DNS de deploy, URLs, env).
    external_context: str | None = None
    # Conta opencode-go fixada para este repositório (nome no roster global).
    opencode_account: str | None = None


class RepositoryUpdate(ApiModel):
    """Edição de configurações de um repositório existente (todos opcionais)."""
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    default_branch: str | None = Field(default=None, max_length=100)
    max_attempts: int | None = None
    max_pm_decisions: int | None = None
    run_timeout: int | None = None
    no_progress_timeout: int | None = None
    task_budget: float | None = None
    cost_per_interaction: float | None = None
    risky_patterns_extra: str | None = None
    db_rule: str | None = None
    allow_auto_tasks: bool | None = None
    allow_external_tasks: bool | None = None
    default_pipeline_id: int | None = None
    auto_summary: bool | None = None
    sandbox: str | None = None
    sandbox_profile: str | None = None
    sandbox_image: str | None = None
    task_targets: list[str] | None = None
    external_context: str | None = None
    opencode_account: str | None = None


class RepositoryOut(ApiModel):
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
    no_progress_timeout: int | None = None
    task_budget: float | None = None
    cost_per_interaction: float | None = None
    risky_patterns_extra: str | None = None
    db_rule: str | None = None
    allow_auto_tasks: bool = False
    allow_external_tasks: bool = False
    default_pipeline_id: int | None = None
    auto_summary: bool = False
    sandbox: str | None = None
    sandbox_profile: str | None = None
    sandbox_image: str | None = None
    # Repositórios onde este projeto pode criar tarefas (allowlist de saída;
    # vazio = restritivo). Reexista a NULL de bancos criados antes da coluna.
    task_targets: list[str] = []
    external_context: str | None = None
    opencode_account: str | None = None

    @field_validator("task_targets", mode="before")
    @classmethod
    def _task_targets_not_null(cls, v):
        return v or []


class RepositoryDeleteInfo(ApiModel):
    """Informações exibidas no diálogo de confirmação de exclusão do projeto
    (`GET /api/repositories/{id}/delete-info`)."""

    active_tasks: int
    checkout_path: str | None


# ---------- Contas opencode-go ----------

class OpenCodeAccountCreate(ApiModel):
    """Criação de uma conta opencode-go (roster global de credenciais)."""

    name: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=1)
    description: str = ""
    is_default: bool = False


class OpenCodeAccountUpdate(ApiModel):
    """Edição de uma conta; `token` ausente = mantém o atual. `is_default=True`
    remove o default de todas as demais (default é exclusivo)."""

    token: str | None = Field(default=None, min_length=1)
    description: str | None = None
    is_default: bool | None = None


class OpenCodeAccountOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str = ""
    is_default: bool = False
    # O token NUNCA é retornado; só se a conta tem credencial válida.
    has_token: bool = True


# ---------- Usuários / Auth ----------

class UserOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: str
    active: bool
    created_at: datetime


class UserCreate(ApiModel):
    """Criação de usuário por admin global (senha em texto puro, hasheada no backend)."""

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=255)
    role: str = Field(default="member", pattern="^(member|admin)$")


class UserUpdate(ApiModel):
    """Edição de usuário por admin global (todos os campos opcionais)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    password: str | None = Field(default=None, min_length=6, max_length=255)
    role: str | None = Field(default=None, pattern="^(member|admin)$")
    active: bool | None = None


class LoginRequest(ApiModel):
    email: str
    password: str


class RegisterRequest(ApiModel):
    """Bootstrap: só aceito com `users` vazio (primeiro registro vira admin global)."""

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=255)


class SessionOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    user_id: int
    expires_at: datetime


class RepositoryUserOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    user_id: int
    role: str
    created_at: datetime
    user: UserOut | None = None


class RepositoryUserUpdate(ApiModel):
    role: Literal["member", "admin"]


class RepositoryMemberCreate(ApiModel):
    user_id: int
    role: Literal["member", "admin"] = "member"


# ---------- Skills de projeto ----------

class RepositorySkillOut(ApiModel):
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

class RobotCreate(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    mission: str = Field(min_length=1)
    role: str = Field(default="implement", max_length=30)
    model: str | None = None
    repository_id: int | None = None


class RobotUpdate(ApiModel):
    mission: str | None = None
    role: str | None = None
    model: str | None = None
    active: bool | None = None
    archived: bool | None = None


class RobotOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int | None = None
    name: str
    mission: str
    role: str
    model: str | None
    active: bool
    archived: bool
    created_at: datetime


# ---------- Pipeline ----------

class PipelineStepIn(ApiModel):
    position: int = Field(ge=0)
    robot_id: int
    post_merge: bool = False
    pause_before: bool = False


class PipelineCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    steps: list[PipelineStepIn] = Field(min_length=1)
    repository_id: int | None = None


class PipelineStepOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    robot_id: int
    post_merge: bool
    pause_before: bool = False
    robot: RobotOut | None = None


class PipelineOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int | None = None
    name: str
    steps: list[PipelineStepOut]
    created_at: datetime


# ---------- Task ----------

class SubTaskIn(ApiModel):
    """Subtarefa definida na criação da task (opcional — o PO também pode gerar)."""

    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    acceptance_criteria: str | None = None


class SubTaskUpdate(ApiModel):
    """Edição de subtarefa durante a execução (injeta contexto)."""

    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None


class SubTaskOut(ApiModel):
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


class TaskCreate(ApiModel):
    repository_id: int
    pipeline_id: int
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    kind: Literal["issue", "bug", "feature", "chore"] = "issue"
    executor: Literal["kimi", "opencode", "codex"] = "kimi"
    # Modelo do executor (precedência: task > Robot.model > default do executor).
    # Vazio/null = não passa `--model` (codex usa o config dele).
    model: str | None = Field(default=None, max_length=200)
    budget_limit: float | None = Field(default=None, gt=0)
    subtasks: list[SubTaskIn] = []
    # Associação organizacional Projeto > Épico (0..1, opcional). O épico deriva o
    # projeto quando enviado sozinho (mesma regra do fluxo de chamados).
    project_id: int | None = None
    epic_id: int | None = None


class DescriptionFromFileOut(ApiModel):
    """Conteúdo de um arquivo `.txt`/`.md`/`.markdown` extraído para uso como
    descrição de tarefa (o arquivo em si não é armazenado no servidor)."""

    description: str


class TaskStepOut(ApiModel):
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
    execution_mode: str | None = None


class TaskProposalOut(ApiModel):
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
    # Pipeline que a task filha usará se aceita (NULL = default do repo/pai).
    pipeline_id: int | None = None
    status: str
    created_at: datetime
    accepted_task_id: int | None = None


class TaskProposalUpdate(ApiModel):
    """Edição de uma proposta ANTES de aceitar (o usuário ajusta título/descrição/kind
    e a pipeline da task filha — a task nasce com os valores editados)."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    kind: str | None = Field(default=None, pattern="^(feature|bug|issue|chore)$")
    pipeline_id: int | None = None


class TaskChangePipelineRequest(ApiModel):
    """Troca a pipeline de uma task AINDA NÃO iniciada (status `created`) e recria
    as fases do zero — usado para "reiniciar o trabalho" com outro pipeline."""

    pipeline_id: int


class TaskStepListOut(ApiModel):
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
    execution_mode: str | None = None


class TaskListItem(ApiModel):
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
    model: str | None = None
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
    project_id: int | None = None
    epic_id: int | None = None
    mode: str = "auto"
    steps: list[TaskStepListOut] = []


class TaskOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    pipeline_id: int
    title: str
    description: str
    kind: str
    status: str
    executor: str = "kimi"
    model: str | None = None
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
    mode: str = "auto"
    pending_action: str | None = None
    chat_status: str = "idle"
    summary: "TaskSummaryOut | None" = None
    created_at: datetime
    updated_at: datetime
    parent_task_id: int | None = None
    responsible_id: int | None = None
    responsible: UserOut | None = None
    project_id: int | None = None
    epic_id: int | None = None
    steps: list[TaskStepOut] = []
    subtasks: list[SubTaskOut] = []
    proposals: list[TaskProposalOut] = []
    children: list["TaskOut"] = []

    @field_validator("steps", mode="before")
    @classmethod
    def _steps_sem_arquivados(cls, v):
        """Fases arquivadas (mudança de pipeline) não aparecem na UI atual."""
        if v is None:
            return []
        return [s for s in v if not getattr(s, "archived", False)]


class TaskSummaryOut(ApiModel):
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


class TimelineEventOut(ApiModel):
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


class StepSummaryOut(ApiModel):
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


class WorkspaceOccurrenceOut(ApiModel):
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
    # Custo acumulado desta execução (soma dos custos dos RunEvent; kimi estimado,
    # opencode custo real). Mesma moeda de `Task.cost_spent`.
    cost: float = 0.0
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
    # Branch onde vive a alteração desta fase (pre-merge: branch da task; pós-merge:
    # default do repositório, após a integração). Ajuda a conferir no remoto.
    branch: str | None = None


class WorkspaceOut(ApiModel):
    """Payload da tela de trabalho (workspace): task + timeline de execuções.

    Em modo manual, inclui o chat (mensagens), as rodadas de agente e a lista de
    agentes disponíveis para o dispatcher/menu."""

    task: TaskOut
    summary: TaskSummaryOut | None = None
    occurrences: list[WorkspaceOccurrenceOut] = []
    decisions: list[dict] = []
    messages: list["TaskMessageOut"] = []
    runs: list["TaskRunOut"] = []
    agents: list[RobotOut] = []


class TaskMessageOut(ApiModel):
    """Uma interação do chat human-in-the-loop de uma task (transcript)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    seq: int
    ts: datetime
    kind: str
    payload: dict = {}
    cost: float = 0.0


class TaskRunOut(ApiModel):
    """Uma rodada de agente no modo human-in-the-loop (histórico)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    robot_id: int | None = None
    robot_name: str = ""
    robot_role: str = "implement"
    instruction: str = ""
    status: str
    final_text: str | None = None
    verdict: str | None = None
    diff_stat: str | None = None
    cost: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ChatSendRequest(ApiModel):
    """Mensagem do usuário no chat human-in-the-loop (modo manual)."""

    text: str = Field(min_length=1, max_length=10000)


class ChatMessageResponse(ApiModel):
    ok: bool
    message: str


class StepDiffOut(ApiModel):
    """Diff real (git) do commit de uma fase — o git é a fonte de verdade."""

    stat: str = ""
    diff: str = ""
    files: list[str] = []
    commit: str | None = None


class StepFileDiffOut(ApiModel):
    """Diff real (git) de UM arquivo dentro do commit de uma fase."""

    path: str = ""
    stat: str = ""
    diff: str = ""
    commit: str | None = None


class InstructionRequest(ApiModel):
    """Instrução do usuário ao agente + (opcional) a partir de qual fase continuar."""

    instruction: str = Field(min_length=1, max_length=10000)
    position: int | None = None


class BlockedContinueRequest(ApiModel):
    """Instrução do usuário para retomar uma fase bloqueada (continuar de onde parou)."""

    instruction: str = Field(min_length=1, max_length=10000)


class FeedbackCreate(ApiModel):
    text: str = Field(min_length=1, max_length=10000)


class RetryRequest(ApiModel):
    """Retry manual de fase: `note` opcional vira feedback externo da task."""

    note: str | None = Field(default=None, max_length=10000)


class ResponsibleUpdate(ApiModel):
    """Reatribuição do responsável por uma tarefa (upsert de repository_users)."""

    user_id: int


class ReviewRequest(ApiModel):
    action: Literal["approve", "cancel"]
    extra_budget: float = Field(default=5.0, ge=0)
    note: str | None = None


class BouncebackRequest(ApiModel):
    target_position: int  # posição do step para onde voltar (ex.: 2 = implement)
    note: str | None = Field(default=None, max_length=2000)
    reviewed_by: str = "humano"  # identificação de quem confirmou


class ApproveStepRequest(ApiModel):
    """Aprovação humana de uma fase com `pause_before` (gate)."""

    position: int  # posição do step aguardando aprovação
    note: str | None = Field(default=None, max_length=10000)


class TaskUpdateRequest(ApiModel):
    """Edição humana da história (descrição/critérios) — permitida em `created` e
    `waiting_approval`. `details` (detalhes da implementação), a associação
    Projeto > Épico (`project_id`/`epic_id`), o `executor` e o `model` das fases
    são permitidos em qualquer status (executor/modelo só não mudam com uma fase
    em execução real).

    A associação distingue **campo ausente** (via `model_fields_set` — não altera)
    de **`null` explícito** (remove): `project_id: null` remove projeto e épico;
    `epic_id: null` remove apenas o épico.
    """

    description: str | None = None
    acceptance_criteria: str | None = None
    details: str | None = None
    project_id: int | None = None
    epic_id: int | None = None
    executor: str | None = Field(default=None, pattern="^(kimi|opencode|codex)$")
    # Modelo do executor da task (""/null = herda Robot.model / default do executor).
    model: str | None = Field(default=None, max_length=200)
    # Modo de execução (auto | manual): alternável em runtime, em qualquer status.
    mode: str | None = Field(default=None, pattern="^(auto|manual)$")


# ---------- Modelos de executor ----------

class CodexModelsOut(ApiModel):
    """Modelos disponíveis para o executor codex (populam o dropdown da UI)."""

    models: list[str] = []
    # Fonte da lista: "cli" (`codex debug models`) ou "config" (AUTOIA_CODEX_MODELS).
    source: str = "config"


class OpenCodeModelsOut(ApiModel):
    """Modelos disponíveis para o executor opencode (populam o dropdown da UI)."""

    models: list[str] = []
    # Fonte da lista: "cli" (`opencode models`) ou "config" (AUTOIA_OPENCODE_MODELS).
    source: str = "config"


# ---------- Eventos ----------

class RunEventOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    seq: int
    ts: datetime
    kind: str
    payload: dict
    cost: float


class ArtifactOut(ApiModel):
    """Metadados de um arquivo gerado por um robô (ex.: screenshot de smoke test)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int
    filename: str
    description: str | None
    created_at: datetime


# ---------- Dashboard ----------

class NoticeOut(ApiModel):
    """Aviso de uma tarefa que requer atenção (guardrail, orçamento, arquitetura...)."""

    task_id: int
    task_title: str
    task_status: str
    repository_id: int
    level: Literal["critical", "warning"]
    kind: str
    message: str
    ts: datetime


class MyTaskOut(ApiModel):
    """Tarefa do usuário no dashboard pessoal (responsable == eu)."""

    id: int
    repository_id: int
    repository_name: str
    title: str
    status: str
    cost_spent: float = 0.0
    budget_limit: float = 0.0
    updated_at: datetime


class MyProjectOut(ApiModel):
    """Participação do usuário em um projeto (papel + contagem de tarefas minhas)."""

    id: int
    name: str
    role: str
    my_tasks_total: int = 0
    my_tasks_active: int = 0
    my_tasks_pending: int = 0


class DashboardOut(ApiModel):
    tasks_by_status: dict[str, int]
    total_cost: float
    total_tasks: int
    guardrail_events: int
    recent_guardrails: list[RunEventOut]
    notices: list[NoticeOut] = []
    # Propostas de tasks filhas aguardando decisão humana (ou já aceitas, com link
    # para a task criada). Rejeitadas saem da lista.
    proposals: list[TaskProposalOut] = []
    # Dashboard pessoal (auth ON): usuário logado, tarefas dele e participações.
    user: UserOut | None = None
    my_tasks: list[MyTaskOut] = []
    projects: list[MyProjectOut] = []


# ---------- Execução (página global) ----------

class WorkerStatusOut(ApiModel):
    alive: bool
    last_heartbeat_sec: float | None = None


class ExecutionOut(ApiModel):
    """Payload da página global "Execução": tasks ativas, eventos ao vivo das fases
    running, propostas pendentes, avisos e status do worker (1 request/poll)."""

    tasks: list[TaskListItem] = []
    current_events: dict[str, list[RunEventOut]] = {}
    proposals: list[TaskProposalOut] = []
    notices: list[NoticeOut] = []
    worker: WorkerStatusOut = WorkerStatusOut(alive=False)


# ---------- Chamados (fluxo de atendimento) ----------

class EpicCreate(ApiModel):
    project_id: int
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: str = Field(default="aberto", pattern="^(aberto|em_andamento|fechado)$")


class EpicUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(aberto|em_andamento|fechado)$")


class EpicOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    description: str = ""
    status: str = "aberto"
    scope: str | None = None
    summary: str | None = None
    # Geração de escopo/resumo em andamento (thread em background).
    generating: bool = False
    created_at: datetime
    updated_at: datetime


class EpicDetailOut(EpicOut):
    chamado_count: int = 0


class ProjectCreate(ApiModel):
    repository_id: int
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: str = Field(default="aberto", pattern="^(aberto|em_andamento|fechado)$")


class ProjectUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(aberto|em_andamento|fechado)$")


class ProjectOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    name: str
    description: str = ""
    status: str = "aberto"
    summary: str | None = None
    # Geração de resumo em andamento (thread em background).
    generating: bool = False
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    """Projeto com seus épicos (e contagem de chamados por épico)."""

    epics: list["EpicOut"] = []
    chamado_count: int = 0


class ChamadoStageTypeOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int | None = None
    name: str
    description: str = ""
    is_initial: bool = False
    allowed_tools: list[str] = []
    close_options: list[str] = []
    delivery_config: dict = {}

    @field_validator("allowed_tools", "close_options", mode="before")
    @classmethod
    def _list_not_null(cls, v):
        return v or []


class ChamadoStageTypeCreate(ApiModel):
    repository_id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    is_initial: bool = False
    allowed_tools: list[str] = []
    close_options: list[str] = []
    delivery_config: dict = {}


class ChamadoCreate(ApiModel):
    repository_id: int
    project_id: int | None = None
    epic_id: int | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    executor: str = Field(default="kimi", pattern="^(kimi|opencode|codex)$")
    # Modelo do executor do chamado (""/null = default do executor).
    model: str | None = Field(default=None, max_length=200)
    budget_limit: float | None = None
    initial_stage_type_id: int | None = None


class ChamadoUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    project_id: int | None = None
    epic_id: int | None = None
    executor: str | None = Field(default=None, pattern="^(kimi|opencode|codex)$")
    model: str | None = Field(default=None, max_length=200)


class ToolInfoOut(ApiModel):
    """Descrição de uma ferramenta disponível na etapa atual do chamado."""

    key: str
    label: str
    description: str


class ChamadoStageOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chamado_id: int
    stage_type_id: int
    position: int
    status: str
    pending_action: str | None = None
    decision: str | None = None
    result: str | None = None
    error: str | None = None
    attempt: int = 1
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stage_type_name: str | None = None


class ChamadoMessageOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chamado_id: int
    stage_id: int
    seq: int
    ts: datetime
    kind: str
    payload: dict = {}
    cost: float = 0.0


class ChamadoOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    project_id: int | None = None
    epic_id: int | None = None
    title: str
    description: str = ""
    workflow_status: str = ""
    status: str = "aberto"
    executor: str = "kimi"
    model: str | None = None
    budget_limit: float = 10.0
    cost_spent: float = 0.0
    error: str | None = None
    responsible_id: int | None = None
    created_at: datetime
    updated_at: datetime
    stages: list[ChamadoStageOut] = []


class ChamadoWorkspaceOut(ApiModel):
    """Payload da tela do chamado: chamado + etapas (histórico) + mensagens +
    etapa atual + ferramentas disponíveis nela."""

    chamado: ChamadoOut
    stages: list[ChamadoStageOut] = []
    messages: list[ChamadoMessageOut] = []
    current_stage: ChamadoStageOut | None = None
    tools: list[ToolInfoOut] = []
    close_options: list[str] = []


class ToolRunRequest(ApiModel):
    """Pedido do usuário para rodar uma ferramenta na etapa atual."""

    text: str = Field(min_length=1, max_length=4000)


class ChamadoMessageResponse(ApiModel):
    ok: bool
    message: str


# ---------- Sistema (configuração geral) ----------

class StorageCategory(ApiModel):
    """Uma categoria do relatório de armazenamento (id = chave estável usada
    pelo frontend; label = nome PT-BR exibido)."""

    id: str
    label: str
    size_bytes: int
    item_count: int
    # True = categoria alvo da limpeza de órfãos; False = apenas medida
    # (database/workspaces/skills nunca são limpáveis).
    cleanable: bool


class StorageReport(ApiModel):
    """Relatório completo do armazenamento do sistema (5 categorias + total)."""

    categories: list[StorageCategory] = []
    total_bytes: int = 0


class CleanRequest(ApiModel):
    """Alvos da limpeza de órfãos (ids estáveis: logs, pytest_tmp, smoke,
    chrome_profiles). Id desconhecido → 400."""

    targets: list[str]


class CleanTargetResult(ApiModel):
    """Resultado da limpeza de um alvo (itens removidos e bytes liberados)."""

    target: str
    item_count: int
    bytes_freed: int


class CleanResult(ApiModel):
    """Resposta da limpeza: detalhe por alvo + total liberado + relatório
    atualizado refletindo a remoção."""

    targets: list[CleanTargetResult] = []
    total_bytes_freed: int = 0
    report: StorageReport = StorageReport()
