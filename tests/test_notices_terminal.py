"""Notices do dashboard não devem mostrar tarefas em estado terminal (done/failed/cancelled)."""

from __future__ import annotations

from app.models import RunEvent, Task


def _setup_notice(flow, task_status: str) -> int:
    """Deixa a task do flow com step guardrail_blocked + evento arch_metric e um status."""
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        task = s.get(Task, task_id)
        task.status = task_status
        step = task.steps[0]
        step.status = "guardrail_blocked"
        step.error = "guardrail: rm -rf"
        s.add(
            RunEvent(
                step_id=step.id,
                seq=99,
                kind="arch_metric",
                payload={"score": 80, "level": "alto", "reasons": ["Dockerfile adicionado"]},
            )
        )
        s.commit()
    return task_id


def _notice_kinds(flow) -> set[str]:
    notices = flow["client"].get("/api/dashboard").json()["notices"]
    return {n["kind"] for n in notices}


def test_notices_somem_quando_task_concluida(flow):
    """Task done/failed não gera notice de guardrail nem de arquitetura."""
    task_id = _setup_notice(flow, "done")
    kinds = _notice_kinds(flow)
    assert "guardrail" not in kinds
    assert "arch" not in kinds

    # failed também é terminal
    _setup_notice(flow, "failed")
    kinds = _notice_kinds(flow)
    assert "guardrail" not in kinds
    assert "arch" not in kinds


def test_notices_permanecem_enquanto_task_ativa(flow):
    """Task ainda ativa (in_progress) mantém os avisos."""
    _setup_notice(flow, "in_progress")
    kinds = _notice_kinds(flow)
    assert "guardrail" in kinds
    assert "arch" in kinds
