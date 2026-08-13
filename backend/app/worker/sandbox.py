"""Sandbox de execução para os robôs (substitui o guardrail de comandos).

Constrói o comando que roda a CLI **dentro** de um isolamento real de sistema.
Primitivo principal: contêiner OCI via Docker (controle de FS, rede, capabilities e
recursos com pouco código). Alternativa leve: bubblewrap (`bwrap`).

Modos (`Settings.sandbox` / `Repository.sandbox`):
- "off"  -> spawn direto (comportamento legado; default até ser validado em produção).
- "fs"   -> isolamento de arquivos e privilégios; rede = host (transitório, sem
            isolamento de rede). Serviços do host via 127.0.0.1.
- "full" -> isolamento + rede bridge com proxy de egress allowlist (fail-closed);
            serviços do host via `host.docker.internal` (host-gateway).

Garantias do sandbox (a meta é "não danifica e não exfiltra", não "não vê"):
- FS do host fora do checkout/estado das CLIs: somente-leitura ou ausente.
- Sem capabilities privilegiadas, sem root, sem devices (`--cap-drop ALL`,
  `--security-opt no-new-privileges`, `--user <uid>:<gid>`).
- Recursos: `--pids-limit`, `--memory`, `--cpus`, tmpfs `/tmp` limitado.
- Sinais: `--init` (tini) propaga SIGTERM/SIGKILL para a CLI; `--rm` + limpeza via
  `--cidfile` quando o docker CLI é morto por SIGKILL.
- Rede full: proxy allowlist (mesma lista de `config.DEFAULT_WHITELISTED_HOSTS`)
  rodando no host; sem proxy/fora da lista -> conexão recusada (fail-closed).
"""

from __future__ import annotations

import logging
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger("autoia.worker.sandbox")

SANDBOX_OFF = "off"
SANDBOX_FS = "fs"
SANDBOX_FULL = "full"
VALID_SANDBOX_MODES = (SANDBOX_OFF, SANDBOX_FS, SANDBOX_FULL)

# Hosts sempre liberados no proxy (serviços do host + loopback).
_DEFAULT_PROXY_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1"}

# Backend de isolamento: "docker" (default) ou "bwrap" (fallback leve, FS-only).
BACKEND_DOCKER = "docker"
BACKEND_BWRAP = "bwrap"


def normalize_mode(mode: str | None) -> str:
    """Valida o modo de sandbox: valores inválidos caem em "off" (com aviso)."""
    mode = (mode or SANDBOX_OFF).strip().lower()
    if mode not in VALID_SANDBOX_MODES:
        log.warning("modo de sandbox inválido '%s' — usando 'off'", mode)
        return SANDBOX_OFF
    return mode


@dataclass
class SandboxConfig:
    """Configuração efetiva do sandbox para uma execução (repo > global)."""

    mode: str = SANDBOX_OFF
    backend: str = BACKEND_DOCKER
    image: str = "autoia-sandbox"
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "1g"
    read_only: bool = True
    # Roda o tini (docker `--init`) como PID 1 do contêiner: reap de zumbis e
    # forward de sinais. Default desligado: com `/usr` do host montado ro
    # (usrmerge do Debian) o docker não consegue criar o mount do docker-init em
    # `/sbin` de forma confiável — sem `--init` o docker ainda encaminha
    # SIGTERM/SIGINT para o PID 1 (--sig-proxy) e o kill de SIGKILL limpa o
    # contêiner via `docker rm -f` (cidfile).
    init: bool = False
    proxy_port: int = 18080
    home: str | None = None
    fail_closed: bool = False
    host_services_base: str = "http://127.0.0.1"
    # Hosts extras do proxy permitidos nesta execução (ex.: remote git do projeto).
    extra_egress_hosts: list[str] = field(default_factory=list)
    # Configuração de ulimit aplicada quando NÃO sandboxado (0 = sem limite).
    ulimit_as_mb: int = 0
    ulimit_nofile: int = 0

    @property
    def enabled(self) -> bool:
        return self.mode != SANDBOX_OFF

    @property
    def host_services_host(self) -> str:
        """Host para acessar serviços do host (loopback) a partir da execução."""
        if self.mode == SANDBOX_FULL:
            return "host.docker.internal"
        return "127.0.0.1"


