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
    for key in ("path", "file_path", "target_path"):
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


def check_command(command: str, patterns: list[str]) -> GuardrailViolation | None:
    if not command or not command.strip():
        return None
    for pattern in patterns:
        if re.search(pattern, command):
            return GuardrailViolation(pattern=pattern, detail=command[:300])
    return None


def check_tool_call(
    tool_call: dict,
    patterns: list[str],
    checkout_path: str | None = None,
) -> GuardrailViolation | None:
    """Avalia uma tool call do kimi contra a política. Retorna violação ou None."""
    function = tool_call.get("function") or {}
    name = function.get("name", "")
    arguments = function.get("arguments", "")

    if name == "Bash":
        command = extract_command(arguments)
        return check_command(command or "", patterns)

    if name in _FILE_TOOLS:
        path = extract_file_path(arguments)
        if path and checkout_path and not path_is_within(path, checkout_path):
            # Leitura de logs do próprio kimi/temporários é permitida (o robô precisa
            # inspecionar a saída de comandos que ele mesmo executou); escrita não.
            if name in ("Read", "Grep") and _read_allowed_extra(path):
                return None
            return GuardrailViolation(
                pattern="path-outside-workspace",
                detail=f"{name} {path} (fora de {checkout_path})",
            )
    return None
