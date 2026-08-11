"""Construção dos prompts das missões dos robôs (com contrato de saída por papel)."""

from __future__ import annotations

from .models import Robot, Task

# Regras reforçadas no prompt, alinhadas com guardrails.py.
GUARDRAIL_INSTRUCTIONS = """## Regras obrigatórias
- Trabalhe SOMENTE dentro do repositório atual (o diretório de trabalho). NÃO leia nem
  escreva arquivos fora dele: as ferramentas de arquivo (Read/Write/Edit/Glob/Grep) só
  funcionam DENTRO do checkout — qualquer caminho fora (ex.: ~/.kimi-code, /tmp, logs do
  pipeline) é BLOQUEADO e encerra a sua execução na hora.
- NÃO rode comandos destrutivos ou de sistema: rm -rf, mkfs, dd, sudo, chown, chmod 777,
  curl, wget, ssh, scp, pip install, npm install -g, make install, shutdown/reboot/halt,
  systemctl, service, killall/pkill, pkexec, su. A infraestrutura (banco, serviços,
  deploy) já está provisionada pelo ambiente — NÃO tente instalar, iniciar ou verificar
  serviços do sistema operacional; use as variáveis de ambiente fornecidas (ex.:
  DATABASE_URL) ou mocks nos testes.
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

# Ferramenta de criação de tarefas filhas.
TASK_SPAWN_TOOL = """### Ferramenta: criar tarefas

Se esta tarefa pode ou deve ser decomposta em tarefas menores (ex.: uma feature complexa
que naturalmente se divide em várias entregas), crie um arquivo `autoia_tasks.json` na
raiz do projeto. O sistema lerá este arquivo automaticamente ao final da fase e criará
as tarefas. Use APENAS se a decomposição for realmente necessária — não crie tarefas
triviais ou de 1 linha.

Formato do arquivo (JSON array):
```json
[
  {
    "title": "Título curto e descritivo",
    "description": "Descrição detalhada do que precisa ser feito",
    "kind": "feature",
    "repository": "nome-do-repo"
  }
]
```

- `title` (obrigatório): nome da tarefa, claro e acionável
- `description` (opcional): detalhes, contexto, critérios de aceite
- `kind` (opcional, default "feature"): "feature", "bug", "issue" ou "chore"
- `repository` (opcional): nome de outro repositório cadastrado no autoia. Se omitido,
  a tarefa é criada neste mesmo repositório. Use para cross-project: ex.: criar task de
  documentação no repo "docs" quando uma feature é implementada no repo "api".
"""

# Ferramenta: marcar subtarefa como implementada (evita re-implementar o que já está na branch).
SUB_TASK_DONE_TOOL = """### Ferramenta: marcar subtarefa como implementada

Se a subtarefa atual JÁ está implementada na branch — código presente, commitado e
atendendo os critérios (ex.: o trabalho foi feito numa execução anterior e o status foi
perdido por um restart do worker) — NÃO reimplemente do zero. Em vez disso, escreva o
arquivo `autoia_subtasks_done.json` na raiz do projeto e o sistema marcará a subtarefa
como implementada automaticamente ao final desta fase.

Formato (JSON array com a posição 1-based da subtarefa):
```json
[1, 2, 4]
```

- Use APENAS quando o código da subtarefa JÁ está commitado na branch e atende os critérios.
- Ainda assim, documente no texto final o que você constatou (arquivos já presentes etc.).
- Se a subtarefa ainda precisa de trabalho, implemente normalmente e NÃO escreva o arquivo.
"""

# Caderno de trabalho: o worker gera autoia_handoff.md no checkout antes de cada fase
# com o histórico COMPLETO das fases anteriores + diff + instrução da fase atual.
HANDOFF_READ = """## Caderno de trabalho (leia antes de começar)
ANTES de começar, LEIA o arquivo `autoia_handoff.md` na raiz do repositório: ele
contém o histórico COMPLETO das fases anteriores, o diff atual da branch e a instrução
desta fase. Baseie seu trabalho nele — é o registro oficial do que já foi feito."""

# Documentação da fase no texto final (papéis que executam/verificam trabalho).
# O worker persiste o texto final INTEGRAL como histórico da fase (fonte de verdade).
HANDOFF_DOCUMENT = """## Documentação obrigatória (handoff)
Ao terminar, o seu TEXTO FINAL é a documentação desta fase — é exatamente o que a
próxima fase receberá como histórico. Escreva com EXATAMENTE estes tópicos:

### O que foi feito
<resumo objetivo do trabalho desta fase>

### Arquivos alterados
- <caminho> — <para quê> (ou "Nenhum" se não alterou arquivos)

### Evidência
<comandos executados e saídas relevantes — trechos REAIS>