def _host_home() -> str:
    return os.path.expanduser("~")


def _home_bin_dirs(home: str) -> list[str]:
    """Diretórios de binário das CLIs no home (kimi, node do opencode) — usados no
    PATH do contêiner e na resolução do binário (o PATH do worker pode estar sem
    esses dirs ou corrompido)."""
    dirs: list[str] = []
    kimi_bin_dir = os.path.join(home, ".kimi-code", "bin")
    if os.path.isdir(kimi_bin_dir):
        dirs.append(kimi_bin_dir)
    nvm_node = os.path.join(home, ".nvm", "versions", "node")
    if os.path.isdir(nvm_node):
        try:
            versions = sorted(os.listdir(nvm_node))
            if versions:
                vdir = os.path.join(nvm_node, versions[-1], "bin")
                if os.path.isdir(vdir):
                    dirs.append(vdir)
        except OSError:
            pass
    return dirs


def resolve_cli_path(bin_path: str | None, home: str | None = None) -> str | None:
    """Resolve o binário da CLI para um path ABSOLUTO (mesmo path é válido dentro
    do contêiner, pois o diretório é montado no mesmo lugar).

    Tenta: path absoluto → `which` no PATH do host → localizações conhecidas no
    home (`.kimi-code/bin`, nvm). Sem resolução, retorna o valor original (o PATH
    do contêiner inclui os dirs das CLIs como fallback).
    """
    if not bin_path:
        return bin_path
    if os.path.isabs(bin_path):
        return os.path.abspath(bin_path)
    found = shutil.which(bin_path)
    if found:
        return found
    for d in _home_bin_dirs(home or _host_home()):
        cand = os.path.join(d, bin_path)
        if os.path.isfile(cand):
            return cand
    return bin_path


def cli_abs_path(bin_path: str | None) -> str:
    """Compatibilidade: resolve para path absoluto (ou mantém o valor)."""
    return resolve_cli_path(bin_path) or bin_path or ""


# Candidatos do binário docker no host (o PATH do worker pode estar corrompido e
# não resolver `docker`; o mesmo path vale dentro/normal do host).
_DOCKER_BIN_CANDIDATES = [
    "/usr/bin/docker",
    "/usr/local/bin/docker",
    "/bin/docker",
    "/sbin/docker",
]


def resolve_docker_bin() -> str:
    """Path absoluto do docker CLI (busca no PATH ou candidatos conhecidos)."""
    found = shutil.which("docker")
    if found:
        return found
    for cand in _DOCKER_BIN_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return "docker"


