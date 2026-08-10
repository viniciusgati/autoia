# Roadmap de features — autoia

Ideias para evoluir a pipeline, organizadas por esforço e impacto.
Estado atual: workspace isolado, timeline visual, heartbeat do worker, 106 testes.

---

## 🚀 Alto impacto, baixo esforço

### 1. Múltiplos workers (paralelismo real)

**Problema:** o worker é single-thread — processa uma task por vez. Tasks ficam em fila.

**Solução:** o workspace isolado (`workspaces/{repo_id}/task_{task_id}/`) já garante que workers não conflitam.
Basta adicionar `--workers N` no CLI do `autoia-worker`:
- `worker_loop()` vira `worker_pool(N)`: N threads, cada uma com seu próprio loop
- `claim_next()` é atômico (UPDATE com WHERE), então N workers não pegam o mesmo step
- N tasks rodam em paralelo, cada uma no seu clone isolado

**Arquivos:** `runner.py` (adicionar `worker_pool`), `main.py` (parse do `--workers`)

---

### 2. Diff colorizado no painel e PhaseDetail

**Problema:** o `diff_stat` é mostrado como `<pre>` monocromático. Difícil de ler rapidamente.

**Solução:** parse simples do diff unificado:
- Linhas `+` → verde (`--ok`)
- Linhas `-` → vermelho (`--err`)
- Linhas `@@` → header com fundo sutil
- Adicionar botão "expandir diff" no painel lateral (hoje truncado em 160px)

**Arquivos:** `PhasePanel.tsx`, `PhaseDetail.tsx`, `styles.css`

---

### 3. Limpeza automática de workspaces

**Problema:** cada task deixa um clone em disco (`workspaces/{repo_id}/task_{task_id}/`). Com o tempo acumula.

**Solução:**
- Worker limpa o workspace da task quando ela atinge `done` ou `failed` (após gravar o último evento)
- Opcional: flag `AUTOIA_KEEP_WORKSPACES=false` (default true) pra debug
- Comando manual: `autoia-cleanup` para limpar workspaces de tasks concluídas há +N dias

**Arquivos:** `runner.py` (cleanup pós-task), `main.py` (comando cleanup)

---

### 4. Retry inteligente (bounce-back com contexto de erro)

**Problema:** quando uma fase falha, o bounce-back reexecuta a fase anterior com os mesmos parâmetros.
O robô não sabe POR QUE falhou, a menos que leia o handoff.

**Solução:** injetar o erro da fase que falhou DIRETO no prompt da fase que vai reexecutar:
```
## ⚠️ A fase seguinte (QA) rejeitou seu trabalho:
> VEREDICTO: NEEDS_WORK
> SUMMARY: história ambígua nos critérios 2 e 4
>
> Corrija os pontos acima e reenvie.
```
Isso já está parcialmente no handoff, mas poderia ser mais explícito no `step_context`.

**Arquivos:** `runner.py` (enriquecer `_build_step_context`), `prompts.py`

---

### 5. Configuração por projeto (override de settings)

**Problema:** `max_attempts`, `max_pm_decisions`, `run_timeout`, `task_budget` e outras configs são globais
(variáveis `AUTOIA_*`). Projetos diferentes têm necessidades diferentes — um projeto pequeno
tolera 2 retries, um crítico precisa de 5.

**Solução:**
- Adicionar colunas opcionais no modelo `Repository`:
  - `max_attempts` (int, nullable)
  - `max_pm_decisions` (int, nullable)
  - `run_timeout` (int, nullable)
  - `task_budget` (float, nullable)
  - `cost_per_interaction` (float, nullable)
  - `risky_patterns_extra` (json/text, nullable — patterns adicionais além dos globais)
  - `db_rule` (text, nullable — override da regra de banco)
  - `allow_auto_tasks` (bool, default false — se robôs podem criar tasks filhas)
  - `default_pipeline_id` (int, nullable — pipeline padrão ao criar task sem especificar)
- Merge: `repo.max_attempts or settings.max_attempts` (repo > global)
- UI: seção "Configurações" na página do repositório (ou modal), mostrando valor efetivo (global se não definido)
- Campo `risky_patterns_extra` é **aditivo** (soma com os globais), os demais são **sobrescrita**

