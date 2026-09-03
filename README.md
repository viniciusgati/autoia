# autoia

Plataforma web de **pipeline autônomo de desenvolvimento**: você registra um repositório
git, cadastra tarefas (issues, bugs, features) e robôs LLM executam o fluxo de ponta a
ponta — cada fase tem um robô (developer → qa → merger), que trabalha numa branch própria
e, no final, o merger integra tudo na branch default. Tudo com **logs no banco**,
**guardrails de segurança** e **orçamento por tarefa com revisão humana**.

> v1.0 — escopo enxuto: fluxo linear de "todos" (sem bounce-back automático), worker
> único, polling na UI.

## Arquitetura

```
┌──────────┐  React SPA (Vite) ── polling ──┐
│  browser │                                 ▼
└──────────┘                       ┌───────────────────┐   SQLite/Postgres
                                   │  FastAPI (REST)   │◄─── (SQLAlchemy)
                                   │  /api/...         │
                                   └────────┬──────────┘
                                            │ claim de steps
                                   ┌────────▼─────────┐
                                   │  Worker (único)  │
                                   │  ─ runner.py     │
                                   └────────┬─────────┘
        ┌───────────────────────────────────┼───────────────────────┐
        │ 1. gitops (clone/branch/commit)   │ 2. kimi -p stream-json|
        │ 3. guardrails em tempo real       │ 4. orçamento (cost)   |
        └───────────────────────────────────┴───────────────────────┘
```

- **Repositories**: repo git registrado (SSH ou caminho local/`file://`), clonado em
  `data/workspaces/<id>/`.
- **Robots**: configuração de agente (`name`, `mission`, `role`). Defaults (v1.1):
  `po` (refine), `qa` (review), `developer` (implement), `tester` (verify),
  `merger` (merge) e `pm` (decisões).
- **Pipelines**: template de fases ordenadas, cada fase aponta para um robô, com
  fases **pós-merge** (rodam na default após a integração). Defaults:
  `po-qa-dev-tester-avaliador-deploytest` (com teste final pós-deploy) e
  `po-qa-dev-tester-avaliador-merge` (sem deploy).
- **Tasks**: a unidade de trabalho. Ao iniciar, cria a branch `autoia/task-<id>` e o
  worker executa as fases em ordem, usando o kimi-code em modo não-interativo
  (`kimi -p --output-format stream-json`) com `cwd` no checkout.
- **TaskSteps**: o "todo" de cada fase (`pending → running → done/failed/guardrail_blocked`).
  Fases `post_merge` rodam na **branch default integrada** (pós-deploy).
- **RunEvents**: observabilidade — **toda interação** com o kimi (mensagens, tool calls,
  resultados), decisões do worker, bloqueios de guardrail e hits de orçamento.

## Fluxo de uma tarefa

1. Registrar repositório (clone).
2. Criar tarefa (título, descrição, tipo, orçamento) → `start`.
3. Worker executa a fase atual com o robô (prompt = missão + contrato de saída +
   contexto + regras). O **PO** transforma a ideia crua em história com critérios de
   aceite; o **QA** revisa a história (veredicto `READY`/`NEEDS_WORK`); o **tester**
   roda a suíte e valida cada critério (veredicto `PASS`/`FAIL` escrito em
   `autoia_verdict.txt`, lido e removido pelo worker).
4. Fase ok → próxima. Última fase (merger) → merge `--no-ff` + push na default (feito
   **pelo worker**, nunca pelo robô).
5. **Bounce-back automático**: fase reprovada (veredicto FAIL/NEEDS_WORK, timeout,
   guardrail ou erro) volta sozinha para a **fase anterior** com o relatório no prompt,
   até `max_attempts`.
6. Guardrail (comando arriscado / loop de calls idênticas / timeout) → interrompe a
   execução e grava o motivo.
