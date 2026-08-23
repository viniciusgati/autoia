"""Testes do executor cmd (cmd_exec.py) com binário fake.

O fake emite o mesmo formato NDJSON do `cmd -p --output-format json`:
frames `event` (tool_running) e um frame final `result` (success/error/max_turns)
com `sessionId`/`usage`/`finalText`.
"""

from __future__ import annotations

import json
import stat

from app.worker import cmd_exec


def _make_fake(tmp_path, lines: list[dict], sleep: float = 0.0) -> str:
    """Cria binário fake do cmd: imprime `lines` como NDJSON e grava argv."""
    counter = len(list(tmp_path.glob("fake_cmd_*")))
    script = tmp_path / f"fake_cmd_{counter}"
    body = ",\n".join(json.dumps(line) for line in lines)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, time\n"
        "with open('argv.txt', 'w') as f:\n"
        "    f.write(' '.join(sys.argv))\n"
        "for line in [\n" + body + "\n]:\n"
        "    print(json.dumps(line))\n"
        "    sys.stdout.flush()\n"
        f"if {sleep}:\n    time.sleep({sleep})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _tool_running(tool: str, description: str = "executa algo"):
    return {
        "type": "event",
        "event": {"type": "tool_running", "toolName": tool, "description": description},
    }


def _result(
    subtype: str = "success",
    final_text: str = "pronto",
    usage: dict | None = None,
    session_id: str = "ses_cmd_1",
    stop_reason: str = "end_turn",
):
    return {
        "type": "result",
        "subtype": subtype,
        "sessionId": session_id,
        "stopReason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        "finalText": final_text,
    }


def _run(tmp_path, lines, timeout: float = 30, max_identical_calls: int = 3, sleep: float = 0.0, **kwargs):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    log_path = str(tmp_path / "run.log")
    events: list[tuple[str, dict, float]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    outcome = cmd_exec.run_cmd(
        "prompt-x",
        cwd=str(cwd),
        cmd_bin=_make_fake(tmp_path, lines, sleep=sleep),
        log_path=log_path,
        timeout=timeout,
        max_identical_calls=max_identical_calls,
        risky_patterns=[],
        checkout_path=str(cwd),
        cost_per_interaction=0.01,
        on_event=on_event,
        **kwargs,
    )
    return outcome, events, cwd


def test_streams_events_and_estimated_cost(tmp_path):
    lines = [
        _tool_running("bash", "roda ls"),
        {"type": "event", "event": {"type": "progress", "message": "pensando"}},
        _result(final_text="tarefa concluída"),
    ]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.exit_code == 0
    assert not outcome.aborted
    assert outcome.final_text == "tarefa concluída"
    assert outcome.session_id == "ses_cmd_1"
    assert outcome.interaction_count == 1  # só a tool conta (progress é ruído)

    kinds = [k for k, _, _ in events]
    assert kinds == ["tool_call", "system"]
    # custo estimado por interação (como o kimi), não real
    total = sum(c for _, _, c in events)
    assert abs(total - 0.01) < 1e-9

    tc = next(p for k, p, _ in events if k == "tool_call")
    assert tc["tool"] == "bash"
    assert tc["description"] == "roda ls"


def test_builds_full_cmd_flags(tmp_path):
    lines = [_result(final_text="ok")]
    _, _, cwd = _run(tmp_path, lines, model="claude-opus-4-1")
    argv = (cwd / "argv.txt").read_text().split()

    assert "-p" in argv  # headless
    assert argv[0].endswith("fake_cmd_0")
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--no-session" in argv
    assert "--yolo" in argv
    assert "--skip-onboarding" in argv
    assert "--trust" in argv
    assert "-m" in argv and argv[argv.index("-m") + 1] == "claude-opus-4-1"


def test_repeated_tool_not_killed(tmp_path):
    """O watchdog de identical-calls NÃO se aplica ao cmd: sem os argumentos no
    stream, repetições de MESMA tool (comandos diferentes) não podem ser
    distinguidas de loop — 10 shell_command seguidos não abortam."""
    lines = [_tool_running("shell_command")] * 10 + [_result(final_text="ok")]
    outcome, events, _ = _run(tmp_path, lines, max_identical_calls=3)

    assert not outcome.aborted
    assert outcome.exit_code == 0
    kinds = [k for k, _, _ in events]
    assert "guardrail_blocked" not in kinds
    assert kinds.count("tool_call") == 10


def test_timeout(tmp_path):
    lines = [_tool_running("bash")]
    outcome, _, _ = _run(tmp_path, lines, timeout=1, sleep=5)

    assert outcome.aborted
    assert outcome.timed_out


def test_result_error_aborts(tmp_path):
    lines = [_result(subtype="error", final_text="")]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "cmd:" in outcome.abort_reason
    assert any(k == "system" and "error" in p for k, p, _ in events)


def test_result_max_turns_aborts(tmp_path):
    lines = [_result(subtype="max_turns", final_text="parcial")]
    outcome, _, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "max_turns" in outcome.abort_reason


def test_exit_code_mapping(tmp_path):
    """Exit codes do cmd viram abort com motivo mapeado (3 auth, 10 credits…)."""
    # fake que sai com código 3 (não autenticado) sem emitir frames
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    fake = tmp_path / "fake_cmd_auth"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def on_event(kind, payload, cost):
        return None

    outcome = cmd_exec.run_cmd(
        "prompt",
        cwd=str(cwd),
        cmd_bin=str(fake),
        log_path=str(tmp_path / "a.log"),
        timeout=30,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(cwd),
        cost_per_interaction=0.01,
        on_event=on_event,
    )
    assert outcome.aborted
    assert "não autenticado" in outcome.abort_reason


def test_always_no_session_never_resumes(tmp_path):
    """O cmd NÃO suporta retomada cross-checkout (sessão por projeto + $HOME do
    sandbox): cada execução roda com `--no-session` e NUNCA com `--resume`, mesmo
    com `resume_session_id` informado — o contexto vem do handoff."""
    lines = [_result(final_text="ok", session_id="ses_abc")]
    fake = _make_fake(tmp_path, lines)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)

    def on_event(kind, payload, cost):
        return None

    o1 = cmd_exec.run_cmd(
        "prompt1", cwd=str(cwd), cmd_bin=fake, log_path=str(tmp_path / "a.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, on_event=on_event,
    )
    assert o1.session_id == "ses_abc"  # só observabilidade
    argv = (cwd / "argv.txt").read_text().split()
    assert "--no-session" in argv
    assert "--resume" not in argv

    # mesmo com resume_session_id, NÃO há --resume (começa sessão nova)
    (cwd / "argv.txt").unlink()
    cmd_exec.run_cmd(
        "continuar", cwd=str(cwd), cmd_bin=fake, log_path=str(tmp_path / "b.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, resume_session_id="ses_abc", on_event=on_event,
    )
    argv = (cwd / "argv.txt").read_text().split()
    assert "--no-session" in argv
    assert "--resume" not in argv
