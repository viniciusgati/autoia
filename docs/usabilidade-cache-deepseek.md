# Auditoria de usabilidade — cache do deepseek / custos na UI

Data: 2026-08-17 · Branch: `autoia/task-77` · Fase: auditor-ux

Escopo: avaliar a **usabilidade** da aplicação sob a ótica da ideia em análise
("usar otimizadamente o cache do deepseek para baratear custos") e das tarefas do
analista com impacto de UI (3 — métricas de cache; 4 — custo real vs estimado no
orçamento/UI; 8 — dashboard de tokens/cache). A avaliação cobre fluxos do usuário,
feedback visual, clareza de textos, consistência e onboarding. O subsistema de
chamados já foi auditado em `docs/usabilidade-chamados.md` (2026-08-13) — referenciado
aqui, não reavaliado a fundo.

---

## Fluxos do usuário

- **Criar tarefa** (`Tasks.tsx`) — formulário com repositório, pipeline, tipo,
  executor, título, descrição (+ import de arquivo txt/md). A criação não navega para
  o detalhe/workspace; o usuário cai na lista de tarefas e precisa clicar no card.
- **Acompanhar execução** (`Workspace.tsx`) — header fixo com status, custo total vs
  orçamento, responsável, executor (select) e etapa em execução; timeline de
  ocorrências por fase (missão → motivo da parada → o que foi resolvido → subtarefas →
  testes → propostas → arquivos/diff), campo de interação fixo no rodapé com
  "continuar a partir de" (posição). Polling de 1,5 s.
- **Reagir a paradas** — alertas no header ("⛔ Bloqueada:", "❌ Erro:", "⚠ Revisão:",
  "🟠 Decisão necessária:") com botão "responder ↓"; bloco de decisão com opções;
  bounce-back/retomada via instrução no campo inferior.
- **Revisar custos** — `TaskCard` (gasto/orçamento, vermelho ≥ 80%), `RepoDashboard`
  ("gasto total" do projeto), `Home` ("custo estimado (R$)" global), `TaskDetail`
  (orçamento no cabeçalho), `PhaseDetail` (+custo por evento) e `Workspace` (custo por
  ocorrência no tooltip e custo total no header).
- **Configurar projeto** (`RepoConfig.tsx`) — orçamento por tarefa (R$), custo por
  interação (R$), timeout, max tentativas, max decisões PM, resumo automático, sandbox,
  regra de banco, etc., todos com `HelpTip`.
- **Configurar robôs/modelo** (`Robots.tsx`) — seletor de modelo por robô (campo de
  texto + presets); sem indicação de preço por modelo.
- **Fluxo de chamados** (`Chamados.tsx`/`ChamadoDetail.tsx`/`Projects.tsx`) — já
  auditado; custo acumulado no header, sem breakdown por etapa (sugestão Fase 1.1).

---

## Problemas de usabilidade

### Moeda inconsistente: UI fala R$, backend fala US$ (crítico p/ "baratear custos")

- `lib/money.ts` documenta: valores vêm do executor e são "tratados como reais na
  interface — sem conversão de moeda". O default do backend é
  `AUTOIA_COST_PER_INTERACTION = 0.01` (US$) e `AUTOIA_TASK_BUDGET = 10.0` (US$,
  README tabela), e `types.ts:373` diz explicitamente "USD".
- Resultado: todo número de custo exibido na UI ("R$ 0,10", "R$ 10,00") **não é o
  valor real em reais** — é um valor em dólar etiquetado como R$. Para o usuário que
  quer medir economia financeira (o objetivo central da ideia), o número exibido é
  enganoso e não auditável.

### "Estimado vs real" invisível na maior parte da UI

