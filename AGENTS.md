# AGENTS.md — autoia

Guia para agentes (e humanos) trabalharem neste repositório. Leia antes de mudar código.

## O que é o projeto

**autoia** é uma plataforma web de **pipeline autônomo de desenvolvimento**: o usuário
registra um repositório git e cria tarefas (ideias cruas); robôs LLM executam o fluxo de
ponta a ponta — escrever a história, revisar, implementar, testar, integrar e testar o
merge — com watchdogs de progresso, orçamento por tarefa e decisões automáticas (PM).

Nada é "mock": os robôs executam o **kimi-code CLI** real
(`kimi -p <prompt> --output-format stream-json`) em um checkout do repo. O sistema não
chama APIs de LLM diretamente.

## Stack

| Camada | Tecnologia |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (SQLite dev / Postgres-ready) |
| Worker | **síncrono, thread-based** (`subprocess`), um único processo |
| Frontend | React 18 + Vite + TypeScript (estrito), react-router, `vite-plugin-pwa` |
| Testes | pytest (`httpx` p/ TestClient) |
| Execução dos robôs | por **task**: `kimi -p --output-format stream-json` (JSONL no stdout) **ou** `opencode run --format json` (campo `Task.executor`: `kimi` \| `opencode`, default `kimi`) |

## Estrutura

```
backend/app/            # pacote `app`
  main.py               # create_app(), seed de robôs/pipelines, run_api/run_worker
  config.py             # Settings (env AUTOIA_*); watchdogs e orçamento
  db.py                 # engine/session; migrate_schema() ADITIVO (ADDITIVE_COLUMNS)
  models.py             # Repository, Robot, Pipeline(+Step), Task(+Step), RunEvent
  schemas.py            # Pydantic; espelha os tipos do frontend
  prompts.py            # contratos de saída por role + build_prompt()
  verdicts.py           # contrato autoia_verdict.txt (parse PASS/FAIL/READY/PM) + autoia_blocked.json/autoia_summary.json
  guardrails.py         # GuardrailViolation + análise (sem enforcement; ver watchdogs)
  budget.py             # custo por interação + limite
  timeline.py           # derivação DETERMINÍSTICA da timeline de execução (sem LLM)
  api/                  # routers REST (repositories, robots, pipelines, tasks, steps, dashboard)
  worker/
    runner.py           # loop do worker: claim -> executa -> decide (bounce-back/PM)
    exec_common.py      # ExecOutcome + kill por grupo/watchdog (compartilhado)
    kimi_exec.py        # subprocess do kimi (stream-json): streaming JSONL, timeout, kill
    opencode_exec.py    # subprocess do opencode (--format json): tool_use, custo REAL
    gitops.py           # clone/branch/commit/merge/push/checkout_default/diff
    project.py          # detecção de ecossistema + AGENTS.md gerado no checkout
    arch_metric.py      # métrica de mudança de arquitetura/deploy (evento arch_metric)
    summarizer.py       # resumo estruturado do desenvolvimento via executor (autoia_summary.json)
    step_summarizer.py  # resumo "O que foi entregue" por fase (autoia_step_summary.json)
    step_mission.py     # missão humana por execução de fase (autoia_step_mission.json)
    handoff.py          # autoia_handoff.md gerado antes de cada execução do robô
frontend/               # Vite + React (páginas em src/pages/, tipos em src/types.ts)
tests/                  # pytest; fixtures compartilhadas em conftest.py
```

## Arquitetura do fluxo (entenda antes de mexer)

- **Task** = lista ordenada de **TaskSteps** (fases). Cada fase tem um **Robot** com um
  `role`: `refine` (po), `review` (qa), `implement` (developer), `verify` (tester),
  `assess` (avaliador), `merge` (merger), `pm`. O seed cria 8 robôs e **2 pipelines**:
  `po-qa-dev-tester-avaliador-deploytest` (po, qa, developer, tester, avaliador, merger
  + deploy-tester pós-merge) e `po-qa-dev-tester-avaliador-merge` (sem fase pós-merge,
  para projetos sem deploy).