**Arquivos:** `models.py` (colunas), `runner.py` (merge repo + settings), `schemas.py`, `repositories.py`, `RepoDashboard.tsx` (UI)

---

### 6. Task spawning — robôs criam tarefas (intra e cross-project)

**Problema:** hoje uma task = uma execução linear do pipeline. Mas ideias complexas naturalmente
se desdobram em múltiplas tarefas. Ex: "implementar autenticação" gera 3 tasks (model, OAuth, perfil).
Isso hoje é manual — o humano precisa criar cada uma.

**Solução:** dar aos robôs a capacidade de criar novas tasks via `tool_call`.

**Fluxo intra-project (mesmo repositório):**
1. O PO (fase `refine`) analisa a ideia e decide: "isso são 3 tarefas separadas"
2. O PO chama a tool `autoia_create_task` 3 vezes, com título, descrição e pipeline
3. O worker intercepta a `tool_call`, valida e cria as tasks filhas no banco
4. As tasks filhas herdam `parent_task_id`, aparecem linkadas na UI
5. A task pai termina como `done` com sumário listando as filhas

**Fluxo cross-project (repositórios diferentes):**
1. Projeto `backend` tem um repo de código; projeto `docs` tem um repo só de documentação
2. O developer (fase `implement`) termina uma feature e chama:
   ```
   autoia_create_task(
     title="Documentar endpoint POST /auth/login",
     description="Adicionar seção na API reference com request/response examples",
     kind="feature",
     repository="docs",
     pipeline="po-qa-dev"
   )
   ```
3. A task é criada no projeto `docs`, linkada à task original do `backend`
4. UI mostra tasks cross-project com badge "📄 docs" indicando o repo destino
5. Config: campo `allow_external_tasks` no repo destino (quem pode receber tasks de fora)

**Exemplo real multi-projeto:**
```
Projeto "api" — Task #5: "Sistema de autenticação" [done]
  ├── Task #6: "Model User + migração" [done]           ← mesmo repo
  ├── Task #7: "Login OAuth Google" [in_progress]        ← mesmo repo
  └── Task #12 (docs): "Documentar auth endpoints" [done] ← repo "docs"

Projeto "docs" — Task #12: "Documentar auth endpoints" [done]
  parent: Task #5 (api)
```

**Detalhes técnicos:**
- Tool: `autoia_create_task(title, description, kind, repository?, pipeline_id?)`
  - Se `repository` omitido → mesmo repo da task atual
  - Se especificado → busca repo por nome exato
- Guardrail: whitelist da tool no `guardrails.py` (não é comando de shell)
- Modelo: `parent_task_id` (FK → Task, nullable) — cria relação entre tasks
- API: endpoint interno `POST /api/tasks` acessível ao worker (já existe)
- UI: na TaskDetail, seção "Tarefas relacionadas" com cards linkados + badge do repo
- Config no repo: `allow_auto_tasks` (criar tasks em si mesmo), `allow_external_tasks` (receber de outros)

**Arquivos:** `models.py` (parent_task_id, allow_auto_tasks, allow_external_tasks),
`runner.py` (tool handler + interceptação de tool_call), `guardrails.py` (whitelist),
`api/tasks.py` (endpoint interno), `TaskDetail.tsx` (UI tree cross-project),
`RepoDashboard.tsx` (config)

---

## 🔔 Médio esforço

### 7. Notificações no browser

**Problema:** usuário precisa ficar olhando a tela pra saber se uma task entrou em `needs_review`.

