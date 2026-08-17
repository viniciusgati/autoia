# Iniciação — Análise: uso otimizado do cache do DeepSeek (redução de custos)

> Documento de iniciação (fase `iniciador` da task #77 — "Analise de feature").
> Mapeia o estado atual do repositório e os pontos de contato com o tema do
> brainstorm, para as fases seguintes (analista → auditor-ux → propositor)
> definirem tarefas com base em fatos do código.

## 1. Ideia original (brainstorm)

- **Título:** Analise de feature
- **Descrição:** "Precisamos pensar em algo para usar otimizadamente o cache do
  deepseek barateando custos."

O repositório analisado é a **própria plataforma autoia** — ou seja, a feature
proposta é uma evolução interna do produto que executa robôs LLM (DeepSeek via
`kimi`/`opencode`).

## 2. Estado atual do projeto

### Stack

| Camada | Tecnologia |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (SQLite dev / Postgres-ready) |
| Worker | síncrono, thread-based (`subprocess`), instância única (flock) |
| Frontend | React 18 + Vite + TypeScript estrito, sem lib de UI, polling |
| Testes | pytest (`httpx` TestClient), 50 arquivos em `tests/`, `fake_kimi` determinístico |
| Executores de robô | `kimi -p --output-format stream-json` **ou** `opencode run --format json` (por task: `Task.executor`) |

### Estrutura (resumo)

```
backend/app/            # main.py (create_app, seed), config.py, db.py, models.py,
                        # schemas.py, prompts.py, verdicts.py, budget.py, timeline.py,
                        # storage.py, guardrails.py + api/ (16 routers) + worker/ (15 módulos)
frontend/src/           # páginas (Workspace, TaskDetail, Chamados, Robots, ...), types.ts
tests/                  # 50 arquivos de teste + conftest.py + fixtures
docs/                   # ROADMAP.md, plano-sandbox-execucao.md, usabilidade-chamados.md
README.md, AGENTS.md, PLAN.md, Blueprints, start.sh, pyproject.toml
```

### O que existe e funciona hoje

- Pipeline autônomo ponta a ponta: Task → TaskSteps (fases) → RunEvent (transcript
  completo), com bounce-back, PM, subtarefas, handoff entre fases
  (`autoia_handoff.md`), resumos/missões por fase via LLM dedicada.
- 4 pipelines no seed, incluindo `iniciador-analista-ux-propositor` (o pipeline
  desta análise).
- Sandbox de execução (off/fs/full), gitops com lock de push, métrica de
  arquitetura, fluxo de chamados (Projeto > Épico > Chamado), auth por sessão.
- Últimos commits na `main` mostram produto ativo: custo por execução na timeline,
  config geral de armazenamento, modelo default `deepseek-v4-flash` + seletor por
  robô, autoia-stop.

## 3. Como a autoia interage com o LLM hoje (base do tema "cache")

### Executores e modelos

- `Task.executor` (`kimi` | `opencode`, default `kimi`) define o CLI de cada fase.
- Modelo: `Settings.opencode_model` default **`deepseek/deepseek-v4-flash`**
  (`config.py`); `Robot.model` (nullable) permite seletor por robô — a UI
  (`frontend/src/pages/Robots.tsx`) oferece deepseek-v4-flash / v4-pro / chat /
  reasoner. No executor kimi, o modelo é o configurado na CLI do usuário (não há
  env `AUTOIA_*` de modelo para kimi).

### Custo

- **kimi**: custo **estimado** por interação (`AUTOIA_COST_PER_INTERACTION`,
  default US$ 0,01) — `budget.py` declara explicitamente que é estimativa até o
  stream-json expor `usage` real de tokens.
- **opencode**: custo **real** do evento `step_finish` (`part.cost`), somado no
  payload `opencode_step` (com `tokens`).
- `RunEvent.cost` acumula em `Task.cost_spent`; orçamento estourado →
  `needs_review` + decisão do PM.

### Retomada de sessão (fator de cache)

- `kimi_exec` captura `meta session.resume_hint` → `TaskStep.session_id`;
  `runner._should_resume` retoma a **mesma conversa** (`kimi -S <id>`) quando a
  fase foi interrompida sem concluir — o contexto fica preservado, o que é o
  maior ganho natural de prefixo-cache.
- **opencode não tem equivalente** de resume de sessão: re-execução começa do zero.

### Estrutura do prompt (fator de cache)

- `prompts.build_prompt` monta: missão + `GIT_WORKFLOW` + `project_info` +
  `skills_info` + `repo_context` (+ critérios/feedback/detalhes) + `HANDOFF_READ` +
  contrato + regras. O conteúdo **dinâmico** (handoff completo + diff + atividade
  de tentativas anteriores) entra **depois** do prefixo estável → o prefixo é
  idêntico entre fases do mesmo robô e entre tasks, favorável ao prefix-cache.

## 4. Lacunas de inicialização (o que falta para o tema)

1. **Sem telemetria de uso real no kimi**: o stream-json não é parseado para
   tokens/uso (só `session.resume_hint` é tratado; demais `meta` vão ao log).
   Impossível medir custo real e economia de cache no executor padrão.
2. **Sem métricas de cache hit** (ex.: `prompt_cache_hit_tokens` / `cached_tokens`
   do provedor): nenhum campo, evento ou dashboard captura isso; nem o `usage` do
   kimi nem o detalhe de cache do opencode são aproveitados (o `step_finish`
   persiste `tokens` no payload do evento, sem uso analítico).
3. **Orçamento estimado não reflete cache**: como o custo kimi é fixo por
   interação, uma redução real de custo por cache não aparece em `cost_spent` nem
   na UI (custo por execução na timeline).
4. **opencode sem resume de sessão**: re-execuções queimam contexto/cache do zero.
5. **Handoff cresce entre fases** (histórico integral + diff + atividade): tokens
   dinâmicos não são cacheáveis; o custo por fase tende a subir ao longo do
   pipeline.
6. **Sem estratégia de modelo por fase/tipo de trabalho** (ex.: missões/resumos
   com LLM dedicada usam o mesmo executor/modelo da task, sem escolha de modelo
   mais barato para prompt curto).
7. **Suíte de testes não executável no ambiente atual**: não há `.venv` nem pytest
   no Python do sistema (ver Evidência) — a suíte (50 arquivos) não foi rodada
   nesta fase; `pip install` é proibido pelas regras de trabalho.

## 5. Para a próxima fase (analista)

- Avaliar como **medir** primeiro: parse de `usage` no stream-json do kimi
  (formato real a confirmar com o CLI) e exposição de cache-hit tokens →
  telemetria é pré-requisito para "baratear custos com cache" de forma auditável.
- Avaliar **estratégias de promoção de cache**: estabilidade do prefixo do prompt
  (o handoff dinâmico já vem depois do prefixo estável — quantificar),
  resume de sessão para opencode, reuso de contexto entre fases da mesma task,
  e se `Robot.model`/executor por fase permite escolher modelo mais barato para
  trabalhos de prompt curto.
- Considerar impacto em orçamento/UI (custo real vs estimado; o README e o
  roadmap já listam "custo real por tokens (usage do kimi)" como item futuro).
- Nenhuma proposta de tarefa foi gerada nesta fase (papel do propositor).

## Evidência (fase iniciador)

- `git branch --show-current` → `autoia/task-77`; `git status` → working tree
  clean; `git log --oneline -10` → `05da874 chore: remove perfil .chrome-smoke...`
  (mais recente na main).
- `python3 --version` → 3.12.3; `pytest`/`python3 -m pytest` → not found;
  `import fastapi` → ModuleNotFoundError (deps não instaladas no ambiente).
- `ls tests/*.py | wc -l` → 50.
- Greps: `deepseek` → `config.py`, `frontend/src/pages/Robots.tsx`,
  `tests/test_sandbox.py`; `usage|tokens|cache` no backend → apenas o docstring
  de `budget.py` (nenhum parse de uso real).
