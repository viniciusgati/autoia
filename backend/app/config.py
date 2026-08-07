"""Configuração da autoia, lida de variáveis de ambiente AUTOIA_*."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Padrões de comandos considerados arriscados no shell dos robôs.
# Robôs nunca precisam: destruir arquivos, mexer no sistema, rede externa,
# privilégios, push ou sair da branch de trabalho.
DEFAULT_RISKY_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bmkfs(\.|\b)",
    r"dd\s+if=.*of=/\s*dev",
    r">\s*/dev/sd",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bsudo\b",
    r"\bchown\b",
    r"\bchmod\s+777",
    r"\bchmod\s+-R\s+777",
    r"git\s+push\b",
    r"git\s+checkout\s+(main|master)\b",
    r"git\s+switch\s+(main|master)\b",
    r"\bmv\s+[/~]",
    r"\bpython\s+-m\s+pip\s+install\b",
    r"npm\s+install\s+-g\b",
    r"\bmake\s+install\b",
    r"\bshutdown\b|\breboot\b|\bhalt\b",
    r"\bsystemctl\b",
    r"\bkillall?\b",
    r"pkexec\b",
    r"\bsu\s+-\b",
]

# Hosts que os robôs podem acessar via curl/wget (vazio = nenhum).
DEFAULT_WHITELISTED_HOSTS: list[str] = []


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
    kimi_bin: str = field(default_factory=lambda: _env("AUTOIA_KIMI_BIN", "kimi"))
    run_timeout: int = field(default_factory=lambda: _int("AUTOIA_RUN_TIMEOUT", 1800))
    max_identical_calls: int = field(default_factory=lambda: _int("AUTOIA_MAX_IDENTICAL_CALLS", 3))
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
    api_host: str = field(default_factory=lambda: _env("AUTOIA_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _int("AUTOIA_API_PORT", 8000))
    frontend_dist: str | None = field(default_factory=_frontend_dist_default)

    def ensure_dirs(self) -> None:
        for d in (self.workspace_dir, self.log_dir, _sqlite_parent(self.database_url)):
            os.makedirs(d, exist_ok=True)


def _sqlite_parent(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        path = database_url[len("sqlite:///") :]
        return os.path.dirname(path) or "."
    return "."
