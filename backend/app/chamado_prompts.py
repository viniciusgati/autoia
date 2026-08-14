"""Prompts e contratos do fluxo de chamados (atendimento — independente da pipeline).

Duas famílias de execução:
1. **Ferramentas de etapa** (ex.: `assistente`, `escopo`): rodam o executor com um
   preset de instrução + contexto do chamado + o pedido do usuário. O robô pode ler
   arquivos do checkout (branch default, read-only) para entender o problema.
2. **Avaliação de fechamento**: ao fechar uma etapa, um robô lê o chamado + o
   transcript da etapa e escreve `chamado_decision.json` (contrato abaixo) — o
   worker valida contra `close_options` do catálogo e aplica a transição.

Também define os contratos de conteúdo de Projeto/Épico (resumo/escopo por LLM).
"""

from __future__ import annotations

from . import verdicts

# ── Presets de ferramentas por etapa (catálogo `allowed_tools`) ────────────
# Cada chave tem rótulo, descrição (para a UI) e instrução (vai no prompt).
TOOL_PRESETS: dict[str, dict] = {
    "assistente": {
        "label": "Assistente",
        "description": "Analisa o problema com o usuário e pode consultar o código-fonte do projeto para entender a causa.",
        "instruction": (
            "Você é o ASSISTENTE de atendimento de um chamado. Ajude o usuário a entender e "
            "resolver o problema relatado. Você está na branch default do repositório (somente "
            "leitura): use ferramentas para ler arquivos, buscar e investigar o código quando "
            "precisar embasar a análise. NÃO faça commit, push ou alterações — apenas leia e "
            "responda. Seja objetivo, em português, e aponte evidências (arquivo:linha) quando "
            "mencionar código."
        ),
    },
    "escopo": {
        "label": "Montar escopo",
        "description": "Transforma o entendimento do problema em um escopo de desenvolvimento (objetivos, entregas e critérios).",
        "instruction": (
            "Você é o analista de ESCOPO de um chamado. A partir do problema e da análise já feita, "
            "monte um escopo de desenvolvimento claro: objetivo, entregas, etapas sugeridas e "
            "critérios de aceite. Você pode ler o repositório (somente leitura) para embasar a "
            "proposta, mas NÃO altere nada. Responda em português, estruturado, pronto para virar "
            "uma história de desenvolvimento."
        ),
    },
    "resposta": {
        "label": "Redigir resposta",
        "description": "Redige a resposta final ao cliente com base no histórico do chamado.",
        "instruction": (
            "Você é o redator de RESPOSTA ao cliente de um chamado. Com base no histórico, redija "
            "a resposta final: linguagem clara e humana, sem jargão técnico desnecessário, "
            "explicando o que foi feito (ou o porquê do cancelamento) e, se aplicável, próximos "
            "passos. Não invente fatos além do contexto."
        ),
    },
}

# ── Contexto do chamado (comum a todas as execuções) ───────────────────────

_MAX_TRANSCRIPT_MESSAGES = 12


def _transcript(chamado, stage) -> str:
    """Últimas interações da etapa em formato legível (sem truncar textos)."""
    messages = [m for m in stage.messages if m.kind in ("user", "assistant_text")]
    messages = messages[-_MAX_TRANSCRIPT_MESSAGES:]
    if not messages:
        return "Nenhuma interação registrada ainda nesta etapa."
    lines = []
    for m in messages:
        label = "Usuário" if m.kind == "user" else "Assistente"
        text = m.payload.get("text") or m.payload.get("content") or ""
        lines.append(f"### {label}\n{text}")
    return "\n\n".join(lines)


def _chamado_context(chamado, stage) -> str:
    project_name = chamado.project.name if chamado.project else "—"
    epic_name = chamado.epic.name if chamado.epic else "—"
    return (
        f"Chamado #{chamado.id} — {chamado.title or '(sem título)'}\n"
        f"Descrição: {chamado.description or '—'}\n"
        f"Projeto: {project_name} | Épico: {epic_name}\n"
        f"Etapa atual: {stage.stage_type.name if stage.stage_type else '?'}\n"
        f"Transcrição da etapa:\n{_transcript(chamado, stage)}"
    )


# ── Ferramenta (assistente/escopo/...) ──────────────────────────────────────