7. Orçamento estourado → tarefa `needs_review`. O robô **PM** decide automaticamente:
   `retry` (repete uma fase), `continuar` (aumenta orçamento) ou `escalar` (deixa para
   humano) — limitado por `MAX_PM_DECISIONS` (default 2).
8. **Teste final pós-merge (v1.2)**: se o pipeline tiver fases `post_merge`, após a
   integração na default o worker faz `checkout` da main (espelho do remote) e o robô
   **deploy-tester** valida o **estado integrado** (suíte completa + critérios).
   Falha pós-merge **não reverte** o merge: tarefa vai para `needs_review` e o PM decide
   (re-testar na main ou escalar para humano). Fases pós-merge nunca fazem commit.

## Guardrails (v1)

- **Isolamento**: robôs rodam com `cwd` restrito ao checkout, em branch própria; nenhum
  `git push` sai do worker; merges serializados.
- **Política de comandos em tempo real**: cada `tool_call` é inspecionada no stream;
  comandos arriscados (`rm -rf`, `mkfs`, `sudo`, `curl/wget`, `ssh/scp`, `chmod 777`,
  `git push`, `git checkout main`, instalações globais, etc.) matam o processo na hora.
  Caminhos de arquivos fora do workspace também são bloqueados.
  Configuração: `AUTOIA_RISKY_PATTERNS` (JSON list).
- **Anti-loop**: `AUTOIA_RUN_TIMEOUT` (30 min), `AUTOIA_MAX_IDENTICAL_CALLS` (3), retry
  limitado por `AUTOIA_MAX_ATTEMPTS` (3).
- **Orçamento**: `AUTOIA_TASK_BUDGET` (US$ 10) e `AUTOIA_COST_PER_INTERACTION`
  (US$ 0,01/interação estimada). Estourou → `needs_review`.

> **Limitação honesta**: o guardrail detecta o comando perigoso ao vê-lo no stream e
> **mata o kimi** — o comando que já foi emitido não pode ser desfeito. Por isso o
> isolamento (branch + workspace + sem push) é a primeira linha de defesa. Use apenas
> repositórios confiáveis.

## Setup

Requisitos: Python ≥ 3.12, Node ≥ 20, git, e o CLI `kimi` no PATH (com login ativo).

```bash
# Backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# Frontend
cd frontend
npm install
```

## Rodando

```bash
# Terminal 1 — API (http://127.0.0.1:9000)
autoia-api
# ou: uvicorn app.main:app

# Terminal 2 — worker (executa as fases)
autoia-worker

# Terminal 3 — frontend (http://localhost:5173)
cd frontend && npm run dev
```

Primeiros passos na UI: **Repositories** → adicionar repo → **Tasks** → nova tarefa
(repo + pipeline `po-qa-dev-tester-avaliador-deploytest`, descreva a **ideia crua**) → iniciar →
acompanhar no **Resumo** (mobile) ou no **TaskDetail** (timeline com polling a cada
~1,5 s, transcript completo dos eventos).

### Acesso pelo celular (PWA)

- O frontend é um **PWA** instalável (manifest + service worker no build): no celular,
  use o navegador, **instale** ("Adicionar à tela inicial") e monitore as tarefas como
  um app.
- Para expor a API + frontend na sua rede: `AUTOIA_API_HOST=0.0.0.0 autoia-api` (a API
  serve também o build do frontend em `frontend/dist`; rode `cd frontend && npm run
  build` antes). Acesse via `http://<ip-da-máquina>:9000`.
- **Atenção**: o service worker (instalação do PWA) exige **HTTPS** (secure context).
  Em LAN use um túnel com HTTPS grátis (`cloudflared tunnel`, `ngrok`) apontando para a
  porta 9000; sem instalar, a UI funciona normalmente via HTTP.
- Tela **Resumo** (`/resumo`): cards concisos por tarefa (status, fase atual, custo,
  último resumo) com polling de 5 s — otimizada para o celular.

