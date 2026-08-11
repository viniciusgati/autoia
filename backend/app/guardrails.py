"""Guardrails: política de comandos e caminhos aplicada em tempo real ao stream do kimi.

Nota de limitação (v1): o guardrail detecta uma chamada perigosa ao vê-la no stream e
então **mata o processo do kimi**. Não é possível impedir a execução do comando já
enviado — por isso o isolamento (cwd restrito ao checkout, branch própria, sem push)
é a primeira linha de defesa, e este detector é a segunda.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass

# Ferramentas do kimi que manipulam caminhos de arquivo.
_FILE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "MultiEdit"}

# Raízes de FORA do checkout cuja LEITURA é permitida: logs do próprio kimi
# (`~/.kimi-code/sessions/.../output.log`, gerados a cada Bash tool) e temporários
# criados pelos robôs (ex.: log de servidor em /tmp). Escrita fora do checkout
# continua sempre bloqueada — a leitura liberada é só para inspecionar a saída
# de comandos que o próprio robô executou.
_READABLE_EXTRA_ROOTS = (
    os.path.join(os.path.expanduser("~"), ".kimi-code", "sessions"),
    tempfile.gettempdir(),
    "/var/tmp",
)

# Arquivos de instrução (AGENTS.md/CLAUDE.md) cuja LEITURA é permitida de qualquer
# lugar. O runtime do agente instrui ler AGENTS.md que cobrem caminhos tocados por
# tool calls (ex.: ao rodar `./gradlew` que toca `~/.gradle`, ele manda ler os
# AGENTS.md ancestrais) — e o workspace da autoia fica DENTRO do repo, então o
# robô tenta ler o AGENTS.md da plataforma (fora do checkout). São documentação
# benigna: ler não vaza código nem permite escrita (que segue sempre bloqueada).
_INSTRUCTION_FILENAMES = {"AGENTS.md", "CLAUDE.md"}


@dataclass
class GuardrailViolation:
    pattern: str
    detail: str


def extract_command(arguments: str) -> str | None:
    """Extrai o comando do argumento JSON de uma tool call `Bash`."""
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("command"), str):
        return data["command"]
    return None


def extract_file_path(arguments: str) -> str | None:
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # aceita snake_case (kimi) e camelCase (opencode: filePath)
    for key in ("path", "file_path", "target_path", "filePath", "targetPath"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def path_is_within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def _read_allowed_extra(path: str) -> bool:
    """Leitura fora do checkout permitida apenas sob raízes de logs/temporários."""
    if not os.path.isabs(path):
        return False
    real = os.path.realpath(path)
    roots = [os.path.realpath(r) for r in _READABLE_EXTRA_ROOTS if r]
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def _read_allowed_instruction(path: str) -> bool:
    """Leitura de arquivos de instrução (AGENTS.md/CLAUDE.md) é permitida de
    qualquer lugar: o runtime do agente manda ler os que cobrem os caminhos que
    ele toca (o workspace fica dentro do repo da autoia). Escrita nunca."""
    return os.path.basename(os.path.realpath(path)) in _INSTRUCTION_FILENAMES


# Ferramentas de inspeção SOMENTE-LEITURA: o termo procurado nunca é executado
# (ex.: `grep -n "curl" gradlew` só procura a palavra — não invoca curl).
# Sem essa exceção, buscas por palavras arriscadas são falsos positivos.
_READONLY_TOOLS = {
    "grep", "rg", "ag", "cat", "head", "tail", "wc",
    "less", "more", "diff", "sort", "uniq", "comm", "cut",
}

# Subcomandos git somente-leitura (grep/log/show/diff/status etc. não executam
# o termo; `git checkout main`/`git push` continuam bloqueados).
_READONLY_GIT_SUBCOMMANDS = {
    "grep", "log", "show", "diff", "status", "branch", "rev-parse",
    "ls-files", "ls-tree", "remote", "config", "blame",
}


# Padrões de rede cujo bloqueio pode ser afrouxado por `whitelisted_hosts`
# (curl/wget para hosts de registro de pacotes é legítimo em build/CI).
_NETWORK_PATTERNS = {r"\bcurl\b", r"\bwget\b"}

# Loopback é SEMPRE permitido para curl/wget: não é rede externa nem exfiltração.
# Robôs legitimamente fazem health check de serviços locais (ex.: ponte 127.0.0.1).
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_URL_RE = re.compile(r"https?://([^/\s'\"]+)", re.IGNORECASE)


def extract_network_targets(command: str) -> list[str]:
    """Hosts alvo de curl/wget no comando (sem porta), minúsculos.
    Vazio se não houver URL explícita."""
    hosts: list[str] = []
    for m in _URL_RE.finditer(command):
        netloc = m.group(1).lower().rstrip(".,;:)")
        if netloc.startswith("["):  # IPv6: [::1]:8080
            host = netloc[1 : netloc.find("]")] if "]" in netloc else netloc
        else:
            host = netloc.split(":", 1)[0]  # remove porta: host:port
        if host:
            hosts.append(host)
    return hosts


def _network_allowed(command: str, whitelisted_hosts: list[str]) -> bool:
    """True se TODOS os alvos http(s) do comando estão na whitelist de hosts
    (ou são loopback — sempre permitidos)."""
    targets = extract_network_targets(command)
    if not targets:
        return False
    allowed = (
        {h.lower().lstrip(".") for h in whitelisted_hosts if h}
        | {h.lstrip(".") for h in _LOOPBACK_HOSTS}
    )
    return all(t.lstrip(".") in allowed for t in targets)


def check_command(
    command: str,
    patterns: list[str],
    whitelisted_hosts: list[str] | None = None,
) -> GuardrailViolation | None:
    if not command or not command.strip():
        return None
    tokens = command.lstrip().split()
    first = tokens[0]
    if first in _READONLY_TOOLS:
        return None
    if first == "git" and len(tokens) > 1 and tokens[1] in _READONLY_GIT_SUBCOMMANDS:
        return None
    whitelisted_hosts = whitelisted_hosts or []
    for pattern in patterns:
        if pattern in _NETWORK_PATTERNS and _network_allowed(command, whitelisted_hosts):
            continue
        if re.search(pattern, command):
            return GuardrailViolation(pattern=pattern, detail=command[:300])
    return None


def check_tool_call(
    tool_call: dict,
    patterns: list[str],
    checkout_path: str | None = None,
    whitelisted_hosts: list[str] | None = None,
) -> GuardrailViolation | None:
    """Avalia uma tool call do kimi contra a política. Retorna violação ou None."""
    function = tool_call.get("function") or {}
    name = function.get("name", "")
    arguments = function.get("arguments", "")

    if name == "Bash":
        command = extract_command(arguments)
        return check_command(command or "", patterns, whitelisted_hosts)

    if name in _FILE_TOOLS:
        path = extract_file_path(arguments)
        if path and checkout_path and not path_is_within(path, checkout_path):
            # Leitura de logs do próprio kimi/temporários é permitida (o robô precisa
            # inspecionar a saída de comandos que ele mesmo executou); escrita não.
            if name in ("Read", "Grep") and (
                _read_allowed_extra(path) or _read_allowed_instruction(path)
            ):
                return None
            return GuardrailViolation(
                pattern="path-outside-workspace",
                detail=f"{name} {path} (fora de {checkout_path})",
            )
    return None
