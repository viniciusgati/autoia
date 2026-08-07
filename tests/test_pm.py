"""Testes do robô PM (decisão e aplicação) e do limite de decisões."""

from __future__ import annotations

import pytest

from app.models import Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "decisão emitida"},
]


def _set_state(flow, **fields):
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        for key, value in fields.items():
            setattr(t, key, value)
        s.commit()


def _set_step(flow, position, status, attempt=1, error=None):
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        step = next(st for st in t.steps if st.position == position)
        step.status = status
        step.attempt = attempt
        step.error = error
        s.commit()


def _pm(flow, fake_kimi, verdict_key):
    flow["settings"].kimi_bin = fake_kimi(HARMLESS, verdict=verdict_key)
    runner._pm_decide(flow["session_factory"], flow["settings"], flow["task"]["id"], "test")


def test_pm_retry_applies(flow, fake_kimi):
    flow["settings"].task_budget = 100.0
    _set_state(flow, status="failed")
    _set_step(flow, 3, "failed", attempt=1, error="tests falharam")

    _pm(flow, fake_kimi, "pm_retry")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "in_progress"
        assert t.pm_decisions == 1
        tester = next(st for st in t.steps if st.position == 3)
        assert tester.status == "pending"
        assert tester.attempt == 2
        anchor = sorted(t.steps, key=lambda x: x.position)[-1]
        assert any(e.kind == "pm_decision" for e in anchor.events)


def test_pm_continue_applies_budget(flow, fake_kimi):
    flow["settings"].task_budget = 1.0
    _set_state(flow, status="needs_review", budget_limit=1.0, error="orçamento estourado")
    _set_step(flow, 0, "pending")

    _pm(flow, fake_kimi, "pm_continue")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "in_progress"
        assert t.budget_limit == pytest.approx(6.0)  # 1.0 + pm_budget_topup 5.0
        assert t.error is None


def test_pm_escalate_keeps_needs_review(flow, fake_kimi):
    _set_state(flow, status="needs_review")
    _set_step(flow, 0, "pending")

    _pm(flow, fake_kimi, "pm_escalate")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "needs_review"
        assert "escalou" in (t.error or "")


def test_pm_invalid_decision_escalates(flow, fake_kimi):
    _set_state(flow, status="failed")

    _pm(flow, fake_kimi, "pm_escalate")  # regra válida; ver inválida abaixo

    # decisão inválida (garbage) -> escalar
    flow["settings"].kimi_bin = fake_kimi(HARMLESS, verdict=None)
    # fake sem veredicto: PM não escreve arquivo -> parse -> escalar
    runner._pm_decide(flow["session_factory"], flow["settings"], flow["task"]["id"], "test")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "needs_review"
        assert "sem veredicto" in (t.error or "") or "inválida" in (t.error or "")


def test_pm_limit_skips(flow, fake_kimi):
    flow["settings"].max_pm_decisions = 0  # limite zero: PM nunca decide
    flow["settings"].kimi_bin = fake_kimi(HARMLESS, verdict="pm_retry")
    _set_state(flow, status="failed")

    runner._maybe_pm(flow["session_factory"], flow["settings"], flow["task"]["id"], "falha")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.pm_decisions == 0
        assert t.status == "failed"
        anchor = sorted(t.steps, key=lambda x: x.position)[-1]
        assert any(e.kind == "pm_skip" for e in anchor.events)
