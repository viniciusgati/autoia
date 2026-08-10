"""Modelos do banco de dados."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# Estados possíveis
TASK_QUEUED = "queued"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_BLOCKED = "blocked"
TASK_NEEDS_REVIEW = "needs_review"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_GUARDRAIL_BLOCKED = "guardrail_blocked"

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

    tasks: Mapped[list["Task"]] = relationship(back_populates="repository")


class Robot(Base):
    __tablename__ = "robots"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    mission: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default="implement")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
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

    repository: Mapped[Repository] = relationship(back_populates="tasks")
    pipeline: Mapped[Pipeline] = relationship()
    parent: Mapped["Task | None"] = relationship(remote_side="Task.id", back_populates="children")
    children: Mapped[list["Task"]] = relationship(back_populates="parent")
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
    log_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_stat: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    task: Mapped[Task] = relationship(back_populates="steps")
    robot: Mapped[Robot] = relationship()
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="step",
        order_by="RunEvent.seq",
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
