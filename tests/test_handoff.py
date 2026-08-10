"""Testes do documento de handoff entre fases (autoia_handoff.md)."""

from __future__ import annotations

import os
import subprocess

from app.models import Task
from app.worker import gitops, handoff, runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


# ---- build_handoff (estrutura do documento) ----

def test_build_handoff_structure():
    md = handoff.build_handoff(
        task_id=1,
        task_title="minha tarefa",
        task_status="in_progress",
        branch="autoia/task-1",
        phase_sections=["### Fase 0 — po (refine) — done\nhistória final"],
        diff=" README.md | 2 +-",
        current="**Fase 1 — qa (review)**",
    )
    assert "# Autoia" in md
    assert "minha tarefa" in md
    assert "autoia/task-1" in md
    assert "### Fase 0 — po (refine) — done" in md
    assert "história final" in md
    assert "README.md" in md
    assert "**Fase 1 — qa (review)**" in md
    assert "Não edite este arquivo" in md


def test_build_handoff_empty_history():
    md = handoff.build_handoff(
        task_id=1,
        task_title="t",
        task_status="pending",
        branch="b",
        phase_sections=[],
        diff="",
        current="Fase 0 — po (refine)",
    )
    assert "você é a primeira" in md
    assert "Diff atual" not in md


# ---- write_handoff (arquivo não versionado) ----

def test_write_handoff_untracked(bare_repo, tmp_path):
    dest = str(tmp_path / "clone")
    gitops.clone(bare_repo, dest)

    handoff.write_handoff(dest, "# handoff\n")

    assert os.path.isfile(os.path.join(dest, "autoia_handoff.md"))
    # ausente do índice e invisível no status, mesmo após add -A
    assert (
        subprocess.run(
            ["git", "ls-files", "autoia_handoff.md"], cwd=dest, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )
    subprocess.run(["git", "add", "-A"], cwd=dest, capture_output=True, text=True)
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True
        ).stdout.strip()
        == ""
    )


# ---- fluxo (runner gera o handoff e nunca o versiona) ----

def test_handoff_written_and_never_committed(flow, fake_kimi):
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = os.path.join(settings.workspace_dir, str(t.repository.id), f"task_{t.id}")

    while True:
        step_id = _run_claim(flow)
        if step_id is None:
            break
        _execute(flow, step_id)

    assert os.path.isfile(os.path.join(checkout, "autoia_handoff.md"))
    log = subprocess.run(
        ["git", "log", "--all", "--oneline", "--", "autoia_handoff.md"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert log.strip() == ""


def test_handoff_phase_two_receives_phase_one_summary(flow, fake_kimi):
    """A fase seguinte encontra o handoff com a documentação COMPLETA da fase anterior."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(
        [
            {
                "role": "assistant",
                "content": (
                    "### O que foi feito\nimplementei a multiplicação\n"
                    "### Arquivos alterados\n- src/calc.py — nova função\n"
                    "### Evidência\npytest: 2 passed\n"
                ),
            }
        ],
        verdict="ready_pass",
    )
    settings.task_budget = 100.0
    task = flow["task"]

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = os.path.join(settings.workspace_dir, str(t.repository.id), f"task_{t.id}")

    _execute(flow, _run_claim(flow))  # fase 0 (po) conclui
    _execute(flow, _run_claim(flow))  # fase 1 (qa) roda com o handoff da fase 0

    md = (open(os.path.join(checkout, "autoia_handoff.md"), encoding="utf-8")).read()
    assert "### Fase 0" in md
    assert "implementei a multiplicação" in md
    assert "src/calc.py" in md

    # o summary no banco está INTEGRAL (fonte de verdade do histórico)
    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        st0 = next(st for st in t.steps if st.position == 0)
        assert "implementei a multiplicação" in (st0.summary or "")


def test_summary_not_truncated(flow, fake_kimi):
    """Texto final longo é persistido COMPLETO em step.summary (requisito: nunca truncar)."""
    settings = flow["settings"]
    long_text = "X" * 3000
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": long_text}], verdict="ready_pass"
    )
    settings.task_budget = 100.0
    task = flow["task"]

    _execute(flow, _run_claim(flow))  # fase 0 (po)

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        st0 = next(st for st in t.steps if st.position == 0)
        assert len(st0.summary or "") > 2000
        assert st0.summary == long_text
