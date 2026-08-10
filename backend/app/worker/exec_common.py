"""Compartilhado entre os executores (kimi / opencode): resultado e helpers de processo."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass


@dataclass
class ExecOutcome:
    """Resultado de uma execução do robô (kimi ou opencode)."""

    exit_code: int | None = None
    final_text: str = ""
    interaction_count: int = 0
    aborted: bool = False
    timed_out: bool = False
    abort_reason: str | None = None


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
