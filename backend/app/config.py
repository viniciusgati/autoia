"""Configuração da autoia, lida de variáveis de ambiente AUTOIA_*."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .worker.project import DEFAULT_DATABASE_RULE


def _load_dotenv(directory: str | None = None) -> None:
    """Carrega um `.env` opcional (KEY=VALUE, sem sobrescrever env já setada).

    Procura em `directory` (para teste), senão no cwd e na raiz do projeto.
    Suporta `\n` no valor (ex.: regras multi-linha). Best-effort.
    """
    candidates: list[Path] = []
    if directory:
        candidates.append(Path(directory) / ".env")
    else:
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(__file__).resolve().parents[2] / ".env")
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().replace("\\n", "\n")
        except OSError:
            pass
        return  # usa o primeiro .env encontrado


_load_dotenv()

# Padrões de comandos considerados arriscados no shell dos robôs.
# Política: bloquear só o que é destrutivo/irreversível no nível do SISTEMA ou
# quebra as garantias do sandbox (sem push, sem sair da branch de trabalho).
# Tudo que é operação de build normal dentro do checkout/temp é permitido:
# `rm -rf` em caminho relativo, curl/wget p/ host da whitelist, pip/npm install,
# systemctl status, kill de processo próprio, etc.
DEFAULT_RISKY_PATTERNS = [
    # `rm -rf` em alvos de sistema (raiz, home, diretórios do SO)
    r"rm\s+-rf\s+/(\s|$)",
    r"rm\s+-rf\s+~/",
    r"rm\s+-rf\s+/(etc|usr|var|home|root|bin|sbin|boot|lib|opt)([/\s]|$)",
    # Destruição de disco
    r"\bmkfs(\.|\b)",
    r"\bdd\s+if=.*of=/\s*dev",
    r">\s*/dev/sd",
    r"\bfdisk\b",
    r"\bparted\b",
    r"\bwipefs\b",
    r"\bblkdiscard\b",
    # Desligamento/reboot do host
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\binit\s+[06]\b",
    # Escalação de privilégio
    r"\bsudo\b",
    r"\bsu\s+-\b",
    r"\bpkexec\b",
    # Rede externa (exceto curl/wget p/ host da whitelist) e exfiltração
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    # Git: nunca push nem sair da branch de trabalho (merge é feito pelo worker)
    r"git\s+push\b",
    r"git\s+checkout\s+(main|master)\b",
    r"git\s+switch\s+(main|master)\b",
]

# Hosts que os robôs podem acessar via curl/wget (vazio = nenhum). Default:
# registros de pacotes usados por builds (Gradle/Maven/Android, npm, pip).
DEFAULT_WHITELISTED_HOSTS: list[str] = [
    "dl.google.com",
    "maven.google.com",
    "repo.maven.apache.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "services.gradle.org",
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "files.pythonhosted.org",
    "pypi.org",
]


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.environ.get(key, str(default)))


def _list(key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(key)
    if raw is None:
        return list(default)
    return json.loads(raw)


def _frontend_dist_default() -> str | None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    dist = os.path.join(root, "..", "frontend", "dist")
    if os.path.isdir(dist):
        return os.path.abspath(dist)
    return os.environ.get("AUTOIA_FRONTEND_DIST") or None


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("AUTOIA_DATABASE_URL", "sqlite:///data/autoia.db"))
    workspace_dir: str = field(default_factory=lambda: _env("AUTOIA_WORKSPACE_DIR", "data/workspaces"))
    log_dir: str = field(default_factory=lambda: _env("AUTOIA_LOG_DIR", "data/logs"))
    # Diretório com as skills dos projetos (`data/skills/<repo_id>/<skill_id>/`).
    skills_dir: str = field(default_factory=lambda: _env("AUTOIA_SKILLS_DIR", "data/skills"))
    kimi_bin: str = field(default_factory=lambda: _env("AUTOIA_KIMI_BIN", "kimi"))
    opencode_bin: str = field(default_factory=lambda: _env("AUTOIA_OPENCODE_BIN", "opencode"))
    # Modelo default do executor opencode (usado quando o robô não define `Robot.model`).
    opencode_model: str = field(
        default_factory=lambda: _env("AUTOIA_OPENCODE_MODEL", "deepseek/deepseek-v4-flash")
    )
    run_timeout: int = field(default_factory=lambda: _int("AUTOIA_RUN_TIMEOUT", 1800))
    max_identical_calls: int = field(default_factory=lambda: _int("AUTOIA_MAX_IDENTICAL_CALLS", 6))
    max_attempts: int = field(default_factory=lambda: _int("AUTOIA_MAX_ATTEMPTS", 3))
    task_budget: float = field(default_factory=lambda: _float("AUTOIA_TASK_BUDGET", 10.0))
    cost_per_interaction: float = field(default_factory=lambda: _float("AUTOIA_COST_PER_INTERACTION", 0.01))
    pm_budget_topup: float = field(default_factory=lambda: _float("AUTOIA_PM_BUDGET_TOPUP", 5.0))
    max_pm_decisions: int = field(default_factory=lambda: _int("AUTOIA_MAX_PM_DECISIONS", 2))
    risky_patterns: list[str] = field(default_factory=lambda: _list("AUTOIA_RISKY_PATTERNS", DEFAULT_RISKY_PATTERNS))
    whitelisted_hosts: list[str] = field(
        default_factory=lambda: _list("AUTOIA_WHITELISTED_HOSTS", DEFAULT_WHITELISTED_HOSTS)
    )
    branch_prefix: str = field(default_factory=lambda: _env("AUTOIA_BRANCH_PREFIX", "autoia"))
    git_user_name: str = field(default_factory=lambda: _env("AUTOIA_GIT_USER_NAME", "autoia"))
    git_user_email: str = field(default_factory=lambda: _env("AUTOIA_GIT_USER_EMAIL", "autoia@local"))
    db_rule: str = field(default_factory=lambda: _env("AUTOIA_DB_RULE", DEFAULT_DATABASE_RULE))
    api_host: str = field(default_factory=lambda: _env("AUTOIA_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _int("AUTOIA_API_PORT", 9000))
    frontend_dist: str | None = field(default_factory=_frontend_dist_default)
    # Gera o resumo de cada fase concluída ("O que foi entregue") via LLM de resumo
    # (zero custo contábil; usa tokens do executor da task). Desligar evita chamadas.
    step_summary: bool = field(default_factory=lambda: _env("AUTOIA_STEP_SUMMARY", "1") == "1")
    # Gera a missão humana de cada execução de fase ("por que esta execução existe")
    # via LLM dedicada (zero custo contábil). Desligar usa só o fallback determinístico.
    step_mission: bool = field(default_factory=lambda: _env("AUTOIA_STEP_MISSION", "1") == "1")
    # Autenticação por sessão: ON exige sessão válida em TODOS os routers /api/*
    # (401 sem cookie); OFF preserva o comportamento atual (require_auth -> None).
    auth_enabled: bool = field(default_factory=lambda: _env("AUTOIA_AUTH_ENABLED", "1") == "1")
    # Validade do cookie autoia_session (dias).
    session_days: int = field(default_factory=lambda: _int("AUTOIA_SESSION_DAYS", 30))
    # Cookie com flag Secure (força também quando o request vier de https).
    cookie_secure: bool = field(default_factory=lambda: _env("AUTOIA_COOKIE_SECURE", "0") == "1")
    # Watchdog de "sem progresso": se o executor ficar `no_progress_timeout` segundos
    # sem emitir NENHUMA saída no stdout (ex.: kimi travado em reasoning), o processo
    # é morto e tratado como timeout (bounce-back/retry). 0 = desligado.
    no_progress_timeout: int = field(
        default_factory=lambda: _int("AUTOIA_NO_PROGRESS_TIMEOUT", 300)
    )
    # Rotação de logs (configuração geral): arquivos `.log` com mtime mais antigo
    # que `log_retention_days` dias são elegíveis à limpeza de órfãos
    # (tela `/config`). 0 desliga a limpeza de logs.
    log_retention_days: int = field(
        default_factory=lambda: _int("AUTOIA_LOG_RETENTION_DAYS", 30)
    )
    # ── Sandbox de execução ────────────────────────────────────────────────────────
    # Modo de isolamento das execuções dos robôs (ver docs/plano-sandbox-execucao.md):
    #   "off"  -> spawn direto (comportamento atual; default até o sandbox ser validado)
    #   "fs"   -> contêiner docker com isolamento de FS/privilégios (rede host, temporário)
    #   "full" -> contêiner + rede bridge com proxy de egress allowlist (fail-closed)
    # O modo pode ser sobrescrito por repositório (`Repository.sandbox`).
    sandbox: str = field(default_factory=lambda: _env("AUTOIA_SANDBOX", "off"))
    # Imagem base mínima (debian slim etc.). O restante da toolchain vem de mounts ro
    # do host — não precisa de imagem por ecossistema.
    sandbox_image: str = field(default_factory=lambda: _env("AUTOIA_SANDBOX_IMAGE", "autoia-sandbox"))
    sandbox_memory: str = field(default_factory=lambda: _env("AUTOIA_SANDBOX_MEMORY", "4g"))
    sandbox_cpus: float = field(default_factory=lambda: _float("AUTOIA_SANDBOX_CPUS", 2.0))
    sandbox_pids_limit: int = field(default_factory=lambda: _int("AUTOIA_SANDBOX_PIDS_LIMIT", 256))
    sandbox_tmpfs_size: str = field(default_factory=lambda: _env("AUTOIA_SANDBOX_TMPFS_SIZE", "1g"))
    # Rootfs do contêiner somente-leitura (mounts rw do checkout/estado continuam).
    sandbox_read_only: bool = field(
        default_factory=lambda: _env("AUTOIA_SANDBOX_READ_ONLY", "1") == "1"
    )
    # tini como PID 1 do contêiner (`--init`): reap de zumbis + forward de sinais.
    # Default desligado (raça com o mount ro de `/usr` no usrmerge); o kill de
    # sinais funciona sem ele via --sig-proxy + `docker rm -f` no SIGKILL.
    sandbox_init: bool = field(
        default_factory=lambda: _env("AUTOIA_SANDBOX_INIT", "0") == "1"
    )
    # Porta do proxy de egress allowlist (modo "full") rodando no host.
    sandbox_proxy_port: int = field(
        default_factory=lambda: _int("AUTOIA_SANDBOX_PROXY_PORT", 18080)
    )
    # Home do usuário do host (dirs de estado das CLIs são montados de lá). Default:
    # expanduser("~"). Útil para ambientes de teste com home isolado.
    sandbox_home: str | None = field(
        default_factory=lambda: _env("AUTOIA_SANDBOX_HOME", "") or None
    )
    # True -> falha do sandbox (docker indisponível) falha a execução (fail-closed).
    # False -> fallback para execução direta + log de aviso (comportamento transitório).
    sandbox_fail_closed: bool = field(
        default_factory=lambda: _env("AUTOIA_SANDBOX_FAIL_CLOSED", "0") == "1"
    )

    def ensure_dirs(self) -> None:
        for d in (
            self.workspace_dir,
            self.log_dir,
            self.skills_dir,
            _sqlite_parent(self.database_url),
        ):
            os.makedirs(d, exist_ok=True)


def _sqlite_parent(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        path = database_url[len("sqlite:///") :]
        return os.path.dirname(path) or "."
    return "."