def build_tool_prompt(chamado, stage, tool_key: str, user_text: str) -> str:
    """Prompt de uma execução de ferramenta na etapa atual."""
    preset = TOOL_PRESETS.get(tool_key, TOOL_PRESETS["assistente"])
    return "\n\n".join(
        [
            preset["instruction"],
            "## Contexto do chamado",
            _chamado_context(chamado, stage),
            "## Pedido do usuário",
            user_text,
            "Responda apenas a este pedido, no formato adequado ao seu papel. "
            f"Arquivo de contrato opcional: {verdicts.CHAMADO_DECISION_FILENAME} NÃO deve ser usado aqui.",
        ]
    )


# ── Avaliação de fechamento de etapa ───────────────────────────────────────

EVALUATION_CONTRACT = f"""
### Contrato de saída OBRIGATÓRIO
Escreva o arquivo `{verdicts.CHAMADO_DECISION_FILENAME}` na raiz do checkout com JSON:
{{
  "decision": "next_stage" | "resposta" | "cancelar" | "concluir",
  "next_stage": "nome_do_tipo_de_etapa" (obrigatório quando decision=next_stage),
  "resposta_texto": "texto completo da resposta ao cliente" (quando decision=resposta),
  "justificativa": "por que esta decisão"
}}
- `next_stage`: o chamado avança para a próxima etapa informada.
- `resposta`: o chamado é encerrado com uma resposta ao cliente (sem desenvolvimento).
- `cancelar`: o chamado é encerrado como cancelado.
- `concluir`: o chamado é encerrado como concluído.
Escolha a decisão adequada com base no contexto. Não invente etapas que não existam.
"""


def build_evaluation_prompt(chamado, stage, allowed_transitions: list[str]) -> str:
    return "\n\n".join(
        [
            "Você é o robô de AVALIAÇÃO de fechamento de etapa de um chamado. Sua missão é decidir "
            "o próximo passo do chamado com base no problema, no que já foi analisado (transcrição) "
            "e no que é possível fazer nesta etapa.",
            "## Contexto do chamado",
            _chamado_context(chamado, stage),
            f"## Transições permitidas nesta etapa\n{', '.join(allowed_transitions) or 'nenhuma'}",
            "Escolha UMA das transições permitidas e escreva o arquivo de decisão.",
            EVALUATION_CONTRACT,
        ]
    )


# ── Conteúdo de Projeto/Épico (recursos LLM) ───────────────────────────────

PROJECT_SUMMARY_CONTRACT = (
    "Escreva um resumo executivo do projeto em português: objetivo, escopo, estado atual, "
    "principais épicos e chamados e prioridades. Texto corrido, 4 a 8 frases."
)


def build_project_summary_prompt(project, epics, chamados) -> str:
    return "\n\n".join(
        [
            "Você é o gerador de RESUMO DE PROJETO da plataforma. " + PROJECT_SUMMARY_CONTRACT,
            f"Projeto: {project.name}\nDescrição: {project.description or '—'}",
            "## Épicos",
            "\n".join(f"- {e.name}: {e.description or '—'}" for e in epics) or "Nenhum épico.",
            "## Chamados",
            "\n".join(
                f"- #{c.id} {c.title} (etapa: {c.workflow_status or '—'}, status: {c.status})"
                for c in chamados
            )
            or "Nenhum chamado.",
        ]
    )


EPIC_SCOPE_CONTRACT = (
    "Escreva o ESCOPO/OBJETIVOS do épico em português: objetivo central, entregas previstas "
    "(derivadas dos chamados) e critérios de conclusão. Estruturado com marcadores."
)


def build_epic_scope_prompt(epic, chamados) -> str:
    return "\n\n".join(
        [
            "Você é o gerador de ESCOPO DE ÉPICO da plataforma. " + EPIC_SCOPE_CONTRACT,
            f"Épico: {epic.name}\nDescrição: {epic.description or '—'}",
            "## Chamados do épico",
            "\n".join(f"- #{c.id} {c.title}: {c.description or '—'}" for c in chamados)
            or "Nenhum chamado.",
        ]
    )


EPIC_SUMMARY_CONTRACT = (
    "Escreva um resumo executivo do épico em português: o que é, progresso dos chamados e "
    "próximos passos. Texto corrido, 3 a 6 frases."
)


def build_epic_summary_prompt(epic, chamados) -> str:
    return "\n\n".join(
        [
            "Você é o gerador de RESUMO DE ÉPICO da plataforma. " + EPIC_SUMMARY_CONTRACT,
            f"Épico: {epic.name}\nDescrição: {epic.description or '—'}",
            "## Chamados do épico",
            "\n".join(
                f"- #{c.id} {c.title} (etapa: {c.workflow_status or '—'}, status: {c.status})"
                for c in chamados
            )
            or "Nenhum chamado.",
        ]
    )
