"""`autoia-stop`: para TODO o sistema sem sobrar nada rodando.

O comando resolve o desligamento em camadas:

1. **Serviços** (`autoia-api`, `autoia-worker`, `autoia-chamado-worker`): SIGTERM.
   Os workers têm handler de shutdown que mata os executores (kimi/opencode +
   contêiner do sandbox) e sai com `_exit(0)` SEM gravar falha fake na fase em
   execução (o step fica `running` e é recuperado como `pending` no próximo start).
2. **Órfãos dos robôs** que escapam do grupo do executor (emuladores Android,
   gradle daemons, servidores fake, processos com cwd dentro do workspace):
   varredura de `/proc` + SIGTERM → SIGKILL. Roda SEMPRE, mesmo que os serviços
   já estejam mortos (ex.: worker morto com SIGKILL antes).
3. **Sandbox**: remove contêineres docker órfãos pelos cidfiles em `data/logs`.
4. **Marcadores**: limpa `.stop-*` e heartbeats para o próximo start não se
   auto-matar nem mostrar lixo.

Sem dependências novas (usa `/proc` diretamente; Linux).
"""

from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import time
from typing import Iterator

from .config import Settings

# Tokens de linha de comando que identificam os serviços do autoia.
SERVICE_TOKENS = {"autoia-api", "autoia-worker", "autoia-chamado-worker"}

# Processos dos robôs/executores que podem sobreviver fora do grupo do executor.
ROBOT_TOKENS = {"kimi", "kimi-code", "opencode", "netsimd"}
ROBOT_PREFIXES = ("qemu-system",)

_GRACE_SECONDS = 5.0


def tokenize(cmdline: str) -> list[str]:
    """Tokens do cmdline (/proc/PID/cmdline é separado por NUL)."""
    return [tok for tok in cmdline.split("\0") if tok]


def _basename(tok: str) -> str:
    return os.path.basename(tok)


def is_service(cmdline: str) -> bool:
    """True se o processo é um serviço do autoia (api/worker/chamado-worker)."""
    return any(_basename(tok) in SERVICE_TOKENS for tok in tokenize(cmdline))


def is_robot_leftover(cmdline: str) -> bool:
    """True se o processo é um órfão conhecido dos robôs (kimi, opencode,
    emulador Android etc.). `crashpad_handler`/`emulator` só contam quando são
    do SDK Android — não mata crashpads de outros apps do usuário."""
    for tok in tokenize(cmdline):
        base = _basename(tok)
        if base in ROBOT_TOKENS or base.startswith(ROBOT_PREFIXES):
            return True
        if base == "emulator" and ("Android/Sdk" in cmdline or "/emulator/" in cmdline):
            return True
        if base == "crashpad_handler" and "Android/Sdk" in cmdline:
            return True
    return False


def is_under_workspace(cwd: str, workspace_dir: str) -> bool:
    """True se o cwd do processo está DENTRO do workspace do autoia (daemons,
    servidores fake e afins lançados pelos robôs nos checkouts)."""
    if not cwd:
        return False
    return cwd == workspace_dir or cwd.startswith(workspace_dir.rstrip("/") + "/")


def iter_proc_table() -> Iterator[tuple[int, str, str]]:
    """Varre /proc e devolve (pid, cmdline, cwd) — best-effort por processo."""
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(entry.rsplit("/", 1)[1])
        except ValueError:
            continue
        try:
            with open(os.path.join(entry, "cmdline"), "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        try:
            cwd = os.readlink(os.path.join(entry, "cwd"))
        except OSError:
            cwd = ""
        yield pid, cmdline, cwd


def find_targets(workspace_dir: str, exclude_pids: set[int] | None = None) -> dict[str, list[int]]:
    """Classifica os processos em serviços e órfãos. Sempre exclui `exclude_pids`
    (default: o próprio processo)."""
    exclude = exclude_pids or set()
    services: list[int] = []
    leftovers: list[int] = []
    for pid, cmdline, cwd in iter_proc_table():
        if pid in exclude:
            continue
        if is_service(cmdline):
            services.append(pid)
            continue
        if is_robot_leftover(cmdline) or is_under_workspace(cwd, workspace_dir):
            leftovers.append(pid)
    return {"services": sorted(set(services)), "leftovers": sorted(set(leftovers))}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Zumbi = processo já morto aguardando reap (filho de quem o varre) — conta
    # como morto: senão o SIGKILL de "teimosos" esperaria a graça inteira à toa.
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("ascii", errors="replace")
        rparen = stat.rfind(")")
        if rparen != -1 and len(stat) > rparen + 2:
            return stat[rparen + 2] != "Z"
    except OSError:
        pass
    return True


def signal_pids(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def kill_pids(pids: list[int], grace: float = _GRACE_SECONDS) -> None:
    """SIGTERM → espera `grace`s → SIGKILL nos teimosos."""
    if not pids:
        return
    signal_pids(pids, signal.SIGTERM)
    deadline = time.monotonic() + grace
    remaining = [pid for pid in pids if _alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [pid for pid in remaining if _alive(pid)]
    signal_pids(remaining, signal.SIGKILL)


def cleanup_sandbox_containers(log_dir: str) -> int:
    """Remove contêineres docker do sandbox pelos cidfiles em `log_dir`.

    Best-effort: sem docker no PATH, só apaga os cidfiles (o docker CLI morto
    deixaria o contêiner para trás, mas sem docker não há contêiner a limpar)."""
    docker = shutil.which("docker")
    removed = 0
    for path in glob.glob(os.path.join(log_dir, ".sandbox-cid-*")):
        try:
            with open(path, encoding="utf-8") as f:
                cid = f.read().strip()
        except OSError:
            cid = ""
        if cid and docker:
            try:
                subprocess.run(
                    [docker, "rm", "-f", cid],
                    capture_output=True, timeout=30, check=False,
                )
                removed += 1
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            os.remove(path)
        except OSError:
            pass
    return removed


def cleanup_markers(workspace_dir: str) -> list[str]:
    """Remove sinalizações de parada pendentes e heartbeats antigos.

    Sem isso, um `.stop-task-N` gravado pela API antes do desligamento mataria
    a PRIMEIRA execução após o restart (o watchdog de parada cooperativa veria o
    arquivo e derrubaria o robô recém-iniciado)."""
    removed: list[str] = []
    for pattern in (".stop-*", ".stop-task-*", "worker.heartbeat", "chamado-worker.heartbeat"):
        for path in glob.glob(os.path.join(workspace_dir, pattern)):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except OSError:
                pass
    return removed


def run_stop() -> None:
    settings = Settings()
    workspace_dir = os.path.abspath(settings.workspace_dir)
    log_dir = os.path.abspath(settings.log_dir)
    me = os.getpid()

    targets = find_targets(workspace_dir, exclude_pids={me})

    services = targets["services"]
    if services:
        print(f"parando serviços do autoia ({len(services)} processo(s): {', '.join(map(str, services))})")
        kill_pids(services)

    leftovers = targets["leftovers"]
    if leftovers:
        print(f"encerrando {len(leftovers)} processo(s) órfão(s) dos robôs: {', '.join(map(str, leftovers))}")
        kill_pids(leftovers)

    containers = cleanup_sandbox_containers(log_dir)
    if containers:
        print(f"{containers} contêiner(es) do sandbox removido(s)")

    markers = cleanup_markers(workspace_dir)
    if markers:
        print(f"marcadores limpos: {', '.join(markers)}")

    if not services and not leftovers and not containers and not markers:
        print("nada rodando para parar.")
    else:
        print("autoia parado.")