- **Fases `post_merge`** rodam na branch **default integrada** (`gitops.checkout_default`
  = fetch + `reset --hard origin/<base>`); fases normais rodam na branch `autoia/task-<id>`.
  O **merge+push acontece na última fase pré-merge** (feito pelo worker, nunca pelo robô).
- **Stack do projeto**: antes de cada execução do kimi (steps e PM), o worker grava um
  `AGENTS.md` **não versionado** na raiz do checkout (excluído via `.git/info/exclude`)
  declarando a tecnologia detectada do projeto + regras de padrão (não introduzir outra
  linguagem/framework) + regra de banco de dados (**PostgreSQL** por padrão; SQLite só em
  testes em memória — configurável via `AUTOIA_DB_RULE`, que pode declarar um PostgreSQL
  local de testes, ex.: host/porta/banco/credenciais). Se o repo já versiona um
  `AGENTS.md`, o dele prevalece. O `config.py` carrega opcionalmente um `.env` na raiz
  (sem sobrescrever env já setada).
- **Contrato de veredicto**: robôs `review`/`verify`/`assess`/`pm` escrevem
  `autoia_verdict.txt` na raiz do checkout (o worker lê, **remove** e decide).
  `verify`/`assess` sem veredicto = FAIL.
  Parse tolerante a preâmbulo (marcador como palavra isolada em qualquer linha) em
  `verdicts.py` — não restrinja à primeira linha.
- **Handoff entre fases** (`autoia_handoff.md`, em `worker/handoff.py`): o worker gera,
  **antes de cada execução do robô** (steps e PM), um documento **não versionado** na
  raiz do checkout com o histórico **COMPLETO** das fases anteriores (resumos integrais
  + veredictos) + diff atual + instrução da fase atual. Os robôs são instruídos no
  prompt a **ler o arquivo antes de começar** (`HANDOFF_READ`) e a documentar o trabalho
  no **texto final** com as seções *O que foi feito / Arquivos alterados / Evidência /
  Pendências / Para a próxima fase* (`HANDOFF_DOCUMENT`; exceto `refine` e `pm`, que têm
  formatos próprios). O worker persiste o texto final **INTEGRAL** em `TaskStep.summary`
  (nunca trunca) e regenera o handoff para a próxima fase.
- **Bounce-back automático**: falha de fase **pré-merge** (veredicto FAIL/NEEDS_WORK,
  timeout, erro) → a **fase anterior** volta a `pending` com o relatório
  completo no contexto, até `max_attempts`. Falha **pós-merge** → **nunca** bounce
  (código já integrado): task `needs_review` + evento `post_merge_failed` + PM decide.
- **Subtarefas**: fases `implement`/`verify` iteram sobre subtarefas (cada uma com seu
  bounce-back). Subtarefa com tentativas esgotadas (`failed`) volta a `pending` **toda
  vez que a fase implement é re-executada** (retry manual, instrução do usuário, decisão
  do PM) — reabrir o developer com só subtarefas `failed`/`done` terminaria a fase em
  `phase_done` sem executar nada (ignorando a instrução em silêncio).
- **PM** (`pm`): decide `retry <pos>` / `continuar` (top-up de orçamento) / `escalar`
  (default seguro). Limite por task: `AUTOIA_MAX_PM_DECISIONS` (default 2). Decisão
  inválida/ausente → **escalar**.
- **Executor por task**: `Task.executor` (`kimi` \| `opencode`, default `kimi`) define o
  CLI que roda cada fase e o PM (escolhido na criação da tarefa; tasks filhas herdam).
  `kimi_exec` estima custo por interação; `opencode_exec` usa o **custo real** do
  `step_finish` do `opencode run --format json` (tool_use com nome+input+output).
  Ambos compartilham `exec_common.ExecOutcome`; `_run_executor` no runner
  faz o dispatch.
- **Orçamento**: custo por interação (`AUTOIA_COST_PER_INTERACTION`, kimi) ou custo real
  (opencode); estourou → `needs_review`. `RunEvent.cost` acumula em `Task.cost_spent`.
