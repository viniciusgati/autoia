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
    artifacts: list[ArtifactOut] = []


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
    created_at: datetime
    updated_at: datetime
    parent_task_id: int | None = None
    steps: list[TaskStepOut] = []
    subtasks: list[SubTaskOut] = []
    children: list["TaskOut"] = []


class FeedbackCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class RetryRequest(BaseModel):
    """Retry manual de fase: `note` opcional vira feedback externo da task."""

    note: str | None = Field(default=None, max_length=10000)


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
    `waiting_approval`."""

    description: str | None = None
    acceptance_criteria: str | None = None


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
    level: Literal["critical", "warning"]
    kind: str
    message: str
    ts: datetime


class DashboardOut(BaseModel):
    tasks_by_status: dict[str, int]
    total_cost: float
    total_tasks: int
    guardrail_events: int
    recent_guardrails: list[RunEventOut]
    notices: list[NoticeOut] = []
