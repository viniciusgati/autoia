"""Execução do OpenAI Codex CLI em modo não-interativo (`codex exec --json`).

O worker consome o stdout de `codex exec <prompt> --json` linha a linha. Cada
linha é um evento JSONL com `type`:

- `thread.started`: carrega o `thread_id` — usado como `session_id` para RETOMAR
  a MESMA conversa numa re-execução da fase (`codex exec resume <id>`), no mesmo
  espírito do `-S` do kimi / `--session` do opencode.
- `item.started`/`item.completed`: itens de atividade. `command_execution` (e
  outros tipos de tool) viram `tool_call`/`tool_result` com o comando e a saída
  (`aggregated_output`); `agent_message` vira `assistant_text` (e o último texto
  é o `final_text` do run).
- `turn.completed`: fecha o turno com `usage` (tokens) — sem custo em moeda.
- `turn.failed`/`error`: abortam o run com o motivo.

Observabilidade é rica: os itens `command_execution`/`file_change` expõem o
comando e a saída completa.

Custo: o stream traz tokens mas não custo em moeda — estimado por interação
(`cost_per_interaction`), como o kimi. Permanecem os watchdogs de timeout,
"sem progresso" e parada cooperativa. O watchdog de tool calls idênticas NÃO se
aplica: o `risky_patterns`/`max_identical_calls` são aceitos por compatibilidade
de assinatura e ignorados (a proteção real é o sandbox externo do autoia).

Retomada de sessão com fallback: `codex exec resume <id>` falha com exit code 2
e mensagem de thread/sessão não encontrada quando o thread_id morreu (ex.: a
sessão foi interrompida por um rewind do usuário e o store do codex não tem mais
o thread). Em vez de derrubar a fase, o executor detecta o padrão no stderr e
RECOMEÇA do zero com uma sessão nova — a re-execução segue, não falha.

Flags: o codex roda com `--sandbox danger-full-access` (o isolamento de verdade
fica no sandbox externo do autoia; o sandbox interno do codex default é
read-only e impediria o robô de escrever) e `--skip-git-repo-check` por robustez.
`--model` só é passado quando há um modelo explícito (task/chamado > robô); sem
modelo o codex usa o `model` do `~/.codex/config.toml` dele.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

from .exec_common import (
    ExecOutcome,
    build_spawn_command,
    cleanup_container,
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

# Tipos de item que representam UMA chamada de ferramenta (viram tool_call/
# tool_result e contam como interação para o custo estimado). Itens de texto/
# raciocínio/plano são ruído de progresso (ignorados).
_INTERACTION_ITEMS = {
    "command_execution",
    "shell_call",
    "local_shell_call",
    "file_edit",
    "file_change",
    "apply_patch",
    "mcp_tool_call",
    "web_search",
}

# Tipos de evento de topo do JSONL que não são atividade (não logar cru).
_JSONL_SKIP_TYPES = {"thread.started", "turn.started"}

log = logging.getLogger("autoia.worker.codex")

# Marcadores de sessão de retomada inválida. `codex exec resume <id>` com um
# thread_id morto/ausente cai com exit code 2 e mensagens como
# "failed to record rollout items: thread ... not found" — antes, isso derrubava
# a fase inteira numa re-execução. Detecta-se o padrão no stderr para RECOMEÇAR
# do zero (sessão nova) em vez de falhar.
_RESUME_BROKEN_MARKERS = (
    "rollout items",
    "thread not found",
    "session not found",
    "thread does not exist",
    "session does not exist",
    "no such thread",
    "no such session",
)


def _resume_broken(stderr: list[str]) -> bool:
    """True se o stderr indica sessão de retomada do codex inválida/ausente."""
    for line in stderr:
        low = line.lower()
        if any(marker in low for marker in _RESUME_BROKEN_MARKERS):
            return True
        if ("thread" in low or "session" in low) and any(
            marker in low for marker in ("not found", "does not exist", "no such", "missing")
        ):
            return True
    return False


# Marcadores de LIMITAÇÃO do provedor (usage/rate limit, créditos). Não é defeito
# de código nem de infra do projeto: a fase deve ESPERAR e retomar sozinha quando
# o provedor liberar — o runner trata `provider_limit:` como retry agendado.
_PROVIDER_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "quota",
    "credits",
    "try again at",
    "too many requests",
)


def _provider_limit_detected(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _PROVIDER_LIMIT_MARKERS)


def _frame_thread_id(obj: dict) -> str | None:
    """Session/thread id do frame (topo do objeto)."""
    for key in ("thread_id", "threadId", "session_id"):
        sid = obj.get(key)
        if sid:
            return str(sid)
    return None


def _item_type(item: dict) -> str:
    return str(item.get("type") or "")


def _failure_reason(obj: dict, event_type: str) -> str:
    """Motivo legível de `turn.failed`/`error`.

    O codex nem sempre preenche `error`/`part.message` nesses eventos (na
    prática chegam payloads vazios); busca-se o motivo em vários campos e, sem
    nada, o rótulo genérico preserva o tipo do evento para o diagnóstico.
    """
    item = obj.get("item") or {}
    candidates = [
        obj.get("error"),
        (obj.get("part") or {}).get("message"),
        (item.get("part") or {}).get("message"),
        item.get("error"),
        item.get("message"),
        obj.get("message"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"erro do codex ({event_type})"


def _item_id(item: dict) -> str | None:
    value = item.get("id")
    return str(value) if value else None


def _tool_input(item: dict) -> dict:
    """Campos de input de um item de tool (comando/caminho quando existirem)."""
    payload = {}
    for key in ("command", "path"):
        value = item.get(key)
        if value not in (None, ""):
            payload[key] = value
    return payload


def _run_codex_stream(
    *,
    cmd: list[str],
    cwd: str,
    codex_bin: str,
    log_path: str,
    timeout: int,
    cost_per_interaction: float,
    no_progress_timeout: int,
    repo_id: int | None,
    stop_file: str | None,
    task_stop_file: str | None,
    sandbox: SandboxConfig | None,
    workspace_dir: str | None,
    extra_env: dict[str, str] | None,
    on_event,
) -> tuple[ExecOutcome, list[str]]:
    """Spawna UMA execução do codex e streama os eventos.

    Retorna `(outcome, stderr_capture)` — o stderr é capturado para o fallback
    de retomada de sessão (`_resume_broken`). O `log_path` é reescrito a cada
    execução (a última prevalece no log da fase).
    """
    outcome = ExecOutcome()
    outcome.sandbox_mode = sandbox.mode if sandbox else None
    log_lock = threading.Lock()
    # cidfile ABSOLUTO: o docker roda com `cwd=checkout` e um caminho relativo
    # (ex.: `data/logs/...`) não existe a partir dali → falha na criação do arquivo.
    cidfile = os.path.join(
        os.path.dirname(os.path.abspath(log_path)),
        f".sandbox-cid-{os.getpid()}-{int(time.time()*1000)}",
    )
    stderr_capture: list[str] = []

    def _drain_stderr(pipe, logf, lock, capture):
        try:
            for line in pipe:
                with lock:
                    logf.write(f"[stderr] {line}")
                    logf.flush()
                capture.append(line)
        except ValueError:
            pass

    with open(log_path, "w", encoding="utf-8") as logf:
        spawn_cmd, spawn_env = build_spawn_command(
            cmd,
            cwd=cwd,
            sandbox=sandbox,
            cli_bin=codex_bin,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            cidfile=cidfile,
        )
        proc = subprocess.Popen(
            spawn_cmd,
            cwd=cwd,
            # `stdin` NUNCA é herdado do worker: o codex exec, com stdin piped,
            # imprime "Reading additional input from stdin..." e tenta ler a
            # continuação da conversa — com um pipe aberto herdado ele BLOQUEIA
            # para sempre (vira "timeout sem progresso"); com DEVNULL o EOF é
            # imediato e o CLI segue/prossegue sem pendência.
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
            target=_drain_stderr,
            args=(proc.stderr, logf, log_lock, stderr_capture),
            daemon=True,
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

        def _abort(reason: str) -> ExecOutcome:
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

                # `thread_id` no topo de TODO evento (mais visível no
                # `thread.started`) — capturado p/ retomar a MESMA sessão.
                sid = _frame_thread_id(obj)
                if sid and not outcome.session_id:
                    outcome.session_id = sid

                event_type = obj.get("type")
                item = obj.get("item") or {}

                if event_type == "item.started":
                    itype = _item_type(item)
                    if itype not in _INTERACTION_ITEMS:
                        continue
                    interactions += 1
                    # Sem watchdog de identical-calls: os itens trazem o comando
                    # (não só o nome), mas sequências de comandos DIFERENTES com
                    # o mesmo tipo (vários command_execution em build/teste) não
                    # podem ser distinguidas de loop sem heurística frágil.
                    abort_reason = _persist(
                        EVENT_TOOL_CALL,
                        {
                            "tool": itype,
                            "id": _item_id(item),
                            "input": _tool_input(item),
                            "status": item.get("status"),
                        },
                        cost_per_interaction,
                    )
                    if abort_reason:
                        return _abort(abort_reason), stderr_capture

                elif event_type == "item.completed":
                    itype = _item_type(item)
                    if itype == "agent_message":
                        text = str(item.get("text") or "")
                        if text:
                            final_text = text
                            abort_reason = _persist(
                                EVENT_ASSISTANT_TEXT, {"content": text}
                            )
                            if abort_reason:
                                return _abort(abort_reason), stderr_capture
                    elif itype in _INTERACTION_ITEMS:
                        exit_code = item.get("exit_code")
                        status = item.get("status")
                        error = item.get("error")
                        if exit_code not in (None, 0):
                            error = error or f"exit {exit_code}"
                        abort_reason = _persist(
                            EVENT_TOOL_RESULT,
                            {
                                "tool": itype,
                                "id": _item_id(item),
                                "status": status,
                                "error": error,
                                "output": str(item.get("aggregated_output") or ""),
                            },
                        )
                        if abort_reason:
                            return _abort(abort_reason), stderr_capture
                    else:
                        with log_lock:
                            logf.write(line + "\n")

                elif event_type == "turn.completed":
                    usage = obj.get("usage") or {}
                    _persist(
                        EVENT_SYSTEM,
                        {
                            "codex_turn": {
                                "usage": usage,
                            }
                        },
                    )

                elif event_type in ("turn.failed", "error"):
                    reason = _failure_reason(obj, event_type)
                    # Loga a linha BRUTA do evento antes de abortar: os campos
                    # originais (rollback/code/etc.) somem no payload resumido e
                    # sem ela não há como diagnosticar falhas do provider.
                    with log_lock:
                        logf.write(line + "\n")
                    _persist(EVENT_SYSTEM, {"error": reason})
                    if _provider_limit_detected(reason):
                        return _abort(f"provider_limit: {reason}"), stderr_capture
                    return _abort(f"codex: {reason}"), stderr_capture

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
            # `_abort` (timeout/erro), que pulavam o unregister e deixavam procs
            # mortos registrados para sempre.
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
    if outcome.exit_code and not outcome.aborted:
        outcome.aborted = True
        # Limitação do provedor pode sair só no stderr (CLI morre antes de emitir
        # evento JSONL) — classifica como `provider_limit:` p/ retry agendado.
        stderr_text = "\n".join(stderr_capture)
        if _provider_limit_detected(stderr_text):
            outcome.abort_reason = f"provider_limit: {stderr_text.strip()[:400]}"
        else:
            outcome.abort_reason = f"codex saiu com código {outcome.exit_code}"
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
    return outcome, stderr_capture


def run_codex(
    prompt: str,
    *,
    cwd: str,
    codex_bin: str,
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
    """Roda o codex e streama eventos. `on_event(kind, payload, cost) -> abort_reason|None`.

    Se `on_event` retornar uma string (ex.: orçamento estourado), o run é abortado.
    `repo_id` identifica o projeto (kill seletivo na exclusão) e `stop_file`, quando
    fornecido, dispara a parada cooperativa: se o arquivo `.stop-<repo_id>` aparecer,
    o processo é morto e o run retorna abortado.
    Síncrono: chamar de um thread/processo dedicado.

    `sandbox` (opcional): configuração de isolamento — com modo ligado, o comando
    roda dentro de um contêiner (mesma árvore do checkout); `workspace_dir` é a raiz
    de workspaces (mount rw) e `extra_env` injeta variáveis no ambiente da execução.

    `resume_session_id` (opcional): id (thread_id) da sessão anterior da MESMA fase
    (timeout/stall → re-execução) — o comando vira `codex exec resume <id>` para
    continuar a mesma conversa (contexto preservado), espelhando o `-S` do kimi.
    Se a sessão de retomada for inválida (thread_id morto — exit code 2 com
    "thread/session not found" no stderr), o executor RECOMEÇA do zero com uma
    sessão nova e emite o evento `codex_resume_fallback`, em vez de derrubar a
    fase (uma sessão perdida não pode travar a automação).
    `risky_patterns`/`max_identical_calls` são mantidos apenas por compatibilidade
    de assinatura com os outros executores (ver docstring do módulo).
    """
    def _execute(resume_id: str | None) -> tuple[ExecOutcome, list[str]]:
        if resume_id:
            # `codex exec resume <session_id> <prompt>`: a retomada NÃO aceita
            # `--sandbox`/`--skip-git-repo-check`/`--model` (só o `exec` normal
            # tem esses flags) — passar qualquer um deles faz o CLI sair com
            # "unexpected argument" (exit 2), quebrando TODA retomada. O sandbox
            # e o modelo vão por `-c` (override de config):
            #   -c 'sandbox_mode="danger-full-access"'  (mesma política do exec)
            #   -c 'model="<model>"'                     (modelo da task, se houver)
            cmd = [
                codex_bin,
                "exec",
                "resume",
                "--json",
                "-c", 'sandbox_mode="danger-full-access"',
            ]
            if model:
                cmd += ["-c", f'model="{model}"']
            cmd += [resume_id, prompt]
        else:
            cmd = [
                codex_bin,
                "exec",
                prompt,
                "--json",
                "--cd",
                os.path.abspath(cwd),
                "--sandbox",
                "danger-full-access",
                "--skip-git-repo-check",
            ]
            if model:
                cmd += ["--model", model]
        return _run_codex_stream(
            cmd=cmd,
            cwd=cwd,
            codex_bin=codex_bin,
            log_path=log_path,
            timeout=timeout,
            cost_per_interaction=cost_per_interaction,
            no_progress_timeout=no_progress_timeout,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            on_event=on_event,
        )

    if resume_session_id:
        outcome, stderr_lines = _execute(resume_session_id)
        if _resume_broken(stderr_lines) and (outcome.aborted or outcome.exit_code):
            log.warning(
                "codex: sessão de retomada %s inválida/ausente — recomeçando do zero",
                resume_session_id,
            )
            try:
                if on_event:
                    on_event(
                        EVENT_SYSTEM,
                        {
                            "codex_resume_fallback": {
                                "resume_session_id": resume_session_id,
                                "detail": "sessão anterior não encontrada — iniciada nova sessão",
                            }
                        },
                        0.0,
                    )
            except Exception:  # pragma: no cover — evento é best-effort
                pass
            return _execute(None)[0]
        return outcome
    return _execute(None)[0]