## Configuração (variáveis de ambiente `AUTOIA_*`)

| Variável | Default | Descrição |
| --- | --- | --- |
| `AUTOIA_DATABASE_URL` | `sqlite:///data/autoia.db` | URL do banco |
| `AUTOIA_WORKSPACE_DIR` | `data/workspaces` | checkouts dos repositórios |
| `AUTOIA_LOG_DIR` | `data/logs` | transcripts brutos dos runs |
| `AUTOIA_KIMI_BIN` | `kimi` | binário do kimi-code |
| `AUTOIA_OPENCODE_BIN` | `opencode` | binário do opencode |
| `AUTOIA_CODEX_BIN` | `codex` | binário do codex (OpenAI Codex CLI) |
| `AUTOIA_CODEX_MODEL` | *(vazio)* | modelo default do codex (vazio = `~/.codex/config.toml`) |
| `AUTOIA_CODEX_MODELS` | `gpt-5.6-luna,…` | modelos do seletor quando o `codex debug models` falha (lista JSON) |
| `AUTOIA_RUN_TIMEOUT` | `1800` | timeout por fase (s) |
| `AUTOIA_MAX_IDENTICAL_CALLS` | `3` | loop de calls idênticas → kill |
| `AUTOIA_MAX_ATTEMPTS` | `3` | limite de retries por fase |
| `AUTOIA_TASK_BUDGET` | `10.0` | orçamento padrão por tarefa (US$) |
| `AUTOIA_PM_BUDGET_TOPUP` | `5.0` | aumento de orçamento quando o PM decide `continuar` |
| `AUTOIA_MAX_PM_DECISIONS` | `2` | limite de decisões do PM por tarefa |
| `AUTOIA_COST_PER_INTERACTION` | `0.01` | custo estimado por interação |
| `AUTOIA_RISKY_PATTERNS` | lista padrão | padrões regex de comandos bloqueados (JSON) |
| `AUTOIA_BRANCH_PREFIX` | `autoia` | prefixo das branches de trabalho |
| `AUTOIA_DB_RULE` | regra padrão | instrução de banco no AGENTS.md gerado (ex.: declarar um PostgreSQL local de testes) |
| `AUTOIA_API_HOST` / `AUTOIA_API_PORT` | `127.0.0.1` / `9000` | bind da API |

## API (resumo)

- `POST /api/repositories` — registra e clona (`{name, url, default_branch}`)
- `GET /api/repositories`, `GET/POST/PUT/DELETE /api/robots`, `GET/POST/DELETE /api/pipelines`
- `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/start`, `POST /api/tasks/{id}/review` (`approve|cancel`),
  `POST /api/tasks/{id}/steps/{pos}/retry`
- `GET /api/steps/{id}/events?kind=&offset=&limit=`, `GET /api/steps/{id}/log`
- `GET /api/dashboard`

## Testes

```bash
pytest
```

Cobrem: parser do stream-json (com kimi fake), guardrails (comando arriscado, caminho
fora do workspace, calls idênticas, timeout), orçamento (`needs_review` + revisão),
gitops com git real (branch/merge/push/conflito) e o fluxo completo da API + worker.

## Roadmap (pós-1.0)

- Bounce-back automático (QA reprovou → volta para o developer com o relatório).
- Custo real por tokens (usage do kimi) em vez de estimativa por interação.
- WebSocket em vez de polling; multi-worker + Postgres + fila externa.
- PRs no GitHub/GitLab em vez de merge direto; sandbox Docker.
- Revisão humana de diff entre fases.

## Segurança

Robôs rodam o kimi com **auto permission** (sem aprovação humana por chamada): eles
podem escrever arquivos e rodar comandos no checkout. Mitigações embutidas: branch
própria por tarefa, sem push de robô, guardrails em tempo real, timeout, orçamento com
ponto de revisão humana, e workspace separado por repositório. **Use apenas repositórios
que você confia.**
