"""Construção dos prompts das missões dos robôs (com contrato de saída por papel)."""

from __future__ import annotations

from .models import Robot, Task

# Regras reforçadas no prompt, alinhadas com guardrails.py.
GUARDRAIL_INSTRUCTIONS = """## Regras obrigatórias
- Trabalhe SOMENTE dentro do repositório atual (o diretório de trabalho). Não leia nem escreva arquivos fora dele.
- NÃO rode comandos destrutivos (rm -rf, mkfs, dd, sudo, chmod 777, shutdown etc.), NÃO use curl/wget/ssh/scp, NÃO instale dependências globais.
- NÃO rode `git push`. NÃO troque para as branches main/master (`git checkout main`). Trabalhe apenas na branch atual.
- Faça commits locais com `git add -A && git commit -m "mensagem"` quando concluir.
- Se algo estiver quebrado, corrija o que estiver ao seu alcance e relate o resto. Não invente resultados."""

GIT_WORKFLOW = """### Fluxo de trabalho git
- Confirme a branch atual com `git status` ou `git branch --show-current` antes de começar.
- Implemente a mudança e faça commit local. Não precisa de push."""

# Evidência obrigatória em qualquer resumo: alimenta auditoria e o bounce-back.
EVIDENCE = """### Evidência
No seu resumo/final, liste os comandos que executou e as saídas relevantes (trechos
reais). Isso é auditado e usado pelas próximas fases — sem evidência, a fase anterior
não tem como saber o que de fato aconteceu."""

CONTRACT_REFINE = """## Formato de saída OBRIGATÓRIO
Escreva a história no texto final com EXATAMENTE estes marcadores:

## Descrição
<descrição em formato "Como <usuário>, quero <capacidade>, para <benefício>", 1–3 parágrafos>

## Critérios de aceite
- [ ] critério verificável 1
- [ ] critério verificável 2
- [ ] ... (3 a 8 critérios)

## Fora de escopo
- <o que NÃO será feito nesta história> (opcional se não houver)

### Regras da história
- Critérios devem ser objetivos e verificáveis (dado/quando/então quando fizer sentido).
- PROIBIDO critério subjetivo ("bonito", "rápido", "fácil de usar") sem alvo mensurável.
- Se a ideia crua for vaga, faça as melhores suposições e deixe-as EXPLÍCITAS na descrição.
NÃO escreva arquivos no repositório: apenas responda com a história.

### Exemplo do formato
## Descrição
Como usuária do módulo de cálculos, quero uma função `multiplicar`, para não depender de cálculo manual.

## Critérios de aceite
- [ ] dado `multiplicar(3, 4)`, então o retorno é `12`
- [ ] dado o comando `pytest` na raiz, então todos os testes passam"""

CONTRACT_REVIEW = """## Formato de saída OBRIGATÓRIO (veredicto)
Revise a história (descrição + critérios de aceite) com o checklist abaixo. Depois
escreva o arquivo `autoia_verdict.txt` na raiz do repositório com o veredicto na
PRIMEIRA linha, exatamente:

READY
SUMMARY: o que está bom

— ou —

NEEDS_WORK
SUMMARY: o que está faltando ou ambíguo, item por item

### Checklist de revisão
1. Critérios são verificáveis (dado/quando/então ou com alvo mensurável)?
2. Cobre o caminho feliz E o de erro?
3. Sem ambiguidade ("e/ou", "depende", adjetivos vagos)?
4. Escopo definido?
5. Completo o suficiente para o developer implementar sem precisar perguntar?

Se emitiu NEEDS_WORK, inclua no SUMMARY a HISTÓRIA CORRIGIDA completa (mesmo formato
que o PO usa: ## Descrição / ## Critérios de aceite) para o PO aplicar diretamente.
NÃO altere código. NÃO faça commit do arquivo de veredicto."""

