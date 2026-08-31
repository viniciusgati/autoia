"""Compartilhado entre os executores (kimi / opencode): resultado e helpers de processo."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .sandbox import (
    SandboxConfig,
    build_bwrap_command,
    build_sandbox_command,
    cleanup_container,
    resolve_cli_path,
)

# Registro global de subprocessos ativos (kimi/opencode), mapeando cada processo
# ao `repository_id` do projeto que ele executa (None quando não se aplica) e ao
# `cidfile` do contêiner do sandbox (se houver — permite `docker rm -f` no kill).
# Permite matar TODOS no shutdown do worker — os robôs rodam em sessão própria
# (start_new_session=True) e NÃO morrem quando o worker morre, virando processos
# órfãos que continuam trabalhando na mesma branch (corrompendo estado) — OU apenas
# os de UM projeto excluído (parada cooperativa via `repo_stop_path`/`kill_repo_procs`).
_ACTIVE_PROCS: dict[subprocess.Popen, tuple[int | None, str | None]] = {}
_ACTIVE_LOCK = threading.Lock()


def register_proc(proc: subprocess.Popen, repo_id: int | None = None, cidfile: str | None = None) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS[proc] = (repo_id, cidfile)


def unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.pop(proc, None)


def _rm_docker_container(proc: subprocess.Popen) -> None:
    """Remove o contêiner do sandbox após SIGKILL no docker CLI (evita órfãos).

    O `docker run` com `--sig-proxy` propaga SIGTERM; no caminho de SIGKILL o CLI
    morre sem derrubar o contêiner — `docker rm -f` limpa pelo cidfile registrado.
    Best-effort (falha de limpeza não propaga erro). A thread principal do executor
    também limpa ao final do run (`cleanup_container`), cobrindo a corrida com o
    `unregister_proc`.
    """
    with _ACTIVE_LOCK:
        cidfile = _ACTIVE_PROCS.get(proc, (None, None))[1]
    cleanup_container(cidfile)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def kill_all_procs(grace: float = 1.0) -> None:
    """SIGTERM no grupo de todos os subprocessos ativos + limpeza do sandbox.

    Além de matar os executores, remove o contêiner e o cidfile de cada processo
    ativo (`_rm_docker_container`) — sem isso, um shutdown do worker via
    `os._exit(0)` pula o `finally` dos executores e deixa o `.sandbox-cid-*`
    órfão; o `docker run` seguinte falha com exit 125 ("container ID file found").

    Após o SIGTERM, espera `grace` e aplica SIGKILL nos teimosos, e REAPA todos
    (`poll`/`wait`) ANTES de retornar — sem o reap, um `os._exit(0)` imediato do
    shutdown deixaria os filhos como ZUMBIS órfãos (quem não tem init reapando
    os mantém vivos no /proc para sempre).
    """
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    if not procs:
        return
    for proc in procs:
        _signal_group(proc, signal.SIGTERM)
        _rm_docker_container(proc)
    deadline = time.monotonic() + grace
    remaining = list(procs)
    while remaining and time.monotonic() < deadline:
        for proc in list(remaining):
            if proc.poll() is not None:
                remaining.remove(proc)
        if remaining:
            time.sleep(0.05)
    for proc in remaining:
        _signal_group(proc, signal.SIGKILL)
    for proc in procs:
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


def kill_repo_procs(repo_id: int) -> None:
    """SIGTERM no grupo dos subprocessos ativos de UM projeto (exclusão).

    A API e o worker são processos separados; o kill seletivo por `repository_id`
    garante que parar um projeto não afeta execuções de outros projetos.
    """
    with _ACTIVE_LOCK:
        procs = [p for p, (rid, _cid) in _ACTIVE_PROCS.items() if rid == repo_id]
    for proc in procs:
        _signal_group(proc, signal.SIGTERM)


def repo_stop_path(workspace_dir: str, repo_id: int) -> str:
    """Caminho do arquivo de sinalização de parada de um projeto (API → worker).

    A API grava `workspace_dir/.stop-<repo_id>` ao excluir o projeto; o worker
    (watchdog do executor e/ou ciclo do heartbeat) mata os subprocessos daquele
    projeto e remove o arquivo.
    """
    return os.path.join(workspace_dir, f".stop-{repo_id}")


def task_stop_path(workspace_dir: str, task_id: int) -> str:
    """Caminho do arquivo de parada de UMA task (API → worker).

    A API grava `workspace_dir/.stop-task-<task_id>` quando o usuário pausa a task
    ou reenvia uma instrução/rewind com uma fase em execução: o executor daquela
    fase (que observa este arquivo) mata o subprocesso e o `_decide` entrega o
    controle de volta ao usuário (não avança). Removido pelo worker após processar.
    """
    return os.path.join(workspace_dir, f".stop-task-{task_id}")


@dataclass
class ExecOutcome:
    """Resultado de uma execução do robô (kimi ou opencode)."""

    exit_code: int | None = None
    final_text: str = ""
    interaction_count: int = 0
    aborted: bool = False
    timed_out: bool = False
    abort_reason: str | None = None
    session_id: str | None = None
    # Observabilidade do sandbox: modo usado e id do contêiner (se houver).
    sandbox_mode: str | None = None
    container_id: str | None = None
    # Violações de segredos detectadas na varredura dos mounts ([] = limpo).
    sandbox_scan: list[str] = field(default_factory=list)


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM no grupo do processo (start_new_session=True); SIGKILL se não sair.

    Com o sandbox, o SIGTERM propaga para o contêiner via `--sig-proxy`/`--init`;
    mas se o docker CLI for morto no meio do startup (ex.: timeout de 1s antes do
    contêiner anexar), o `--rm` não roda — o contêiner é removido SEMPRE pelo
    cidfile (`docker rm -f`), best-effort.
    """
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        proc.wait()
    _rm_docker_container(proc)


