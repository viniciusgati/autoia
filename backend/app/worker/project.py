"""Detecção de características do projeto (linguagem, comandos de teste sugeridos).

Os robôs recebem esse resumo no prompt para não precisarem adivinhar o ecossistema
(hoje o tester tenta `pytest`, `npm test`, `go test`... às cegas).
"""

from __future__ import annotations

import os

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
