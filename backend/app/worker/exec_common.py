"""Compartilhado entre os executores (kimi / opencode): resultado e helpers de processo."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

# Registro global de subprocessos ativos (kimi/opencode). Permite matar todos
# no shutdown do worker — os robôs rodam em sessão própria (start_new_session=True)
# e NÃO morrem quando o worker morre, virando processos órfãos que continuam
# trabalhando na mesma branch (corrompendo estado). Sem isso, restart do worker
# deixava robôs antigos executando em paralelo.
_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def register_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)


def unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.discard(proc)


def kill_all_procs() -> None:
    """SIGTERM no grupo de todos os subprocessos ativos (não bloqueia esperando)."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


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


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM no grupo do processo (start_new_session=True); SIGKILL se não sair."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


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