### Pendências
- <o que não foi possível fazer, ou "Nenhuma">

### Para a próxima fase
<instruções diretas ao próximo robô: o que verificar, o que falta, onde olhar>"""

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

## Plano de implementação
Divida o trabalho em subtarefas ordenadas e independentes, cada uma com seu próprio
escopo e critérios verificáveis. Cada subtarefa deve ser implementável em uma sessão
(código focado, ~1–3 arquivos).

### Subtarefa 1: <título curto>
**Escopo:** <o que implementar, 1–3 frases>
**Critérios:**
- [ ] critério verificável
- [ ] ...

### Subtarefa 2: <título curto>
**Escopo:** <o que implementar>
**Critérios:**
- [ ] ...

... (2 a 6 subtarefas; se a tarefa for muito simples, 1 subtarefa é aceitável)

### Regras da história
- Critérios devem ser objetivos e verificáveis (dado/quando/então quando fizer sentido).
- PROIBIDO critério subjetivo ("bonito", "rápido", "fácil de usar") sem alvo mensurável.
- Se a ideia crua for vaga, faça as melhores suposições e deixe-as EXPLÍCITAS na descrição.
- Subtarefas devem ser independentes: a ordem é de implementação, mas cada uma pode ser
  testada isoladamente com seus próprios critérios.
NÃO escreva arquivos no repositório: apenas responda com a história.

### Exemplo do formato
## Descrição
Como usuária do módulo de cálculos, quero uma função `multiplicar`, para não depender de cálculo manual.

## Critérios de aceite
- [ ] dado `multiplicar(3, 4)`, então o retorno é `12`
- [ ] dado o comando `pytest` na raiz, então todos os testes passam

## Plano de implementação

### Subtarefa 1: Implementar função multiplicar
**Escopo:** Criar a função `multiplicar(a, b)` no módulo `calculos.py` com type hints e docstring.
**Critérios:**
- [ ] dado `multiplicar(3, 4)`, retorna `12`
- [ ] dado `multiplicar(-2, 5)`, retorna `-10`

### Subtarefa 2: Adicionar testes
**Escopo:** Criar `test_calculos.py` com pytest cobrindo a função multiplicar.
**Critérios:**
- [ ] `pytest` passa com 2+ testes para `multiplicar`
- [ ] cobre caso positivo, negativo e zero"""

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

### Testes visuais (smoke test com screenshots)
Se o projeto tiver interface visual (web, desktop, mobile), faça um smoke test visual:
- Inicie o projeto e use as ferramentas de navegador (kimi-webbridge) para abrir as
  telas principais e verificar visualmente o funcionamento.
- Para cada tela/fluxo testado, tire um screenshot e salve no diretório
  `autoia_screenshots/` na raiz do checkout. Use nomes descritivos como
  `login.png`, `dashboard.png`, `form-erro.png`.
- Documente cada screenshot no SUMMARY com o que foi testado e se passou ou falhou.
  Exemplo: "smoke-login.png: tela de login carregou, campos visíveis, botão funcional — OK"

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

CONTRACT_ASSESS = """## Formato de saída OBRIGATÓRIO (veredicto)
Esta fase é a AVALIAÇÃO FINAL da tarefa, ANTES da integração (merge).
- Analise a tarefa completa: história (descrição + critérios de aceite), os resumos das
  fases anteriores e o diff da branch contra a base (git diff --stat contra a branch
  base informada na missão).
- Valide CADA critério de aceite contra o que foi implementado, um por um, e confira
  também: escopo respeitado (nada fora da tarefa), solução coerente e idiomática, sem
  lixo (arquivos temporários, debug, credenciais) e sem dívidas não justificadas.
- NÃO altere código nem arquivos do projeto: você AVALIA e REPORTA. Correções voltam
  automaticamente (bounce-back) com o seu relatório.
- Só depois de avaliar tudo, escreva o arquivo `autoia_verdict.txt` na raiz do
  repositório com a PRIMEIRA linha exatamente:

PASS
SUMMARY: o que foi avaliado, decisões e justificativas

— ou —

FAIL
SUMMARY: relatório estruturado das falhas (abaixo)

Se QUALQUER critério não atendido, escopo estourado ou problema de qualidade, o
veredicto é FAIL. No FAIL, use EXATAMENTE este formato no SUMMARY (é o que o developer
usará para corrigir):

FALHAS:
- item: <o que faltou ou falhou>
  evidência: <o que você viu no código/diff>
- item: ...

### Exemplo (PASS)
PASS
SUMMARY: validei os critérios 1–4 no código e no diff; escopo respeitado, sem lixo.

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

# Declaração de bloqueio: o agente pede intervenção do usuário quando não consegue
# continuar com segurança/autonomia (ambiguidade, decisão, dependência, permissão...).
BLOCKED_TOOL = """## Ferramenta: declarar bloqueio (pedir instrução ao usuário)

