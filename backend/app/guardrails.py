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
import shlex
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

# Opções de `find` que carregam o alvo procurado. `-name`/`-iname` vêm antes de
# `-path` de propósito: `find / -path /proc -prune -o -name "x"` procura `x`,
# não `/proc` (o antigo "último token citado" acertava isso por acidente).
_FIND_TARGET_FLAGS = (
    "-name", "-iname", "-regex", "-iregex",
    "-lname", "-ilname", "-path", "-ipath", "-wholename", "-iwholename",
)

# Opções de grep/rg/ag que consomem o PRÓXIMO token como valor (não é o termo
# buscado). Sem essa lista, `grep --include "*.js" "termo"` fingerprintaria o
# glob e dois termos diferentes colidiriam.
_GREP_VALUE_OPTIONS = {
    "-e", "--regexp", "-f", "--file", "-m", "--max-count",
    "-A", "--after-context", "-B", "--before-context", "-C", "--context",
    "--include", "--exclude", "--exclude-dir", "--include-dir",
    "-g", "--glob", "-t", "--type", "-T", "--type-not",
    "--color", "--colour", "--label", "--binary-files", "--devices",
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


def _read_range_suffix(arguments: dict) -> str:
    """Identifica o TRECHO lido, para não tratar offsets diferentes como loop.

    Ler `main.js` de ~7 mil linhas em 6 blocos distintos é exploração legítima;
    repetir o MESMO bloco é que é sinal de loop. Sem offset/limit explícitos o
    fingerprint permanece igual ao anterior (leitura do arquivo inteiro).
    """
    start = arguments.get("offset", arguments.get("start_line", arguments.get("start")))
    end = arguments.get("limit", arguments.get("end_line", arguments.get("end")))
    if start is None and end is None:
        return ""

    def _norm(value) -> str:
        if value is None:
            return ""
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)[:20]

    return f":{_norm(start) if start is not None else '0'}:{_norm(end)}"


def _shell_segments(command: str) -> list[list[str]]:
    """Divide um comando shell em segmentos de tokens, respeitando aspas.

    Operadores (`;`, `&&`, `||`, `|`) separam segmentos. O `shlex` evita que um
    `|`/`;` DENTRO de uma aspa quebre o padrão de busca. É o que impede
    `grep ...; echo "EXIT:$?"` de fingerprintar o `$?` do echo.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and not token.strip(";&|"):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _find_target(tokens: list[str]) -> str | None:
    args = tokens[1:]
    for flag in _FIND_TARGET_FLAGS:
        for index, token in enumerate(args[:-1]):
            if token == flag:
                return args[index + 1]
    return None


def _grep_target(tokens: list[str]) -> str | None:
    args = tokens[1:]
    # Forma explícita (`grep -e "termo"`) sempre vence.
    for index, token in enumerate(args):
        if token in ("-e", "--regexp") and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--regexp="):
            return token.split("=", 1)[1]

    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in _GREP_VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _grep_scope(tokens: list[str]) -> list[str]:
    """Arquivos/diretórios pesquisados por um `grep`/`rg`/`ag` de shell.

    O termo buscado NÃO é escopo. Ignora opções e os valores que elas consomem
    (`-e`, `--include`, `-A`, ...). Sem o escopo, todo `grep "perfil"` colapsa
    em `grep:perfil` e procurar o mesmo termo em arquivos DIFERENTES vira
    "busca repetida" (falso positivo da task-324).
    """
    args = tokens[1:]
    scope: list[str] = []
    positional: list[str] = []
    term_from_option = False
    skip_next = False
    for index, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in ("-e", "--regexp"):
            term_from_option = True
            skip_next = True
            continue
        if token.startswith("--regexp="):
            term_from_option = True
            continue
        if token in _GREP_VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positional.append(token)
    if term_from_option:
        scope = positional
    elif positional:
        # O primeiro posicional é o termo; os demais são os arquivos/dirs.
        scope = positional[1:]
    return scope


def _bash_search_target(tokens: list[str], verb: str) -> str | None:
    if verb == "find":
        target = _find_target(tokens)
    elif verb in ("grep", "rg", "ag", "findstr"):
        target = _grep_target(tokens)
    else:
        target = next((t for t in tokens[1:] if not t.startswith("-")), None)
    if not target:
        return None
    target = target.strip().rstrip("/\\")
    if not target:
        return None
    if verb in ("find", "locate", "tree", "which", "type"):
        target = os.path.basename(target)
    return target.lower()[:120]


def _bash_search_fingerprint(command: str) -> str | None:
    """Intenção da busca de um comando shell, por segmento.

    `grep "a" f; echo "EXIT:$?"` -> `grep:a` (não `grep:exit:$?`). Um comando
    com múltiplas buscas conta pela PRIMEIRA; o watchdog de chamadas idênticas
    cobre repetições literais do comando inteiro.
    """
    for tokens in _shell_segments(command):
        if not tokens:
            continue
        verb = tokens[0].lower().split("/")[-1]
        if verb == "git" and len(tokens) > 1 and tokens[1] == "grep":
            tokens = ["grep", *tokens[2:]]
            verb = "grep"
        if verb not in _SEARCH_COMMANDS:
            continue
        target = _bash_search_target(tokens, verb)
        if not target:
            return verb
        # grep/rg/ag: o ESCOPO (arquivo/diretório) diferencia procurar o mesmo
        # termo em fontes diferentes (exploração legítima) de repetir a MESMA
        # busca (loop). `find` mantém só o nome procurado: o alvo é o arquivo.
        if verb in ("grep", "rg", "ag", "findstr"):
            scope = _grep_scope(tokens)
            if scope:
                normalized = "|".join(_path_target(p) for p in scope[:4])
                return f"{verb}:{target}:{normalized}"
        return f"{verb}:{target}"
    return None


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
            fingerprint = f"{tool}:{term.strip()[:120].lower()}"
            # O mesmo termo em diretórios diferentes não é loop (task 143).
            path = arguments.get("path") or arguments.get("include")
            if isinstance(path, str) and path.strip():
                fingerprint += f":{_path_target(path)}"
            return fingerprint
        path = arguments.get("path") or arguments.get("filePath") or arguments.get("pattern")
        if not isinstance(path, str) or not path.strip():
            return None
        suffix = _read_range_suffix(arguments) if tool == "read" else ""
        return f"{tool}:{_path_target(path)}{suffix}"

    if tool in ("bash", "shell"):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        command = (arguments or {}).get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not command.strip():
            return None
        return _bash_search_fingerprint(command)
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