- **Watchdogs de execução** (em `kimi_exec`/`opencode_exec`): o guardrail de comandos
  arriscados foi **removido** — a detecção era pós-emissão (o comando já rodava quando a
  `tool_call` chegava no stream), não impedia o dano e gerava falsos positivos que
  interrompiam trabalho legítimo (curl em loopback, `git push` em repo local de teste).
  A proteção real virá com o **sandbox de execução** (ainda a fazer). Permanecem: loop de
  mesma tool call N vezes (`max_identical_calls`) → kill; timeout por fase
  (`AUTOIA_RUN_TIMEOUT`); watchdog de "sem progresso" (`AUTOIA_NO_PROGRESS_TIMEOUT`).
  `guardrails.py` mantém `GuardrailViolation` (usado pelo watchdog de loop) e as funções
  de análise — sem uso de enforcement por enquanto.
- **Observabilidade**: **toda** interação vira `RunEvent` (assistant_text, tool_call,
  tool_result, system…) com payload **completo, sem truncar**. Log bruto em
  `data/logs/<step_id>.log`.
- **Métrica de arquitetura**: na fase de integração (última pré-merge) o worker grava o
  evento `arch_metric` (`score` 0-100, `level` alto/médio/baixo, `reasons`) a partir do
  diff da branch — sinaliza mudança drástica de deploy/arquitetura. O Dashboard expõe
  `notices`: tarefas que requerem atenção (`needs_review`, bloqueadas, custo
  alto ≥ 80% do orçamento, arquitetura).
- **Timeline de execução** (`app/timeline.py`): a UI de acompanhamento tem 3 níveis de
  detalhe (Resumo / Acompanhamento / Técnico). A timeline é **derivada de forma
  determinística** dos `RunEvent` (sem LLM) — cada `tool_call` vira um evento próprio
  (pareada com o `tool_result` para `status`/`output`/`duration_ms`) e cada evento tem um
  `summary` em PT-BR. Endpoint `GET /api/tasks/{id}/timeline`. `RunEvent` continua sendo a
  fonte de verdade.
- **Resumo do desenvolvimento** (`worker/summarizer.py` + `TaskSummary`): LLM dedicada
  (via executor da task, `role=summary` — contrato `autoia_summary.json` estruturado)
  gera um resumo persistido no banco (última versão em `Task.summaries[0]`); `GET
  /api/tasks/{id}/summary` + `POST .../summary/regenerate` (background). Falha NUNCA
  afeta o pipeline. A LLM só interpreta; `details`/`resume_instruction`/eventos são a
  fonte de verdade. **`Repository.auto_summary`** (config do projeto) liga a geração
  automática: `worker` chama `_maybe_auto_summary` a cada fase decidida e após a
  decisão do PM, em thread daemon com heartbeat próprio (guarda `_SUMMARY_IN_FLIGHT`
  evita gerações sobrepostas).
- **Bloqueio + retomada por instrução**: agente escreve `autoia_blocked.json`
  (`reason_type`/`reason`/`question`) quando não consegue continuar sozinho → worker marca
  fase e task como `blocked` (não é falha). `POST /api/tasks/{id}/blocked/continue` grava
  `Task.resume_instruction` (separada do contexto original), reabre a MESMA fase
  (attempt+1) e registra `user_intervention`/`execution_resumed` na timeline. A instrução
  entra no handoff/prompt da retomada. `Task.details` = detalhes adicionados pelo usuário
  durante o fluxo (entram no handoff das próximas fases).
- **Pedir decisão ao usuário** (`autoia_decision.json`, contrato `DECISION_TOOL` no
  prompt): o agente PARA e pergunta (`question`/`options`/`context`) quando uma escolha
  muda o rumo do trabalho. O worker trata como `blocked` com `block_reason_type =
  "decision_request"` (opções em `Task.block_options`); a resposta do usuário flui pelo
  mesmo caminho de instrução (`blocked/continue` ou `/instruction`).
