"""Testes do robô PM (decisão e aplicação) e do limite de decisões."""

from __future__ import annotations

import pytest

from app.models import RunEvent, Task
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


def test_pm_retry_by_role_name_falls_back_to_failed_step(flow, fake_kimi):
    """PM escreveu `retry tester` (nome do robô, sem posição): o runner resolve o
    nome para a fase correspondente — sem isso, a decisão era descartada como
    inválida (escalar)."""
    flow["settings"].task_budget = 100.0
    _set_state(flow, status="needs_review", error="timeout após 1800s")
    _set_step(flow, 3, "failed", attempt=1, error="timeout após 1800s")

    _pm(flow, fake_kimi, "pm_retry_role")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "in_progress"
        assert t.pm_decisions == 1
        tester = next(st for st in t.steps if st.position == 3)
        assert tester.status == "pending"
        assert tester.attempt == 2


def test_pm_retry_por_nome_escolhe_a_fase_certa(flow, fake_kimi):
    """`retry tester` (nome) reabre a fase do TESTER, não a primeira fase falha
    (qa) — antes o nome era descartado e o fallback pegava a errada (task-194)."""
    flow["settings"].task_budget = 100.0
    _set_state(flow, status="failed")
    _set_step(flow, 0, "done")
    _set_step(flow, 1, "failed", attempt=2, error="veredicto NEEDS_WORK")
    _set_step(flow, 2, "done")
    _set_step(flow, 3, "failed", attempt=1, error="FAIL")

    _pm(flow, fake_kimi, "pm_retry_role")  # DECISÃO: retry tester

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        qa = next(st for st in t.steps if st.position == 1)
        tester = next(st for st in t.steps if st.position == 3)
        assert tester.status == "pending"   # a fase pedida foi reaberta
        assert tester.attempt == 2
        assert qa.status == "pending"       # cadeia anterior também (claim exige done)
        assert t.status == "in_progress"


def test_pm_retry_reabre_cadeia_com_fase_anterior_failed(flow, fake_kimi):
    """PM retry de uma fase com fase anterior failed reabre a CADEIA inteira —
    reabrir só o alvo deixava a task travada, pois o claim exige todas as fases
    anteriores `done` (task-194)."""
    flow["settings"].task_budget = 100.0
    _set_state(flow, status="failed")
    _set_step(flow, 0, "done")
    _set_step(flow, 1, "failed", attempt=2, error="veredicto NEEDS_WORK")
    _set_step(flow, 2, "pending", attempt=2)
    _set_step(flow, 3, "created", attempt=1)

    _pm(flow, fake_kimi, "pm_retry")  # DECISÃO: retry 3

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "in_progress"
        qa = next(st for st in t.steps if st.position == 1)
        dev = next(st for st in t.steps if st.position == 2)
        tester = next(st for st in t.steps if st.position == 3)
        # a cadeia não-concluída até o alvo volta a pending com orçamento novo
        assert qa.status == "pending" and qa.attempt == 3
        assert dev.status == "pending" and dev.attempt == 3
        assert tester.status == "pending" and tester.attempt == 2
        assert t.current_step == 1


def test_pm_retry_reabre_fase_done_no_teto(flow, fake_kimi):
    """PM retry do PO (`done`, 3/3) quando o QA reprovou a história: reabre a

    partir do alvo — PO + fases seguintes — com orçamento novo. Antes a cadeia
    só considerava `status != done`, vinha vazia e a task morria em
    "PM: retry inválido/limitado" (task-350).
    """
    flow["settings"].task_budget = 100.0
    _set_state(flow, status="failed")
    _set_step(flow, 0, "done", attempt=3)
    _set_step(flow, 1, "failed", attempt=1, error="veredicto NEEDS_WORK")
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        qa = next(st for st in t.steps if st.position == 1)
        qa.summary = "NEEDS_WORK\nSUMMARY: falta refetch da família após o aceite"
        qa.verdict = "NEEDS_WORK"
        s.commit()

    _pm(flow, fake_kimi, "pm_retry_po")  # DECISÃO: retry 0

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        po = next(st for st in t.steps if st.position == 0)
        qa = next(st for st in t.steps if st.position == 1)
        assert t.status == "in_progress"
        assert t.error is None
        assert po.status == "pending" and po.attempt == 1  # novo ciclo
        assert qa.status == "pending"
        # O relatório do QA permanece: é o motivo da correção do PO.
        assert qa.summary.startswith("NEEDS_WORK")
        assert qa.verdict == "NEEDS_WORK"
        assert t.current_step == 0


def test_pm_context_mostra_execucoes_reais(flow):
    """O PM precisa ver o número REAL de execuções (run), não só o `attempt`:
    o QA re-executado após cada correção do PO fica com attempt 1."""
    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        qa = next(st for st in t.steps if st.position == 1)
        s.add(RunEvent(step_id=qa.id, seq=1, kind="attempt_started", payload={}))
        s.add(RunEvent(step_id=qa.id, seq=2, kind="attempt_started", payload={}))
        s.commit()
        context = runner._pm_context(s, t)
        assert "execuções reais: 2" in context


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


def test_pm_handoff_with_subtasks_does_not_crash(flow, fake_kimi, tmp_path):
    """Regressão: o PM gerava o handoff com um step "fantasma" sem `post_merge`,
    e _build_handoff quebrava (AttributeError) quando a task tinha subtarefas
    ("erro interno do worker")."""
    import os

    from app.models import SUB_PENDING, SubTask
    from app.worker.runner import _task_workspace

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        checkout = _task_workspace(flow["settings"], t.repository.id, t.id)
        os.makedirs(os.path.join(checkout, ".git"), exist_ok=True)
        s.add(SubTask(task_id=t.id, position=0, title="Sub 1", status=SUB_PENDING))
        t.status = "failed"
        s.commit()

    flow["settings"].max_pm_decisions = 2
    flow["settings"].kimi_bin = fake_kimi(HARMLESS, verdict="pm_escalate")
    # não deve lançar AttributeError
    runner._pm_decide(flow["session_factory"], flow["settings"], flow["task"]["id"], "test")

    with flow["session_factory"]() as s:
        t = s.get(Task, flow["task"]["id"])
        assert t.status == "needs_review"
        assert "escalou" in (t.error or "")
        assert any(e.kind == "pm_decision" for e in sorted(t.steps, key=lambda x: x.position)[-1].events)
