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


def _plan_text(titles: list[str]) -> str:
    """Texto final do PO com um plano de implementação (para gerar subtarefas)."""
    parts = ["## Descrição\ndescrição", "## Critérios de aceite\n- [ ] ok"]
    block = ["## Plano de implementação"]
    for i, title in enumerate(titles, start=1):
        block.append(
            f"### Subtarefa {i}: {title}\n"
            f"**Escopo:** escopo de {title}\n"
            f"**Critérios:**\n- [ ] ok {title}"
        )
    return "\n\n".join(parts + ["\n".join(block)])


def test_po_rewrite_replaces_pending_subtasks(flow, fake_kimi):
    """Bounce-back QA→PO: o PO REESCREVE o plano de implementação e, se nenhuma
    subtarefa foi trabalhada, o plano antigo é substituído (sem isso, QA/verify/
    assess leriam subtarefas obsoletas e reprovavam em loop)."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task = flow["task"]

    # po (0) entrega plano com 2 subtarefas
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": _plan_text(["A", "B"])}],
        verdict="ready_pass",
    )
    _execute(flow, _run_claim(flow))  # po conclui

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        assert [st.title for st in sorted(t.subtasks, key=lambda x: x.position)] == ["A", "B"]
        assert all(st.status == "pending" for st in t.subtasks)

    # qa (1) NEEDS_WORK -> bounce para o po
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="needs_work")
    _execute(flow, _run_claim(flow))

    # po (0) re-executa com UM plano novo (2 -> 3 subtarefas diferentes)
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": _plan_text(["X", "Y", "Z"])}],
        verdict="ready_pass",
    )
    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        titles = [st.title for st in sorted(t.subtasks, key=lambda x: x.position)]
        assert titles == ["X", "Y", "Z"], titles
        assert all(st.status == "pending" for st in t.subtasks)


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


def test_bounceback_manual_ignora_limite(flow, settings):
    """Bounceback via API é ação MANUAL: liberado mesmo com o step no teto de
    `max_attempts` (limite só vale para o bounce-back automático do worker)."""
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        t.status = "needs_review"
        t.error = "deploy falhou"
        dev = next(st for st in t.steps if st.position == 2)
        dev.status = "done"
        dev.attempt = settings.max_attempts  # já no teto automático
        tester = next(st for st in t.steps if st.position == 3)
        tester.status = "failed"
        tester.error = "teste não passou"
        s.commit()

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/bounceback",
        json={"target_position": 2, "reviewed_by": "humano"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    dev = next(st for st in data["steps"] if st["position"] == 2)
    assert dev["status"] == "pending"
    assert dev["attempt"] == settings.max_attempts + 1


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


def test_avaliador_fail_bounces_to_developer(flow, fake_kimi):
    """Avaliador (pos 4, pré-merge) FAIL -> volta à fase `implement` (developer,
    pos 2), que é quem corrige código — não ao tester (re-rodar o tester sobre
    código inalterado só reproduziria PASS e looping até esgotar tentativas)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    # po, qa, developer, tester passam
    for _ in range(4):
        _execute(flow, _run_claim(flow))

    # avaliador FALHA -> bounce para a fase `implement` (developer)
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    _execute(flow, _run_claim(flow))

    state = _state(flow, task["id"])
    assert state["status"] == "in_progress"  # bounce-back, não failed
    dev = _step(state, 2)
    tester = _step(state, 3)
    avaliador = _step(state, 4)
    assert dev["status"] == "pending"
    assert dev["attempt"] == 2
    assert tester["status"] == "done"  # não foi reaberto por engano
    assert avaliador["status"] == "failed"
    assert "FAIL" in (avaliador["error"] or "")
    assert state["current_step"] == 2