- **Workspace (tela de trabalho)** — rota `/:repoId/tasks/:taskId/workspace`
  (`frontend/src/pages/Workspace.tsx`). A timeline é uma **lista cronológica de
  execuções de fase** ("ocorrências"): `timeline.derive_task_occurrences` agrupa os
  `RunEvent` por `(step, attempt)` usando `attempt_started` como fronteira — re-execuções
  viram NOVAS ocorrências e o histórico nunca é apagado. Endpoint
  `GET /api/tasks/{id}/workspace` (task + ocorrências + resumos + propostas por step +
  decisões) + `GET /api/tasks/{id}/steps/{position}/diff` (diff real via git,
  `gitops.diff_for_step`, commit `(fase N)`) + `POST /api/tasks/{id}/instruction`
  (instrução + `position` opcional p/ reexecutar a partir de uma fase — reusa a lógica do
  bounceback via `_rewind_pipeline`). Header fixo com status/custo/controles, campo de
  interação inferior fixo. `TaskDetail` atual fica para auditoria técnica.
- **"O que foi entregue" por fase** (`worker/step_summarizer.py` + tabela `StepSummary`):
  LLM dedicada (via executor da task, zero custo contábil) gera `autoia_step_summary.json`
  por `(step, attempt)` quando a fase termina `done` **ou falha** (gate
  `AUTOIA_STEP_SUMMARY`, default ligado), com **tom humano** (porquê de cada mudança;
  na falha, explicação clara do que falhou e onde). A UI mostra o `delivered` (LLM) ou o
  texto final do robô como fallback.
  `TaskStep.goal` ("O que será feito") é derivado deterministicamente da mission + título.
- **Missão de cada execução de fase** (`worker/step_mission.py` + tabela `StepMission`):
  o card de etapa responde "por que esta execução existe e o que ela precisa resolver"
  (não repete o objetivo original em re-execuções). LLM dedicada (via executor da task,
  zero custo contábil) gera `autoia_step_mission.json` em **background, no início da
  fase** — a `run` (contador real de `attempt_started`) chaveia a missão por ocorrência,
  e o arquivo fica excluído do histórico (`.git/info/exclude`) porque a fase pode
  commitar com `git add -A` enquanto a missão é gerada. Enquanto a missão LLM não fica
  pronta (ou se falhar), a UI usa `timeline.fallback_mission` (determinístico): instrução
  do usuário que motivou a execução > parada/reprovação da tentativa anterior da mesma
  fase > bounce-back de fase posterior > missão por papel. Gate `AUTOIA_STEP_MISSION`.

## Padrões de desenvolvimento (backend)

- Python tipado: `from __future__ import annotations`; SQLAlchemy com
  `Mapped[...]`/`mapped_column`; `Settings` é `@dataclass` lendo env `AUTOIA_*`
  (`config.py`); Pydantic v2 (`model_config = ConfigDict(from_attributes=True)`).
- **Sessões SQLAlchemy**: nunca compartilhe sessão entre threads. Use o
  `session_factory` (criado com `make_session_factory`); abra `with session_factory() as
  s:` por unidade de trabalho e **commite antes** de chamar outra função que abra sessão
  (evita lock no SQLite WAL). Não confie em lazy-load fora do `with`.
- **Worker é síncrono**: não converta para asyncio. Subprocess com
  `start_new_session=True` (kill por grupo); watchdogs de progresso avaliam no loop de
  leitura do stdout; `_kill_group` = SIGTERM → SIGKILL. No startup, o worker recupera steps
  `running` órfãos de restart/crash anterior (`recover_stale_steps` → voltam a
  `pending` para re-execução) — sem isso, um worker morto no meio de uma fase travava
  a task para sempre. **Instância única**: `acquire_worker_lock` (flock em
  `data/workspaces/worker.lock`) — um segundo `autoia-worker` se recusa a iniciar
  (dois workers disputando tasks causam fases rodando em paralelo). Além disso, o
  `claim_next` nunca reclama outra fase de uma task que já tem step `running`.