def docker_available() -> bool:
    """True se o docker responde (daemon acessível). Resultado em cache."""
    docker_bin = resolve_docker_bin()
    if docker_bin == "docker" and not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            [docker_bin, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker_image_available(image: str) -> bool:
    """True se a imagem do sandbox existe localmente (evita falha no meio da fase)."""
    try:
        result = subprocess.run(
            [resolve_docker_bin(), "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cleanup_container(cidfile: str | None) -> None:
    """Remove o contêiner do sandbox pelo cidfile (`docker rm -f`), best-effort.

    Chamado pelo `kill_group` (watchdog) E pela thread principal do executor ao
    final do run — garante que o contêiner não fica órfão mesmo com a corrida
    entre a limpeza do watchdog e o unregister da thread principal.
    """
    if not cidfile:
        return
    try:
        cid = open(cidfile, encoding="utf-8").read().strip()
        if cid:
            subprocess.run(
                [resolve_docker_bin(), "rm", "-f", cid], capture_output=True, timeout=20
            )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------

# Diretórios de estado das CLIs no home do host: (subpath, ro|rw).
_HOME_CLI_DIRS: list[tuple[str, str]] = [
    (".config/opencode", "ro"),   # credenciais/config (leitura necessária)
    (".local/share/opencode", "rw"),  # estado/sessões (1,8 G)
    (".kimi-code", "rw"),         # binário + sessões + plugins
    (".kimi-webbridge", "rw"),    # daemon webbridge
    (".nvm", "ro"),               # runtime node do opencode (symlink)
    (".npm", "rw"),               # cache npm (evita re-download a cada execução)
    (".cache", "rw"),             # caches de build (pip, etc.)
]

# Paths sensíveis (relativos ao home ou absolutos) que NUNCA podem entrar nos mounts
# do sandbox — nem ro, nem rw. Chaves/credenciais do host ficam fora do alcance do
# robô; a meta é "não danifica e não exfiltra". Os dirs de estado autorizados das
# CLIs (`_HOME_CLI_DIRS`) são a exceção (leitura/config necessária).
_SECRET_HOME_PATHS = [
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".gitconfig",
    ".npmrc",
    ".pypirc",
    ".config/gcloud",
    ".config/gh",
    ".config/github-copilot",
    ".config/git",
]
# Paths absolutos sensíveis (independentes do home).
_SECRET_ABS_PATHS = [
    "/root",
    "/etc/shadow",
    "/etc/passwd",
    "/etc/ssl/private",
    "/etc/ssh",
]

# Diretórios de sistema do host montados RO (toolchain para builds, sem escrita).
_SYSTEM_RO_DIRS = [
    "/usr",
    "/usr/local",
    "/lib",
    "/lib64",
    "/bin",
    "/opt",
    "/etc/ssl",
    "/etc/ssl/certs",
    "/etc/ca-certificates",
]

# Candidatos do binário docker-init no host. Com `/usr` do host montado ro, o docker
# não consegue colocar o init no lugar esperado (`/sbin/docker-init`) por conta
# própria — montamos explicitamente (mesmo path absoluto dentro do contêiner).
_DOCKER_INIT_CANDIDATES = [
    "/usr/libexec/docker/docker-init",
    "/usr/libexec/docker/docker-init-linux",
    "/sbin/docker-init",
    "/usr/local/libexec/docker-init",
]


def _docker_init_flags(workspace_dir: str) -> list[str]:
    """Flags do `--init` + montagens necessárias para o docker-init coexistir com o
    mount ro de `/usr` (usrmerge do Debian: `/sbin` é symlink para `/usr/sbin`, que
    fica ro — o docker não consegue criar o mount do init lá).

    Recipe (validado): um diretório REAL do host montado em `/sbin` (vira ponto de
    montagem gravável) + o binário docker-init do host em `/sbin/docker-init:ro`.
    Sem o binário detectado, segue com `--init` puro (pode falhar com `/usr` ro —
    decisão do operador).
    """
    for cand in _DOCKER_INIT_CANDIDATES:
        if os.path.isfile(cand):
            sbin_host = os.path.join(workspace_dir, ".sandbox-sbin")
            try:
                os.makedirs(sbin_host, exist_ok=True)
            except OSError:
                sbin_host = "/tmp"
            return [
                "--init",
                "-v", f"{cand}:/sbin/docker-init:ro",
                "-v", f"{sbin_host}:/sbin",
            ]
    return ["--init"]


def _mount_specs(
    checkout: str,
    workspace_dir: str,
    cli_bins: list[str],
    home: str,
) -> tuple[list[str], bool]:
    """Lista de specs de bind mount (`origem:destino[:modo]`) e flag `source_under_tmp`
    (True se alguma origem rw fica sob /tmp — nesse caso /tmp vira bind, não tmpfs)."""
    specs: list[str] = []
    rw_sources: list[str] = []

    def add(src: str, mode: str) -> None:
        if not src or not os.path.isdir(src):
            return
        if any(s.startswith(src.rstrip("/") + ":") or s.startswith(src.rstrip("/") + "/:") for s in specs):
            return
        specs.append(f"{src}:{src}:{mode}")
        if mode == "rw":
            rw_sources.append(src)

    # Checkout e workspace raiz: rw (o gitops do worker opera no host; o robô vê a
    # mesma árvore — o mesmo path absoluto dentro e fora do contêiner).
    add(checkout, "rw")
    add(workspace_dir, "rw")

    # Estado/config das CLIs no home do host.
    for sub, mode in _HOME_CLI_DIRS:
        add(os.path.join(home, sub), mode)

    # Diretório de cada binário da CLI não coberto por um mount existente: garante
    # que fakes de teste (em tmp_path) e binários fora do home sejam alcançáveis.
    for bin_path in cli_bins:
        parent = os.path.dirname(cli_abs_path(bin_path))
        if not parent or parent in ("/usr/bin", "/bin", "/usr/local/bin"):
            continue
        add(parent, "rw")

    # Toolchain do host: somente-leitura.
    for d in _SYSTEM_RO_DIRS:
        add(d, "ro")

    source_under_tmp = any(s.startswith("/tmp/") for s in rw_sources)
    return specs, source_under_tmp


def _secret_violations(mounts: list[str], home: str) -> list[str]:
    """Lista os mounts que expõem paths sensíveis do host (relativo ao home).

    Retorna mensagens descritivas (origem + qual segredo casou); [] quando limpo.
    Os diretórios de estado autorizados das CLIs não são considerados violação —
    são o acesso necessário às credenciais/config das CLIs (leitura).
    """
    sensitive = [os.path.abspath(os.path.join(home, p)) for p in _SECRET_HOME_PATHS]
    sensitive += [os.path.abspath(p) for p in _SECRET_ABS_PATHS]
    violations: list[str] = []
    for spec in mounts:
        src = os.path.abspath(spec.split(":", 2)[0])
        for sens in sensitive:
            if src == sens or src.startswith(sens.rstrip(os.sep) + os.sep):
                violations.append(f"{src} (expoe segredo: {sens})")
                break
    return violations


def scan_secret_mounts(
    config: SandboxConfig,
    checkout: str,
    workspace_dir: str,
    cli_bin: str | None,
) -> list[str]:
    """Varredura de segredos dos mounts EFETIVOS do sandbox para esta execução.

    Usada pelo runner ANTES de cada execução sandboxed: garante que nenhum path
    sensível do host (chaves SSH, credenciais de nuvem/registros, `/etc/shadow`…)
    entrou nos mounts — nem ro, nem rw. `[]` = limpo. Config `off` → sempre limpo.
    """
    if config.mode == SANDBOX_OFF:
        return []
    home = config.home or _host_home()
    mounts, _ = _mount_specs(checkout, workspace_dir, [cli_bin or ""], home)
    return _secret_violations(mounts, home)


# ---------------------------------------------------------------------------
# Builder do comando sandboxado
# ---------------------------------------------------------------------------


def _container_env(config: SandboxConfig, extra_env: dict | None) -> dict[str, str]:
    home = config.home or _host_home()
    # PATH do contêiner: dirs de binário das CLIs do home + entradas válidas do PATH
    # do host + dirs padrão. Não depende do PATH do worker (pode estar corrompido
    # ou sem `~/.kimi-code/bin`) — é o que garante que `kimi`/`opencode` resolvem.
    path_entries: list[str] = []
    for d in _home_bin_dirs(home) + [
        p for p in (os.environ.get("PATH") or "").split(":") if p and os.path.isdir(p)
    ] + ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]:
        if d not in path_entries:
            path_entries.append(d)
    env = {
        "PATH": ":".join(path_entries),
        "HOME": home,
        "AUTOIA_HOST_SERVICES_BASE": config.host_services_base,
        "AUTOIA_SANDBOX": config.mode,
    }
    if config.mode == SANDBOX_FULL:
        proxy = f"http://host.docker.internal:{config.proxy_port}"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["NO_PROXY"] = ""
    if extra_env:
        env.update(extra_env)
    return env


def build_sandbox_command(
    cmd: list[str],
    *,
    config: SandboxConfig,
    checkout: str,
    workspace_dir: str,
    cli_bin: str | None,
    extra_env: dict | None = None,
    cidfile: str | None = None,
) -> list[str] | None:
    """Monta o comando que roda `cmd` dentro do sandbox (docker).

    Retorna None quando o sandbox está desligado (`off`) — o executor usa o comando
    direto. Com o sandbox ligado, retorna a lista de args do `docker run`; o stdout
    continua sendo o JSONL da CLI (o worker consome igual).
    """
    if config.mode == SANDBOX_OFF:
        return None

    checkout = os.path.abspath(checkout)
    workspace_dir = os.path.abspath(workspace_dir)
    home = config.home or _host_home()
    mounts, source_under_tmp = _mount_specs(checkout, workspace_dir, [cli_bin or ""], home)

    uid, gid = os.getuid(), os.getgid()
    docker_cmd = [
        resolve_docker_bin(),
        "run",
        "--rm",
        "--name", f"autoia-sbx-{os.getpid()}-{int(time.time() * 1000)}",
    ]
    if config.init:
        docker_cmd += _docker_init_flags(workspace_dir)
    docker_cmd += [
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(config.pids_limit),
        "--memory", config.memory,
        "--cpus", str(config.cpus),
        "--workdir", checkout,
    ]
    if config.read_only:
        docker_cmd += ["--read-only"]

    if config.mode == SANDBOX_FULL:
        # Rede bridge + host-gateway: serviços do host via host.docker.internal;
        # egress passa pelo proxy de allowlist (HTTP(S)_PROXY no contêiner).
        docker_cmd += ["--network", "bridge", "--add-host", "host.docker.internal:host-gateway"]
    else:
        # Modo "fs": isolamento de FS/privilégios com rede host (transitório).
        docker_cmd += ["--network", "host"]

    if source_under_tmp:
        # Alguma origem rw (checkout/fake) fica sob /tmp do host — bind de /tmp em
        # vez de tmpfs (senão os arquivos do host ficam invisíveis no contêiner).
        docker_cmd += ["-v", "/tmp:/tmp:rw"]
    else:
        # tmpfs com `exec`: o docker monta tmpfs com `noexec` por padrão, o que
        # quebraria execução de scripts/binários temporários (ex.: pytest cria
        # fakes executáveis em /tmp → PermissionError).
        docker_cmd += ["--tmpfs", f"/tmp:rw,size={config.tmpfs_size},mode=1777,exec"]

    for key, value in _container_env(config, extra_env).items():
        docker_cmd += ["--env", f"{key}={value}"]
    if cidfile:
        docker_cmd += ["--cidfile", cidfile]
    for spec in mounts:
        docker_cmd += ["-v", spec]
    docker_cmd += [config.image, *cmd]
    return docker_cmd


def build_bwrap_command(
    cmd: list[str],
    *,
    checkout: str,
    workspace_dir: str,
    cli_bin: str | None,
    home: str,
    extra_env: dict | None = None,
) -> list[str]:
    """Variante bubblewrap (fallback leve): isolamento de FS com a mesma árvore de
    mounts, sem rede externa (`--unshare-net`). Não suporta allowlist de egress —
    rede do contêiner fica só loopback."""
    checkout = os.path.abspath(checkout)
    workspace_dir = os.path.abspath(workspace_dir)
    mounts, _ = _mount_specs(checkout, workspace_dir, [cli_bin or ""], home)
    bwrap = [
        "bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--ro-bind", "/sys", "/sys",
        "--ro-bind", "/etc", "/etc",
        "--tmpfs", "/tmp",
    ]
    for spec in mounts:
        src, dest, mode = spec.split(":", 2)
        if mode == "ro":
            bwrap += ["--ro-bind", src, dest]
        else:
            bwrap += ["--bind", src, dest]
    bwrap += ["--chdir", checkout]
    env = _container_env(SandboxConfig(mode=SANDBOX_FS, home=home), extra_env)
    for key, value in env.items():
        bwrap += ["--setenv", key, value]
    bwrap += ["--", *cmd]
    return bwrap


# ---------------------------------------------------------------------------
# Proxy de egress (allowlist, fail-closed) — modo "full"
# ---------------------------------------------------------------------------

# Allowlist compartilhada do proxy (thread-safe). Inicializada com hosts do sistema;
# `add_proxy_hosts` permite ampliar por execução (ex.: remote git do projeto).
_proxy_allowlist: set[str] = set(_DEFAULT_PROXY_HOSTS)
_proxy_lock = threading.Lock()
_proxy_server: ThreadingHTTPServer | None = None


def add_proxy_hosts(hosts: list[str]) -> None:
    with _proxy_lock:
        for h in hosts:
            if h:
                _proxy_allowlist.add(h.split(":")[0].lower())


def ensure_egress_proxy(port: int, whitelist: list[str] | None = None) -> int:
    """Garante o proxy de egress rodando no host (daemon thread). Retorna a porta."""
    global _proxy_server
    with _proxy_lock:
        if whitelist:
            _proxy_allowlist.update(h.lower() for h in whitelist)
        if _proxy_server is None:
            # Bind 0.0.0.0: o contêiner chega no host via host-gateway (bridge). A
            # proteção é a allowlist fail-closed — fora dela, 403/recusa.
            server = ThreadingHTTPServer(("0.0.0.0", port), _EgressHandler)
            _proxy_server = server
            threading.Thread(
                target=server.serve_forever, daemon=True, name="autoia-egress-proxy"
            ).start()
            log.info("proxy de egress allowlist ouvindo em 0.0.0.0:%s", port)
        return _proxy_server.server_address[1]


def stop_egress_proxy() -> None:
    global _proxy_server
    with _proxy_lock:
        if _proxy_server is not None:
            _proxy_server.shutdown()
            _proxy_server.server_close()
            _proxy_server = None


class _EgressHandler(BaseHTTPRequestHandler):
    """Forward proxy HTTP + CONNECT com allowlist de hosts (fail-closed).

    Aceita apenas hosts da allowlist (`_proxy_allowlist` + `host.docker.internal`).
    Fora da lista: 403 (HTTP) / recusa (CONNECT). Usa `HTTP_CONNECTION` fixa e não
    relê cabeçalhos de rede além do necessário — o processo é uma ferramenta interna.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silencia o log por request
        pass

    @staticmethod
    def _allowed(host: str) -> bool:
        hostname = (host or "").split(":", 1)[0].lower().strip("[]")
        return hostname in _proxy_allowlist or hostname in _DEFAULT_PROXY_HOSTS

    def _deny(self) -> None:
        body = b"autoia egress proxy: host fora da allowlist (fail-closed)\n"
        self.send_response(403)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward_http(self) -> None:
        url = urlsplit(self.path)
        if not self._allowed(url.hostname or ""):
            self._deny()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        try:
            conn = HTTPConnection(url.hostname, url.port or 80, timeout=30)
            conn.request(self.command, url.path or "/", body, dict(self.headers))
            resp = conn.getresponse()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("transfer-encoding", "connection", "proxy-authenticate"):
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(resp.length or 0))
            self.end_headers()
            if resp.length:
                self.wfile.write(resp.read())
        except OSError as exc:
            log.warning("proxy egress: falha ao encaminhar %s: %s", url.hostname, exc)
            try:
                self.send_response(502)
                self.end_headers()
            except OSError:
                pass

    def _tunnel(self, host: str, port: int) -> None:
        try:
            out = socket.create_connection((host, port), timeout=30)
        except OSError as exc:
            log.warning("proxy egress: CONNECT %s:%s falhou: %s", host, port, exc)
            try:
                self.send_response(502)
                self.end_headers()
            except OSError:
                pass
            return
        self.send_response(200)
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.flush()
        sockets = [self.connection, out]
        self.close_connection = True
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 30)
                if not readable:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (out if s is self.connection else self.connection).sendall(data)
        except OSError:
            pass
        finally:
            out.close()

    def do_CONNECT(self):
        host_port = self.path
        hostname = host_port.split(":", 1)[0]
        if not self._allowed(hostname):
            self._deny()
            return
        try:
            port = int(host_port.split(":", 1)[1])
        except (IndexError, ValueError):
            self._deny()
            return
        self._tunnel(hostname, port)

    do_GET = _forward_http
    do_POST = _forward_http
    do_PUT = _forward_http
    do_DELETE = _forward_http
    do_HEAD = _forward_http
