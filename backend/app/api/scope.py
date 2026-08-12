"""Escopo de visibilidade de tasks para endpoints agregados (ex.: /api/execution).

Regra que espelha a atuação de `_ensure_can_act` (tasks.py): com auth ON e sem
filtro explícito, o usuário enxerga uma task se participa do projeto dela, OU é
o `responsible_id`, OU a task não tem responsável ("sem responsável = qualquer
autenticado atua"). Filtro explícito por projeto e auth OFF preservam o
comportamento atual.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import or_
from sqlalchemy.orm import Query

from ..models import Task


def task_scope_filter(
    repo_ids: list[int] | None,
    user_id: int | None,
    repository_id: int | None = None,
) -> Any | None:
    """Condição SQLAlchemy do escopo de visibilidade; `None` = sem filtro (global).

    - `repository_id` explícito → só tasks do projeto (comportamento preservado);
    - auth ON sem filtro → participa do projeto OU é responsável OU sem responsável;
    - auth OFF (`user_id=None`) → `None` (global).
    """
    if repository_id is not None:
        return Task.repository_id == repository_id
    if user_id is not None:
        return or_(
            Task.repository_id.in_(repo_ids or []),
            Task.responsible_id == user_id,
            Task.responsible_id.is_(None),
        )
    return None


def make_scoped(
    repo_ids: list[int] | None,
    user_id: int | None,
    repository_id: int | None = None,
) -> Callable[[Query], Query]:
    """Factory de callable `q -> q` que aplica o escopo a uma query com `Task`."""
    cond = task_scope_filter(repo_ids, user_id, repository_id)

    def _scoped(q: Query) -> Query:
        return q.filter(cond) if cond is not None else q

    return _scoped
