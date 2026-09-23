"""Execução do opencode CLI em modo não-interativo (--format json).

O worker consome o stdout do `opencode run <prompt> --format json --dir <cwd>` linha
a linha. Cada linha é um evento JSON: `step_start`, `tool_use` (tool + state.input /
state.output), `text`, `step_finish` (com custo REAL do provedor em `cost`) e `error`.
Todo evento carrega `sessionID` no topo do objeto — capturado para retomar a MESMA
sessão numa re-execução da fase (`--session <id>`, espelhando o `-S` do kimi).

O guardrail de comandos arriscados foi REMOVIDO: a `tool_use` chega DEPOIS da
execução (mesma limitação do kimi) — matar o processo não impedia o comando e gerava
falsos positivos que interrompiam trabalho legítimo. A proteção real virá do sandbox
da execução. Permanecem os watchdogs de progresso: loop de tool calls idênticas,
timeout total e timeout de "sem progresso".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

from .. import guardrails
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

log = logging.getLogger(__name__)

# Tipos de evento emitidos para o callback (mesmos do kimi_exec).
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_ASSISTANT_TEXT = "assistant_text"
EVENT_GUARDRAIL_BLOCKED = "guardrail_blocked"
EVENT_SYSTEM = "system"

# Ferramentas do opencode (minúsculas) -> nomes usados pelo guardrails.
_TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    "glob": "Glob",
    "grep": "Grep",
    "webfetch": "WebFetch",
    "agent": "Agent",
}

_JSONL_SKIP_TYPES = {"step_start", "event", "shell", "session_start", "session_finish"}


def _tool_call_part(part: dict) -> dict | None:
    """Normaliza uma tool_use do opencode para o formato do guardrails."""
    tool = part.get("tool")
    if not tool:
        return None
    name = _TOOL_NAME_MAP.get(str(tool), str(tool))
    args = part.get("state", {}).get("input", {})
    return {
        "tool": str(tool),
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def run_opencode(
    prompt: str,
    *,
    cwd: str,
    opencode_bin: str,
    log_path: str,
    timeout: int,
    max_identical_calls: int,
    risky_patterns: list[str],
    checkout_path: str,
    whitelisted_hosts: list[str] = (),
    model: str | None = None,
    no_progress_timeout: int = 0,
    max_repeated_searches: int = 0,
    resume_session_id: str | None = None,
    repo_id: int | None = None,
    stop_file: str | None = None,
    task_stop_file: str | None = None,
    sandbox: SandboxConfig | None = None,
    workspace_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    on_event,
) -> ExecOutcome:
    """Roda o opencode e streama eventos. `on_event(kind, payload, cost) -> abort_reason|None`.

    Se `on_event` retornar uma string (ex.: orçamento estourado), o run é abortado.
    `repo_id` identifica o projeto (kill seletivo na exclusão) e `stop_file`, quando
    fornecido, dispara a parada cooperativa: se o arquivo `.stop-<repo_id>` aparecer,
    o processo é morto e o run retorna abortado.
    Síncrono: chamar de um thread/processo dedicado.

    `sandbox` (opcional): configuração de isolamento — com modo ligado, o comando
    roda dentro de um contêiner (mesma árvore do checkout); `workspace_dir` é a raiz
    de workspaces (mount rw) e `extra_env` injeta variáveis no ambiente da execução.

    `resume_session_id` (opcional): id da sessão anterior da MESMA fase (timeout/
    stall → re-execução) — o comando ganha `--session <id>` para continuar a mesma
    conversa (contexto/cache preservados), espelhando o `-S <id>` do kimi.

    Resiliência: se a retomada falhar porque a sessão não existe mais no
    armazenamento da conta atual (`Session not found` — ex.: a conta opencode-go
    mudou entre a execução original e a retomada, isolando `XDG_DATA_HOME`), o run
    cai para uma execução NOVA sem `--session` em vez de falhar a fase. A retomada
    é otimização (contexto); o handoff/prompt da fase já traz a atividade anterior.
    """
    outcome = _run_opencode_once(
        prompt, cwd=cwd, opencode_bin=opencode_bin, log_path=log_path,
        timeout=timeout, max_identical_calls=max_identical_calls,
        risky_patterns=risky_patterns, checkout_path=checkout_path,
        whitelisted_hosts=whitelisted_hosts, model=model,
        no_progress_timeout=no_progress_timeout,
        max_repeated_searches=max_repeated_searches,
        resume_session_id=resume_session_id, repo_id=repo_id,
        stop_file=stop_file, task_stop_file=task_stop_file, sandbox=sandbox,
        workspace_dir=workspace_dir, extra_env=extra_env, on_event=on_event,
    )
    if (
        resume_session_id
        and outcome.exit_code not in (0, None)
        and _log_has_session_not_found(log_path)
    ):
        log.warning(
            "opencode: retomada da sessão %s falhou (Session not found) — "
            "nova execução sem --session (sessão não existe no estado da conta)",
            resume_session_id,
        )
        try:
            on_event(
                EVENT_SYSTEM,
                {
                    "opencode_resume_fallback": (
                        f"sessão {resume_session_id} não encontrada no estado da conta "
                        "— executando sem retomada"
                    )
                },
                0.0,
            )
        except Exception:  # noqa: BLE001 — callback nunca deve quebrar o fallback
            log.exception("opencode: falha ao registrar fallback de retomada")
        outcome = _run_opencode_once(
            prompt, cwd=cwd, opencode_bin=opencode_bin, log_path=log_path,
            timeout=timeout, max_identical_calls=max_identical_calls,
            risky_patterns=risky_patterns, checkout_path=checkout_path,
            whitelisted_hosts=whitelisted_hosts, model=model,
            no_progress_timeout=no_progress_timeout,
            max_repeated_searches=max_repeated_searches,
            resume_session_id=None, repo_id=repo_id,
            stop_file=stop_file, task_stop_file=task_stop_file, sandbox=sandbox,
            workspace_dir=workspace_dir, extra_env=extra_env, on_event=on_event,
        )
    return outcome


def _log_has_session_not_found(log_path: str) -> bool:
    """True se o log da execução contém o erro de retomada do opencode
    (`Error: Session not found` no stderr)."""
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            return "Session not found" in fh.read()
    except OSError:
        return False


def _run_opencode_once(
    prompt: str,
    *,
    cwd: str,
    opencode_bin: str,
    log_path: str,
    timeout: int,
    max_identical_calls: int,
    risky_patterns: list[str],
    checkout_path: str,
    whitelisted_hosts: list[str] = (),
    model: str | None = None,
    no_progress_timeout: int = 0,
    max_repeated_searches: int = 0,
    resume_session_id: str | None = None,
    repo_id: int | None = None,
    stop_file: str | None = None,
    task_stop_file: str | None = None,
    sandbox: SandboxConfig | None = None,
    workspace_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    on_event,
) -> ExecOutcome:
    """Uma execução do opencode (sem fallback de retomada)."""
    # cwd ABSOLUTO: o workspace do worker pode ser relativo (`data/workspaces/...`),
    # mas o `--dir` vai dentro do container (workdir absoluto) — um caminho
    # relativo não resolve lá ("Failed to change directory", task 127).
    cwd = os.path.abspath(cwd)
    cmd = [opencode_bin, "run", prompt, "--format", "json", "--dir", cwd]
    if resume_session_id:
        # Retoma a MESMA sessão da execução anterior (contexto/cache preservados).
        cmd += ["--session", resume_session_id]
    if model:
        cmd += ["-m", model]
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
            cli_bin=opencode_bin,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            cidfile=cidfile,
        )
        proc = subprocess.Popen(
            spawn_cmd,
            cwd=cwd,
            # stdin isolado: o worker não deve oferecer input interativo ao CLI
            # (ver o comentário equivalente em codex_exec.py).
            stdin=subprocess.DEVNULL,
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
        last_call_key: tuple | None = None
        identical_count = 0
        semantic_tracker = guardrails.SemanticLoopTracker(max_repeated_searches)

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

                # O id da sessão aparece no topo de TODOS os eventos JSONL (campo
                # `sessionID`) — capturado para retomar a MESMA sessão numa
                # re-execução da fase (timeout/stall), espelhando o
                # `session.resume_hint` do kimi.
                if obj.get("sessionID"):
                    outcome.session_id = str(obj["sessionID"])

                event_type = obj.get("type")
                part = obj.get("part") or {}

                if event_type == "tool_use":
                    tc = _tool_call_part(part)
                    if tc is None:
                        continue
                    interactions += 1

                    key = (tc["tool"], tc["arguments"])
                    if key == last_call_key:
                        identical_count += 1
                    else:
                        last_call_key = key
                        identical_count = 1

                    violation = None
                    if identical_count >= max_identical_calls:
                        violation = guardrails.GuardrailViolation(
                            pattern="identical-calls",
                            detail=f"{tc['tool']} repetido {identical_count}x seguidas",
                        )
                    if violation is None:
                        violation = semantic_tracker.register(
                            tc["tool"], part.get("state", {}).get("input")
                        )

                    abort_reason = _persist(
                        EVENT_TOOL_CALL,
                        {
                            "tool": tc["tool"],
                            "input": part.get("state", {}).get("input"),
                            "violation": violation.detail if violation else None,
                        },
                    )
                    if violation:
                        return _abort(
                            f"guardrail: {violation.pattern}: {violation.detail}",
                            {"pattern": violation.pattern, "detail": violation.detail},
                        )
                    if abort_reason:
                        return _abort(abort_reason)

                    state = part.get("state", {})
                    _persist(
                        EVENT_TOOL_RESULT,
                        {
                            "tool": tc["tool"],
                            "status": state.get("status"),
                            "error": state.get("error"),
                            "output": state.get("output", ""),
                        },
                    )

                elif event_type == "text":
                    text = part.get("text", "")
                    if text:
                        final_text = text
                        interactions += 1
                        abort_reason = _persist(
                            EVENT_ASSISTANT_TEXT, {"content": text}
                        )
                        if abort_reason:
                            return _abort(abort_reason)

                elif event_type == "step_finish":
                    # custo REAL do provedor (soma parcial; o worker acumula e
                    # avalia o orçamento a cada evento)
                    cost = float(part.get("cost") or 0.0)
                    abort_reason = _persist(
                        EVENT_SYSTEM,
                        {
                            "opencode_step": {
                                "reason": part.get("reason"),
                                "tokens": part.get("tokens"),
                                "cost": cost,
                            }
                        },
                        cost,
                    )
                    if abort_reason:
                        return _abort(abort_reason)

                elif event_type == "error":
                    reason = str(part.get("message") or obj.get("error") or "erro do opencode")
                    _persist(EVENT_SYSTEM, {"error": reason})
                    outcome.aborted = True
                    outcome.abort_reason = f"opencode: {reason}"
                    kill_group(proc)
                    stderr_thread.join(timeout=5)
                    return outcome

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
            # `_abort`/erro, que pulavam o unregister e deixavam procs mortos
            # registrados para sempre.
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
