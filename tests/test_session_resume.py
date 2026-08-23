"""Retomada de sessão (kimi -S): fases que CONCLUÍRAM (phase_done ou veredicto)
nunca retomam a conversa antiga — regressão da task-92, em que o avaliador
re-publicava o veredicto FAIL antigo sem reavaliar (stale verdict)."""

from __future__ import annotations

import subprocess

from app.models import RunEvent, TaskStep
from app.worker import runner
from app.worker import sandbox as sb


def _eff(tmp_path) -> runner.EffectiveSettings:
    return runner.EffectiveSettings(
        max_attempts=3, max_pm_decisions=0, run_timeout=30, task_budget=10.0,
        cost_per_interaction=0.01, pm_budget_topup=0, risky_patterns=[],
        whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="opencode",
        opencode_model="x", log_dir=str(tmp_path / "logs"),
        workspace_dir=str(tmp_path / "ws"), branch_prefix="autoia",
        max_identical_calls=3, no_progress_timeout=0,
        sandbox=sb.SandboxConfig(mode="off"),
    )


def _events(s, step: TaskStep, kinds: list[str]) -> None:
    for i, kind in enumerate(kinds, start=1):
        s.add(RunEvent(step_id=step.id, seq=i, kind=kind, payload={}))


def _add_session(flow, position: int) -> int:
    sf = flow["session_factory"]
    with sf() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.position == position)
            .one()
        )
        step.session_id = "sess_antiga"
        s.commit()
        return step.id


def test_should_resume_verdict_concluiu_retorna_none(flow):
    """Veredicto (inclusive FAIL) = fase concluída: re-execução recomeça do zero."""
    step_id = _add_session(flow, 4)
    sf = flow["session_factory"]
    with sf() as s:
        step = s.get(TaskStep, step_id)
        _events(s, step, ["attempt_started", "verdict"])
        s.commit()
    with sf() as s:
        step = s.get(TaskStep, step_id)
        assert runner._should_resume(s, step) is None


def test_should_resume_interrompida_retorna_sessao(flow):
    """Interrupção sem conclusão (timeout/stall) retoma a MESMA sessão."""
    step_id = _add_session(flow, 4)
    sf = flow["session_factory"]
    with sf() as s:
        step = s.get(TaskStep, step_id)
        _events(s, step, ["attempt_started"])
        s.commit()
    with sf() as s:
        step = s.get(TaskStep, step_id)
        assert runner._should_resume(s, step) == "sess_antiga"


def test_should_resume_phase_done_retorna_none(flow):
    step_id = _add_session(flow, 4)
    sf = flow["session_factory"]
    with sf() as s:
        step = s.get(TaskStep, step_id)
        _events(s, step, ["attempt_started", "phase_done"])
        s.commit()
    with sf() as s:
        step = s.get(TaskStep, step_id)
        assert runner._should_resume(s, step) is None


def test_handle_failure_verdict_limpa_session(flow, tmp_path):
    """Falha por veredicto descarta o session_id: a próxima execução começa do zero."""
    step_id = _add_session(flow, 0)
    sf = flow["session_factory"]
    with sf() as s:
        step = s.get(TaskStep, step_id)
        task = step.task
        runner._handle_failure(
            _eff(tmp_path), s, step, task,
            "veredicto FAIL (esperado PASS)", "verdict", "failed",
        )
        assert step.session_id is None
        s.commit()
    with sf() as s:
        assert s.get(TaskStep, step_id).session_id is None


def _clone_checkout(bare_repo, tmp_path) -> str:
    dest = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(bare_repo), str(dest)], check=True)
    return str(dest)


def _head_short(checkout: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=checkout,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_consume_verdict_hash_divergente_emite_aviso(flow, bare_repo, tmp_path):
    """Veredicto citando um commit diferente do HEAD atual gera `stale_verdict_warning`."""
    checkout = _clone_checkout(bare_repo, tmp_path)
    with open(f"{checkout}/autoia_verdict.txt", "w", encoding="utf-8") as fh:
        fh.write("PASS\nSUMMARY: avaliado o diff.\nHEAD: 0000000\n")
    sf = flow["session_factory"]
    with sf() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.position == 4)
            .one()
        )
        label = runner._consume_verdict(s, step, checkout)
        assert label == "PASS"
        s.commit()
        kinds = [
            ev.kind
            for ev in s.query(RunEvent)
            .filter(RunEvent.step_id == step.id, RunEvent.kind == "stale_verdict_warning")
            .all()
        ]
        assert kinds


def test_consume_verdict_hash_atual_sem_aviso(flow, bare_repo, tmp_path):
    checkout = _clone_checkout(bare_repo, tmp_path)
    head = _head_short(checkout)
    with open(f"{checkout}/autoia_verdict.txt", "w", encoding="utf-8") as fh:
        fh.write(f"PASS\nSUMMARY: avaliado o diff.\nHEAD: {head}\n")
    sf = flow["session_factory"]
    with sf() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.position == 4)
            .one()
        )
        assert runner._consume_verdict(s, step, checkout) == "PASS"
        s.commit()
        warnings = (
            s.query(RunEvent)
            .filter(RunEvent.step_id == step.id, RunEvent.kind == "stale_verdict_warning")
            .count()
        )
        assert warnings == 0
