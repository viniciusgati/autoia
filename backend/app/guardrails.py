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

# Diretórios onde o CLI do agente guarda as skills/plugins que ele lê para se
# instruir (ex.: `~/.kimi-code/plugins/managed/kimi-webbridge/skills/.../references/operations.md`,
# `SKILL.md`). É documentação benigna — leitura não vaza código do projeto nem
# permite escrita (que segue sempre bloqueada). Restrita a arquivos `.md` para
# não expor configs/binários dos plugins (que podem conter segredos).
_AGENT_DOC_ROOTS = (
    os.path.join(os.path.expanduser("~"), ".kimi-code", "skills"),
    os.path.join(os.path.expanduser("~"), ".kimi-code", "plugins"),
    os.path.join(os.path.expanduser("~"), ".config", "opencode", "skills"),
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


def _read_allowed_agent_docs(path: str) -> bool:
    """Leitura da documentação das skills do próprio CLI (SKILL.md, references/*.md).

    O runtime do agente lê esses arquivos para se instruir ao usar uma skill
    (ex.: kimi-webbridge) — sem isso qualquer uso de skill com referências fora
    do checkout é bloqueado como path-outside-workspace. Restrito a `.md`.
    """
    if not os.path.isabs(path):
        return False
    if not os.path.realpath(path).lower().endswith((".md", ".markdown")):
        return False
    real = os.path.realpath(path)
    return any(
        real == os.path.realpath(root) or real.startswith(os.path.realpath(root) + os.sep)
        for root in _AGENT_DOC_ROOTS
        if root
    )


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

# Verbos de shell cuja intenção é BUSCA/inspeção de arquivos ou termos — base do
# detector de loop semântico (repetição da MESMA busca é sinal de agente perdido).
_SEARCH_COMMANDS = {"find", "grep", "rg", "ag", "locate", "findstr", "tree", "which", "type"}

# Ferramentas de busca do agente (opencode/kimi) que entram no detector.
_SEARCH_TOOLS = {"read", "glob", "grep"}

_QUOTED_TOKEN_RE = re.compile(r"""['"]([^'"]{2,})['"]""", re.IGNORECASE)


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
                _read_allowed_extra(path)
                or _read_allowed_instruction(path)
                or _read_allowed_agent_docs(path)
            ):
                return None
            return GuardrailViolation(
                pattern="path-outside-workspace",
                detail=f"{name} {path} (fora de {checkout_path})",
            )
    return None


