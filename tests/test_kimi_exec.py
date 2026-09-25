"""Testes do runner do kimi: parser do stream-json, guardrails, timeout.

Usa um binário kimi FAKE (script Python) que emite sequências JSONL determinísticas,
para não depender do kimi real nem da rede.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from app.worker import kimi_exec


def _make_fake_kimi(tmp_path, lines) -> str:
    """Cria um script executável que imprime `lines` (lista de dicts) e sai."""
    script = tmp_path / "fake_kimi"
    body = ",\n".join(json.dumps(line) for line in lines)
    script.write_text(
        f"#!/usr/bin/env python3\n"
        f"import sys, json, time\n"
        f"for line in [\n{body}\n]:\n"
        f"    print(json.dumps(line))\n"
        f"    sys.stdout.flush()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _collector():
    events: list[tuple] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    return events, on_event


def _run(tmp_path, lines, *, timeout=30, max_identical_calls=3, patterns=None, on_event=None, cost=0.01):
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    fake = _make_fake_kimi(tmp_path, lines)
    return kimi_exec.run_kimi(
        "prompt",
        cwd=str(checkout),
        kimi_bin=fake,
        log_path=str(tmp_path / "run.log"),
        timeout=timeout,
        max_identical_calls=max_identical_calls,
        risky_patterns=patterns if patterns is not None else [
            r"\brm\s+-rf\b",
            r"\bcurl\b",
            r"git\s+push\b",
        ],
        checkout_path=str(checkout),
        cost_per_interaction=cost,
        on_event=on_event,
    )


def test_normal_run_streams_events(tmp_path):
    lines = [
        {"role": "meta", "type": "system.version", "version": "0.34.0"},
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "README.md"},
        {"role": "assistant", "content": "Listei. Tarefa concluída."},
    ]
    events, on_event = _collector()
    outcome = _run(tmp_path, lines, on_event=on_event)

    assert outcome.exit_code == 0
    assert "Tarefa concluída" in outcome.final_text
    assert outcome.interaction_count == 2
    kinds = [e[0] for e in events]
    assert kinds == ["tool_call", "tool_result", "assistant_text"]
    # custo cobrado na primeira interação (tool_call) e na resposta
    assert events[0][2] == 0.01
    assert events[2][2] == 0.01
    assert events[1][2] == 0.0


def test_risky_command_not_blocked_after_guardrail_removal(tmp_path):
    """Guardrail de comandos removido: comando 'arriscado' NÃO aborta mais (a
    detecção pós-emissão não impedia o comando e gerava falsos positivos)."""
    lines = [
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"rm -rf /"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "feito"},
    ]
    events, on_event = _collector()
    outcome = _run(tmp_path, lines, on_event=on_event)

    assert not outcome.aborted
    assert outcome.exit_code == 0
    assert "feito" in outcome.final_text
    kinds = [e[0] for e in events]
    assert "tool_result" in kinds
    assert "guardrail_blocked" not in kinds


def test_write_outside_workspace_not_blocked(tmp_path):
    """Path containment do guardrail removido: escrita fora do checkout NÃO aborta
    (a proteção real virá do sandbox de execução)."""
    lines = [
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Write", "arguments": '{"path":"/etc/passwd","content":"x"}'}}]},
    ]
    events, on_event = _collector()
    outcome = _run(tmp_path, lines, on_event=on_event)

    assert not outcome.aborted
    assert outcome.exit_code == 0


def test_identical_calls_detected(tmp_path):
    call = {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"echo a"}'}}]}
    lines = [call] * 4
    events, on_event = _collector()
    outcome = _run(tmp_path, lines, max_identical_calls=3, on_event=on_event)

    assert outcome.aborted
    assert "identical-calls" in outcome.abort_reason


def test_on_event_can_abort_budget(tmp_path):
    lines = [
        {"role": "assistant", "content": "resposta"},
    ]

    def on_event(kind, payload, cost):
        return "orçamento estourado: 1.00 >= 1.00"

    outcome = _run(tmp_path, lines, on_event=on_event)
    assert outcome.aborted
    assert "orçamento" in outcome.abort_reason


def test_timeout_kills_process(tmp_path):
    script = tmp_path / "sleep_kimi"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "print('{\"role\":\"assistant\",\"content\":\"começando\"}')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)

    events, on_event = _collector()
    outcome = kimi_exec.run_kimi(
        "prompt",
        cwd=str(checkout),
        kimi_bin=str(script),
        log_path=str(tmp_path / "run.log"),
        timeout=1,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(checkout),
        cost_per_interaction=0.01,
        on_event=on_event,
    )

    assert outcome.timed_out
    assert outcome.aborted
    assert "timeout" in outcome.abort_reason


def test_no_progress_watchdog_aborts_on_silence(tmp_path):
    """Executor sem saída por `no_progress_timeout` segundos é morto e tratado
    como timeout (cobre hang do kimi estagnado em reasoning)."""
    # fake que não emite NADA no stdout
    fake = tmp_path / "fake_kimi_hang"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(30)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    events, on_event = _collector()
    t0 = time.monotonic()
    outcome = kimi_exec.run_kimi(
        "prompt",
        cwd=str(checkout),
        kimi_bin=str(fake),
        log_path=str(tmp_path / "hang.log"),
        timeout=30,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(checkout),
        cost_per_interaction=0.01,
        no_progress_timeout=1,
        on_event=on_event,
    )
    elapsed = time.monotonic() - t0
    assert outcome.aborted
    assert outcome.timed_out
    assert "sem progresso" in outcome.abort_reason
    assert "nenhuma linha no stdout" in outcome.abort_reason  # nunca emitiu nada
    assert elapsed < 20, f"deveria abortar rápido, levou {elapsed:.1f}s"


def test_stall_reason_distingue_cli_nao_subida_de_silence_posterior():
    """O motivo do kill diz SE o executor chegou a falar: sem isso o boot preso do
    emulador e o hang do LLM produziam a mesma mensagem (task 195)."""
    from app.worker.exec_common import stall_reason

    with_stdout = stall_reason(900, True)
    without = stall_reason(900, False)
    assert with_stdout == "timeout sem progresso (900s sem saída)"
    assert "sem progresso" in without
    assert "nenhuma linha no stdout desde o spawn" in without
    # marcador usado por guardrails/verificação continua presente nos dois
    assert "sem progresso" in without and "sem progresso" in with_stdout


def test_no_progress_watchdog_reset_com_atividade(tmp_path):
    """Uma linha nova no stdout REINICIA o orçamento do watchdog.

    É o que a sentinela `AUTOIA_BOOTSTRAP_DONE` do bootstrap do sandbox faz: a
    janela de boot (emulador Android, minutos, só stderr) não pode consumir o
    orçamento da própria execução — era a causa do `timeout sem progresso (900s)`
    no meio de fases legítimas (task 195).
    """
    from threading import Event

    from app.worker.exec_common import make_no_progress_watchdog

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    last_activity = [time.monotonic()]
    stalled = Event()
    stop = make_no_progress_watchdog(2, proc, last_activity, stalled)
    try:
        time.sleep(1.4)  # antes do limite de 2s (sem atividade ainda)
        assert not stalled.is_set()
        last_activity[0] = time.monotonic()  # equivalente à sentinela do stdout
        time.sleep(1.2)  # ~2,6s desde o spawn, ~1,2s desde a atividade
        assert not stalled.is_set(), "atividade no stdout deveria ter reiniciado o relógio"
    finally:
        stop.set()
        proc.kill()
        proc.wait(timeout=5)


def test_kimi_captures_session_id_and_resumes(tmp_path):
    """O meta `session.resume_hint` é capturado no outcome; com `resume_session_id`
    o kimi é chamado com `-S <id>` (retomada da mesma conversa)."""
    fake = tmp_path / "fake_kimi_sess"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "with open('argv.txt', 'w') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'role':'assistant','content':'ok'}))\n"
        "print(json.dumps({'role':'meta','type':'session.resume_hint','session_id':'session_abc'}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    events, on_event = _collector()

    # 1ª execução: captura o session_id
    o1 = kimi_exec.run_kimi(
        "prompt1", cwd=str(checkout), kimi_bin=str(fake),
        log_path=str(tmp_path / "a.log"), timeout=30, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout),
        cost_per_interaction=0.01, on_event=on_event,
    )
    assert o1.session_id == "session_abc"

    # 2ª execução: retoma com -S
    (checkout / "argv.txt").unlink()
    kimi_exec.run_kimi(
        "continuar", cwd=str(checkout), kimi_bin=str(fake),
        log_path=str(tmp_path / "b.log"), timeout=30, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout),
        cost_per_interaction=0.01, resume_session_id="session_abc", on_event=on_event,
    )
    argv = json.loads((checkout / "argv.txt").read_text())
    assert "-S" in argv
    assert "session_abc" in argv
    assert argv[argv.index("-S") + 1] == "session_abc"


def test_kimi_with_skills_dir_passes_flag(tmp_path):
    """Com `skills_dir`, o comando inclui `--skills-dir <dir>`; sem ele, não muda."""
    fake = tmp_path / "fake_kimi_argv"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "with open('argv.txt', 'w') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'role':'assistant','content':'ok'}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    events, on_event = _collector()
    skills_dir = str(tmp_path / "checkout" / ".autoia" / "skills")

    kimi_exec.run_kimi(
        "prompt", cwd=str(checkout), kimi_bin=str(fake),
        log_path=str(tmp_path / "a.log"), timeout=30, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout),
        cost_per_interaction=0.01, on_event=on_event, skills_dir=skills_dir,
    )
    argv = json.loads((checkout / "argv.txt").read_text())
    assert "--skills-dir" in argv
    assert argv[argv.index("--skills-dir") + 1] == skills_dir

    # sem skills_dir, o comando não muda (flag ausente)
    (checkout / "argv.txt").unlink()
    kimi_exec.run_kimi(
        "prompt", cwd=str(checkout), kimi_bin=str(fake),
        log_path=str(tmp_path / "b.log"), timeout=30, max_identical_calls=3,
        risky_patterns=[], checkout_path=str(checkout),
        cost_per_interaction=0.01, on_event=on_event,
    )
    argv = json.loads((checkout / "argv.txt").read_text())
    assert "--skills-dir" not in argv