CONTRACT_VERIFY = """## Formato de saída OBRIGATÓRIO (veredicto)
Esta fase é a GARANTIA DE QUALIDADE AUTOMATIZADA: sem humano revisando depois.
- Detecte e rode a suíte de testes do projeto (use o ecossistema indicado acima) e
  anote o resultado real no SUMMARY.
- Valide CADA critério de aceite da história contra o código implementado, um por um.
  Se o projeto não tiver testes, escreva e rode um teste mínimo que exercite o que foi
  implementado.
- NÃO altere código nem arquivos do projeto: você VERIFICA e REPORTA. Correções voltam
  para o developer automaticamente (bounce-back) com o seu relatório.
- Só depois de tudo verificado, escreva o arquivo `autoia_verdict.txt` na raiz do
  repositório com a PRIMEIRA linha exatamente:

PASS
SUMMARY: o que foi testado, comandos rodados e resultados

— ou —

FAIL
SUMMARY: relatório estruturado das falhas (abaixo)

Se houver QUALQUER critério não atendido ou teste falhando, o veredicto é FAIL.
No FAIL, use EXATAMENTE este formato no SUMMARY (é o que o developer usará para corrigir):

FALHAS:
- critério 1: <o que falhou>
  comando: <o que você rodou>
  saída: <trecho real da saída>
- critério 2: ...

### Exemplo (PASS)
PASS
SUMMARY: rodei `pytest` (4 testes, 4 passaram). Validei os critérios 1–3; todos atendidos.

NÃO faça commit do arquivo de veredicto."""

CONTRACT_PM = """## Formato de saída OBRIGATÓRIO (decisão)
Você controla o projeto. Analise o contexto da tarefa (status, falhas, orçamento gasto,
tentativas, relatórios) e decida o melhor próximo passo, escrevendo o arquivo
`autoia_verdict.txt` na raiz do repositório com EXATAMENTE uma das opções na primeira linha:

DECISÃO: retry <posição>
MOTIVO: <porquê — falha plausivelmente corrigível>

— ou —

DECISÃO: continuar
MOTIVO: <porquê — progresso real, precisa de mais orçamento>

— ou —

DECISÃO: escalar
MOTIVO: <porquê — precisa de humano>

### Heurísticas de decisão
- retry: a falha tem causa clara e plausivelmente corrigível, e a mesma fase falhou
  menos de 2 vezes.
- continuar: há progresso real e o orçamento acabou no meio do caminho.
- escalar: a mesma fase falhou 2+ vezes, o orçamento gasto passou de ~80% do limite,
  o erro é ambíguo, ou faltam informações para decidir com segurança.
Quando em dúvida, ESCALAR (default seguro).
NÃO altere arquivos do projeto. NÃO faça commit do arquivo de veredicto."""

_CONTRACTS = {
    "refine": CONTRACT_REFINE,
    "review": CONTRACT_REVIEW,
    "verify": CONTRACT_VERIFY,
    "pm": CONTRACT_PM,
}


def build_prompt(
    robot: Robot,
    task: Task,
    step_context: str,
    default_branch: str,
    project_info: str = "",
) -> str:
    mission = (robot.mission or "").strip()
    mission = (
        mission.replace("{task_title}", task.title or "")
        .replace("{task_description}", task.description or "")
        .replace("{step_context}", step_context or "")
        .replace("{default_branch}", default_branch or "main")
    )

    parts = [mission, GIT_WORKFLOW]
    if project_info:
        parts.append(project_info)
    if task.acceptance_criteria:
        parts.append(f"### Critérios de aceite da história\n{task.acceptance_criteria}")
    if step_context:
        parts.append(f"### Contexto das fases\n{step_context}")
    contract = _CONTRACTS.get(robot.role)
    if contract:
        parts.append(contract)
    parts.append(EVIDENCE)
    parts.append(GUARDRAIL_INSTRUCTIONS)
    return "\n\n".join(p for p in parts if p)