**Solução:** usar a [Notification API](https://developer.mozilla.org/en-US/docs/Web/API/Notification):
- No `TaskDetail`, quando `task.status === "needs_review"`, dispara notificação
- Service Worker (já existe via `vite-plugin-pwa`) pode receber push notifications
- Alternativa mais simples: som de alerta + favicon animado (badge no ícone da aba)

**Arquivos:** `TaskDetail.tsx` (hook de notificação), `index.html` (permissão)

---

### 8. Webhook pós-task

**Problema:** não tem como integrar com CI/CD externo. Quando uma task termina, ninguém fica sabendo fora do autoia.

**Solução:**
- Campo `webhook_url` no modelo `Repository`
- Quando task atinge `done`, `failed`, ou `needs_review`, dispara POST com payload JSON:
  ```json
  {
    "event": "task.completed",
    "task_id": 5,
    "title": "Adicionar login OAuth",
    "status": "done",
    "cost_spent": 2.35,
    "steps": [...]
  }
  ```
- Configurável por repositório no form de criação/edição

**Arquivos:** `models.py` (coluna), `schemas.py`, `repositories.py` (API), `runner.py` (disparo)

---

### 9. Pull Request em vez de push direto

**Problema:** o merge é feito direto na branch default (main). Em times com code review humano, seria melhor abrir um PR.

**Solução:**
- Flag `merge_strategy` no `Repository`: `"direct"` (atual) ou `"pull_request"`
- No modo PR: em vez de merge+push, o worker faz push só da branch da task e chama a API do GitHub/GitLab pra abrir PR
- Precisa de token de acesso (campo `git_token` no repo)
- O título e descrição do PR são gerados a partir da história da task

**Arquivos:** `models.py`, `gitops.py` (push sem merge + API do provider), `repositories.py`

---

## 📊 Mais ambicioso

### 10. Dashboard analítico de métricas

**Problema:** o Dashboard atual (`RepoDashboard`) só mostra cards com contagens. Não dá pra ver tendências ou gargalos.

**Solução:** página dedicada com:
- **Pipeline health:** taxa de sucesso por fase (ex: "developer falha 30% das vezes, tester falha 5%")
- **Tempo médio por fase:** gráfico de barras — onde o pipeline passa mais tempo?
- **Custo acumulado:** linha do tempo de gastos, por task e total
- **Gargalo detector:** fases que causam mais bounce-backs
- Dados agregados do `RunEvent` e `TaskStep` (já temos tudo no banco)

**Arquivos:** nova página `Metrics.tsx`, endpoint `GET /api/metrics/{repo_id}`, queries agregadas

---

### 11. Fases condicionais / pipeline dinâmico

**Problema:** todo pipeline segue a mesma sequência fixa. Se o projeto não tem testes, o tester roda em vão.

**Solução:** regras de skip no pipeline:
- `skip_if: no_test_files` — pula tester se `glob("**/test_*")` retorna vazio
- `skip_if: is_documentation` — pula implementação se a task é doc-only
- O step marcado como `skipped` aparece na timeline em cinza claro com badge "pulado"
- A detecção roda no início do pipeline (ou o PO avalia e sugere skip)

**Arquivos:** `models.py` (campo `skip_condition`), `runner.py` (avaliação de skip), `Pipelines.tsx` (UI)

---

### 12. Chat interativo com a task em andamento

**Problema:** depois que a task inicia, não tem como "conversar" com ela. Se o developer interpretou errado, a tarefa vai até o fim errada e o tester rejeita.

**Solução:** interface de chat na página da task:
- Caixa de texto "falar com o robô atual"
- A mensagem é injetada no próximo `autoia_handoff.md` com prefixo `## 💬 Mensagem do humano:`
- O robô lê no início da fase e ajusta o comportamento
- Histórico de mensagens fica visível no chat da fase

**Arquivos:** `TaskDetail.tsx` (UI), `handoff.py` (injeção), endpoint `POST /api/tasks/{id}/message`

---

## Priorização sugerida

| Ordem | Feature | Por quê |
|---|---|---|
| 1 | Configuração por projeto | Fundação — cada repo com suas regras, workers, budget |
| 2 | Task spawning | Robôs criam tasks filhas, pipeline vira orquestrador |
| 3 | Múltiplos workers | Paralelismo real, ainda mais importante com task spawning |
| 4 | Diff colorizado | Impacto visual, 30 min de código |
| 5 | Limpeza de workspaces | Evita acumular lixo em disco |
| 6 | Retry inteligente | Melhora taxa de sucesso do bounce-back |
| 7 | Notificações browser | UX — não precisa ficar olhando a tela |
| 8 | Webhook pós-task | Integração com CI/CD externo |
| 9 | Pull Request | Code review humano antes do merge |
| 10 | Dashboard de métricas | Visibilidade de gargalos |
| 11 | Fases condicionais | Evita trabalho inútil |
| 12 | Chat interativo | Intervenção humana no meio do pipeline |