def _path_target(path: str) -> str:
    """Alvo de um path/pattern de leitura para o detector de loop: os 3 últimos
    componentes, minúsculos.

    Basename sozinho colide homônimos legítimos (ex.: `page.tsx` em rotas
    diferentes do Next.js — a task 143 morreu por ler 7 `page.tsx` distintos),
    e o caminho completo depende do checkout (absoluto no opencode, relativo no
    kimi). Os 3 últimos componentes diferenciam arquivos iguais em diretórios
    diferentes e ainda colapsam relativo/absoluto do mesmo arquivo.
    """
    parts = [p for p in path.rstrip("/\\").replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    return "/".join(parts[-3:]).lower()[:120]


def _search_fingerprint(tool: str, arguments) -> str | None:
    """Fingerprint da INTENÇÃO de busca de uma tool call, para o detector de loop.

    Retorna uma string que identifica o alvo procurado (arquivo/termo/direcionário),
    ou None quando a chamada não é uma busca (não entra no detector). Chamadas de
    busca com o MESMO alvo — mesmo que por caminhos/argumentos diferentes — geram o
    MESMO fingerprint (ex.: `find /home -name "autoia_verdict.txt"` e
    `find / -name "autoia_verdict.txt"` colapsam em `bash:find:autoia_verdict.txt`).
    """
    if not isinstance(tool, str) or not tool.strip():
        return None
    tool = tool.strip().lower()

    if tool in _SEARCH_TOOLS:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            return None
        if tool == "grep":
            term = arguments.get("pattern") or arguments.get("query")
            if not isinstance(term, str) or not term.strip():
                return None
            return f"{tool}:{term.strip()[:120].lower()}"
        path = arguments.get("path") or arguments.get("filePath") or arguments.get("pattern")
        if not isinstance(path, str) or not path.strip():
            return None
        return f"{tool}:{_path_target(path)}"

    if tool in ("bash", "shell"):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        command = (arguments or {}).get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not command.strip():
            return None
        tokens = command.lstrip().split()
        verb = tokens[0].lower().split("/")[-1]
        if verb not in _SEARCH_COMMANDS:
            return None
        quoted = _QUOTED_TOKEN_RE.findall(command)
        if quoted:
            return f"{verb}:{quoted[-1].strip().lower()[:120]}"
        for token in reversed(tokens[1:]):
            cleaned = token.strip("'\"").rstrip("/\\")
            if cleaned and not cleaned.startswith("-") and cleaned not in ("|", "&&", ";", "||"):
                return f"{verb}:{os.path.basename(cleaned).lower()[:120]}"
        return verb
    return None


# Orientação ao robô quando uma execução é interrompida — vira prompt da
# re-execução (o robô precisa saber o que fez de errado para NÃO repetir).
# Determinística (sem LLM): o motivo real vem do guardrail que matou a execução.
_GUIDANCE_BY_PATTERN: dict[str, str] = {
    "repeated-search": (
        "Você repetiu a MESMA busca várias vezes pelo mesmo alvo e o sistema "
        "interrompeu a execução para não gastar recursos em loop. O alvo "
        "provavelmente não existe ou está em outro caminho/formato. NÃO repita "
        "a mesma busca: confirme o caminho com `ls`/glob, use um termo mais "
        "amplo, procure por outro arquivo — ou siga sem esse artefato. Se o "
        "dado realmente não existe, registre a pendência no texto final em vez "
        "de insistir."
    ),
    "identical-calls": (
        "Você repetiu a MESMA chamada de ferramenta várias vezes seguidas e o "
        "sistema interrompeu a execução para não gastar recursos em loop. Pare "
        "de repetir a chamada idêntica: mude a estratégia (outro comando, "
        "arquivo ou abordagem) ou siga adiante."
    ),
    "path-outside-workspace": (
        "Você tentou acessar um caminho FORA do checkout e o sistema "
        "interrompeu a execução. Trabalhe SOMENTE dentro do diretório do "
        "repositório; para artefatos temporários (screenshots, logs) use um "
        "diretório dentro do próprio checkout (ex.: `autoia_screenshots/`)."
    ),
}

_VIOLATION_RE = re.compile(r"^guardrail:\s*([\w-]+)\s*:\s*(.*)$", re.DOTALL)

# Marcadores textuais de timeout no motivo do abort (kimi/opencode/codex).
_TIMEOUT_MARKERS = ("timeout", "tempo limite", "sem progresso", "stall")


def parse_violation(reason: str | None) -> tuple[str, str] | None:
    """Extrai `(pattern, detail)` de um motivo no formato `guardrail: <p>: <d>`."""
    match = _VIOLATION_RE.match((reason or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def interruption_guidance(reason: str | None) -> str:
    """Seção de prompt (PT-BR) explicando por que a execução anterior foi
    interrompida e o que fazer diferente.

    Retorna "" quando o motivo não tem orientação específica (ex.: erro de
    commit/exit code) — nesses casos o motivo cru já entra no prompt/handoff.
    """
    text = (reason or "").strip()
    if not text:
        return ""
    parsed = parse_violation(text)
    if parsed:
        pattern, detail = parsed
        guidance = _GUIDANCE_BY_PATTERN.get(
            pattern,
            "A execução anterior foi interrompida pelo guardrail. Reveja o que "
            "estava fazendo e mude a abordagem — não repita o comportamento.",
        )
        header = (
            "## Execução anterior desta etapa foi interrompida pelo guardrail\n\n"
            f"- Motivo: `{pattern}` — {detail or '(sem detalhe)'}\n"
        )
        return f"{header}\n### O que fazer diferente\n{guidance}"
    lowered = text.lower()
    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return (
            "## Execução anterior desta etapa foi interrompida (tempo)\n\n"
            f"- Motivo: {text[:300]}\n\n"
            "### O que fazer diferente\n"
            "A execução estourou o limite de tempo ou ficou sem progresso. NÃO "
            "repita a mesma sequência longa: simplifique o caminho (rode apenas "
            "a suíte rápida/headless, evite subir emulador/navegador/servidor "
            "externo), faça commits menores e registre no texto final o que foi "
            "pulado e por quê."
        )
    return ""


class SemanticLoopTracker:
    """Detecta repetição de INTENÇÃO de busca dentro de UMA execução do executor.

    Complementa o watchdog de tool calls idênticas (`max_identical_calls`): este
    conta o MESMO alvo procurado N vezes (mesmo com comandos/caminhos diferentes),
    sinal clássico de agente perdido caçando relatório/arquivo inexistente.
    `max_repeats` = limite; 0 desliga. Retorna uma `GuardrailViolation` ao estourar.
    """

    def __init__(self, max_repeats: int):
        self.max_repeats = max(0, int(max_repeats or 0))
        self._counts: dict[str, int] = {}

    def register(self, tool: str, arguments) -> GuardrailViolation | None:
        if self.max_repeats <= 0:
            return None
        fp = _search_fingerprint(tool, arguments)
        if fp is None:
            return None
        count = self._counts.get(fp, 0) + 1
        self._counts[fp] = count
        if count >= self.max_repeats:
            return GuardrailViolation(
                pattern="repeated-search",
                detail=f"busca repetida {count}x pelo mesmo alvo: {fp}",
            )
        return None
