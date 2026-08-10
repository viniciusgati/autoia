"""Testes do bounce-back automático (falha volta para a fase anterior)."""

from __future__ import annotations

import os
import subprocess

from app.models import Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _state(flow, task_id) -> dict:
    """Snapshot serializado da task (sessão já fechada)."""
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        return {
            "status": t.status,
            "current_step": t.current_step,
            "steps": [
                {
                    "position": st.position,
                    "status": st.status,
                    "attempt": st.attempt,
                    "error": st.error,
                }
                for st in sorted(t.steps, key=lambda x: x.position)
            ],
        }


def _step(state: dict, position: int) -> dict:
    return next(st for st in state["steps"] if st["position"] == position)


def test_tester_fail_bounces_to_developer(flow, fake_kimi):
    """developer (pos 2) conclui; tester (pos 3) FAIL -> developer volta a pending."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    # po (0) e qa (1): po sem veredicto obrigatório; qa dá READY
    for _ in range(2):
        _execute(flow, _run_claim(flow))

    # developer (2) conclui
    _execute(flow, _run_claim(flow))

    # troca o fake para o tester FALHAR
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    _execute(flow, _run_claim(flow))

    state = _state(flow, task["id"])
    assert state["status"] == "in_progress"  # bounce-back, não failed
    dev = _step(state, 2)
    tester = _step(state, 3)
    assert dev["status"] == "pending"
    assert dev["attempt"] == 2
    assert tester["status"] == "failed"
    assert "FAIL" in (tester["error"] or "")
    assert state["current_step"] == 2


def test_qa_needs_work_bounces_to_po(flow, fake_kimi):
    """QA (pos 1) NEEDS_WORK -> PO (pos 0) volta a pending."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="needs_work")
    settings.task_budget = 100.0
    task = flow["task"]

    _execute(flow, _run_claim(flow))  # po conclui
    _execute(flow, _run_claim(flow))  # qa NEEDS_WORK

    state = _state(flow, task["id"])
    assert state["status"] == "in_progress"
    po = _step(state, 0)
    assert po["status"] == "pending"
    assert po["attempt"] == 2


def test_needs_work_content_reaches_bounce_back(flow, fake_kimi):
    """O conteúdo do NEEDS_WORK (correção pedida) é preservado para quem vai corrigir."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="needs_work")
    settings.task_budget = 100.0
    task = flow["task"]

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = os.path.join(settings.workspace_dir, str(t.repository.id), f"task_{t.id}")

    _execute(flow, _run_claim(flow))  # po conclui
    _execute(flow, _run_claim(flow))  # qa NEEDS_WORK

    # o veredicto (com o SUMMARY) ficou gravado na fase que falhou
    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        qa = next(st for st in t.steps if st.position == 1)
        assert qa.verdict == "NEEDS_WORK"
        assert "historia ambigua" in (qa.summary or "")

    # o handoff da fase refeita (po, bounce-back) entrega a correção pedida
    _execute(flow, _run_claim(flow))  # po re-executa com o handoff da falha
    md = open(os.path.join(checkout, "autoia_handoff.md"), encoding="utf-8").read()
    assert "FALHOU" in md
    assert "historia ambigua" in md


def test_first_phase_failure_marks_task_failed(flow, tmp_path):
    """Fase inicial (po, pos 0) falha sem anterior -> task failed direto."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    script = tmp_path / "failing_kimi"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    import stat as stat_mod

    script.chmod(script.stat().st_mode | stat_mod.S_IEXEC)
    settings.kimi_bin = str(script)
    task = flow["task"]

    _execute(flow, _run_claim(flow))

    state = _state(flow, task["id"])
    assert state["status"] == "failed"
    assert _step(state, 0)["status"] == "failed"


def test_recover_stale_steps(flow):
    """Step running órfão (worker morto no meio) volta a pending no startup."""
    settings = flow["settings"]
    task = flow["task"]

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        st = sorted(t.steps, key=lambda x: x.position)[0]
        st.status = "running"
        s.commit()

    recovered = runner.recover_stale_steps(flow["session_factory"])
    assert recovered == 1

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        st = sorted(t.steps, key=lambda x: x.position)[0]
        assert st.status == "pending"
        assert st.started_at is None
        assert any(e.kind == "worker_recovered" for e in st.events)


def test_max_attempts_bounds_bounce_back(flow, fake_kimi):
    """Tester falha repetidamente; bounce-back limitado por max_attempts (3)."""
    settings = flow["settings"]
    settings.max_attempts = 3
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    # po, qa, developer
    for _ in range(3):
        _execute(flow, _run_claim(flow))

    # tester falha 3x, cada vez devolve para o developer
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    _execute(flow, _run_claim(flow))  # tester fail 1 -> dev attempt 2
    _execute(flow, _run_claim(flow))  # dev 2 conclui
    _execute(flow, _run_claim(flow))  # tester fail 2 -> dev attempt 3
    _execute(flow, _run_claim(flow))  # dev 3 conclui
    _execute(flow, _run_claim(flow))  # tester fail 3 -> dev 3<3 false -> task failed

    state = _state(flow, task["id"])
    assert state["status"] == "failed"
    dev = _step(state, 2)
    assert dev["attempt"] == 3
    assert _step(state, 3)["status"] == "failed"


def test_agents_md_written_and_never_committed(flow, fake_kimi):
    """AGENTS.md é gerado no checkout em cada fase (inclui pós-merge) e nunca é versionado."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = os.path.join(settings.workspace_dir, str(t.repository.id), f"task_{t.id}")

    # roda o fluxo completo (todas as fases + merge + pós-merge)
    while True:
        step_id = _run_claim(flow)
        if step_id is None:
            break
        _execute(flow, step_id)

    assert os.path.isfile(os.path.join(checkout, "AGENTS.md"))
    # nunca commitado — nem na branch da task, nem na default, nem no merge
    log = subprocess.run(
        ["git", "log", "--all", "--oneline", "--", "AGENTS.md"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert log.strip() == ""
    state = _state(flow, task["id"])
    assert state["status"] == "done"


def test_avaliador_fail_bounces_to_tester(flow, fake_kimi):
    """Avaliador (pos 4, pré-merge) FAIL -> tester (pos 3) volta a pending (bounce-back)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    # po, qa, developer, tester passam
    for _ in range(4):
        _execute(flow, _run_claim(flow))

    # avaliador FALHA -> bounce para a fase anterior (tester)
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    _execute(flow, _run_claim(flow))

    state = _state(flow, task["id"])
    assert state["status"] == "in_progress"  # bounce-back, não failed
    tester = _step(state, 3)
    avaliador = _step(state, 4)
    assert tester["status"] == "pending"
    assert tester["attempt"] == 2
    assert avaliador["status"] == "failed"
    assert "FAIL" in (avaliador["error"] or "")
    assert state["current_step"] == 3
