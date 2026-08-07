"""Orçamento por tarefa.

v1: custo estimado por interação (AUTOIA_COST_PER_INTERACTION). Se no futuro o
stream-json expuser `usage` real de tokens, substitui-se a estimativa pelo valor real.
"""

from __future__ import annotations

from .config import Settings


def interaction_cost(settings: Settings) -> float:
    return settings.cost_per_interaction


def budget_exceeded(cost_spent: float, budget_limit: float) -> bool:
    return (cost_spent or 0.0) >= (budget_limit or 0.0)
