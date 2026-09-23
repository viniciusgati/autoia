"""Varredura de tasks 'travadas' (`_sweep_stuck_tasks`): tasks em que/in_progress
sem fase running nem pending e com a primeira fase ativa não-concluída em
failed/guardrail_blocked nunca são reclamadas pelo `claim_next` — a varredura as
move para `needs_review` (decisão humana) em vez de deixá-las presas."""

from __future__ import annotations

from app.models import (
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    RunEvent,
    TaskStep,
)
from app.worker import runner


def _all_steps(session_factory, task_id):
    with session_factory() as s:
        return (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id)
            .order_by(TaskStep.position)
            .all()
        )


def _task_status(session_factory, task_id):
    from app.models import Task

    with session_factory() as s:
        task = s.get(Task, task_id)
        return task.status, task.error


def test_sweep_move_task_travada_para_needs_review(flow):
    """Task `in_progress` com todas as fases failed (sem pending/running e sem
    fase anterior para bounce) fica sem nada reclamável → needs_review."""
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        for st in s.query(TaskStep).filter(TaskStep.task_id == task_id):
            st.status = STEP_FAILED
            st.error = "timeout"
        from app.models import Task

        task = s.get(Task, task_id)
        task.status = TASK_IN_PROGRESS
        s.commit()

    # Sem a varredura, o claim não encontra nada.
    assert runner.claim_next(flow["session_factory"]) is None

    runner._sweep_stuck_tasks(flow["session_factory"])

    status, error = _task_status(flow["session_factory"], task_id)
    assert status == TASK_NEEDS_REVIEW
    assert "pipeline travado" in error
    with flow["session_factory"]() as s:
        ev = (
            s.query(RunEvent)
            .join(TaskStep)
            .filter(TaskStep.task_id == task_id, RunEvent.kind == "task_stuck_needs_review")
            .one_or_none()
        )
        assert ev is not None


def test_sweep_nao_mexe_task_com_fase_pendente(flow):
    """Com uma fase `pending`, a task é reclamável — a varredura não mexe."""
    task_id = flow["task"]["id"]
    steps = _all_steps(flow["session_factory"], task_id)
    with flow["session_factory"]() as s:
        for st in s.query(TaskStep).filter(TaskStep.task_id == task_id):
            st.status = STEP_DONE
        steps[0].status = STEP_FAILED
        steps[1].status = STEP_PENDING
        from app.models import Task

        task = s.get(Task, task_id)
        task.status = TASK_IN_PROGRESS
        s.commit()

    runner._sweep_stuck_tasks(flow["session_factory"])

    status, _ = _task_status(flow["session_factory"], task_id)
    assert status == TASK_IN_PROGRESS


def test_sweep_nao_mexe_task_com_fronteira_created(flow):
    """Fronteira `created` (fase ainda não alcançada) não é falha — a varredura
    não deve tratar como travada."""
    task_id = flow["task"]["id"]
    steps = _all_steps(flow["session_factory"], task_id)
    with flow["session_factory"]() as s:
        for st in s.query(TaskStep).filter(TaskStep.task_id == task_id):
            st.status = STEP_DONE
        steps[0].status = "created"  # fronterira `created` (fase não alcançada)
        from app.models import Task

        task = s.get(Task, task_id)
        task.status = TASK_IN_PROGRESS
        s.commit()

    runner._sweep_stuck_tasks(flow["session_factory"])

    status, _ = _task_status(flow["session_factory"], task_id)
    assert status == TASK_IN_PROGRESS