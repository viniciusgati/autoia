# Análise — Uso otimizado do cache do DeepSeek (redução de custos)

> Relatório do analista (fase `plan` da task #77 — "Analise de feature").
> Consolida as lacunas do estado atual (mapeado pelo iniciador em
> `docs/iniciacao-cache-deepseek.md`) com verificação direta no código, e
> propõe tarefas priorizadas para o propositor transformar em propostas.

## 1. Lacunas identificadas

1. **Sem telemetria de uso/tokens no executor kimi** — `kimi_exec.py` só
   parseia `meta session.resume_hint` (linhas 241–248); qualquer outro evento
   `meta` vai apenas para o log bruto. A documentação oficial do kimi-code
   (páginas `kimi-command.html` e `sessions.html`) **não documenta um campo
   `usage` no stream-json** — o formato real precisa ser confirmado
   empiricamente com o CLI (spike). *Impacto*: sem dados de tokens não existe
   custo real, métrica de cache nem base auditável para "baratear custos"; o
   README já lista "custo real por tokens (usage do kimi)" como roadmap.

2. **Sem métricas de cache hit** — nenhum campo, evento ou tela captura
   tokens de cache (`prompt_cache_hit_tokens`/`cached_tokens` do provedor). No
   opencode, o `step_finish` persiste `cost` e `tokens` no payload do evento
   (linhas 260–274 do `opencode_exec.py`), mas sem nenhum uso analítico.
   *Impacto*: é impossível provar/quantificar a economia de cache — o objetivo
   central da ideia — sem métrica.

3. **Custo kimi é estimativa fixa por interação** — `budget.interaction_cost`
   retorna `cost_per_interaction` (default US$ 0,01); `RunEvent.cost` →
   `Task.cost_spent` e o orçamento (limite → `needs_review`) usam essa base. A
   economia real de cache não aparece em lugar nenhum; a UI chega a rotular o
   valor como estimado ("kimi estimado / opencode real", `Workspace.tsx:287`).
   *Impacto*: o usuário não enxerga o benefício financeiro do cache, e o
   orçamento pode disparar `needs_review` por estimativa sem relação com o
   custo real (ou subestimar quando o cache é eficiente).

4. **opencode sem resume de sessão** — `TaskStep.session_id` só é usado no
   kimi (`kimi -S <id>` na retomada de fase interrompida; `kimi_exec.py:87–89`
   + `runner._should_resume`); no opencode o comando é sempre
   `opencode run <prompt>` sem estado (`opencode_exec.py:107`). *Impacto*:
   re-execuções (bounce-back/retry manual) recomeçam do zero e queimam
   contexto/cache — o custo real do opencode (mais caro por ser real) se
   multiplica nas retentativas.

5. **Handoff/prompt cresce entre fases** — o `step_context` montado pelo
   runner inclui o histórico integral das fases anteriores (resumos +
   veredictos + diff + atividade), que é conteúdo **dinâmico não cacheável**
   (`runner._build_step_context` → `prompts.build_prompt`). O prefixo do
   prompt já é estável (favorável ao prefix-cache), mas a parte dinâmica pesa
   e tende a crescer a cada fase. *Impacto*: custo por fase tende a subir ao
   longo do pipeline, diluindo o ganho de cache do prefixo.

6. **Sem estratégia de modelo por tipo de trabalho** — as LLMs dedicadas de
   missão (`step_mission`) e resumo (`step_summary`) usam o mesmo
   executor/modelo da task, embora sejam prompts curtos; `Robot.model` existe,
   mas o kimi não tem env de modelo na autoia (usa o configurado no CLI do
   usuário) e o opencode usa `opencode_model` default (`config.py`).
   *Impacto*: trabalhos de prompt curto pagam modelo pesado; sem env de modelo
   para o kimi, a autoia não controla a escolha do modelo no executor padrão.

7. **Formato real do `usage` do kimi não confirmado** — pré-requisito de
   conhecimento: a doc oficial não cobre o campo; é preciso capturar o JSON
   real de uma execução `kimi -p --output-format stream-json` (log) antes de
   implementar parse. *Impacto*: implementar parse às cegas é risco de
   construir sobre formato inexistente/diferente — por isso vira tarefa de
   investigação (spike), não suposição.

### Sugestões opcionais (não lacunas bloqueantes)

- **Dashboard de tokens/cache por tarefa** (tela Resumo/Workspace): tokens por
  fase, cache hit %, custo real vs estimado — visibilidade do impacto do cache.
- **Reuso de contexto entre fases da mesma task** via sessão do kimi
  (`--session`/`--continue` são documentados pelo CLI) em vez de só na
  re-execução da mesma fase.
- **Compressão de contexto do CLI** (`/compact` documentado) como alternativa
  ao item 5 para reduzir tokens dinâmicos.

## 2. Tarefas sugeridas

1. **Spike: capturar e documentar o stream-json real do kimi (usage/tokens)** —
   rodar `kimi -p --output-format stream-json` numa execução simples, inspecionar
   os eventos `meta` e registrar em `docs/` o formato real de `usage`
   (exemplo JSON) — ou a conclusão de que o campo não existe no stream.
   *Verificável*: documento em `docs/` com trecho real do JSON.

2. **Parse de `usage` no kimi_exec + persistência de tokens** — parsear o
   evento de uso (tokens de input/output/cache-hit quando presentes), estender
   `RunEvent` com campos aditivos de tokens (`db.ADDITIVE_COLUMNS`) e registrar
   um `RunEvent.kind` próprio (ex.: `usage`) com o payload completo.
   *Verificável*: teste em `test_kimi_exec.py` com fake emitindo `usage` no
   `meta`; evento com tokens no payload; migração aditiva.

3. **Métricas de cache hit (kimi + opencode) e exposição na timeline/UI** —
   capturar tokens de cache de ambos os executores (no opencode, do payload do
   `step_finish`), somar por ocorrência em `timeline.py` e expor na tela
   Workspace e Resumo. *Verificável*: endpoint de timeline retorna campos de
   cache; UI exibe quando > 0.

4. **Custo real no orçamento com fallback estimado** — `budget.interaction_cost`
   passa a calcular custo real por tokens (preço por token configurável via
   `AUTOIA_*`) quando `usage` existir, mantendo a estimativa como fallback;
   UI sinaliza real vs estimado. *Verificável*: teste de orçamento com fake
   emitindo `usage` → `cost_spent` usa valor real; fallback preserva o
   comportamento atual.

5. **Resume de sessão para opencode** — investigar a flag de continuação do
   `opencode run` (equivalente a `--session`/`--continue`; a confirmar com o
   CLI) e reutilizar `TaskStep.session_id` no dispatch do runner, espelhando
   `_should_resume`/`_resume_prompt` do kimi. *Verificável*: teste de
   re-execução de fase com executor opencode em que o comando spawnado contém
   o id da sessão anterior.

6. **Handoff/prompt enxuto por fase** — compactar as seções antigas do
   `step_context` (resumos de fases antigas em 1–2 linhas; textos integrais
   continuam no banco/eventos) para reduzir tokens dinâmicos por fase.
   *Verificável*: teste de tamanho do prompt em fase tardia abaixo de um
   limite configurado; conteúdo integral preservado no banco.

7. **Modelo mais barato para LLMs dedicadas e env de modelo para o kimi** —
   permitir `AUTOIA_KIMI_MODEL`/uso de `Robot.model` para prompts curtos
   (missão/resumo) com modelo mais barato (ex.: v4-flash); `-m <model>` no
   comando do executor quando definido. *Verificável*: config aceita env;
   executor spawna `-m` quando configurado; teste de seed com modelo por robô.

8. **(Opcional) Dashboard de tokens/cache por tarefa** — agregado de tokens,
   cache hit % e custo real vs estimado na tela Resumo/Workspace.
   *Verificável*: nova seção na UI com dados do endpoint de timeline.

## 3. Priorização

- **Fase 1 — medir (pré-requisito auditável):** tarefas 1 → 2 → 3. Sem
  telemetria não há como provar economia de cache; o spike (1) é rápido e
  desbloqueia o parse (2); métricas de cache (3) dependem do parse. Alto
  impacto, baixo-médio esforço. É a resposta direta à ideia: "otimizar cache
  barateando custos" exige medição.
- **Fase 2 — economizar:** tarefas 5 (resume opencode) e 7 (modelo por
  trabalho). Ganhos diretos de custo sem mudar contrato de dados. Médio-alto
  impacto, médio esforço.
- **Fase 3 — refinar:** tarefas 4 (custo real no orçamento/UI) e 6 (handoff
  enxuto). Dependem da telemetria da Fase 1 para terem dados; ajustam orçamento
  e custo por fase. Médio impacto, médio esforço.
- **Opcional:** tarefa 8 (dashboard) — valor de visibilidade, pode seguir o
  roadmap.

**Justificativa (impacto × esforço):** medir primeiro evita otimizar sem
métrica (o erro clássico de performance); resume/opencode e modelo por tipo de
trabalho são os ganhos de custo mais baratos de implementar; custo real e
handoff enxuto consolidam o ciclo depois que os dados existem.

## 4. Evidência

- `git branch --show-current` → `autoia/task-77`; `git status` → working tree
  clean, branch 1 commit à frente de origin/main.
- Leitura do handoff (`autoia_handoff.md`) e do relatório do iniciador
  (`docs/iniciacao-cache-deepseek.md`).
- `backend/app/worker/kimi_exec.py`: apenas `meta session.resume_hint` é
  tratado (linhas 244–245); demais `meta` → log (linhas 247–248). Comando de
  retomada `kimi -S <id> -p ...` (linha 89).
- `backend/app/worker/opencode_exec.py`: `step_finish` persiste
  `{"opencode_step": {reason, tokens, cost}}` no payload (linhas 260–274);
  comando sem resume (linha 107).
- `backend/app/budget.py`: `interaction_cost()` = `cost_per_interaction`
  (estimativa; docstring prevê troca quando o stream-json expuser `usage`).
- `backend/app/config.py`: `opencode_model="deepseek/deepseek-v4-flash"` (linha
  134), `cost_per_interaction=0.01` (linha 141), sem env de modelo para kimi.
- `backend/app/prompts.py`: `build_prompt` monta prefixo estável (missão +
  GIT_WORKFLOW + project_info + skills + repo_context + critérios + step_context
  + feedback/details/resume + HANDOFF_READ + contrato) — dinâmico entra depois
  do estável (favorável a prefix-cache).
- `backend/app/worker/runner.py`: `_should_resume` (linhas 537–559, usa
  `step.session_id` e eventos `attempt_started`/`phase_done`); `on_event`
  acumula `t.cost_spent` e avalia orçamento (linhas 1064–1090); `session_id`
  persistido no outcome (linhas 1112–1113).
- `backend/app/models.py`: `TaskStep.session_id` (linha 481),
  `RunEvent.payload` (JSON, linha 529) e `RunEvent.cost` (linha 530).
- `backend/app/timeline.py`: custo agregado por ocorrência (linhas 607–621).
- `frontend/src/pages/Workspace.tsx`: exibe `occ.cost` com tooltip "kimi
  estimado / opencode real" (linhas 286–288).
- `README.md` linha 184: roadmap "Custo real por tokens (usage do kimi) em vez
  de estimativa por interação."
- Docs oficiais do kimi-code (fetch): `kimi-command.html` — stream-json emite
  mensagens `assistant`/`tool_calls`/`tool` (sem campo `usage` documentado);
  resume via `--session [id]`/`-S` e `--continue`/`-c`; `sessions.html` —
  compressão de contexto automática (`/compact`) e persistência em
  `~/.kimi-code/sessions/`.
- `python3 --version` → 3.12.3; `python3 -m pytest --version` → "No module
  named pytest"; `import fastapi` → ModuleNotFoundError — suíte (50 arquivos)
  **não executada** nesta fase (sem `.venv`; `pip install` proibido pelas
  regras de trabalho).

## 5. Pendências

- Nenhuma proposta de tarefa gerada nesta fase — o papel `propose`
  (propositor, fim do pipeline `iniciador-analista-ux-propositor`) consolida
  as tarefas 1–8 acima em `autoia_tasks.json`.
- Formato do `usage` do kimi e flag de resume do opencode dependem de
  confirmação empírica com os CLIs reais (tarefas 1 e 5 — spikes).
- Suíte de testes não rodada (limitação do ambiente), como na fase anterior.

## 6. Para a próxima fase (auditor de usabilidade)

- O impacto de UX das tarefas 3, 4 e 8 (exibir tokens/cache/custo real na UI,
  tooltip e dashboard) deve ser avaliado com o usuário em mente: o que ele vê
  quando o cache funciona, quando o custo é real vs estimado, e quando o
  orçamento estoura por estimativa.
- As demais tarefas (1, 2, 5, 6, 7) são internas (backend/worker) e têm pouco
  impacto direto de usabilidade — o auditor pode focar nas telas Workspace,
  Resumo e TaskDetail (tooltips de custo, campo de modelo por robô).