Se você NÃO puder continuar com segurança ou autonomia (ex.: duas abordagens possíveis
e nenhuma é claramente melhor, requisito ambíguo, dependência de outra tarefa, decisão
técnica que precisa de humano, falha de ferramenta, autorização necessária), NÃO
invente nem adivinhe. Em vez disso, escreva o arquivo `autoia_blocked.json` na raiz do
repositório com esta estrutura:

```json
{
  "status": "blocked",
  "reason_type": "decision_required",
  "reason": "motivo claro e objetivo do bloqueio",
  "question": "pergunta direta ao usuário sobre como continuar"
}
```

- `reason_type` (obrigatório): um de `decision_required`, `insufficient_info`,
  `ambiguity`, `dependency`, `authorization`, `tool_failure`, `guardrail`, `other`.
- `reason` (obrigatório): o porquê do bloqueio.
- `question` (opcional): a pergunta cuja resposta destrava a continuidade.
- NÃO faça commit deste arquivo. O sistema pausa a execução, mostra o motivo ao usuário
  e o retoma a partir daqui quando o usuário fornecer uma instrução. Isso NÃO é falha:
  é um pedido legítimo de intervenção."""

# Resumo estruturado do desenvolvimento (LLM dedicada — só interpreta, nunca desenvolve).
CONTRACT_SUMMARY = """## Sua função: RESUMIR, não desenvolver
Você é uma LLM dedicada a RESUMIR um desenvolvimento já executado. NÃO altere código,
não gere código, não corrija nada: apenas INTERPRETE o contexto fornecido e produza um
resumo objetivo, que permita a uma pessoa entender o que aconteceu sem abrir logs.

## Formato de saída OBRIGATÓRIO (arquivo)
Escreva o arquivo `autoia_summary.json` na raiz do repositório com EXATAMENTE esta
estrutura JSON:

```json
{
  "summary": "Resumo objetivo do desenvolvimento (o que foi feito).",
  "request": "Descrição resumida do que foi solicitado.",
  "implementation": "O que foi efetivamente implementado.",
  "changes": ["Alteração importante 1", "Alteração importante 2"],
  "result": "completed",
  "issues": ["Problema ou limitação relevante"],
  "files": ["arquivo relevante"],
  "tasks_summary": "Resumo das tarefas executadas e pendentes"
}
```

Regras:
- `result` é um de: `completed`, `partial`, `failed`, `pending`.
- `changes`, `issues` e `files` são arrays (podem ser vazios).
- Seja concreto: evite frases genéricas ("melhorias gerais", "refatorações").
- `issues` só com algo relevante; caso contrário, array vazio.
- NÃO faça commit deste arquivo."""

_CONTRACTS = {
    "refine": CONTRACT_REFINE,
    "review": CONTRACT_REVIEW,
    "verify": CONTRACT_VERIFY,
    "assess": CONTRACT_ASSESS,
    "pm": CONTRACT_PM,
    "summary": CONTRACT_SUMMARY,
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
    if task.feedback:
        parts.append(
            f"### Feedback externo (do usuário/sistema)\n{task.feedback}\n\n"
            "Leve em conta este feedback no seu trabalho — pode conter erros de deploy, "
            "pedidos de ajuste ou informações do ambiente."
        )
    if task.details:
        parts.append(
            f"### Detalhes adicionados pelo usuário (contexto da implementação)\n{task.details}\n\n"
            "O usuário adicionou estes detalhes para orientar ou corrigir a implementação. "
            "São posteriores à solicitação original — leve-os em conta."
        )
    if task.resume_instruction:
        parts.append(
            f"### Intervenção do usuário (retomada)\n{task.resume_instruction}\n\n"
            "A execução anterior foi bloqueada e o usuário forneceu esta instrução para "
            "você continuar EXATAMENTE de onde parou, no mesmo contexto. Obedeça-a."
        )
    parts.append(HANDOFF_READ)
    contract = _CONTRACTS.get(robot.role)
    if contract:
        parts.append(contract)
    # refine (história) e pm (decisão) têm formatos de saída próprios; os demais
    # documentam o trabalho no texto final, que vira o histórico da fase no handoff.
    if robot.role not in ("refine", "pm", "summary"):
        parts.append(HANDOFF_DOCUMENT)
    parts.append(EVIDENCE)
    parts.append(GUARDRAIL_INSTRUCTIONS)
    # Todo agente pode declarar bloqueio pedindo intervenção do usuário.
    if robot.role not in ("refine", "pm", "summary"):
        parts.append(BLOCKED_TOOL)
    parts.append(TASK_SPAWN_TOOL)
    return "\n\n".join(p for p in parts if p)
