"""Contrato de veredicto e parse da história (po).

Robôs de verificação (qa, tester, pm) escrevem `autoia_verdict.txt` na raiz do checkout;
o worker lê, apaga e decide o rumo. O po (refine) emite a história no texto final com
marcadores `## Descrição` e `## Critérios de aceite`.
"""

from __future__ import annotations

import json
import os
import re

VERDICT_FILENAME = "autoia_verdict.txt"

# Declaração de bloqueio do agente: quando ele NÃO consegue continuar com segurança
# (ambiguidade, decisão, dependência, permissão, ferramenta...), escreve este arquivo
# estruturado no checkout. O worker lê, apaga e marca a task/fase como `blocked`.
BLOCKED_FILENAME = "autoia_blocked.json"

# Resumo estruturado gerado pela LLM dedicada (contrato de saída).
SUMMARY_FILENAME = "autoia_summary.json"

# Resumo de UMA fase ("O que foi entregue") gerado pela LLM dedicada a resumo.
STEP_SUMMARY_FILENAME = "autoia_step_summary.json"

# Missão de UMA execução de fase ("por que esta execução existe") — LLM dedicada.
STEP_MISSION_FILENAME = "autoia_step_mission.json"

# Pedido de decisão do agente ao usuário (pausa a fase até o humano responder).
DECISION_FILENAME = "autoia_decision.json"

# Decisão de fechamento de etapa de um chamado (escrita pelo robô de avaliação no
# checkout; o chamado-worker lê, remove e aplica a transição de estágio).
CHAMADO_DECISION_FILENAME = "chamado_decision.json"

# Veredictos esperados por papel
V_READY = "READY"
V_NEEDS_WORK = "NEEDS_WORK"
V_PASS = "PASS"
V_FAIL = "FAIL"

# Decisões do PM
PM_RETRY = "retry"
PM_CONTINUE = "continue"
PM_ESCALATE = "escalate"

# Resultados aceitos do resumo (contrato autoia_summary.json).
SUMMARY_RESULTS = ("completed", "partial", "failed", "pending")


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


def read_block(checkout: str) -> dict | None:
    """Lê a declaração de bloqueio do agente (autoia_blocked.json), tolerante."""
    path = os.path.join(checkout, BLOCKED_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "reason_type": str(data.get("reason_type") or "other")[:50],
        "reason": str(data.get("reason") or "agente não conseguiu continuar")[:2000],
        "question": str(data.get("question") or "")[:2000],
    }


def remove_block(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, BLOCKED_FILENAME))
    except FileNotFoundError:
        pass


def read_summary(checkout: str) -> dict | None:
    """Lê o resumo gerado pela LLM (autoia_summary.json), tolerante e com defaults."""
    path = os.path.join(checkout, SUMMARY_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def _list(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value]
        return []

    result = str(data.get("result") or "").strip().lower()
    if result not in SUMMARY_RESULTS:
        result = None
    return {
        "summary": str(data.get("summary") or ""),
        "request": str(data.get("request") or "").strip() or None,
        "implementation": str(data.get("implementation") or "").strip() or None,
        "changes": _list("changes"),
        "result": result,
        "issues": _list("issues"),
        "files": _list("files"),
        "tasks_summary": str(data.get("tasks_summary") or "").strip() or None,
    }


def remove_summary(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, SUMMARY_FILENAME))
    except FileNotFoundError:
        pass


def read_step_summary(checkout: str) -> dict | None:
    """Lê o resumo de fase (autoia_step_summary.json), tolerante e com defaults."""
    path = os.path.join(checkout, STEP_SUMMARY_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def _list(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value]
        return []

    result = str(data.get("result") or "").strip().lower()
    if result not in SUMMARY_RESULTS:
        result = None
    return {
        "summary": str(data.get("summary") or ""),
        "changes": _list("changes"),
        "result": result,
        "issues": _list("issues"),
        "files": _list("files"),
    }


def remove_step_summary(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, STEP_SUMMARY_FILENAME))
    except FileNotFoundError:
        pass


def read_step_mission(checkout: str) -> dict | None:
    """Lê a missão da execução (autoia_step_mission.json), tolerante."""
    path = os.path.join(checkout, STEP_MISSION_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    mission = str(data.get("mission") or "").strip()
    if not mission:
        return None
    return {"mission": mission}


def remove_step_mission(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, STEP_MISSION_FILENAME))
    except FileNotFoundError:
        pass


def read_decision(checkout: str) -> dict | None:
    """Lê o pedido de decisão do agente (autoia_decision.json), tolerante."""
    path = os.path.join(checkout, DECISION_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    question = str(data.get("question") or data.get("reason") or "")
    options = data.get("options")
    if not isinstance(options, list):
        options = []
    options = [str(o) for o in options if str(o).strip()][:8]
    context = str(data.get("context") or "")[:2000]
    return {
        "question": question[:2000],
        "options": options,
        "context": context,
    }


def remove_decision(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, DECISION_FILENAME))
    except FileNotFoundError:
        pass


def read_chamado_decision(checkout: str) -> dict | None:
    """Lê a decisão de fechamento de etapa de um chamado (chamado_decision.json).

    Tolerante: `decision` pode ser `next_stage` (com `next_stage`), `resposta`
    (com `resposta_texto`), `cancelar` ou `concluir`; `justificativa` é opcional.
    Inválido/ausente → None (o worker trata como erro e mantém a etapa ativa).
    """
    path = os.path.join(checkout, CHAMADO_DECISION_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in ("next_stage", "resposta", "cancelar", "concluir"):
        return None
    return {
        "decision": decision,
        "next_stage": str(data.get("next_stage") or "").strip()[:100] or None,
        "resposta_texto": str(data.get("resposta_texto") or "").strip() or None,
        "justificativa": str(data.get("justificativa") or "").strip() or None,
    }


def remove_chamado_decision(checkout: str) -> None:
    try:
        os.remove(os.path.join(checkout, CHAMADO_DECISION_FILENAME))
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
