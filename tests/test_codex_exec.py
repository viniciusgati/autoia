"""Testes do executor codex (codex_exec.py) com binário fake.

O fake emite o mesmo formato JSONL do `codex exec --json`: eventos
`thread.started`/`turn.started`/`item.*`/`turn.completed`/`error` — ver
`backend/app/worker/codex_exec.py` para a documentação do schema.
"""

from __future__ import annotations

import stat

from app.worker import codex_exec


def _make_fake(tmp_path, lines: list[dict], sleep: float = 0.0, exit_code: int = 0) -> str:
    """Cria binário fake do codex: imprime `lines` como JSONL e grava argv."""
    counter = len(list(tmp_path.glob("fake_codex_*")))
    script = tmp_path / f"fake_codex_{counter}"
    # `repr` em vez de JSON: o corpo vira um literal python (o JSON tem null/true
    # que não são literais válidos). A saída do processo é JSON (json.dumps).
    body = ",\n".join(repr(line) for line in lines)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, time\n"
        "with open('argv.txt', 'w') as f:\n"
        "    f.write(' '.join(sys.argv))\n"
        "for line in [\n" + body + "\n]:\n"
        "    print(json.dumps(line))\n"
        "    sys.stdout.flush()\n"
        f"if {sleep}:\n    time.sleep({sleep})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _thread_started(thread_id: str = "thr_1"):
    return {"type": "thread.started", "thread_id": thread_id}


def _turn_started():
    return {"type": "turn.started"}


def _item_started(item_type: str, item_id: str = "item_0", **extra):
    return {"type": "item.started", "item": {"id": item_id, "type": item_type, "status": "in_progress", **extra}}


def _item_completed(item_type: str, item_id: str = "item_0", **extra):
    return {"type": "item.completed", "item": {"id": item_id, "type": item_type, **extra}}


def _agent_message(text: str):
    return {"type": "item.completed", "item": {"id": "item_x", "type": "agent_message", "text": text}}


def _turn_completed(usage: dict | None = None):
    return {
        "type": "turn.completed",
        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0},
    }


