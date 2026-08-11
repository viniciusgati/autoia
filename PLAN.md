# PLAN.md — Monitor de Execução 2.0

Plano consolidado para: **propostas de tarefas com aprovação humana** (Parte B) → **página global "Execução"** (Parte A) → **TaskDetail como timeline-chat** (Parte C). Ordem de execução: **B → A → C**.

---

## Contexto / descobertas (validadas no código)

- `Resumo.tsx` e `Dashboard.tsx` são **código morto** (não roteados em `frontend/src/App.tsx`).
- `TaskDetail` usa `Timeline.tsx` + `PhasePanel.tsx` (usados **só ali**) — serão substituídos pelo chat, com toggle "Conversa | Técnico" preservando a visão antiga.
- Spawn atual: o robô propõe escrevendo `autoia_tasks.json` (prompt `TASK_SPAWN_TOOL` em `backend/app/prompts.py`); `_spawn_tasks` (`backend/app/worker/runner.py`) cria tasks filhas **só se `repo.allow_auto_tasks`** (default False) — sem o flag a proposta é descartada em silêncio.
- **Decisão:** propostas **sempre** ficam pendentes de aprovação humana (cards com **aceitar / rejeitar**); `allow_auto_tasks` vira **obsoleto** (ignorado; coluna mantida por compat; toggle removido da UI).
- Dados para reconstruir a conversa por tentativa: evento `attempt_started` marca cada tentativa; o texto final de cada tentativa = **último `assistant_text`** do intervalo; `step.summary` = relatório da **última** tentativa. Eventos disponíveis: `phase_done`, `bounce_back`, `merged`/`merge_failed`, `guardrail_blocked`, `budget_hit`, `subtask_*`, `task_spawned`, `subtasks_generated`, `pm_decision`, `human_gate`.
- Worker é **estritamente sequencial** → ordem (fase, tentativa) = ordem cronológica → o chat pode ser reconstruído sem mudança de schema.

---

## Parte B (base) — Propostas com aprovação humana

### Backend

1. **`backend/app/models.py`** — novo modelo `TaskProposal`:
   - `id`, `task_id` (FK), `step_id` (FK, nullable), `position` (int), `title`, `description`, `kind`, `target_repository_id` (FK, nullable), `status` (`pending` | `accepted` | `rejected`), `created_at`, `accepted_task_id` (FK, nullable — task criada ao aceitar).
   - Tabela nova é criada por `Base.metadata.create_all` (não precisa de migração aditiva).

2. **`backend/app/schemas.py`** — `TaskProposalOut`; adicionar `proposals: list[TaskProposalOut]` em `TaskOut` (TaskDetail carrega junto).

3. **`backend/app/worker/runner.py`** — refatorar `_spawn_tasks`:
   - **sempre** parseia `autoia_tasks.json` e grava/atualiza propostas `pending` (dedup por `task_id + title`; re-execuções não duplicam);
   - evento `task_spawned {count, titles}`;
   - **remover a auto-criação** de tasks;
   - extrair a criação da task filha (steps do pipeline, `parent_task_id`, `executor` herdado, budget) para função reutilizável no accept.

4. **`backend/app/api/tasks.py`**:
   - `GET /api/tasks/{id}/proposals`
   - `POST /api/tasks/{id}/proposals/{pid}/accept` → cria a task filha (valida `allow_external_tasks` p/ repo alvo) + marca `accepted` + `accepted_task_id` + evento
   - `POST /api/tasks/{id}/proposals/{pid}/reject` → marca `rejected` + evento

5. **`frontend/src/pages/RepoDashboard.tsx`** — remover o toggle `allow_auto_tasks` da UI de settings (a API continua aceitando o campo por compat).

### Frontend

- `frontend/src/types.ts` + `frontend/src/api.ts` (list/accept/reject de propostas).
- Novo componente `ProposalCard` (título, badge kind/repo, descrição, botões aceitar/rejeitar, link para a task criada quando aceita). Usado na página Execução (Parte A) e no chat (Parte C).

---

## Parte A — Página global "Execução" (`/execucao`)

