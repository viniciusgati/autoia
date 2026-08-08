"""Documento de handoff entre fases (`autoia_handoff.md`).

O worker gera o documento no checkout ANTES de cada execução do kimi (steps e PM):
histórico COMPLETO das fases anteriores (sem truncar), o diff atual da branch e a
instrução da fase atual. O robô lê o arquivo e documenta o trabalho no TEXTO FINAL;
o worker persiste o texto final completo em `TaskStep.summary` (fonte de verdade do
histórico) e regenera o documento para a próxima fase.

O arquivo NÃO é versionado (excluído via .git/info/exclude, como o AGENTS.md gerado):
é o caderno de trabalho da task, não parte do repositório do usuário.
"""

from __future__ import annotations

import os

from . import project

HANDOFF_FILENAME = "autoia_handoff.md"


def handoff_path(checkout: str) -> str:
    return os.path.join(checkout, HANDOFF_FILENAME)


def build_handoff(
    *,
    task_id: int,
    task_title: str,
    task_status: str,
    branch: str,
    phase_sections: list[str],
    diff: str,
    current: str,
    feedback: str = "",
) -> str:
    """Monta o documento de handoff (markdown) para a fase atual."""
    parts = [
        "# Autoia — caderno de trabalho da tarefa (NÃO versionado)",
        "",
        "Este arquivo é o handoff entre as fases do pipeline. LEIA-O ANTES DE COMEÇAR:",
        "ele contém o histórico completo do que as fases anteriores fizeram, o diff",
        "atual da branch e o que esta fase deve documentar ao terminar.",
        "",
        f"- Tarefa #{task_id}: {task_title}",
        f"- Status: {task_status} | Branch: {branch}",
        "",
    ]
    if feedback:
        parts += [
            "## Feedback externo (do usuário/sistema)",
            feedback.strip(),
            "",
        ]
    parts += [
        "## Histórico das fases",
        "",
    ]
    if phase_sections:
        parts.extend(phase_sections)
    else:
        parts.append("_(nenhuma fase concluída ainda — você é a primeira)_")
    parts.append("")
    if diff:
        parts.append("## Diff atual da branch")
        parts.append("```")
        parts.append(diff)
        parts.append("```")
        parts.append("")
    parts.append("## Sua fase")
    parts.append(current)
    parts.append("")
    parts.append(
        "Ao terminar, documente esta fase no seu TEXTO FINAL (é o que a próxima fase "
        "verá no histórico). Não edite este arquivo."
    )
    return "\n".join(parts)


def write_handoff(checkout: str, content: str) -> None:
    """Escreve o handoff na raiz do checkout e o exclui do versionamento (best-effort)."""
    with open(handoff_path(checkout), "w", encoding="utf-8") as fh:
        fh.write(content)
    project.exclude_local(checkout, HANDOFF_FILENAME)
