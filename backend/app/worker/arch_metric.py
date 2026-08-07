"""Métrica de mudança de arquitetura/deploy (evento `arch_metric`).

Heurística para sinalizar quando uma task mexe de forma drástica na arquitetura ou no
deploy do projeto: arquivos de container/CI/orquestração, dependências de framework,
migrations, volume de linhas e mudanças estruturais (muitos arquivos adicionados/
removidos). Pesos e limiares abaixo são calibráveis — ajuste junto com os testes.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .gitops import DiffChange

# (padrão, peso). Padrão terminando em "/" = prefixo de diretório (startswith);
# senão é fnmatch sobre o caminho. Adicionar/remover (A/D) dobra o peso.
_ARCH_PATTERNS: list[tuple[str, int]] = [
    # container / CI / orquestração / infra (peso 4)
    ("Dockerfile*", 4),
    ("docker-compose*", 4),
    ("compose*.yml", 4),
    ("compose*.yaml", 4),
    (".github/workflows/", 4),
    (".gitlab-ci.yml", 4),
    ("Jenkinsfile", 4),
    (".circleci/", 4),
    ("k8s/", 4),
    ("helm/", 4),
    ("terraform/", 4),
    ("infra/", 4),
    ("serverless.yml", 4),
    # dependências / build / deploy (peso 3)
    ("package.json", 3),
    ("pyproject.toml", 3),
    ("requirements*.txt", 3),
    ("Makefile*", 3),
    ("alembic/", 3),
    ("migrations/", 3),
    ("deploy/", 3),
    ("nginx*", 3),
    ("go.mod", 3),
    ("Cargo.toml", 3),
    ("Gemfile", 3),
    ("pom.xml", 3),
    ("build.gradle", 3),
    ("Procfile", 3),
    # configuração de build/scripts (peso 2)
    ("vite.config.*", 2),
    ("webpack.config.*", 2),
    ("next.config.*", 2),
    ("tsconfig.json", 2),
    ("scripts/", 2),
    ("bin/", 2),
]

# Limiares de nível (score 0-100).
_LEVEL_ALTO = 60
_LEVEL_MEDIO = 30


@dataclass
class ArchMetric:
    score: int
    level: str  # "alto" | "médio" | "baixo"
    reasons: list[str] = field(default_factory=list)


def _match(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatch(path, pattern)


def compute_arch_metric(changes: list[DiffChange]) -> ArchMetric:
    """Calcula a métrica a partir das mudanças do diff da branch."""
    points = 0
    reasons: list[str] = []
    structural = [c for c in changes if c.status in ("A", "D")]

    for change in changes:
        for pattern, weight in _ARCH_PATTERNS:
            if _match(pattern, change.path):
                multiplier = 2 if change.status in ("A", "D") else 1
                points += weight * multiplier
                reasons.append(f"{change.status} {change.path}")
                break

    total_lines = sum(c.added + c.deleted for c in changes)
    if total_lines >= 1000:
        points += 2
        reasons.append(f"volume grande de alteração ({total_lines} linhas)")
    if total_lines >= 3000:
        points += 2

    if len(changes) >= 5 and len(structural) >= 0.5 * len(changes):
        points += 3
        reasons.append(
            f"mudança estrutural ({len(structural)}/{len(changes)} arquivos adicionados/removidos)"
        )

    score = min(100, points * 10)
    if score >= _LEVEL_ALTO:
        level = "alto"
    elif score >= _LEVEL_MEDIO:
        level = "médio"
    else:
        level = "baixo"
    return ArchMetric(score=score, level=level, reasons=reasons)
