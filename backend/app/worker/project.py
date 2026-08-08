"""Detecção de características do projeto (linguagem, comandos de teste sugeridos).

Os robôs recebem esse resumo no prompt para não precisarem adivinhar o ecossistema
(hoje o tester tenta `pytest`, `npm test`, `go test`... às cegas).
"""

from __future__ import annotations

import os

from . import gitops

# (marcador de arquivo, linguagem/ecossistema, comandos de teste sugeridos)
_RULES: list[tuple[str, str, list[str]]] = [
    ("package.json", "node/npm", ["npm test", "npx jest", "npx vitest"]),
    ("pytest.ini", "python", ["pytest"]),
    ("pyproject.toml", "python", ["pytest"]),
    ("setup.py", "python", ["pytest"]),
    ("requirements.txt", "python", ["pytest"]),
    ("go.mod", "go", ["go test ./..."]),
    ("Cargo.toml", "rust", ["cargo test"]),
    ("Makefile", "make", ["make test"]),
    ("build.gradle", "java/gradle", ["./gradlew test"]),
    ("pom.xml", "java/maven", ["mvn test"]),
    ("Package.swift", "swift", ["swift test"]),
    ("Gemfile", "ruby", ["bundle exec rspec"]),
]


def detect_project(checkout: str) -> str:
    """Retorna um resumo do ecossistema detectado (ou '' se nada for detectado)."""
    try:
        entries = os.listdir(checkout)
    except OSError:
        return ""
    lower_names = {name.lower() for name in entries}

    found: list[tuple[str, str, list[str]]] = []
    for marker, lang, cmds in _RULES:
        if marker in lower_names:
            found.append((lang, cmds, marker))
        elif marker.startswith(".") and any(name.endswith(marker) for name in lower_names):
            found.append((lang, cmds, marker))

    if not found:
        return ""

    lines = ["Linguagem/ecossistema detectado no repositório:"]
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for lang, cmds, marker in found:
        key = (lang, tuple(cmds))
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"- {lang} (indicado por {marker}); comandos de teste sugeridos: "
            + ", ".join(cmds)
        )
    return "\n".join(lines)


# Regra de banco de dados incluída no AGENTS.md gerado: PostgreSQL é o padrão para
# projetos de verdade; SQLite fica restrito a testes rápidos/em memória. Sobrescreva
# via AUTOIA_DB_RULE (ex.: projetos que exigem outro banco por contrato).
DEFAULT_DATABASE_RULE = (
    "- Use PostgreSQL como banco de dados padrão do projeto (produção e desenvolvimento).\n"
    "- NÃO use SQLite para armazenar dados de verdade: reserve-o apenas para testes rápidos "
    "em memória, se o framework suportar.\n"
    "- Se o projeto já possui uma camada de dados definida (conexão, migrations, ORM), "
    "siga-a e não troque o banco sem motivo.\n"
    "- Se o projeto ainda não tem banco, configure PostgreSQL desde o início (ex.: "
    "variável de ambiente de conexão + driver oficial + migrations versionadas) — "
    "não escolha SQLite por conveniência."
)

# AGENTS.md gerado pela autoia na raiz de cada checkout, antes de cada execução do kimi.
# O kimi lê AGENTS.md nativamente do diretório de trabalho; o arquivo NÃO é versionado
# (excluído via .git/info/exclude) para nunca entrar no histórico do repositório.
AGENTS_MD_TEMPLATE = """# AGENTS.md — guia para agentes que trabalham neste repositório

Este arquivo é gerado pela autoia em cada checkout e NÃO é versionado. Siga as
instruções abaixo além do que estiver no seu prompt.

## Tecnologia deste projeto
{project_info}

## Banco de dados
{db_rule}

## Padrão obrigatório
- Trabalhe SOMENTE na tecnologia deste repositório (linguagem, framework, bibliotecas,
  estrutura e convenções de código). Não introduza outra linguagem, framework ou padrão
  diferente do que o projeto já usa.
- Siga o estilo, a organização e a nomenclatura dos arquivos existentes.
- Use a suíte de testes existente e os comandos dela; não crie um ecossistema paralelo
  de ferramentas.
- Não refatore código não relacionado à tarefa."""

AGENTS_MD_NO_STACK = (
    "Nenhum ecossistema específico foi detectado. Identifique a linguagem, framework e "
    "convenções já existentes no repositório e siga-as — não introduza outra stack."
)


def build_agents_md(project_info: str, db_rule: str = DEFAULT_DATABASE_RULE) -> str:
    """Conteúdo do AGENTS.md gerado: stack detectada + regra de banco + regras de padrão."""
    return AGENTS_MD_TEMPLATE.format(
        project_info=project_info or AGENTS_MD_NO_STACK, db_rule=db_rule
    )


def ensure_agents_md(checkout: str, project_info: str, db_rule: str = DEFAULT_DATABASE_RULE) -> None:
    """Escreve um AGENTS.md não versionado na raiz do checkout com a stack do projeto.

    Se o repositório já versiona um AGENTS.md próprio, o dele prevalece (não
    sobrescrevemos). O arquivo gerado é excluído do commit via .git/info/exclude,
    então `git add -A` dos robôs nunca o versiona. Best-effort: o chamador decide
    o que fazer com OSError/GitError.
    """
    if gitops.is_tracked(checkout, "AGENTS.md"):
        return
    with open(os.path.join(checkout, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write(build_agents_md(project_info, db_rule))
    exclude_local(checkout, "AGENTS.md")


def exclude_local(checkout: str, name: str) -> None:
    """Garante `name` no .git/info/exclude do checkout (idempotente)."""
    exclude_path = os.path.join(checkout, ".git", "info", "exclude")
    try:
        with open(exclude_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    if name in lines:
        return
    lines.append(name)
    with open(exclude_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