def _run(tmp_path, lines, timeout: float = 30, max_identical_calls: int = 3, sleep: float = 0.0, **kwargs):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    log_path = str(tmp_path / "run.log")
    events: list[tuple[str, dict, float]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    outcome = codex_exec.run_codex(
        "prompt-x",
        cwd=str(cwd),
        codex_bin=_make_fake(tmp_path, lines, sleep=sleep),
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
        _thread_started(thread_id="thr_abc"),
        _turn_started(),
        _item_started("command_execution", command="bash -lc ls"),
        _agent_message("tarefa concluída"),
        _turn_completed(),
    ]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.exit_code == 0
    assert not outcome.aborted
    assert outcome.final_text == "tarefa concluída"
    assert outcome.session_id == "thr_abc"
    assert outcome.interaction_count == 1  # só a tool conta (texto é ruído)

    kinds = [k for k, _, _ in events]
    assert kinds == ["tool_call", "assistant_text", "system"]
    # custo estimado por interação (como o kimi), não real
    total = sum(c for _, _, c in events)
    assert abs(total - 0.01) < 1e-9

    tc = next(p for k, p, _ in events if k == "tool_call")
    assert tc["tool"] == "command_execution"
    assert tc["input"]["command"] == "bash -lc ls"

    system = next(p for k, p, _ in events if k == "system")
    assert "usage" in system["codex_turn"]


def test_builds_full_cmd_flags(tmp_path):
    lines = [_thread_started(), _agent_message("ok")]
    _, _, cwd = _run(tmp_path, lines, model="gpt-5.6-luna")
    argv = (cwd / "argv.txt").read_text().split()

    assert argv[0].endswith("fake_codex_0")
    assert argv[1] == "exec"
    assert argv[2] == "prompt-x"
    assert "--json" in argv
    assert "--cd" in argv and argv[argv.index("--cd") + 1] == str(cwd)
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert "--skip-git-repo-check" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "gpt-5.6-luna"


def test_model_flag_omitted_when_empty(tmp_path):
    lines = [_thread_started(), _agent_message("ok")]
    _, _, cwd = _run(tmp_path, lines, model="")
    argv = (cwd / "argv.txt").read_text().split()
    assert "--model" not in argv


def test_tool_result_with_output(tmp_path):
    lines = [
        _item_started("command_execution", command="npm test", exit_code=None),
        _item_completed("command_execution", command="npm test", exit_code=0, status="completed",
                        aggregated_output="tests ok\n"),
        _agent_message("fim"),
    ]
    outcome, events, _ = _run(tmp_path, lines)
    assert not outcome.aborted
    kinds = [k for k, _, _ in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    tr = next(p for k, p, _ in events if k == "tool_result")
    assert tr["status"] == "completed"
    assert tr["output"] == "tests ok\n"


def test_repeated_tool_not_killed(tmp_path):
    """O watchdog de identical-calls NÃO se aplica ao codex: sequências de tools
    (mesmo tipo, comandos diferentes) não são distinguíveis de loop sem heurística
    frágil — 10 command_execution seguidos não abortam."""
    lines = [_item_started("command_execution", command=f"cmd {i}") for i in range(10)]
    lines.append(_agent_message("ok"))
    outcome, events, _ = _run(tmp_path, lines, max_identical_calls=3)

    assert not outcome.aborted
    assert outcome.exit_code == 0
    kinds = [k for k, _, _ in events]
    assert "guardrail_blocked" not in kinds
    assert kinds.count("tool_call") == 10


def test_timeout(tmp_path):
    lines = [_item_started("command_execution", command="sleep")]
    outcome, _, _ = _run(tmp_path, lines, timeout=1, sleep=5)

    assert outcome.aborted
    assert outcome.timed_out


def test_error_event_aborts(tmp_path):
    lines = [{"type": "error", "error": "rate limit excedido"}]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "provider_limit:" in outcome.abort_reason
    assert "rate limit" in outcome.abort_reason
    assert any(k == "system" and "error" in p for k, p, _ in events)


def test_usage_limit_classificado_como_provider_limit(tmp_path):
    """Mensagem de usage limit do provedor NÃO é "codex saiu com código 2": vira
    `provider_limit:` para o runner agendar retomada automática."""
    lines = [
        {"type": "turn.failed", "error": "You've hit your usage limit. try again at 6:54 PM"},
    ]
    outcome, events, _ = _run(tmp_path, lines)
    assert outcome.aborted
    assert outcome.abort_reason.startswith("provider_limit:")
    assert "usage limit" in outcome.abort_reason
    assert any(k == "system" and "error" in p for k, p, _ in events)


def test_usage_limit_no_stderr_classificado_como_provider_limit(tmp_path):
    """Se o CLI morre antes de emitir evento JSONL, o usage limit no stderr ainda
    é classificado como `provider_limit:` (retry agendado)."""
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    script = tmp_path / "fake_codex_stderr_limit"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('error: You have hit your usage limit, try again later\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(2)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    outcome = codex_exec.run_codex(
        "prompt", cwd=str(cwd), codex_bin=str(script), log_path=str(tmp_path / "r.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, on_event=lambda k, p, c: None,
    )
    assert outcome.aborted
    assert outcome.exit_code == 2
    assert outcome.abort_reason.startswith("provider_limit:")
    assert "usage limit" in outcome.abort_reason


def test_turn_failed_aborts(tmp_path):
    lines = [{"type": "turn.failed", "error": "máximo de turnos"}]
    outcome, _, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "máximo de turnos" in outcome.abort_reason


def test_turn_failed_sem_motivo_preserva_tipo_do_evento(tmp_path):
    """`turn.failed`/`error` com payload vazio não podem mais virar o genérico
    "erro do codex" sem contexto: o rótulo preserva o tipo do evento."""
    lines = [{"type": "turn.failed"}]
    outcome, events, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "codex:" in outcome.abort_reason
    assert "turn.failed" in outcome.abort_reason
    sys = next(p for k, p, _ in events if k == "system")
    assert "turn.failed" in sys["error"]


def test_error_motivo_aninhado_em_item(tmp_path):
    """O motivo pode vir em `item.error` (não só no `error` de topo)."""
    lines = [
        {
            "type": "error",
            "item": {"type": "command_execution", "error": "comando rejeitado"},
        }
    ]
    outcome, _, _ = _run(tmp_path, lines)

    assert outcome.aborted
    assert "comando rejeitado" in outcome.abort_reason


def test_turn_failed_loga_linha_bruta(tmp_path):
    """Antes de abortar, a linha JSONL original do evento de erro é preservada
    no log (campos como rollback/code somem no payload resumido)."""
    line = {"type": "turn.failed", "error": "explodiu", "rollback": {"reason": "x"}}
    _, _, cwd = _run(tmp_path, [line])
    log = (cwd.parent / "run.log").read_text(encoding="utf-8")

    assert '"type": "turn.failed"' in log
    assert "rollback" in log



def test_exit_code_mapping(tmp_path):
    """Exit code não-zero sem frames → abort com motivo genérico."""
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    fake = tmp_path / "fake_codex_exit"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def on_event(kind, payload, cost):
        return None

    outcome = codex_exec.run_codex(
        "prompt",
        cwd=str(cwd),
        codex_bin=str(fake),
        log_path=str(tmp_path / "a.log"),
        timeout=30,
        max_identical_calls=3,
        risky_patterns=[],
        checkout_path=str(cwd),
        cost_per_interaction=0.01,
        on_event=on_event,
    )
    assert outcome.aborted
    assert "codex saiu com código 3" in outcome.abort_reason


def test_resume_uses_exec_resume(tmp_path):
    """Re-execução com `resume_session_id` → `codex exec resume <id> <prompt>`.

    O subcomando `resume` NÃO aceita `--sandbox`/`--skip-git-repo-check`/`--model`
    (passar qualquer um faz o CLI sair com "unexpected argument", exit 2). A
    política de sandbox e o modelo vão por `-c` (override de config):
    `-c 'sandbox_mode="danger-full-access"'` e `-c 'model="..."'`."""
    lines = [_thread_started(thread_id="thr_abc"), _agent_message("ok")]
    fake = _make_fake(tmp_path, lines)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)

    def on_event(kind, payload, cost):
        return None

    outcome = codex_exec.run_codex(
        "continuar", cwd=str(cwd), codex_bin=fake, log_path=str(tmp_path / "b.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, resume_session_id="thr_abc", model="gpt-5.6-luna",
        on_event=on_event,
    )
    assert outcome.session_id == "thr_abc"
    argv = (cwd / "argv.txt").read_text().split()
    assert argv[1] == "exec"
    assert argv[2] == "resume"
    assert 'sandbox_mode="danger-full-access"' in argv
    assert 'model="gpt-5.6-luna"' in argv
    # prompt é o último posicional, depois do session id
    assert argv[-1] == "continuar"
    assert "thr_abc" in argv
    # resume não aceita os flags do `exec` normal
    assert "--sandbox" not in argv
    assert "--skip-git-repo-check" not in argv
    assert "--model" not in argv


def test_resume_fallback_quando_sessao_morta(tmp_path):
    """`codex exec resume` com thread/sessão ausente NÃO derruba a fase: o
    executor detecta "thread/session not found" no stderr, recomeça do zero com
    uma sessão nova e emite `codex_resume_fallback` (a automação não pode travar
    numa sessão de retomada perdida)."""
    script = tmp_path / "fake_codex_fallback"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "with open('argv.txt', 'a') as f:\n"
        "    f.write(' '.join(sys.argv) + '\\n')\n"
        "if 'resume' in sys.argv:\n"
        "    sys.stderr.write('ERROR codex_core::session: failed to record rollout items: "
        "thread thr_morta not found\\n')\n"
        "    sys.stderr.flush()\n"
        "    sys.exit(2)\n"
        "print('{\"type\": \"thread.started\", \"thread_id\": \"thr_nova\"}')\n"
        "print('{\"type\": \"item.completed\", \"item\": {\"type\": \"agent_message\", "
        "\"text\": \"avaliado do zero\"}}')\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    events: list[tuple[str, dict, float]] = []

    def on_event(kind, payload, cost):
        events.append((kind, payload, cost))
        return None

    outcome = codex_exec.run_codex(
        "continuar", cwd=str(cwd), codex_bin=str(script), log_path=str(tmp_path / "b.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, resume_session_id="thr_morta",
        on_event=on_event,
    )
    # fallback: sessão nova concluiu com sucesso
    assert not outcome.aborted
    assert outcome.exit_code == 0
    assert outcome.session_id == "thr_nova"
    assert outcome.final_text == "avaliado do zero"
    # duas execuções: resume falho + fresh
    argv = (cwd / "argv.txt").read_text().splitlines()
    assert len(argv) == 2
    assert "exec resume" in argv[0] and "thr_morta" in argv[0]
    assert "exec resume" not in argv[1]
    # evento do fallback registrado na timeline
    assert any(k == "system" and "codex_resume_fallback" in p for k, p, _ in events)


def test_resume_sem_indicacao_de_sessao_morta_nao_faz_fallback(tmp_path):
    """Sem o padrão "thread/session not found" no stderr, uma falha no resume é
    falha de verdade (exit != 0) — não recomeça do zero às cegas."""
    script = tmp_path / "fake_codex_resume_err"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "with open('argv.txt', 'a') as f:\n"
        "    f.write(' '.join(sys.argv) + '\\n')\n"
        "sys.stderr.write('provider unavailable\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(2)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    cwd = tmp_path / "checkout"
    cwd.mkdir(exist_ok=True)
    outcome = codex_exec.run_codex(
        "continuar", cwd=str(cwd), codex_bin=str(script), log_path=str(tmp_path / "b.log"),
        timeout=30, max_identical_calls=3, risky_patterns=[], checkout_path=str(cwd),
        cost_per_interaction=0.01, resume_session_id="thr_x", on_event=lambda k, p, c: None,
    )
    assert outcome.aborted
    assert outcome.exit_code == 2
    # sem fallback: apenas UMA execução (o resume falho) no argv.txt
    argv = (cwd / "argv.txt").read_text().splitlines()
    assert len(argv) == 1
    assert "exec resume" in argv[0] and "thr_x" in argv[0]