- **Robustez contra hang do executor**: além do timeout total (`run_timeout`), há o
  watchdog de **sem progresso** (`AUTOIA_NO_PROGRESS_TIMEOUT`, default 300 s; 0 =
  desligado): se o kimi/opencode ficar N s sem emitir NENHUMA saída no stdout
  (`make_no_progress_watchdog` em `exec_common.py`), o processo é morto e tratado
  como timeout → bounce-back/retry. **Retomada de sessão**: o `kimi` emite
  `meta session.resume_hint` com `session_id` no stream-json; o worker guarda em
  `TaskStep.session_id` e, numa re-execução da MESMA fase que foi interrompida sem
  concluir (`_should_resume`), chama `kimi -S <id>` para **continuar a mesma conversa**
  (contexto do LLM preservado) em vez de começar do zero. A fase concluída (phase_done)
  não retoma. Como fallback, o handoff/prompt da retomada inclui a **atividade da
  execução anterior** da fase (`_step_prior_activity`, determinístico).
- **Nunca trunque payloads** de `RunEvent` nem do log — "textos completos" é requisito.
- **Migração de schema é aditiva**: colunas novas entram em `models.py` **e** em
  `ADDITIVE_COLUMNS` (`db.py`). Nunca drop/rename coluna sem plano de migração.
- **Mensagens/strings em PT-BR** (UI, prompts, logs, commits), exceto identificadores.

## Padrões de desenvolvimento (frontend)

- TypeScript estrito; **sem biblioteca de UI** (CSS puro com variáveis em
  `styles.css`); sem store global. `types.ts` espelha `schemas.py`.
- Atualização em tempo real via **polling** (1,5 s no TaskDetail, 5 s no Resumo) — não
  introduza WebSocket sem discussão.
- PWA: `vite-plugin-pwa`; registro do SW só em produção (`import.meta.env.PROD`).
- A API serve o build (`frontend/dist`) — após mudar o frontend, rode `npm run build`
  para o build servido ficar atualizado.

## Padrões de desenvolvimento (testes)

- **Nunca rodar o kimi/opencode real nos testes**: use a fixture `fake_kimi` (conftest)
  que emite JSONL determinístico (formatos kimi `-p` **e** opencode `run <prompt>`)
  e opcionalmente escreve `autoia_verdict.txt` via regras (`VERDICT_RULES`:
  `ready_pass`, `fail`, `needs_work`, `pm_*`). Para criar arquivos no checkout, use
  `write_file=...`.
- Git real é permitido em `tmp_path` (fixture `bare_repo` — repo bare com commit inicial).
- `flow` fixture: app + session_factory + repo + task iniciada. `settings` fixture tem
  `max_pm_decisions=0` por padrão (PM desligado; ative nos testes de PM).
- Ao mudar `build_prompt`/contratos/missions, verifique `test_project.py`
  (`test_build_prompt_includes_project_info`) e o parse em `test_verdicts.py`.

## Como adicionar um robô novo

1. `main.py` → `SEED_ROBOTS` (name, role, mission em PT; mission aceita placeholders
   `{task_title}`, `{task_description}`, `{step_context}`, `{default_branch}`).
2. Se o papel precisar de comportamento novo no worker (veredicto, checkout, escrita de
   story), trate em `runner.py` por `robot.role` e atualize `prompts._CONTRACTS`.
3. Crie um pipeline no seed (`SEED_PIPELINES`) usando o robô.
4. Adicione testes (seed + fluxo com `fake_kimi`).

## Comandos

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest                      # suíte (52 testes; exige git no PATH)
autoia-api                  # API :9000 (serve frontend/dist; AUTOIA_API_HOST=0.0.0.0 p/ LAN)
autoia-worker               # worker (processo separado)
cd frontend && npm install && npm run dev   # frontend dev :5173
cd frontend && npm run build                # atualiza dist (servido pela API)
```

## O que NÃO fazer

- Não rodar `git commit`/`push` sem pedido explícito do usuário.
- Não adicionar bounce-back automático para fases pós-merge (é `needs_review` por
  design — reverter merge é arriscado).
- Não reverter o merge automaticamente em falha pós-merge (decisão do PM/humano).
- Não truncar conteúdo de eventos/log (requisito "textos exatos").
- Não rodar kimi real em testes nem adicionar dependências sem confirmar antes.
- Não modificar `data/`, `.venv`, `node_modules`, `frontend/dist` (ignorados).