def drain_stderr(pipe, logf, lock) -> None:
    try:
        for line in pipe:
            with lock:
                logf.write(f"[stderr] {line}")
                logf.flush()
    except ValueError:
        pass  # arquivo fechado (processo abortado)


def make_watchdog(timeout: int, proc: subprocess.Popen) -> tuple[threading.Timer, threading.Event]:
    """Watchdog de timeout: mata o processo após `timeout`s; retorna (timer, evento)."""
    timed_out = threading.Event()

    def _watchdog() -> None:
        timed_out.set()
        kill_group(proc)

    timer = threading.Timer(timeout, _watchdog)
    timer.daemon = True
    timer.start()
    return timer, timed_out


def make_no_progress_watchdog(
    stall_seconds: int,
    proc: subprocess.Popen,
    last_activity: list[float],
    stalled: threading.Event,
) -> threading.Event:
    """Watchdog de "sem progresso": mata o processo se ficar `stall_seconds`s sem
    NENHUMA saída no stdout (o executor atualiza `last_activity[0]` a cada linha).

    Cobre casos de hang do CLI/LLM (ex.: kimi estagnado em reasoning) que o timeout
    total só pegaria no fim. Retorna um `Event` de parada p/ o chamador cancelar.
    """
    stop = threading.Event()

    def _watch() -> None:
        while not stop.is_set():
            if time.monotonic() - last_activity[0] > stall_seconds:
                stalled.set()
                kill_group(proc)
                return
            stop.wait(timeout=5)

    t = threading.Thread(target=_watch, daemon=True, name="no-progress-watchdog")
    t.start()
    return stop


def make_stop_watchdog(
    stop_files: list[str], proc: subprocess.Popen, stopped: threading.Event
) -> threading.Event:
    """Watchdog de parada cooperativa: se UM dos arquivos de sinalização aparecer
    (projeto excluído pela API ou task pausada/instruída pelo usuário enquanto o
    robô roda), mata o processo.

    A API e o worker são processos separados — os arquivos são o canal
    compartilhado. Retorna um `Event` de parada para o chamador cancelar o watcher.
    """
    stop = threading.Event()

    def _watch() -> None:
        while not stop.is_set():
            if any(os.path.isfile(p) for p in stop_files):
                stopped.set()
                kill_group(proc)
                return
            stop.wait(timeout=0.5)

    t = threading.Thread(target=_watch, daemon=True, name="stop-watchdog")
    t.start()
    return stop


def apply_resource_limits(cmd: list[str], *, as_mb: int = 0, nofile: int = 0) -> list[str]:
    """Envolve `cmd` em `bash -c` com limites de recurso quando não sandboxado.

    `ulimit -v <as_mb>` (espaço de endereço) e `ulimit -n <nofile>` (fd) protegem o
    host contra consumo descontrolado da CLI (loop, vazamento de fd). 0 = sem limite.
    O comando interno é passado como argv (sem interpolação de string) — sem shell
    injection.
    """
    if as_mb <= 0 and nofile <= 0:
        return cmd
    pre = []
    if as_mb > 0:
        pre.append(f"ulimit -v {as_mb * 1024} 2>/dev/null")
    if nofile > 0:
        pre.append(f"ulimit -n {nofile} 2>/dev/null")
    pre.append('exec "$@"')
    return ["bash", "-c", "; ".join(pre), "autoia", *cmd]


def build_spawn_command(
    cmd: list[str],
    *,
    cwd: str,
    sandbox: SandboxConfig | None,
    cli_bin: str | None = None,
    workspace_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    cidfile: str | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Monta o comando final do executor: sandbox (docker/bwrap) ou direto + ulimits.

    Retorna `(cmd_final, env)`: `env` None = herda o ambiente do worker; caso
    contrário é o ambiente do Popen (extra_env injetado em modo não-sandboxado).
    O stdout do processo final continua sendo o JSONL da CLI — o worker consome igual.
    """
    if sandbox is not None and sandbox.enabled:
        # Resolve o binário da CLI para path absoluto (o mesmo path vale dentro do
        # contêiner — o diretório é montado no mesmo lugar). Sem isso, `kimi`/`opencode`
        # bare dependeriam do PATH do contêiner (que pode não ter o dir).
        inner = list(cmd)
        resolved = resolve_cli_path(cli_bin, sandbox.home)
        if resolved and inner and inner[0] == cli_bin:
            inner[0] = resolved
        if sandbox.backend == "bwrap":
            final = build_bwrap_command(
                inner,
                checkout=cwd,
                workspace_dir=workspace_dir or cwd,
                cli_bin=resolved or cli_bin,
                home=sandbox.home or os.path.expanduser("~"),
                extra_env=extra_env,
            )
        else:
            final = build_sandbox_command(
                inner,
                config=sandbox,
                checkout=cwd,
                workspace_dir=workspace_dir or cwd,
                cli_bin=resolved or cli_bin,
                extra_env=extra_env,
                cidfile=cidfile,
            )
        return final or inner, None
    final = apply_resource_limits(
        cmd, as_mb=sandbox.ulimit_as_mb if sandbox else 0, nofile=sandbox.ulimit_nofile if sandbox else 0
    )
    if extra_env:
        env = dict(os.environ)
        env.update(extra_env)
        return final, env
    return final, None