- Único lugar com distinção: tooltip do custo por ocorrência no Workspace ("kimi
  estimado / opencode real", `Workspace.tsx:287`). Em todas as outras superfícies
  (TaskCard, RepoDashboard, Home, TaskDetail, Resumo) o custo aparece como número
  único sem rótulo de estimativa.
- Consequência concreta: o alerta "💰 ORÇAMENTO ESTOUROU" (Workspace) e o vermelho de
  ≥ 80% do orçamento (TaskCard) disparam sobre **estimativa** kimi (fixa por
  interação, `budget.interaction_cost`), podendo travar a tarefa em `needs_review`
  com custo real muito menor — e o usuário não tem como saber que o número é
  estimado.

### Zero visibilidade de tokens/cache na UI

- Nenhuma tela mostra tokens consumidos, `cache hit %` ou economia de cache — nem no
  Workspace, nem no Resumo, nem no TaskDetail, nem no dashboard. Grep por
  `tokens|cache` na UI só encontra markdown tokens e o cache de ETag da API
  (`api.ts:36-89`).
- O backend já persiste tokens no payload do `step_finish` do opencode
  (`opencode_exec.py:269`) sem uso analítico; o kimi nem parseia `usage`
  (`kimi_exec.py:244` só trata `session.resume_hint`).
- Para o usuário, o objetivo da ideia ("cache do deepseek barateando custos") é
  **inverificável na interface**: não há como ver a economia acontecer.

### Vocabulário de custo inconsistente entre telas

- A mesma métrica aparece como: "Custo total" (Workspace), "gasto total"
  (RepoDashboard), "gasto" (Execution), "custo acumulado" (types/API), "custo
  estimado (R$)" (Home global), "Orçamento" (TaskDetail), "Custo/interação (R$)"
  (RepoConfig), "gasto X / orçamento Y" (TaskCard `fmtBudget`). Rótulos diferentes
  para a mesma coisa confundem a leitura e dificultam comparar telas.

### Falta de contexto financeiro na escolha de executor/modelo

- No formulário de criação (`Tasks.tsx:107-113`) o seletor "Executor" (kimi code /
  opencode) não tem ajuda; o usuário não sabe que kimi = custo estimado e opencode =
  custo real, nem o impacto disso no orçamento.
- `Robots.tsx` permite escolher modelo por robô sem mostrar custo por modelo (preço,
  se é cacheável, se é flash/pro). O usuário que quer "baratear" não tem base para
  escolher.
- O kimi não tem env de modelo na autoia (config.py não expõe `AUTOIA_KIMI_MODEL`) —
  a UI não deixa o usuário escolher o modelo do kimi, embora o opencode permita.

### Alerta de orçamento sem % e sem contexto

- TaskCard pinta o custo de vermelho quando `cost_spent/budget_limit >= 0.8` mas não
  mostra a porcentagem ("80% do orçamento estimado usado" não aparece em lugar
  nenhum). O usuário vê o número mudar de cor sem saber o limiar nem o que significa.
- O HelpTip do orçamento em RepoConfig diz "Limite de gasto por tarefa. Se estourar,
  a task vai para needs_review" — correto, mas não avisa que o gasto pode ser
  estimado (kimi) e que a estimativa não inclui economia de cache.

### Onboarding: jargão interno sem explicação

- Pipeline default "po-qa-dev-tester-avaliador-deploytest": o usuário escolhe na
  criação sem saber o que cada fase faz nem que existem alternativas (4 pipelines no
  seed). A única ajuda é textual no README ("Primeiros passos na UI").
- Empty states existem e são bons ("Nenhuma tarefa neste projeto.", "Nenhuma etapa
  executada ainda." + botão Iniciar), mas não há onboarding guiado da primeira tarefa
  (o que esperar, quanto vai custar, quanto tempo).

### (Menor) Custo por ocorrência só no tooltip

- `Workspace.tsx:286-290` mostra `fmtCost(occ.cost)` apenas no header do card, com o
  significado escondido no tooltip; sem hover, o usuário não sabe que aquele número é
  o custo da execução nem se é estimado/real. Chamados têm o mesmo problema por etapa
  (já registrado como sugestão Fase 1.1 em `usabilidade-chamados.md`).

---

## Recomendações priorizadas

1. **Corrigir a moeda e rotular estimado/real em toda a UI (P0 — pré-requisito de
   confiança)** — decidir a unidade canônica (USD como fonte; exibir em R$ **com
   conversão explícita e taxa configurável**, ou exibir USD sem disfarce). Em todos os
   pontos de custo (TaskCard, RepoDashboard, Home, TaskDetail, Workspace, PhaseDetail,
   Execution) adicionar distinção visível "estimado" vs "real" (badge/tooltip) e
   sinalizar o alerta de orçamento estourado quando baseado em estimativa. Mapa:
   `lib/money.ts` + todas as páginas que importam `fmtCost`. Benefício: o usuário
   passa a confiar nos números antes de qualquer otimização de cache.

2. **Expor tokens e cache hit na UI (P0 — valida o objetivo da ideia)** — com o parse
   de `usage` (kimi) e os `tokens` já persistidos (opencode), somar por ocorrência em
   `timeline.py` e exibir no card da ocorrência (Workspace) e no Resumo: tokens
   input/output/cache e `cache hit %` por execução e acumulado por tarefa. Isso é a
   tarefa 3 do analista (depende das tarefas 1–2: spike + parse no executor).

3. **Custo real no orçamento com fallback estimado (P1 — tarefa 4 do analista)** —
   `budget.interaction_cost` usa custo por tokens quando houver `usage`; UI sinaliza
   real vs estimado no header do Workspace e no alerta de orçamento. Benefício:
   orçamento deixa de disparar `needs_review` com base em estimativa irrelevante.

4. **Dashboard de tokens/cache por tarefa e global (P1 — tarefa 8 do analista)** —
   card no Home/RepoDashboard com "custo real vs estimado" e "economia estimada de
   cache (R$)" e agregado por projeto. Benefício: o usuário vê a economia acontecer
   — é a resposta visível à ideia.

5. **Ajuda contextual na escolha de executor e modelo (P1)** — `HelpTip` no seletor
   de executor da criação (`Tasks.tsx`) explicando estimado vs real; em `Robots.tsx`,
   mostrar o preço/posição do modelo (flash vs pro) e indicar que o kimi não tem
   seletor de modelo na autoia (pendência das tarefas 6–7 do analista). Benefício:
   decisão de custo informada.

6. **Alerta de orçamento com % e texto claro (P2)** — "X% do orçamento usado
   (estimado)" no TaskCard/Workspace em vez de só cor vermelha; limiar configurável.
   Benefício: o usuário entende o estado de custo sem adivinhar.

7. **Onboarding da primeira tarefa (P2)** — descrição curta de cada fase do pipeline
   na tela de criação (tooltip no select de pipeline) e, no primeiro acesso ao
   Workspace de uma tarefa nova, uma linha de orientação ("a execução é automática;
   você pode pausar, mandar instruções e ver o custo por fase"). Benefício: reduz
   abandono na primeira experiência.

---

## Evidência

Comandos executados nesta fase (branch `autoia/task-77`):

- `git status` → `On branch autoia/task-77` / `Your branch is ahead of 'origin/main' by 2 commits` /
  `nothing to commit, working tree clean`; `git branch --show-current` → `autoia/task-77`;
  `git log --oneline -5` → `483a4e6 docs(analista): lacunas e tarefas p/ uso otimizado do cache do
  deepseek (task 77)`, `362fd63 docs(iniciacao): mapeamento do estado atual p/ análise de cache do
  deepseek (task 77)`.
- `python3 -m pytest --version` → `No module named pytest` — suíte não executável neste ambiente
  (mesmo das fases anteriores; nenhum código alterado nesta fase, apenas auditoria).

Leituras/greps (trechos reais relevantes):

- `frontend/src/lib/money.ts:3-4` → "Os valores de custo vêm do executor (opencode real / kimi
  estimado) e são tratados como reais na interface — sem conversão de moeda."
- `frontend/src/types.ts:373` → `/** Custo acumulado desta execução (USD; kimi estimado, opencode real). */`
- `frontend/src/pages/Home.tsx:352` → rótulo `custo estimado (R$)`; `RepoConfig.tsx:232/242` →
  `Orçamento (R$)` / `Custo/interação (R$) <HelpTip>Custo estimado por chamada ao kimi (tool_call +
  resposta)…`; `backend/app/config.py:140-141` → `task_budget=10.0`, `cost_per_interaction=0.01`
  (US$, README:152-155 tabela).
- `frontend/src/pages/Workspace.tsx:286-290` → custo por ocorrência só com tooltip "Custo desta
  execução (kimi estimado / opencode real)"; `Workspace.tsx:708-711` → `Custo total: <b>{fmtCost(task.cost_spent)}</b>`.
- `frontend/src/components/TaskCard.tsx:53-54,126-128` → `costPct >= 0.8` → `task-card-cost-warn`
  (vermelho sem % exibido).
- `backend/app/worker/kimi_exec.py:244` → único parse de meta: `session.resume_hint`
  (nada de `usage`/tokens/cache). `backend/app/worker/opencode_exec.py:260-274` → `step_finish`
  persiste `tokens`/`cost` reais no payload, sem uso analítico na UI.
- `backend/app/timeline.py:619-621` → `occ["cost"] = round(sum(... ev.get("cost") ...))` — custo por
  ocorrência determinístico, sem tokens.
- `backend/app/budget.py:12-13` → `interaction_cost = settings.cost_per_interaction` (estimativa
  fixa; docstring prevê troca por `usage`).
- Grep `cost|custo|tokens|token|cache` em `frontend/src` → nenhuma ocorrência de cache-hit/tokens
  na UI (só markdown tokens e ETag cache em `api.ts:36-89`).
- `frontend/src/pages/Tasks.tsx:107-113` → select Executor sem ajuda; `Robots.tsx` → seletor de
  modelo por robô sem preço/custo.
- `frontend/src/pages/RepoConfig.tsx` → HelpTips em todos os campos de configuração (padrão de
  ajuda bom, a replicar nas recomendações 5-7).
- `docs/usabilidade-chamados.md` → auditoria anterior (custo por etapa/mensagem segue como
  sugestão Fase 1.1, item 1).

Arquivo criado: `docs/usabilidade-cache-deepseek.md` (esta auditoria). Nenhum código alterado.

## Pendências / para a próxima fase (propositor)

- A implementação das recomendações 1–4 depende da telemetria real (tarefas 1–2 do
  analista: spike do `usage` do kimi e parse/persistência de tokens).
- Sem testes executáveis neste ambiente (pytest ausente), recomendações de UI precisam
  de cobertura manual + testes quando houver ambiente (padrões existentes:
  `tests/test_chamados.py` para UI/API; `fake_kimi`).
- Consolidar no `autoia_tasks.json` as tarefas 1–8 do analista, priorizando as de UI
  (3, 4, 8) com os achados desta auditoria (moeda, estimado/real, dashboard).
