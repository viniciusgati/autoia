"""Prioridade do prompt de subtarefa (regressão da task-279).

A instrução de retomada é task-level e pode ser de uma subtarefa ANTERIOR. Ela
não pode sobrepor a subtarefa atual nem esconder o relatório de verificação
dessa subtarefa — era isso que fazia o developer revalidar a subtarefa 3 e
declarar a 4 como "já implementada" sem código.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.worker.subtask import _build_subtask_implement_prompt


def test_subtarefa_atual_e_relatorio_prevalecem_sobre_instrucao_historica():
    task = SimpleNamespace(
        title="T10 — Acessibilidade",
        description="descrição da história",
        acceptance_criteria="critérios gerais",
        steps=[],
        subtasks=[],
        resume_instruction="Corrija a subtarefa 3: Foco, teclado e diálogos.",
    )
    subtask = SimpleNamespace(
        position=3,
        title="Alvos de toque >= 44 px",
        description="ajustar os alvos",
        acceptance_criteria="testes a11y-layout e a11y-foco",
        summary="FAIL — faltou tests/a11y-layout.test.js",
        error=None,
    )

    prompt = _build_subtask_implement_prompt(
        task,
        subtask,
        "node/npm",
        "main",
        "autoia/task-1",
        "/tmp/opencode/checkout-inexistente",
    )

    # A subtarefa atual vem DEPOIS da instrução histórica e é marcada como
    # prioridade; o relatório da verificação dela está no prompt.
    pos_instrucao = prompt.index("## Intervenção do usuário (retomada)")
    pos_subtarefa = prompt.index("## Subtarefa 4: Alvos de toque >= 44 px")
    assert pos_instrucao < pos_subtarefa
    assert "Prioridade máxima" in prompt
    assert "Se ela citar outra subtarefa, ignore-a" in prompt
    assert "### Relatório da verificação anterior desta subtarefa" in prompt
    assert "FAIL — faltou tests/a11y-layout.test.js" in prompt
