"""Testes do executor opencode (opencode_exec.py) com binário fake.

O fake emite o mesmo formato JSONL do `opencode run --format json`:
step_start / tool_use / text / step_finish (com cost real) / error.
"""

from __future__ import annotations

import json
import stat
import time

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


def _run(tmp_path, lines, timeout: float = 30, max_identical_calls: int = 3, risky_patterns: list[str] | None = None, sleep: float = 0.0, cwd_override: str | None = None, **kwargs):
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    log_path = str(tmp_path / "run.log")
    events: list[tuple[str, dict, float]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    outcome = opencode_exec.run_opencode(
        "prompt-x",
        cwd=cwd_override or str(cwd),
        opencode_bin=_make_fake(tmp_path, lines, sleep=sleep),
        log_path=log_path,
        timeout=timeout,
        max_identical_calls=max_identical_calls,
        risky_patterns=risky_patterns or [],
        checkout_path=cwd_override or str(cwd),
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


def test_risky_command_not_blocked_after_guardrail_removal(tmp_path):
    lines = [_tool("bash", {"command": "rm -rf /"})]
    outcome, events, _ = _run(
        tmp_path, lines, risky_patterns=[r"\brm\s+-rf\b"]
    )

    assert not outcome.aborted
    assert outcome.exit_code == 0
    kinds = [k for k, _, _ in events]
    assert "guardrail_blocked" not in kinds


def test_read_outside_checkout_not_blocked(tmp_path):
    lines = [_tool("read", {"filePath": "/etc/passwd"})]
    outcome, _, _ = _run(tmp_path, lines)

    assert not outcome.aborted
    assert outcome.exit_code == 0


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


def test_dir_sempre_absoluto(tmp_path, monkeypatch):
    """Regressão (task 127): o `--dir` vai dentro do container (workdir absoluto)
    — um cwd relativo (`data/workspaces/...`) não resolve lá. Deve virar absoluto."""
    import os

    lines = [_text("ok"), _finish(0.001)]
    monkeypatch.chdir(tmp_path)
    rel = "data/workspaces/4/task_127"
    os.makedirs(rel)
    _run(tmp_path, lines, cwd_override=rel)
    argv = (tmp_path / rel / "argv.txt").read_text()
    assert "--dir" in argv
    abs_dir = argv.split("--dir", 1)[1].split()[0]
    assert os.path.isabs(abs_dir)
    assert abs_dir == os.path.abspath(rel)


def test_captures_session_id_and_resumes(tmp_path):
    """O `sessionID` (topo de todo evento JSONL) é capturado no outcome; com
    `resume_session_id` o opencode é chamado com `--session <id>` (retomada da
    MESMA sessão — espelha o `-S` do kimi)."""
    lines = [
        {"type": "step_start", "part": {"type": "step-start"}, "sessionID": "ses_abc"},
        _text("ok"),
        _finish(0.001),
    ]
    fake = _make_fake(tmp_path, lines)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    events: list[str] = []

    def on_event(kind, payload, cost):
        events.append(kind)
        return None

    # 1ª execução: captura o session_id e NÃO leva a flag de continuação
    o1 = opencode_exec.run_opencode(
        "prompt1", cwd=str(cwd), opencode_bin=fake, log_path=str(tmp_path / "a.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        on_event=on_event,
    )
    assert o1.session_id == "ses_abc"
    argv = (cwd / "argv.txt").read_text().split()
    assert "--session" not in argv

    # 2ª execução: retoma a MESMA sessão com --session <id>
    (cwd / "argv.txt").unlink()
    o2 = opencode_exec.run_opencode(
        "continuar", cwd=str(cwd), opencode_bin=fake, log_path=str(tmp_path / "b.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        resume_session_id="ses_abc", on_event=on_event,
    )
    argv = (cwd / "argv.txt").read_text().split()
    assert "--session" in argv
    assert argv[argv.index("--session") + 1] == "ses_abc"
    # a sequência de eventos emitida não muda com o resume
    assert len(events) == 4  # 2 runs × (assistant_text + system)


def test_resume_fallback_sem_sessao(tmp_path):
    """Retomada com sessão inexistente (`Error: Session not found`) cai para uma
    execução NOVA sem `--session` em vez de falhar a fase — cenário real: a conta
    opencode-go mudou entre a execução original e a retomada, isolando o
    `XDG_DATA_HOME` (a sessão da execução antiga não existe no armazenamento novo)."""
    fake = tmp_path / "fake_fallback"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "with open('argv.txt', 'a') as f:\n"
        "    f.write('RUN:' + ' '.join(sys.argv) + '\\n')\n"
        "if '--session' in sys.argv:\n"
        "    sys.stderr.write('Error: Session not found\\n')\n"
        "    sys.exit(1)\n"
        "for l in [\n"
        "  " + json.dumps({"type": "text", "part": {"type": "text", "text": "ok-nova-sessao"}}) + ",\n"
        "  " + json.dumps({"type": "step_finish", "part": {"type": "step-finish", "reason": "stop", "cost": 0.001, "tokens": {"input": 1, "output": 1}}}) + "\n"
        "]:\n"
        "    print(json.dumps(l))\n"
        "    sys.stdout.flush()\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    events: list[tuple[str, dict]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload))
        return None

    outcome = opencode_exec.run_opencode(
        "continuar", cwd=str(cwd), opencode_bin=str(fake),
        log_path=str(tmp_path / "run.log"), timeout=30,
        max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        resume_session_id="ses_morta", on_event=on_event,
    )

    assert outcome.exit_code == 0
    assert not outcome.aborted
    assert outcome.final_text == "ok-nova-sessao"
    runs = (cwd / "argv.txt").read_text().splitlines()
    assert len(runs) == 2
    assert "--session" in runs[0]      # 1ª tentativa tentou retomar
    assert "--session" not in runs[1]  # fallback rodou sem retomada
    fallback = next(
        p for k, p in events if k == "system" and "opencode_resume_fallback" in p
    )
    assert "ses_morta" in fallback["opencode_resume_fallback"]


def test_resume_ok_nao_faz_fallback(tmp_path):
    """Retomada bem-sucedida não aciona o fallback (só UM run, com --session)."""
    lines = [
        {"type": "text", "part": {"type": "text", "text": "ok"}, "sessionID": "ses_viva"},
        _finish(0.001),
    ]
    fake = _make_fake(tmp_path, lines)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    events: list[str] = []

    def on_event(kind, payload, cost):
        events.append(kind)
        return None

    outcome = opencode_exec.run_opencode(
        "continuar", cwd=str(cwd), opencode_bin=fake,
        log_path=str(tmp_path / "run.log"), timeout=30,
        max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        resume_session_id="ses_viva", on_event=on_event,
    )
    assert outcome.exit_code == 0
    argv = (cwd / "argv.txt").read_text().split()
    assert "--session" in argv
    assert argv.count("--session") == 1


def test_error_event_quota_classificado_como_provider_limit(tmp_path):
    """Evento `error` com marca de limitação do provedor (usage limit de uma conta
    opencode-go esgotada) vira `provider_limit:` — o runner agenda retomada ou
    ROTACIONA para a próxima conta, em vez de tratar como defeito de código."""
    lines = [
        _text("começando"),
        {"type": "error", "part": {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro."}},
    ]
    outcome, events, _ = _run(tmp_path, lines)
    assert outcome.aborted
    assert outcome.abort_reason.startswith("provider_limit:")
    sys_events = [p["error"] for k, p, _ in events if k == "system" and "error" in p]
    assert sys_events and "usage limit" in sys_events[0]


def test_error_event_normal_nao_e_provider_limit(tmp_path):
    """Erro genérico do opencode continua sendo abort normal (`opencode:`), não
    provider_limit."""
    lines = [
        _text("x"),
        {"type": "error", "part": {"type": "error", "message": "Internal server error"}},
    ]
    outcome, _, _ = _run(tmp_path, lines)
    assert outcome.aborted
    assert outcome.abort_reason.startswith("opencode:")
    assert "provider_limit" not in outcome.abort_reason


def test_saida_nao_zero_com_quota_no_log_classificada_provider_limit(tmp_path):
    """Sem evento `error` (o CLI morre com status != 0 escrevendo a limitação no
    log/stderr), o fallback pós-run classifica `provider_limit:`."""
    fake = tmp_path / "fake_quota"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('step-start')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('quota exceeded, try again at 08:00 PM\\n')\n"
        "sys.exit(1)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)

    def on_event(kind, payload, cost):
        return None

    outcome = opencode_exec.run_opencode(
        "prompt-x", cwd=str(cwd), opencode_bin=str(fake),
        log_path=str(tmp_path / "run.log"), timeout=30,
        max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        on_event=on_event,
    )
    assert outcome.exit_code == 1
    assert outcome.aborted
    assert outcome.abort_reason.startswith("provider_limit:")


# ---------------------------------------------------------------------------
# Cota do provedor no log PRÓPRIO do opencode (task 195)
# ---------------------------------------------------------------------------


def _estado(tmp_path, linhas: list[str]) -> str:
    log_dir = tmp_path / "opencode" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "opencode.log").write_text("".join(l + "\n" for l in linhas))
    return str(tmp_path)


def test_opencode_state_log_usa_xdg_e_cai_no_home():
    assert opencode_exec.opencode_state_log({"HOME": "/home/x"}) == (
        "/home/x/.local/share/opencode/log/opencode.log"
    )
    assert opencode_exec.opencode_state_log({"HOME": "/h", "XDG_DATA_HOME": "/acc"}) == (
        "/acc/opencode/log/opencode.log"
    )
    assert opencode_exec.opencode_state_log({"XDG_DATA_HOME": "/acc"}) == (
        "/acc/opencode/log/opencode.log"
    )


def test_provider_limit_detecta_erro_fresco(tmp_path):
    """`Go usage limit exceeded` recente no log do opencode → classificado."""
    from datetime import datetime, timezone

    agora = datetime.now(timezone.utc).isoformat()
    xdg = _estado(tmp_path, [
        f'timestamp={agora} level=ERROR run=a message="stream error" '
        'error.error="AI_APICallError: Go usage limit exceeded"',
    ])
    msg = opencode_exec.provider_limit_from_opencode_state(
        {"XDG_DATA_HOME": xdg}, started=time.time() - 10
    )
    assert msg is not None
    assert "usage limit" in msg


def test_provider_limit_ignora_erro_antigo_ou_de_outro_nivel(tmp_path):
    """O arquivo é compartilhado por todos os processos e nunca é truncado: só
    aceitamos `level=error` com timestamp DEPOIS do início do run atual."""
    from datetime import datetime, timezone, timedelta

    antigo = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    xdg = _estado(tmp_path, [
        f'timestamp={antigo} level=ERROR run=velho error.error="Go usage limit exceeded"',
        f'timestamp={antigo} level=INFO run=velho message="quota" ',
    ])
    assert opencode_exec.provider_limit_from_opencode_state(
        {"XDG_DATA_HOME": xdg}, started=time.time() - 10
    ) is None


def test_provider_limit_sem_arquivo_retorna_none(tmp_path):
    assert opencode_exec.provider_limit_from_opencode_state(
        {"XDG_DATA_HOME": str(tmp_path / "nao-existe")}, started=time.time()
    ) is None


def _hang_fake(tmp_path) -> str:
    """CLI que NÃO emite nada no stdout (simula o opencode silencioso na cota)."""
    fake = tmp_path / "fake_opencode_silent"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def test_run_opencode_reclassifica_stall_como_provider_limit(tmp_path):
    """O hang silencioso de cota vira `provider_limit:` (não 'timeout sem
    progresso') — assim o runner rotaciona a conta do roster em vez de
    bounce-back (task 195: fase do developer presa ~40 min)."""
    from datetime import datetime, timezone

    agora = datetime.now(timezone.utc).isoformat()
    xdg = _estado(tmp_path, [
        f'timestamp={agora} level=ERROR run=a message="stream error" '
        'error.error="AI_APICallError: Go usage limit exceeded"',
    ])
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)

    outcome = opencode_exec.run_opencode(
        "prompt-x", cwd=str(cwd), opencode_bin=_hang_fake(tmp_path),
        log_path=str(tmp_path / "run.log"), timeout=30,
        max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        no_progress_timeout=1,
        extra_env={"XDG_DATA_HOME": xdg},
        on_event=lambda *a: None,
    )
    assert outcome.aborted
    assert outcome.abort_reason.startswith("provider_limit:")
    assert "usage limit" in outcome.abort_reason


def test_run_opencode_stall_sem_cota_continua_sendo_timeout(tmp_path):
    """Sem marcador de cota fresco, o silêncio continua sendo 'sem progresso'."""
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    outcome = opencode_exec.run_opencode(
        "prompt-x", cwd=str(cwd), opencode_bin=_hang_fake(tmp_path),
        log_path=str(tmp_path / "run2.log"), timeout=30,
        max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        no_progress_timeout=1,
        extra_env={"XDG_DATA_HOME": str(tmp_path / "vazio")},
        on_event=lambda *a: None,
    )
    assert outcome.aborted
    assert "sem progresso" in outcome.abort_reason
