"""Execução do Command Code (cmd) em modo não-interativo (headless NDJSON).

O worker consome o stdout de `cmd -p <prompt> --output-format json --yolo` linha a
linha. Cada linha é um frame NDJSON com `type`:

- `event`: um `AgentEvent` da run — o relevante é `tool_running`
  (`toolName` + `description`). Diferente do kimi/opencode, o stream headless do
  cmd NÃO expõe o input/output das chamadas de ferramenta — a observabilidade é
  resumida (nome + descrição), sem payload completo por tool.
- `result`: frame FINAL, sempre o último. `subtype` (`success`/`error`/`max_turns`),
  `finalText`, `usage` (tokens), `sessionId` e `stopReason`.

O `sessionId` dos frames é capturado apenas para observabilidade (não é usado
para retomada): diferente do kimi/opencode, o cmd não suporta retomar uma sessão
headless de outro diretório/checkout (a sessão é por projeto — slug do cwd — e
com sandbox o $HOME muda), então cada execução roda com `--no-session` e começa
do zero; o contexto da fase anterior vem do handoff.

Custo: o `usage` traz tokens, mas não custo em moeda — estimado por interação
(`cost_per_interaction`), como o kimi.

Exit codes relevantes do cmd: 0 ok, 3 auth, 4 permission, 5 rate-limit,
6 connection, 7 server, 8 max turns, 10 credits. `max_turns` vira abort próprio
(o cmd já devolve `subtype: "max_turns"` no frame result — o exit code é redundante).

Permanecem os watchdogs de progresso: timeout total, "sem progresso" e parada
cooperativa. O watchdog de tool calls idênticas NÃO se aplica ao cmd — sem os
argumentos da tool no stream, só restaria o NOME como chave, e qualquer sequência
de N shell_command seguidos (comandos diferentes) mataria trabalho legítimo.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from .. import guardrails  # noqa: F401  (mantido p/ compatibilidade de import)
from .exec_common import (
    ExecOutcome,
    build_spawn_command,
    cleanup_container,
    drain_stderr,
    kill_group,
    make_no_progress_watchdog,
    make_stop_watchdog,
    make_watchdog,
    register_proc,
    unregister_proc,
)
from .sandbox import SandboxConfig

# Tipos de evento emitidos para o callback (mesmos do kimi/opencode).
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_ASSISTANT_TEXT = "assistant_text"
EVENT_GUARDRAIL_BLOCKED = "guardrail_blocked"
EVENT_SYSTEM = "system"

# Tipos de frame NDJSON do headless que não são eventos de atividade.
_JSONL_SKIP_TYPES = {"session_init", "session_id"}

# Exit codes do cmd que viram abort com motivo próprio (o resto não-0 é erro
# genérico). `8` (max_turns) é coberto pelo subtype no frame result; 130
# (SIGINT/SIGTERM) é tratado pelos watchdogs/parada cooperativa.
_EXIT_REASONS = {
    3: "cmd: não autenticado",
    4: "cmd: permissão negada (--yolo ausente?)",
    5: "cmd: rate limit excedido",
    6: "cmd: falha de rede",
    7: "cmd: erro do servidor (5xx)",
    10: "cmd: créditos insuficientes",
}


def _frame_session_id(obj: dict) -> str | None:
    """Session id do frame (chave de topo, mesmo padrão do opencode)."""
    sid = obj.get("sessionId") or obj.get("sessionID")
    return str(sid) if sid else None


def _tool_name(part: dict) -> str | None:
    """Nome da ferramenta num evento tool_running do cmd."""
    return part.get("toolName") or part.get("tool_name")


def run_cmd(
    prompt: str,
    *,
    cwd: str,
    cmd_bin: str,
    log_path: str,
    timeout: int,
    max_identical_calls: int,
    risky_patterns: list[str],
    checkout_path: str,
    whitelisted_hosts: list[str] = (),
    cost_per_interaction: float,
    no_progress_timeout: int = 0,
    resume_session_id: str | None = None,
    model: str | None = None,
    repo_id: int | None = None,
    stop_file: str | None = None,
    task_stop_file: str | None = None,
    sandbox: SandboxConfig | None = None,
    workspace_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    on_event,
) -> ExecOutcome:
    """Roda o cmd e streama eventos. `on_event(kind, payload, cost) -> abort_reason|None`.

    Se `on_event` retornar uma string (ex.: orçamento estourado), o run é abortado.
    `repo_id` identifica o projeto (kill seletivo na exclusão) e `stop_file`, quando
    fornecido, dispara a parada cooperativa: se o arquivo `.stop-<repo_id>` aparecer,
    o processo é morto e o run retorna abortado.
    Síncrono: chamar de um thread/processo dedicado.

    `sandbox` (opcional): configuração de isolamento — com modo ligado, o comando
    roda dentro de um contêiner (mesma árvore do checkout); `workspace_dir` é a raiz
    de workspaces (mount rw) e `extra_env` injeta variáveis no ambiente da execução.
    """
    cmd = [cmd_bin, "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["-m", model]
    # O `cmd` NÃO suporta retomada cross-checkout como o kimi/opencode: a sessão é
    # por projeto (slug do diretório) e com sandbox o $HOME muda — um `--resume`
    # apontando para a sessão de outra execução falha com "No session found to
    # resume". Então o executor cmd SEMPRE começa sessão nova: `--no-session`
    # (sem transcript no disco — o contexto da fase anterior vem do handoff).
    cmd += ["--no-session", "--yolo", "--skip-onboarding", "--trust"]
    outcome = ExecOutcome()
    outcome.sandbox_mode = sandbox.mode if sandbox else None
    log_lock = threading.Lock()
    # cidfile ABSOLUTO: o docker roda com `cwd=checkout` e um caminho relativo
    # (ex.: `data/logs/...`) não existe a partir dali → falha na criação do arquivo.
    cidfile = os.path.join(
        os.path.dirname(os.path.abspath(log_path)),
        f".sandbox-cid-{os.getpid()}-{int(time.time()*1000)}",
    )

    with open(log_path, "w", encoding="utf-8") as logf:
        spawn_cmd, spawn_env = build_spawn_command(
            cmd,
            cwd=cwd,
            sandbox=sandbox,
            cli_bin=cmd_bin,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            cidfile=cidfile,
        )
        proc = subprocess.Popen(
            spawn_cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=spawn_env,
        )
        register_proc(proc, repo_id=repo_id, cidfile=cidfile if sandbox and sandbox.enabled else None)

        stderr_thread = threading.Thread(
            target=drain_stderr, args=(proc.stderr, logf, log_lock), daemon=True
        )
        stderr_thread.start()

        watchdog, timed_out = make_watchdog(timeout, proc)
        stalled = threading.Event()
        last_activity = [time.monotonic()]
        stall_stop = (
            make_no_progress_watchdog(no_progress_timeout, proc, last_activity, stalled)
            if no_progress_timeout > 0
            else None
        )
        stopped = threading.Event()
        stop_files = [p for p in (stop_file, task_stop_file) if p]
        stop_watch = (
            make_stop_watchdog(stop_files, proc, stopped)
            if stop_files
            else None
        )

        seq = 0
        interactions = 0
        final_text = ""

        def _persist(kind: str, payload: dict, cost: float = 0.0) -> str | None:
            nonlocal seq
            seq += 1
            with log_lock:
                logf.write(f"[{kind}] {json.dumps(payload, ensure_ascii=False)}\n")
                logf.flush()
            return on_event(kind, payload, cost) if on_event else None

        def _abort(reason: str, log_violation: dict | None = None) -> ExecOutcome:
            if log_violation:
                _persist(EVENT_GUARDRAIL_BLOCKED, log_violation)
            outcome.aborted = True
            outcome.abort_reason = reason
            kill_group(proc)
            stderr_thread.join(timeout=5)
            return outcome

        try:
            for line in proc.stdout:
                last_activity[0] = time.monotonic()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    with log_lock:
                        logf.write(line + "\n")
                    continue

                sid = _frame_session_id(obj)
                if sid and not outcome.session_id:
                    outcome.session_id = sid

                event_type = obj.get("type")
                part = obj.get("event") or obj.get("part") or {}

                if event_type == "event":
                    inner = part.get("type") or ""
                    if inner == "tool_running":
                        tool = _tool_name(part)
                        if not tool:
                            continue
                        interactions += 1

                        # SEM watchdog de identical-calls aqui: o stream headless do
                        # cmd não expõe os argumentos da tool, então a chave seria
                        # só o NOME — qualquer sequência de 6 shell_command seguidos
                        # (comandos DIFERENTES: ls, git status, npm test…) mataria
                        # trabalho legítimo. Sem os argumentos não há como distinguir
                        # loop de progresso real (falso positivo — mesmo motivo da
                        # remoção do guardrail de comandos arriscados).
                        abort_reason = _persist(
                            EVENT_TOOL_CALL,
                            {
                                "tool": tool,
                                "description": part.get("description") or "",
                            },
                            cost_per_interaction,
                        )
                        if abort_reason:
                            return _abort(abort_reason)
                    # Demais AgentEvents (tool_running é o único com tool; os
                    # outros são ruído de progresso) — ignora.
                elif event_type == "result":
                    subtype = obj.get("subtype") or ""
                    final_text = obj.get("finalText") or final_text
                    if subtype == "max_turns":
                        return _abort("cmd: atingiu o limite de turnos (max_turns)")
                    if subtype == "error":
                        err = obj.get("error") or "erro do cmd"
                        _persist(EVENT_SYSTEM, {"error": str(err)})
                        outcome.aborted = True
                        outcome.abort_reason = f"cmd: {err}"
                        kill_group(proc)
                        stderr_thread.join(timeout=5)
                        return outcome
                    # success: o texto final fica no `finalText`; tokens no `usage`.
                    usage = obj.get("usage") or {}
                    _persist(
                        EVENT_SYSTEM,
                        {
                            "cmd_result": {
                                "subtype": subtype,
                                "stopReason": obj.get("stopReason"),
                                "usage": usage,
                            }
                        },
                    )
                else:
                    if event_type not in _JSONL_SKIP_TYPES:
                        with log_lock:
                            logf.write(line + "\n")
        finally:
            watchdog.cancel()
            if stall_stop is not None:
                stall_stop.set()
            if stop_watch is not None:
                stop_watch.set()
            # Sai do registro em QUALQUER caminho — inclusive nos returns do
            # `_abort` (guardrail/timeout/erro), que pulavam o unregister e
            # deixavam procs mortos registrados para sempre.
            unregister_proc(proc)

        proc.wait()
        stderr_thread.join(timeout=10)

    if stopped.is_set() and not outcome.aborted:
        outcome.aborted = True
        outcome.abort_reason = "execução interrompida (projeto excluído ou parada/instrução do usuário)"
    elif stalled.is_set() and not outcome.aborted:
        outcome.aborted = True
        outcome.timed_out = True
        outcome.abort_reason = f"timeout sem progresso ({no_progress_timeout}s sem saída)"
    elif timed_out.is_set() and not outcome.aborted:
        outcome.aborted = True
        outcome.timed_out = True
        outcome.abort_reason = f"timeout após {timeout}s"

    outcome.exit_code = proc.returncode
    outcome.final_text = final_text
    outcome.interaction_count = interactions
    if outcome.exit_code and outcome.exit_code not in (0, 8) and not outcome.aborted:
        outcome.aborted = True
        outcome.abort_reason = _EXIT_REASONS.get(
            outcome.exit_code, f"cmd saiu com código {outcome.exit_code}"
        )
    if sandbox and sandbox.enabled:
        try:
            cid = open(cidfile, encoding="utf-8").read().strip()
            if cid:
                outcome.container_id = cid
        except OSError:
            pass
        # Limpeza garantida (o watchdog também tenta; aqui não há corrida).
        cleanup_container(cidfile)
        try:
            os.remove(cidfile)
        except OSError:
            pass
    return outcome
