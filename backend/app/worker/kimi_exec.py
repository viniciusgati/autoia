"""Execução do kimi-code em modo não-interativo (stream-json).

O worker consome o stdout do `kimi -p --output-format stream-json` linha a linha. Cada
linha é um JSON com `role`: `meta`, `assistant` (texto ou tool_calls) ou `tool`
(resultado).

O guardrail de comandos arriscados foi REMOVIDO: como a detecção é pós-emissão (o
comando já rodou quando a tool_call chega no stream), matar o processo não impedia o
dano e gerava falsos positivos que interrompiam trabalho legítimo. A proteção real
virá do sandbox da execução (isolamento do checkout/containers). Permanecem os
watchdogs de progresso: loop de tool calls idênticas, timeout total e timeout de
"sem progresso".
"""

from __future__ import annotations

import json
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

# Tipos de evento emitidos para o callback.
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_ASSISTANT_TEXT = "assistant_text"
EVENT_GUARDRAIL_BLOCKED = "guardrail_blocked"
EVENT_SYSTEM = "system"

KimiOutcome = ExecOutcome  # alias para compatibilidade


def run_kimi(
    prompt: str,
    *,
    cwd: str,
    kimi_bin: str,
    log_path: str,
    timeout: int,
    max_identical_calls: int,
    risky_patterns: list[str],
    checkout_path: str,
    whitelisted_hosts: list[str] = (),
    cost_per_interaction: float,
    no_progress_timeout: int = 0,
    resume_session_id: str | None = None,
    repo_id: int | None = None,
    stop_file: str | None = None,
    task_stop_file: str | None = None,
    skills_dir: str | None = None,
    sandbox: SandboxConfig | None = None,
    workspace_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    on_event,
) -> KimiOutcome:
    """Roda o kimi e streama eventos. `on_event(kind, payload, cost) -> abort_reason|None`.

    Se `on_event` retornar uma string (ex.: orçamento estourado), o run é abortado.
    `repo_id` identifica o projeto (kill seletivo na exclusão) e `stop_file`, quando
    fornecido, dispara a parada cooperativa: se o arquivo `.stop-<repo_id>` aparecer,
    o processo é morto e o run retorna abortado.
    Síncrono: chamar de um thread/processo dedicado.

    `skills_dir` (opcional): diretório no checkout com as skills do projeto
    (`.autoia/skills/`), anunciado ao kimi via `--skills-dir <path>`.

    `sandbox` (opcional): configuração de isolamento — com modo ligado, o comando
    roda dentro de um contêiner (mesma árvore do checkout); `workspace_dir` é a raiz
    de workspaces (mount rw) e `extra_env` injeta variáveis no ambiente da execução.
    """
    cmd = [kimi_bin, "-p", prompt, "--output-format", "stream-json"]
    if resume_session_id:
        # Retoma a MESMA conversa da execução anterior (contexto preservado).
        cmd = [kimi_bin, "-S", resume_session_id, "-p", prompt, "--output-format", "stream-json"]
    if skills_dir:
        # Skills do projeto materializadas no checkout (`.autoia/skills/`).
        cmd += ["--skills-dir", skills_dir]
    outcome = KimiOutcome()
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
            cli_bin=kimi_bin,
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
        last_call_key: tuple | None = None
        identical_count = 0

        def _persist(kind: str, payload: dict, cost: float = 0.0) -> str | None:
            nonlocal seq
            seq += 1
            with log_lock:
                logf.write(f"[{kind}] {json.dumps(payload, ensure_ascii=False)}\n")
                logf.flush()
            return on_event(kind, payload, cost) if on_event else None

        def _abort(reason: str, log_violation: dict | None = None) -> KimiOutcome:
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

                role = obj.get("role")

                if role == "assistant" and obj.get("tool_calls"):
                    interactions += 1
                    for index, tc in enumerate(obj["tool_calls"]):
                        key = (
                            (tc.get("function") or {}).get("name"),
                            (tc.get("function") or {}).get("arguments"),
                        )
                        if key == last_call_key:
                            identical_count += 1
                        else:
                            last_call_key = key
                            identical_count = 1

                        violation = None
                        if identical_count >= max_identical_calls:
                            violation = guardrails.GuardrailViolation(
                                pattern="identical-calls",
                                detail=f"{key[0]} repetido {identical_count}x seguidas",
                            )

                        cost = cost_per_interaction if index == 0 else 0.0
                        abort_reason = _persist(
                            EVENT_TOOL_CALL,
                            {
                                "tool_call": tc,
                                "violation": violation.detail if violation else None,
                            },
                            cost,
                        )
                        if violation:
                            return _abort(
                                f"guardrail: {violation.pattern}: {violation.detail}",
                                {"pattern": violation.pattern, "detail": violation.detail},
                            )
                        if abort_reason:
                            return _abort(abort_reason)

                elif role == "tool":
                    _persist(
                        EVENT_TOOL_RESULT,
                        {
                            "tool_call_id": obj.get("tool_call_id"),
                            "content": str(obj.get("content", "")),
                        },
                    )

                elif role == "assistant" and obj.get("content"):
                    final_text = obj["content"]
                    interactions += 1
                    abort_reason = _persist(
                        EVENT_ASSISTANT_TEXT,
                        {"content": obj["content"]},
                        cost_per_interaction,
                    )
                    if abort_reason:
                        return _abort(abort_reason)

                elif role == "meta":
                    # Captura o id da sessão (para retomar a MESMA conversa numa
                    # re-execução da fase após timeout/stall).
                    if obj.get("type") == "session.resume_hint" and obj.get("session_id"):
                        outcome.session_id = str(obj["session_id"])
                    else:
                        with log_lock:
                            logf.write(line + "\n")

                else:
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
