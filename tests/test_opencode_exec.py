"""Testes do executor opencode (opencode_exec.py) com binário fake.

O fake emite o mesmo formato JSONL do `opencode run --format json`:
step_start / tool_use / text / step_finish (com cost real) / error.
"""

from __future__ import annotations

import json
import stat

import pytest

from app.worker import opencode_exec


def _make_fake(tmp_path, lines: list[dict], sleep: float = 0.0) -> str:
    """Cria binário fake do opencode: imprime `lines` como JSONL e grava argv."""
    counter = len(list(tmp_path.glob("fake_opencode_*")))
    script = tmp_path / f"fake_opencode_{counter}"
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


def _tool(tool: str, input_: dict, output: str = "ok", status: str = "completed"):
    return {
        "type": "tool_use",
        "part": {"type": "tool", "tool": tool, "state": {"status": status, "input": input_, "output": output}},
    }


def _text(text: str):
    return {"type": "text", "part": {"type": "text", "text": text}}


def _finish(cost: float, reason: str = "stop"):
    return {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "reason": reason,
            "cost": cost,
            "tokens": {"input": 10, "output": 5},
        },
    }


def _run(tmp_path, lines, timeout: float = 30, max_identical_calls: int = 3, risky_patterns: list[str] | None = None, sleep: float = 0.0, **kwargs):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    log_path = str(tmp_path / "run.log")
    events: list[tuple[str, dict, float]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    outcome = opencode_exec.run_opencode(
        "prompt-x",
        cwd=str(cwd),
        opencode_bin=_make_fake(tmp_path, lines, sleep=sleep),
        log_path=log_path,
        timeout=timeout,
        max_identical_calls=max_identical_calls,
        risky_patterns=risky_patterns or [],
        checkout_path=str(cwd),
        on_event=on_event,
        **kwargs,
    )
    return outcome, events, cwd


def test_streams_events_and_real_cost(tmp_path):
    lines = [
        {"type": "step_start", "part": {"type": "step-start"}},
        _tool("bash", {"command": "ls"}),
        _text("tudo ok"),
        _finish(0.0042),
        _text("final"),
        _finish(0.0058),
    ]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.exit_code == 0
    assert not outcome.aborted
    assert outcome.final_text == "final"
    assert outcome.interaction_count == 3  # tool + 2 textos

    kinds = [k for k, _, _ in events]
    assert kinds == ["tool_call", "tool_result", "assistant_text", "system", "assistant_text", "system"]
    # custo real acumulado (step_finish), não estimativa por interação
    total = sum(c for _, _, c in events)
    assert abs(total - 0.01) < 1e-9

    tc = next(p for k, p, _ in events if k == "tool_call")
    assert tc["tool"] == "bash"
    assert tc["input"] == {"command": "ls"}
    tr = next(p for k, p, _ in events if k == "tool_result")
    assert tr["output"] == "ok"


def test_guardrail_kills_on_risky_command(tmp_path):
    lines = [_tool("bash", {"command": "rm -rf /"})]
    outcome, events, _ = _run(
        tmp_path, lines, risky_patterns=[r"\brm\s+-rf\b"]
    )

    assert outcome.aborted
    assert outcome.abort_reason.startswith("guardrail")
    kinds = [k for k, _, _ in events]
    assert kinds[-1] == "guardrail_blocked"
    payload = events[-1][1]
    assert payload["pattern"] == r"\brm\s+-rf\b"


def test_guardrail_path_outside_checkout(tmp_path):
    lines = [_tool("read", {"filePath": "/etc/passwd"})]
    outcome, _, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert outcome.abort_reason.startswith("guardrail")


def test_identical_calls_kill(tmp_path):
    lines = [_tool("bash", {"command": "ls"})] * 3
    outcome, _, _ = _run(tmp_path, lines, max_identical_calls=3)

    assert outcome.aborted
    assert "identical" in outcome.abort_reason


def test_timeout(tmp_path):
    lines = [_text("comecou")]
    outcome, _, _ = _run(tmp_path, lines, timeout=1, sleep=5)

    assert outcome.aborted
    assert outcome.timed_out


def test_error_event_aborts(tmp_path):
    lines = [{"type": "error", "part": {"type": "error", "message": "falha no provedor"}}]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "falha no provedor" in outcome.abort_reason
    assert any(k == "system" and "error" in p for k, p, _ in events)


def test_model_passed_as_flag(tmp_path):
    lines = [_text("ok"), _finish(0.001)]
    _, _, cwd = _run(tmp_path, lines, model="provider/modelo-x")
    argv = (cwd / "argv.txt").read_text()
    assert "-m" in argv
    assert "provider/modelo-x" in argv
