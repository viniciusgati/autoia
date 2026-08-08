"""Contrato de veredicto e parse da história (po).

Robôs de verificação (qa, tester, pm) escrevem `autoia_verdict.txt` na raiz do checkout;
o worker lê, apaga e decide o rumo. O po (refine) emite a história no texto final com
marcadores `## Descrição` e `## Critérios de aceite`.
"""

from __future__ import annotations

import os
import re

VERDICT_FILENAME = "autoia_verdict.txt"

# Veredictos esperados por papel
V_READY = "READY"
V_NEEDS_WORK = "NEEDS_WORK"
V_PASS = "PASS"
V_FAIL = "FAIL"

# Decisões do PM
PM_RETRY = "retry"
PM_CONTINUE = "continue"
PM_ESCALATE = "escalate"


def verdict_path(checkout: str) -> str:
    return os.path.join(checkout, VERDICT_FILENAME)


def read_verdict(checkout: str) -> str | None:
    path = verdict_path(checkout)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().strip()


def remove_verdict(checkout: str) -> None:
    try:
        os.remove(verdict_path(checkout))
    except FileNotFoundError:
        pass


def _find_marker(raw: str | None, *markers: str) -> str | None:
    """Procura um marcador como palavra isolada em QUALQUER linha (tolerante a preâmbulo)."""
    if not raw:
        return None
    for line in raw.splitlines():
        words = line.strip().upper().split()
        if not words:
            continue
        word = words[0].rstrip(":")
        for marker in markers:
            if word == marker:
                return marker
    return None


def parse_pass_fail(raw: str | None) -> str | None:
    return _find_marker(raw, V_PASS, V_FAIL)


def parse_ready_work(raw: str | None) -> str | None:
    return _find_marker(raw, V_READY, V_NEEDS_WORK)


def parse_pm_decision(raw: str | None) -> dict:
    """Extrai a decisão do PM do veredicto. Inválido/ausente → escalar (seguro)."""
    if not raw:
        return {"action": PM_ESCALATE, "position": None, "reason": "sem veredicto do PM"}
    text = raw.strip()
    reason = ""
    match = re.search(r"MOTIVO:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if match:
        reason = match.group(1).strip()[:500]

    retry = re.search(r"DECISÃO:\s*retry\s*(\d+)", text, re.IGNORECASE)
    if retry:
        return {"action": PM_RETRY, "position": int(retry.group(1)), "reason": reason or "retry indicado"}
    if re.search(r"DECISÃO:\s*continuar", text, re.IGNORECASE):
        return {"action": PM_CONTINUE, "position": None, "reason": reason or "continuar com mais orçamento"}
    if re.search(r"DECISÃO:\s*escalar", text, re.IGNORECASE):
        return {"action": PM_ESCALATE, "position": None, "reason": reason or "escalado pelo PM"}
    return {"action": PM_ESCALATE, "position": None, "reason": f"decisão inválida do PM: {text[:300]}"}


def parse_story(text: str) -> tuple[str, str]:
    """Separa descrição e critérios de aceite da história escrita pelo po."""
    description = text.strip()
    criteria = ""
    marker = "## Critérios de aceite"
    if marker.lower() in text.lower():
        # acha a posição real do marcador (case-insensitive)
        lowered = text.lower()
        idx = lowered.index(marker.lower())
        criteria = text[idx + len(marker) :].strip()
        description = text[:idx].strip()
        # remove possível "## Descrição" duplicado do começo da descrição
        desc_marker = "## Descrição"
        if desc_marker.lower() in description.lower():
            didx = description.lower().index(desc_marker.lower())
            description = description[didx + len(desc_marker) :].strip()
    return description, criteria


def _split_subtasks(text: str) -> list[str]:
    """Separa o bloco de subtarefas em blocos por `### Subtarefa N:`."""
    import re
    # encontra divisões por "### Subtarefa N:" (case-insensitive, com número ou sem)
    pattern = re.compile(r"(?=^###\s+Subtarefa\s+\d+:)", re.MULTILINE | re.IGNORECASE)
    parts = pattern.split(text)
    return [p.strip() for p in parts if p.strip()]


def parse_subtasks(text: str) -> list[dict]:
    """Extrai subtarefas do texto (bloco `## Plano de implementação`).

    Retorna lista de dicts com title, description, acceptance_criteria.
    Se não houver plano, retorna lista vazia.
    """
    marker = "## Plano de implementação"
    lowered = text.lower()
    idx = lowered.find(marker.lower())
    if idx == -1:
        return []

    # pega do marcador até o próximo `## ` de nível 2 ou fim do texto
    section = text[idx + len(marker):]
    next_h2 = re.search(r"\n##\s", section)
    if next_h2:
        section = section[:next_h2.start()]

    blocks = _split_subtasks(section.strip())
    if not blocks:
        return []

    subtasks: list[dict] = []
    pattern = re.compile(r"^###\s+Subtarefa\s+(\d+):\s*(.+)", re.IGNORECASE)
    for block in blocks:
        match = pattern.match(block)
        if not match:
            continue
        title = match.group(2).strip()
        body = block[match.end():].strip()

        # extrai Escopo
        scope_match = re.search(r"\*\*Escopo:\*\*\s*(.+?)(?=\n\*\*Critérios|\Z)", body, re.DOTALL)
        description = scope_match.group(1).strip() if scope_match else ""

        # extrai Critérios
        criteria_match = re.search(r"\*\*Critérios:\*\*\s*(.+?)(?=\n###\s+Subtarefa|\Z)", body, re.DOTALL)
        acceptance_criteria = criteria_match.group(1).strip() if criteria_match else ""

        subtasks.append({
            "title": title,
            "description": description,
            "acceptance_criteria": acceptance_criteria,
        })
    return subtasks
