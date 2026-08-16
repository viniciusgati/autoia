"""Testes do comando autoia-stop (parada total) e do shutdown limpo do worker."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import stopall


def _sleep_proc(cwd: str | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        ["sleep", "60"], cwd=cwd, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Matchers puros
# ---------------------------------------------------------------------------

class TestMatchers:
    def test_is_service(self):
        assert stopall.is_service("/x/venv/bin/python3\0/x/venv/bin/autoia-worker\0--workers\03")
        assert stopall.is_service("/x/venv/bin/autoia-api")
        assert stopall.is_service("python3\0autoia-chamado-worker")
        assert not stopall.is_service("python3\0-m\0uvicorn")
        assert not stopall.is_service("bash")

    def test_is_robot_leftover(self):
        assert stopall.is_robot_leftover("/usr/local/bin/kimi-code\0-p\0x")
        assert stopall.is_robot_leftover("opencode\0run")
        assert stopall.is_robot_leftover("/sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless\0-avd")
        assert stopall.is_robot_leftover("/sdk/emulator/netsimd")
        assert stopall.is_robot_leftover("/home/u/Android/Sdk/emulator/emulator\0-avd\0Pixel")
        assert stopall.is_robot_leftover("/home/u/Android/Sdk/emulator/crashpad_handler\0--x")
        # crashpad de outros apps NÃO conta
        assert not stopall.is_robot_leftover("/opt/google/chrome/crashpad_handler\0--x")
        assert not stopall.is_robot_leftover("python3\0-u\0relay.py")

    def test_is_under_workspace(self):
        assert stopall.is_under_workspace("/data/workspaces/5/task_44", "/data/workspaces")
        assert stopall.is_under_workspace("/data/workspaces", "/data/workspaces")
        assert not stopall.is_under_workspace("/data/workspaces2", "/data/workspaces")
        assert not stopall.is_under_workspace("", "/data/workspaces")


# ---------------------------------------------------------------------------
# Varredura e kill (processos reais)
# ---------------------------------------------------------------------------

class TestSweep:
    def test_find_targets_classifica_e_exclui_self(self, tmp_path):
        ws = tmp_path / "workspaces"
        (ws / "5" / "task_44").mkdir(parents=True)

        leftover = _sleep_proc(cwd=str(ws / "5" / "task_44"))
        service = subprocess.Popen(
            ["bash", "-c", "exec -a autoia-worker sleep 60"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)  # deixa os processos aparecerem no /proc
            targets = stopall.find_targets(str(ws), exclude_pids={os.getpid()})
            assert leftover.pid in targets["leftovers"]
            assert service.pid in targets["services"]
            assert os.getpid() not in targets["services"]
        finally:
            stopall.kill_pids([leftover.pid, service.pid])

    def test_kill_pids_mata_os_processos(self, tmp_path):
        ws = tmp_path / "workspaces"
        (ws / "task_1").mkdir(parents=True)
        procs = [_sleep_proc(cwd=str(ws / "task_1")) for _ in range(2)]
        try:
            stopall.kill_pids([p.pid for p in procs])
            # Como são filhos do pytest, ficam zumbis até o wait — usa poll().
            for proc in procs:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pytest.fail("processo deveria ter morrido no SIGTERM/SIGKILL")
        finally:
            stopall.kill_pids([p.pid for p in procs])

    def test_kill_pids_tolera_pid_inexistente(self):
        stopall.kill_pids([999999])  # não pode lançar


# ---------------------------------------------------------------------------
# Limpeza de marcadores e contêineres
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_markers(self, tmp_path):
        ws = tmp_path / "workspaces"
        ws.mkdir()
        files = [".stop-1", ".stop-task-44", "worker.heartbeat", "chamado-worker.heartbeat"]
        for name in files:
            (ws / name).write_text("x")
        (ws / "keep.txt").write_text("x")

        removed = stopall.cleanup_markers(str(ws))

        for name in files:
            assert name in removed
            assert not (ws / name).exists()
        assert (ws / "keep.txt").exists()

    def test_cleanup_sandbox_containers(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        cidfile = log_dir / ".sandbox-cid-1234-1712345678901"
        cidfile.write_text("abc123\n")
        (log_dir / ".sandbox-cid-other").write_text("def456\n")

        calls: list[list[str]] = []
        fake_docker = tmp_path / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"open({str(tmp_path / 'docker_calls.txt')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        )
        fake_docker.chmod(0o755)
        monkeypatch.setattr(
            stopall.shutil, "which",
            lambda name: str(fake_docker) if name == "docker" else None,
        )

        removed = stopall.cleanup_sandbox_containers(str(log_dir))

        assert removed == 2
        calls_file = tmp_path / "docker_calls.txt"
        assert calls_file.exists()
        text = calls_file.read_text()
        assert "rm -f abc123" in text
        assert "rm -f def456" in text
        # cidfiles apagados
        assert not cidfile.exists()
        assert not (log_dir / ".sandbox-cid-other").exists()


# ---------------------------------------------------------------------------
# Shutdown limpo do worker (handler mata executores e sai sem gravar falha)
# ---------------------------------------------------------------------------

class TestWorkerShutdown:
    def test_handler_mata_executores_e_sai_com_zero(self, tmp_path):
        backend = Path(__file__).resolve().parents[1] / "backend"
        script = (
            "import subprocess, sys, time, logging\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from app.worker import exec_common\n"
            "from app.main import _install_worker_shutdown_handler\n"
            "logger = logging.getLogger('autoia.worker')\n"
            "proc = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
            "exec_common.register_proc(proc)\n"
            "_install_worker_shutdown_handler(logger)\n"
            "print(proc.pid, flush=True)\n"
            "while True:\n"
            "    time.sleep(0.5)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(backend)
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(backend)],
            stdout=subprocess.PIPE, text=True, env=env,
        )
        try:
            sleep_pid = int(child.stdout.readline().strip())
            os.kill(child.pid, signal.SIGTERM)
            child.wait(timeout=15)
            assert child.returncode == 0  # os._exit(0) do handler
            assert _wait_dead(sleep_pid), "o executor fake deveria ter morrido junto"
        finally:
            if child.poll() is None:
                child.kill()
