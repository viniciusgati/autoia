"""Execução do kimi-code em modo não-interativo (stream-json) com guardrails em tempo real.

O worker consome o stdout do `kimi -p --output-format stream-json` linha a linha. Cada
linha é um JSON com `role`: `meta`, `assistant` (texto ou tool_calls) ou `tool`
(resultado). A cada tool_call, os guardrails são avaliados; se algo violar a política,
o processo é morto (SIGTERM no grupo) e o run é abortado com o motivo.

Limitação honesta (v1): não dá para impedir o comando que já foi emitido pelo kimi —
o guardrail detecta e para a execução. A primeira linha de defesa é o isolamento
(cwd restrito ao checkout, branch própria, sem push).
"""

from __future__ import annotations

import json
import subprocess
import threading

from .. import guardrails
from .exec_common import (
    ExecOutcome,
    drain_stderr,
    kill_group,
    make_watchdog,
    register_proc,
    unregister_proc,
)

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
    on_event,
) -> KimiOutcome:
    """Roda o kimi e streama eventos. `on_event(kind, payload, cost) -> abort_reason|None`.

    Se `on_event` retornar uma string (ex.: orçamento estourado), o run é abortado.
    Síncrono: chamar de um thread/processo dedicado.
    """
    cmd = [kimi_bin, "-p", prompt, "--output-format", "stream-json"]
    outcome = KimiOutcome()
    log_lock = threading.Lock()

    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        register_proc(proc)

        stderr_thread = threading.Thread(
            target=drain_stderr, args=(proc.stderr, logf, log_lock), daemon=True
        )
        stderr_thread.start()

        watchdog, timed_out = make_watchdog(timeout, proc)

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

                        violation = guardrails.check_tool_call(
                            tc, risky_patterns, checkout_path, whitelisted_hosts
                        )
                        if violation is None and identical_count >= max_identical_calls:
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

                else:
                    with log_lock:
                        logf.write(line + "\n")

        finally:
            watchdog.cancel()

        proc.wait()
        stderr_thread.join(timeout=10)
        unregister_proc(proc)

    if timed_out.is_set() and not outcome.aborted:
        outcome.aborted = True
        outcome.timed_out = True
        outcome.abort_reason = f"timeout após {timeout}s"

    outcome.exit_code = proc.returncode
    outcome.final_text = final_text
    outcome.interaction_count = interactions
    return outcome