### Backend

- **`backend/app/api/execution.py`** — `GET /api/execution` (1 request/poll; reusa `_build_notices` de `backend/app/api/dashboard.py`; filtro `?repository_id=`):
```jsonc
{
  "tasks": [TaskOut…],                            // ativas: created/queued/in_progress/needs_review/waiting_approval/blocked/paused
  "current_events": { "<step_id>": [RunEventOut…] },  // últimos ~30 eventos da fase running
  "proposals": [TaskProposalOut…],                // pendentes de aprovação
  "notices": [NoticeOut…],
  "worker": { "alive": bool, "last_heartbeat_sec": n }
}
```

### Frontend

- **`frontend/src/pages/Execution.tsx`** (rota `/execucao` em `App.tsx` + item **"Execução"** na seção Projetos do sidebar; polling 5s):
  1. header: status do worker · contagens · filtro por projeto;
  2. **sessões ativas**: card por task rodando → título/badge/executor, etapa atual, stepper, **comando atual**, **feed ao vivo**, **próximos passos** (fases pending à frente + "merge+push na main");
  3. **atenção humana**: needs_review / waiting_approval / blocked / guardrail → motivo + ações (aprovar, retornar ao dev, pausar, cancelar) — reuso `TaskCard`;
  4. **propostas**: cards das tasks propostas (pendentes) com aceitar/rejeitar;
  5. paradas (paused / created).

---

## Parte C — TaskDetail 2.0: timeline como chat (`/4/tasks/7`)

Sem mudança de backend — o chat é montado no frontend a partir de `getTask` + `listEvents(stepId)` por fase.

- **`frontend/src/lib/chat.ts`** — `buildTurns(task, eventsByStep)`:
  - mensagem 0: **📋 a tarefa** (descrição + critérios de aceite);
  - por fase, **um turno por tentativa**: conteúdo = `step.summary` (última) ou **último `assistant_text`** do intervalo; metadados: robô (nome+role), tentativa, veredicto, diff, custo, erro, duração;
  - **turnos system inline**: `bounce_back` ("QA reprovou → PO vai corrigir"), `merged`/`merge_failed`, `guardrail_blocked`, `budget_hit`, `subtasks_generated`, **`task_spawned` → cards de propostas**, `pm_decision`, `human_gate`.
- **`frontend/src/components/TaskChat.tsx`** — conversa estilo chat: bolhas com avatar/nome do robô, destaque em re-execução (`attempt > 1`, "↺"), conteúdo markdown, veredicto, expandir detalhes técnicos (eventos/diff/artifacts), link `/phase/:id`; fase `running` = bolha ao vivo (comando atual + eventos streaming) + auto-scroll.
- **`frontend/src/pages/TaskDetail.tsx` 2.0** — mantém: header sticky (status/custo/executor/pausar/cancelar), revisão humana (aprovar/retornar ao dev), edição de história em `waiting_approval`, feedback externo, subtasks. Troca a timeline vertical + painel pelo chat, com toggle **Conversa | Técnico**.

---

## Testes

- **`tests/test_proposals.py`**: `_spawn_tasks` grava pendentes + dedup; accept cria child (parent/steps/executor) e marca aceita; reject; validação cross-repo; propostas em `TaskOut`.
- **`tests/test_execution.py`**: tasks ativas, `current_events`, `proposals`, notices, worker.
- **Frontend**: `npx tsc --noEmit` + `npm run build` (não há vitest no front).

---

## Limpeza

- Remover `frontend/src/pages/Resumo.tsx` e `frontend/src/pages/Dashboard.tsx` (código morto).
- Migrar helpers úteis para `lib/`: `sessionEventLine`, `diffSummary`, `faseAtual`, `etapaAtualLabel`, `tempoDecorrido` (hoje em `Resumo.tsx`).

---

## Ordem de execução

1. **B** (propostas + backend + ProposalCard) — fundação de dados;
2. **A** (endpoint `/api/execution` + página Execução);
3. **C** (lib/chat + TaskChat + TaskDetail 2.0) + limpeza final.
