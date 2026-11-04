"""Regressões da task-264: execução superseded não derruba a execução nova.

A mesma corrida tinha duas pontas:

* `_fail_step_hard` (rede de proteção do `finally` do worker) agora é amarrado ao
  TOKEN do claim (`started_at`): o `finally` de uma execução antiga — presa num PM
  lento por limite do provedor — não derruba a execução NOVA reclamada depois de um
  "tenta novamente";
* a decisão AUTOMÁTICA do PM é descartada se o usuário interveio durante a decisão
  (senão "escalar" travava a task em `needs_review` por cima da execução nova que
  estava trabalhando).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models import Task, TaskStep
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "decisão emitida"},
]


def test_fail_step_hard_nao_derruba_execucao_reclamada_de_novo(flow):
    """O token antigo não fecha a fase que outra execução já reclamou; o token
    atual fecha normalmente."""
    sf = flow["session_factory"]
    first = runner.claim_next_with_token(sf)
    assert first is not None
    step_id, old_token = first

    # Usuário "tenta novamente": a fase volta a `pending`; outra execução reclama.
    with sf() as s:
        step = s.get(TaskStep, step_id)
        step.status = "pending"
        step.started_at = None
        s.commit()
    second = runner.claim_next_with_token(sf)
    assert second is not None
    assert second[0] == step_id
    assert second[1] != old_token

    # O `finally` da execução ANTIGA não pode derrubar a execução NOVA.
    runner._fail_step_hard(
        sf, step_id, reason="erro interno do worker", expected_started_at=old_token
    )
    with sf() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == "running"
        assert step.error is None
        assert not any(e.kind == "step_forced_failed" for e in step.events)

    # A própria execução (token atual) continua conseguindo fechar a fase.
    runner._fail_step_hard(sf, step_id, reason="boom", expected_started_at=second[1])
    with sf() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == "failed"
        assert step.error == "boom"


def test_worker_loop_finally_nao_derruba_execucao_nova(flow, monkeypatch, tmp_path):
    """Cenário exato da task-264: a execução antiga estoura DEPOIS de a fase já ter
    sido reclamada de novo — o `finally` do loop não pode marcá-la failed."""
    settings = flow["settings"]
    settings.peak_hours_windows = ""  # janela de pico desligada no teste
    session_factory = flow["session_factory"]
    task_id = flow["task"]["id"]

    calls = {"claim": 0}
    state: dict = {}

    def fake_claim(_sf):
        calls["claim"] += 1
        if calls["claim"] > 1:
            raise SystemExit  # sai do loop sem ser engolida
        with session_factory() as s:
            step = (
                s.query(TaskStep)
                .filter(TaskStep.task_id == task_id)
                .order_by(TaskStep.position)
                .first()
            )
            state["step_id"] = step.id
            state["old_token"] = datetime(2026, 1, 1, 12, 0, 0)
            step.status = "running"
            step.started_at = state["old_token"]
            s.commit()
        return state["step_id"], state["old_token"]

    def executor_superado(*_args, **_kwargs):
        # Simula o "tenta novamente" + outro worker reclamando a fase.
        with session_factory() as s:
            step = s.get(TaskStep, state["step_id"])
            step.status = "running"
            step.started_at = datetime(2026, 1, 1, 12, 5, 0)
            s.commit()
        raise RuntimeError("execução antiga morreu depois de ser superada")

    monkeypatch.setattr(runner, "claim_next_with_token", fake_claim)
    monkeypatch.setattr(runner, "execute_step", executor_superado)
    monkeypatch.setattr(runner, "_maybe_auto_summary", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_step_summary", lambda *a, **k: None)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SystemExit):
        runner.worker_loop(settings, session_factory, str(workspace))

    with session_factory() as s:
        step = s.get(TaskStep, state["step_id"])
        assert step.status == "running"  # a execução NOVA segue viva
        assert step.error is None
        assert not any(e.kind == "step_forced_failed" for e in step.events)


def test_pm_automatico_descarta_decisao_apos_intervencao(flow, fake_kimi, monkeypatch):
    """PM automático não aplica decisão se o usuário interveio durante a decisão."""
    sf = flow["session_factory"]
    task_id = flow["task"]["id"]
    settings = flow["settings"]
    settings.max_pm_decisions = 2
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="pm_retry")

    with sf() as s:
        task = s.get(Task, task_id)
        task.status = "failed"
        task.error = "tests falharam"
        for st in task.steps:
            if st.position == 3:
                st.status = "failed"
                st.error = "tests falharam"
        s.commit()

    original_run = runner._run_executor

    def run_e_intervem(*args, **kwargs):
        # Intervenção humana DURANTE o PM (como o "tenta novamente" da UI).
        with sf() as s:
            step = (
                s.query(TaskStep)
                .filter(TaskStep.task_id == task_id)
                .order_by(TaskStep.position)
                .first()
            )
            runner._system_event(
                s, step, "user_intervention", {"instruction": "tenta novamente"}
            )
            runner._system_event(
                s, step, "execution_resumed", {"step": 0, "instruction": "tenta novamente"}
            )
            s.commit()
        return original_run(*args, **kwargs)

    monkeypatch.setattr(runner, "_run_executor", run_e_intervem)
    runner._maybe_pm(sf, settings, task_id, "falha antiga")

    with sf() as s:
        task = s.get(Task, task_id)
        # Decisão NÃO aplicada: task e fase continuam como estavam.
        assert task.status == "failed"
        assert task.error == "tests falharam"
        tester = next(st for st in task.steps if st.position == 3)
        assert tester.status == "failed"
        anchor = sorted(task.steps, key=lambda x: x.position)[-1]
        kinds = [e.kind for e in anchor.events]
        assert "pm_decision_discarded" in kinds
        assert "pm_decision" not in kinds
