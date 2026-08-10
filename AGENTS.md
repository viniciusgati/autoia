# AGENTS.md — autoia

Guia para agentes (e humanos) trabalharem neste repositório. Leia antes de mudar código.

## O que é o projeto

**autoia** é uma plataforma web de **pipeline autônomo de desenvolvimento**: o usuário
registra um repositório git e cria tarefas (ideias cruas); robôs LLM executam o fluxo de
ponta a ponta — escrever a história, revisar, implementar, testar, integrar e testar o
merge — com guardrails de segurança, orçamento por tarefa e decisões automáticas (PM).

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
  config.py             # Settings (env AUTOIA_*); padrões de risco dos guardrails
  db.py                 # engine/session; migrate_schema() ADITIVO (ADDITIVE_COLUMNS)
  models.py             # Repository, Robot, Pipeline(+Step), Task(+Step), RunEvent
  schemas.py            # Pydantic; espelha os tipos do frontend
  prompts.py            # contratos de saída por role + build_prompt()
  verdicts.py           # contrato autoia_verdict.txt (parse PASS/FAIL/READY/PM)
  guardrails.py         # política em tempo real de tool_calls (check_tool_call)
  budget.py             # custo por interação + limite
  api/                  # routers REST (repositories, robots, pipelines, tasks, steps, dashboard)
  worker/
    runner.py           # loop do worker: claim -> executa -> decide (bounce-back/PM)
    exec_common.py      # ExecOutcome + kill por grupo/watchdog (compartilhado)
    kimi_exec.py        # subprocess do kimi (stream-json): streaming JSONL, timeout, kill
    opencode_exec.py    # subprocess do opencode (--format json): tool_use, custo REAL
    gitops.py           # clone/branch/commit/merge/push/checkout_default/diff
    project.py          # detecção de ecossistema + AGENTS.md gerado no checkout
    arch_metric.py      # métrica de mudança de arquitetura/deploy (evento arch_metric)
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
  timeout, guardrail, erro) → a **fase anterior** volta a `pending` com o relatório
  completo no contexto, até `max_attempts`. Falha **pós-merge** → **nunca** bounce
  (código já integrado): task `needs_review` + evento `post_merge_failed` + PM decide.
- **PM** (`pm`): decide `retry <pos>` / `continuar` (top-up de orçamento) / `escalar`
  (default seguro). Limite por task: `AUTOIA_MAX_PM_DECISIONS` (default 2). Decisão
  inválida/ausente → **escalar**.
- **Executor por task**: `Task.executor` (`kimi` \| `opencode`, default `kimi`) define o
  CLI que roda cada fase e o PM (escolhido na criação da tarefa; tasks filhas herdam).
  `kimi_exec` estima custo por interação; `opencode_exec` usa o **custo real** do
  `step_finish` do `opencode run --format json` (tool_use com nome+input+output).
  Ambos compartilham `exec_common.ExecOutcome` e os guardrails; `_run_executor` no runner
  faz o dispatch.
- **Orçamento**: custo por interação (`AUTOIA_COST_PER_INTERACTION`, kimi) ou custo real
  (opencode); estourou → `needs_review`. `RunEvent.cost` acumula em `Task.cost_spent`.
- **Guardrails** (em `kimi_exec`/`opencode_exec` + `guardrails.py`): cada `tool_call` é
  inspecionada no stream; comando arriscado (`rm -rf`, `sudo`, `curl`, `git push`,
  `git checkout main`…) ou caminho fora do checkout → **mata o processo** (SIGTERM no
  grupo) e grava `guardrail_blocked`. Loop: mesma tool call N vezes
  (`max_identical_calls`) → kill. Timeout por fase (`AUTOIA_RUN_TIMEOUT`).
- **Observabilidade**: **toda** interação vira `RunEvent` (assistant_text, tool_call,
  tool_result, system, guardrail…) com payload **completo, sem truncar**. Log bruto em
  `data/logs/<step_id>.log`.
- **Métrica de arquitetura**: na fase de integração (última pré-merge) o worker grava o
  evento `arch_metric` (`score` 0-100, `level` alto/médio/baixo, `reasons`) a partir do
  diff da branch — sinaliza mudança drástica de deploy/arquitetura. O Dashboard expõe
  `notices`: tarefas que requerem atenção (guardrail, `needs_review`, bloqueadas, custo
  alto ≥ 80% do orçamento, arquitetura).

## Padrões de desenvolvimento (backend)

- Python tipado: `from __future__ import annotations`; SQLAlchemy com
  `Mapped[...]`/`mapped_column`; `Settings` é `@dataclass` lendo env `AUTOIA_*`
  (`config.py`); Pydantic v2 (`model_config = ConfigDict(from_attributes=True)`).
- **Sessões SQLAlchemy**: nunca compartilhe sessão entre threads. Use o
  `session_factory` (criado com `make_session_factory`); abra `with session_factory() as
  s:` por unidade de trabalho e **commite antes** de chamar outra função que abra sessão
  (evita lock no SQLite WAL). Não confie em lazy-load fora do `with`.
- **Worker é síncrono**: não converta para asyncio. Subprocess com
  `start_new_session=True` (kill por grupo); guardrails avaliam no loop de leitura do
  stdout; `_kill_group` = SIGTERM → SIGKILL. No startup, o worker recupera steps
  `running` órfãos de restart/crash anterior (`recover_stale_steps` → voltam a
  `pending` para re-execução) — sem isso, um worker morto no meio de uma fase travava
  a task para sempre. **Instância única**: `acquire_worker_lock` (flock em
  `data/workspaces/worker.lock`) — um segundo `autoia-worker` se recusa a iniciar
  (dois workers disputando tasks causam fases rodando em paralelo). Além disso, o
  `claim_next` nunca reclama outra fase de uma task que já tem step `running`.
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
