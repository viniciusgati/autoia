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

## Princípio: autonomia com solução definitiva (não corrigir "na unha")

O sistema precisa ser **autônomo**: a tarefa anda sozinha do início ao fim. Toda vez
que uma tarefa travar ou falhar, a correção preferida é **definitiva e via
prompt/modelo** — dar ao robô o contexto e a avaliação de que ele precisa para se
recuperar na re-execução:

- **Guardrail/timeout** → a avaliação do que aconteceu entra no prompt da re-execução
  (`guardrails.interruption_guidance`) para o robô **não repetir** o comportamento que
  matou a execução (ex.: loop de busca no mesmo alvo). O guardrail é rede de proteção,
  não o conserto: o conserto é o modelo entender e mudar de estratégia.
- **Reprovação de verificação** → o relatório completo (`autoia_verdict.txt` + resumo
  da fase) entra no handoff da fase que vai corrigir.
- **Bloqueio/decisão** → o robô PARA e pergunta (`autoia_blocked.json` /
  `autoia_decision.json`); a resposta do humano volta como instrução.
- **Falha de fluxo/estado** (deadlock de claim, reabertura incoerente, fase que nunca
  é reclamada) → corrija a **causa** no fluxo (ex.: reabrir a cadeia de fases
  não-concluídas), não a instância da tarefa.

Evite "consertar na unha" (patch pontual de estado/banco só para a tarefa andar): isso
esconde a causa e o sistema trava de novo na próxima tarefa. Correção manual de uma
task específica é **último recurso**, documentada — a solução definitiva deve entrar no
código/prompt (com teste) para valer para todas as tarefas.

## Stack

| Camada | Tecnologia |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (SQLite dev / Postgres-ready) |
| Worker | **síncrono, thread-based** (`subprocess`), um único processo |
| Frontend | React 18 + Vite + TypeScript (estrito), react-router, `vite-plugin-pwa` |
| Testes | pytest (`httpx` p/ TestClient) |
| Execução dos robôs | por **task**: `kimi -p --output-format stream-json` (JSONL no stdout) **ou** `opencode run --format json` **ou** `codex exec --json` (campo `Task.executor`: `kimi` \| `opencode` \| `codex`, default `kimi`) |

## Estrutura

```
backend/app/            # pacote `app`
  main.py               # create_app(), seed de robôs/pipelines, run_api/run_worker
  config.py             # Settings (env AUTOIA_*); watchdogs e orçamento
  db.py                 # engine/session; migrate_schema() ADITIVO (ADDITIVE_COLUMNS)
  models.py             # Repository, Robot, Pipeline(+Step), Task(+Step), RunEvent + Chamado/Epic/Project
  schemas.py            # Pydantic; espelha os tipos do frontend
  prompts.py            # contratos de saída por role + build_prompt()
  chamado_prompts.py    # prompts/contratos do fluxo de chamados (ferramentas + avaliação + conteúdo)
  verdicts.py           # contrato autoia_verdict.txt (parse PASS/FAIL/READY/PM) + autoia_blocked.json/autoia_summary.json + chamado_decision.json
  guardrails.py         # GuardrailViolation + análise (sem enforcement; ver watchdogs)
  budget.py             # custo por interação + limite
  timeline.py           # derivação DETERMINÍSTICA da timeline de execução (sem LLM)
  model_catalog.py      # catálogos de modelo SEMPRE do CLI (opencode models / codex debug models)
  storage.py            # configuração geral: medição (scan_storage) + limpeza de órfãos (clean_storage)
  api/                  # routers REST (repositories, robots, pipelines, tasks, steps, dashboard, chamados, system)
  worker/
    runner.py           # loop do worker: claim -> executa -> decide (bounce-back/PM)
    chamado_runner.py   # loop do CHAMADO-worker: ferramentas de etapa + avaliação de fechamento
    exec_common.py      # ExecOutcome + kill por grupo/watchdog (compartilhado)
    kimi_exec.py        # subprocess do kimi (stream-json): streaming JSONL, timeout, kill
    opencode_exec.py    # subprocess do opencode (--format json): tool_use, custo REAL
    codex_exec.py       # subprocess do codex (exec --json): thread/turn/item, custo estimado
    sandbox.py          # sandbox de execução (docker/bwrap): mounts, flags, rede, proxy egress
    gitops.py           # clone/branch/commit/merge/push/checkout_default/diff + lock/unlock push
    project.py          # detecção de ecossistema + AGENTS.md gerado no checkout
    arch_metric.py      # métrica de mudança de arquitetura/deploy (evento arch_metric)
    summarizer.py       # resumo estruturado do desenvolvimento via executor (autoia_summary.json)
    step_summarizer.py  # resumo "O que foi entregue" por fase (autoia_step_summary.json)
    step_mission.py     # missão humana por execução de fase (autoia_step_mission.json)
    handoff.py          # autoia_handoff.md gerado antes de cada execução do robô
frontend/               # Vite + React (páginas em src/pages/, tipos em src/types.ts)
tests/                  # pytest; fixtures compartilhadas em conftest.py
```

## Subsistema de CHAMADOS (fluxo de atendimento — paralelo à pipeline)

- **Conceito**: `Chamado` é entidade NOVA, independente de `Task`. Hierarquia
  **Projeto > Épico > Chamado** (`Project`/`Epic` pertencem a um `Repository`).
  O chamado percorre etapas de um **catálogo** (`ChamadoStageType`, global ou por
  repo; seed `SEED_STAGE_TYPES` em `main.py`: entrada/analise/desenvolvimento/deploy).
  `Chamado.workflow_status` = nome da etapa atual (status PRINCIPAL, sem relação com
  pipeline); `Chamado.status` = vida do chamado (aberto/em_andamento/respondido/
  cancelado/concluido/falhou).
- **Etapas** (`ChamadoStage`): `pendente`→`ativa` (aguardando usuário) → `aguardando`
  (ação encaminhada) → `executando` (worker) → `fechada`. `pending_action` sinaliza o
  que processar: `tool:<chave>` (rodar a ferramenta) ou `evaluate` (avaliar fechamento).
  Ao fechar, `decision` = `next_stage:<tipo>` | `resposta` | `cancelar` | `concluir`,
  validado contra `close_options` do catálogo (decisão inválida mantém a etapa `ativa`).
- **Ferramentas por etapa**: `allowed_tools` do catálogo (assistente/escopo/resposta —
  presets em `chamado_prompts.TOOL_PRESETS`). Rodam o executor (kimi/opencode) contra o
  checkout do chamado (`chamado_<id>`/`chamado_runner.py`) na branch default **read-only**
  (`gitops.checkout_default` + `lock_push` do `_run_executor`), **sem commit/veredicto/
  merge/bounce**. Interações viram `ChamadoMessage` (transcript por etapa, payload
  SEMPRE completo — espelho do `RunEvent`, porém task-independente).
- **Avaliação de fechamento**: robô escreve `chamado_decision.json` (parse em
  `verdicts.py`); worker aplica a transição. `POST /api/chamados/{id}/close` encaminha
  a avaliação; `POST .../tools/{tool}` encaminha a ferramenta com a última msg `user`.
- **Worker separado**: `autoia-chamado-worker` (`app.main:run_chamado_worker`), lock
  próprio (`chamado-worker.lock`), heartbeat `chamado-worker.heartbeat` (endpoint
  `/api/chamados/worker/status`). Recupera etapas `executando` órfãs no startup.
- **Conteúdo LLM de Projeto/Épico** (`chamado_runner.start_content_generation`):
  one-shot sem checkout (resumo do projeto, escopo/resumo do épico) em thread daemon
  com guarda de geração em voo; endpoints `.../summary/regenerate` e `.../scope/regenerate`.
- **Frontend**: páginas `Chamados.tsx` (lista+criação), `ChamadoDetail.tsx` (stepper de
  etapas + chat + ferramentas + fechamento), `Projects.tsx` (projetos/épicos + LLM);
  rotas `/:repoId/chamados`, `/:repoId/chamados/:id`, `/:repoId/projects`. Polling 2–5 s.
- **Testes**: `tests/test_chamados.py` usa `fake_kimi` com `write_file=chamado_decision.json`
  (regra própria, não `VERDICT_RULES`); o runner roda síncrono via `chamado_runner.claim/execute`.

## Arquitetura do fluxo (entenda antes de mexer)

- **Task** = lista ordenada de **TaskSteps** (fases). Cada fase tem um **Robot** com um
  `role`: `refine` (po), `review` (qa), `implement` (developer), `verify` (tester),
  `assess` (avaliador), `merge` (merger), `pm`. O seed cria 24 robôs e **12 pipelines**:
  `po-qa-dev-tester-avaliador-deploytest` (po, qa, developer, tester, avaliador, merger
  + deploy-tester pós-merge), `po-qa-dev-tester-avaliador-merge` (sem fase pós-merge,
  para projetos sem deploy), `po-qa-dev-tester-avaliador-deploytest-browser` (com
  browser-tester pós-merge), `iniciador-analista-ux-propositor` (brainstorm/análise:
  inicia o projeto, define tarefas/lacunas, audita usabilidade e SOMENTE o propositor
  (role `propose`, fase final) escreve `autoia_tasks.json` → propostas PENDENTES de
  decisão humana, sem merge automático de tasks filhas — iniciador/analista/auditor-ux
  não recebem a ferramenta de propostas e `_spawn_tasks` ignora o arquivo fora da fase
  do propositor), `advpl-po-qa-dev-tester-avaliador-merge` (fluxo ADVPL/Protheus:
  usa o `developer-advpl` em vez do developer) e as **deep-v2-*** (por superfície da
  tarefa: `deep-v2-backend`, `deep-v2-frontend`, `deep-v2-fullstack` + variantes
  `-lean`). Robôs novos do seed: **`qa-lean`** (role `review`, revisão de história
  enxuta) e **`validador`** (role `verify` — substitui o `tester` nas deep-v2, com
  missão de "provar que a entrega funciona" separada do `avaliador`/`assess`, que
  vira a avaliação final de qualidade/escopo). `deploy-tester`/`browser-tester`
  pós-merge nas deep-v2 de frontend/fullstack; backend só com `deploy-tester`.
- **Pipeline `android-noemu` (Android SEM emulador)** — para projetos Android cuja
  verificação não pode depender de emulador/dispositivo (boot/qemu = timeout e
  `infra_blocked`). Fases: `android-noemu-po` → `android-noemu-qa` →
  `android-noemu-dev` → `android-noemu-validador` → `android-noemu-avaliador` →
  `android-noemu-merger` → `android-noemu-deploy-tester` (pós-merge). Todos os robôs
  carregam `ANDROID_NOEMU_POLICY` (`main.py`): proíbem `connectedAndroidTest`/
  `connectedCheck`/`adb`/emulador/system image, exigem testes em `src/test`
  (JVM/Robolectric), `./gradlew test`, `assembleDebug`/`compileDebugKotlin` e revisão
  de código; ausência de device NÃO é FAIL (vai para `NÃO VERIFICADO:`). O contrato
  genérico de verify/assess cede à diretriz quando a missão declara device fora de
  escopo. Use o perfil de toolchain **`android-compose-37`** no repositório — o perfil
  `android-emulator-35` sobe o emulador no bootstrap e deve ficar fora deste fluxo.
- **Fluxo ADVPL (Protheus/TOTVS)** — regras específicas do desenvolvedor ADVPL
  (`developer-advpl`, role `implement`; detecção por `gitops.is_advpl_robot`, nome
  contendo "advpl"):
  1. **Commit por run com prefixo do executor**: mensagem `[<executor>] - <goal da
     fase>` (`gitops.advpl_commit_message`, usando `Task.executor` — kimi/opencode);
     nas subtarefas, o prefixo usa o título da subtarefa.
  2. **Push a cada fase de desenvolvimento**: o worker publica a branch
     `autoia/task-<id>` no remoto (`gitops.push_branch`) após cada fase pré-merge do
     desenvolvedor (e por subtarefa), em vez de só no merge final.
  3. **Pull antes de cada execução do desenvolvedor**: `gitops.pull_branch`
     (fast-forward only, best-effort) atualiza a branch do remoto antes do robô rodar,
     para ele ver o que mudou (ex.: push de execução anterior após re-clone/reset).
  Robôs não-ADVPL seguem o comportamento original (`autoia: <título> (fase N)`, sem
  push por fase).
- **Associação Projeto > Épico na Task** (`Task.project_id`/`epic_id`, 0..1
  opcional — colunas aditivas em `db.ADDITIVE_COLUMNS["tasks"]`): metadados
  organizacionais, espelhando o fluxo de chamados — NÃO entram no handoff/prompt,
  não afetam execução/timeline/orçamento e são editáveis em qualquer status
  (padrão do campo `details`). `epic_id` (com ou sem `project_id`) deriva o
  `project_id` (o épico prevalece); trocar só o `project_id` limpa o épico se ele
  não pertencer ao novo projeto; `null` explícito remove (`project_id: null`
  remove projeto e épico; `epic_id: null` remove apenas o épico) — distinguido de
  campo ausente via `model_fields_set` no `TaskUpdateRequest` (`_apply_task_association`).
- **Propostas de tasks filhas** (`autoia_tasks.json` → `TaskProposal`, geradas SÓ
  pela fase do propositor): cada entrada tem descrição rica obrigatória (seções
  Contexto/Escopo/Critérios de aceite/Fora de escopo; mínimo ~350 caracteres) —
  entrada vaga é DESCARTADA com evento `task_spawn_rejected` (motivo na timeline);
  `surface` (`backend|frontend|fullstack`) ou `pipeline` (nome exato) → a proposta
  **já nasce com a pipeline da filha** (`deep-v2-<surface>` do repo > global;
  fallback: default do repo → pipeline da mãe, com `task_spawn_warning` se não achar);
  `depends_on` (posições 1-based do arquivo) → encadeamento traduzido para as posições
  reais. `coverage` (IDs `H1.1` etc. do `docs/PRODUTO.md`) é persistido em
  `task_proposals.coverage_json` e exibido no card; o worker audita o lote contra o
  documento e emite `task_spawn_coverage_warning` (histórias sem proposta, proposta
  backend que menciona UI, UI adiada sem tarefa de frontend/fullstack). **Regra de
  valor vertical**: história com resultado visível ao usuário exige `frontend` ou
  `fullstack`; UI não pode ficar "para depois" sem a proposta complementar no mesmo
  lote. Quando o documento de produto existe, o worker **acrescenta** uma proposta
  final `Aceite ponta a ponta do produto (browser real)` (`fullstack`, `coverage` =
  todas as histórias, dependente de todas; evento `task_spawn_e2e`) — ela valida o
  estado integrado e nunca é auto-aceita. O aceite (`POST .../proposals/{id}/accept`) resolve dependências para
  `Task.depends_on_task_id` (predecessora PENDENTE → 400; rejeitada → dispensada) e
  `POST .../proposals/accept-all` aceita tudo em ordem e já enfileira (`start=true`);
  o `claim_next` **só reclama a task quando a dependência está `done`** (evita
  branches paralelas/conflito de merge); `_sweep_stuck_tasks` move dependentes de
  dependência `failed`/`cancelled` para `needs_review` (`task_dependency_blocked`) e
  os **re-enfileira sozinho** (`task_dependency_resumed`) quando a dependência volta
  a andar (instrução humana/PM retry) ou conclui — sem isso o dependente ficava preso
  em `needs_review` para sempre e só destravava com "pode seguir" manual (task-252).
  A UI (`RepoProposals`) agrupa por task de origem e mostra ordem (`1º`, `2º`...),
  "requer antes" e a cobertura (`📌 cobre: H1.1, H2.2`) — as colunas são aditivas
  (`task_proposals.depends_on_json`, `task_proposals.coverage_json`,
  `tasks.depends_on_task_id`).
- **Fases `post_merge`** rodam na branch **default integrada** (`gitops.checkout_default`
  = fetch + `reset --hard origin/<base>`); fases normais rodam na branch `autoia/task-<id>`.
  O **merge+push acontece na última fase pré-merge** (feito pelo worker, nunca pelo robô).
  Fases pós-merge **NUNCA entram no ciclo de subtarefas** (`implement`/`verify` com
  `task.subtasks`): `deploy-tester`/`browser-tester` executam sempre como verificação
  livre na default integrada. Sem esse guard, elas caíam em `_decide_subtask_verify`,
  achavam todas as subtarefas já verificadas pelo validador pré-merge e terminavam
  `PASS` em ~2s sem abrir navegador/rodar nada (270 fases no histórico). Além disso,
  verificação de subtarefas só carimba `PASS` se a tentativa atual teve execução real
  (`tool_call`/`assistant_text`); sem execução vira `INCONCLUSIVE` → avaliador/revisão.
- **Fases somente-leitura** (`refine`/`review`/`assess`/`verify`): produzem apenas
  história/veredicto (texto final ou `autoia_verdict/blocked/decision`) e **não podem
  alterar código**. O worker detecta árvore suja e commits feitos pelo robô e descarta
  (`git checkout -- .`/`git clean -fd`/`reset --hard`, evento
  `readonly_phase_changes_reverted`) antes de decidir/commitar a fase. Exceções:
  `analyze` (iniciador pode criar o scaffold), `implement` e `merge` (resolução de
  conflito). Sem isso, o PO implementava e commitava o trabalho inteiro (task-326:
  35 arquivos de código/testes na fase 0) e o QA/avaliador podiam “consertar” código
  sem passar pelo developer.
- **História do PO só é gravada se for história**: o `refine` só atualiza
  `Task.description`/`acceptance_criteria` quando o texto final contém
  `## Descrição` **e** `## Critérios de aceite`. Texto solto/relatório de
  implementação gera evento `refine_story_missing` e a descrição original é
  preservada (task-326: a ideia crua foi substituída por um relatório do PO).
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
  `verdicts.py` — não restrinja à primeira linha. `review` tem regra de **convergência**:
  READY quando a história é implementável; depois que o PO aplica os apontamentos de um
  veredicto anterior, a revisão verifica **só esses itens** e não abre novos de polimento
  (evita loop QA↔PO — task-350).
- **Handoff entre fases** (`autoia_handoff.md`, em `worker/handoff.py`): o worker gera,
  **antes de cada execução do robô** (steps e PM), um documento **não versionado** na
  raiz do checkout com o histórico **COMPLETO** das fases anteriores (resumos integrais
  + veredictos) + diff atual + instrução da fase atual. Os robôs são instruídos no
  prompt a **ler o arquivo antes de começar** (`HANDOFF_READ`) e a documentar o trabalho
  no **texto final** com as seções *O que foi feito / Arquivos alterados / Evidência /
  Pendências / Para a próxima fase* (`HANDOFF_DOCUMENT`; exceto `refine` e `pm`, que têm
  formatos próprios). O worker persiste o texto final **INTEGRAL** em `TaskStep.summary`
  (nunca trunca) e regenera o handoff para a próxima fase. Numa reabertura por
  rewind/PM, a fase posterior que reprovou volta a `pending` mas seu relatório
  (`summary`/`verdict`/`error`) é **preservado** e entra no handoff do alvo — sem
  isso a instrução "corrija os apontamentos do QA" reabria o PO sem os apontamentos
  (task-350).
- **Bounce-back automático**: falha de fase **pré-merge** (veredicto FAIL/NEEDS_WORK,
  timeout, erro) → a **fase anterior** volta a `pending` com o relatório
  completo no contexto, até `max_attempts`. Falha **pós-merge** → **nunca** bounce
  (código já integrado): task `needs_review` + evento `post_merge_failed` + PM decide.
  Exceção: falha de **execução** numa subtarefa (`implement` com subtarefas —
  guardrail/timeout/commit) **re-tenta a PRÓPRIA fase**, não a anterior: a falha é do
  robô se perdendo (ex.: loop de busca), não da história; o prompt da subtarefa leva a
  avaliação (`guardrails.interruption_guidance`) e a subtarefa fica `pending` com o
  motivo. Reabrir o qa/po revisaria a história sem relação com o problema, queimaria
  as tentativas deles e travaria a pipeline (task-194). Esgotada a fase → `needs_review`.
- **Verificação inconclusiva → assessor**: veredicto **AUSENTE** na verificação (verify
  — validador/tester) por falha de infraestrutura (emulador/navegador/timeout/permissão)
  NÃO é defeito de código: em vez de bounce-back para o developer (nada a corrigir) ou
  de parar em `needs_review`, o worker encaminha o veredicto FINAL ao **avaliador** (role
  `assess`) quando ele existe na sequência — evento `handoff_to_assess`; o assessor
  decide PASS/FAIL por **revisão de código/diff**, sem depender da infraestrutura que
  quebrou. Vale para a fase verify com subtarefas (`_decide_subtask_verify`, `inconclusive:`) e
  para a verify sem subtarefas (`_decide`, veredicto AUSENTE). Sem assessor à frente →
  `needs_review` com diagnóstico. O prompt de verify orienta o robô a NUNCA deixar o
  veredicto ausente (escreve `autoia_verdict.txt` com o que conseguiu verificar + seção
  `NÃO VERIFICADO:` e FAIL com justificativa de infra se a evidência for séria); o
  CONTRACT_ASSESS orienta o avaliador a validar por revisão de código nesse caso.
- **Reabertura coerente de fase** (`_rewind_pipeline` da instrução e PM `retry`): reabrir
  uma fase com uma **anterior não-concluída** (`failed`/`guardrail_blocked`) reabre a
  **cadeia** da primeira não-concluída até o alvo — reabrir só o alvo deixava a task
  travada para sempre, pois `claim_next` exige todas as fases anteriores `done`
  (task-194). O PM `retry` de uma fase **`done`** reabre a partir dela (o próprio alvo
  + as fases seguintes) com **orçamento novo**, desde que o limite de decisões permita —
  é o caminho para corrigir a história no PO mesmo com o PO já concluído (task-350).
  `_sweep_stuck_tasks` também move para `needs_review` a task cuja primeira
  fase não-concluída está `failed`/`guardrail_blocked` mesmo com pendings posteriores.
- **Subtarefas**: fases `implement`/`verify` iteram sobre subtarefas (cada uma com seu
  bounce-back). **Subtarefa NÃO tem limite próprio de tentativas** — o contador é
  cumulativo entre implement e verify e, com limite, travava a task para sempre (nem a
  retomada humana renovava o orçamento). O limite real do loop é a **tentativa da fase
  implement** (incrementada a cada bounce-back; esgotada, a task vai a `needs_review`
  com `subtarefas reprovadas e fase de correção esgotada`). Re-execução da fase
  implement: subtarefas com tentativas esgotadas
  (`failed`) ou presas em `implementing` (worker morto no meio) voltam a `pending` —
  sem isso, reabrir o developer com só subtarefas `failed`/`done` terminaria a fase em
  `phase_done` sem executar nada (ignorando a instrução em silêncio). Se ainda assim
  NÃO há subtarefa pendente mas existe motivo externo (`resume_instruction` do usuário
  ou fase posterior `failed`/`guardrail_blocked` — ex.: FAIL do avaliador), o worker
  roda UMA **execução livre** do developer (`subtask.py:_run_free_implement`,
  evento `subtask_free_run`): o robô corrige o trabalho já entregue com a instrução/
  relatório no prompt e o texto final vira o `TaskStep.summary`. Sem motivo externo, a
  fase conclui sem rodar (nada a fazer). A instrução de retomada também entra DIRETA
  no prompt de subtarefa pendente (`_build_subtask_implement_prompt`), sem depender só
  do handoff.
- **Bookkeeping `autoia_subtasks_done.json` é do SISTEMA**: após cada execução da fase
  implement, o worker normaliza o arquivo como lista **acumulada** (posições 1-based
  de todas as subtarefas `implemented`/`done`) e commita se mudou
  (`_normalize_and_commit_subtasks_done`) — o robô não pode gravar só a posição atual
  (a task-86 morreu porque o dev gravou `[4]` em vez de `[1, 2, 3, 4]`). O prompt da
  ferramenta (`SUB_TASK_DONE_TOOL`) deixa explícito o conteúdo cumulativo. Se o repo
  **ignora** o arquivo (`.gitignore`), ele fica no checkout sem commit; qualquer falha
  de git no bookkeeping é best-effort (log) e **não derruba a fase** — antes um
  `git add` de arquivo ignorado esgotava as tentativas (task-343).
- **PM** (`pm`): decide `retry <pos>` / `continuar` (top-up de orçamento) / `escalar`
  (default seguro). Limite por task: `AUTOIA_MAX_PM_DECISIONS` (default 2). Decisão
  inválida/ausente → **escalar**. A decisão AUTOMÁTICA (`_maybe_pm`) captura o último
  `RunEvent` da task antes de rodar e é **descartada** se o usuário interveio durante
  o PM (instrução/"tentar novamente" → evento `pm_decision_discarded`): o estado que
  a motivou não existe mais e aplicar "escalar" por cima travava a task em
  `needs_review` mesmo com a execução nova concluindo (task-264). O PM manual
  (botão/API, sem âncora) aplica sempre. O contexto do PM mostra as **execuções reais**
  (`run`, contagem de `attempt_started`), não só o `attempt` — o QA re-executado após
  cada correção do PO mantém `attempt 1` e parecia "tentativa 1" (task-350).
- **Executor por task**: `Task.executor` (`kimi` \| `opencode` \| `codex`, default
  `kimi`) define o CLI que roda cada fase e o PM (escolhido na criação da tarefa;
  tasks filhas herdam). `kimi_exec` estima custo por interação; `opencode_exec`
  usa o **custo real** do `step_finish` do `opencode run --format json` (tool_use
  com nome+input+output); `codex_exec` roda `codex exec --json` (OpenAI Codex CLI;
  eventos `thread.*`/`turn.*`/`item.*`, `--model` só com modelo explícito, retomada
  por `codex exec resume <thread_id>`, custo estimado por interação). Todos
  compartilham `exec_common.ExecOutcome`; `_run_executor` no runner faz o dispatch.
- **Modelo por task/chamado**: `Task.model`/`Chamado.model` (colunas aditivas,
  editáveis em runtime como o executor) escolhem o modelo do executor. Precedência:
  **task > `Robot.model` > catálogo do CLI** (`model_catalog.resolve_opencode_model`:
  primeiro slug do `opencode models`; CLI fora → execução sem `-m`). O modelo da
  task vale também para PM, resumos/missões e subtarefas. **Sem lista fixa de
  fallback** (as envs `AUTOIA_OPENCODE_MODEL`/`AUTOIA_OPENCODE_MODELS`/
  `AUTOIA_CODEX_MODELS` foram removidas): o seletor de modelo do frontend é um
  **combobox com busca** que popula de `GET /api/system/{codex,opencode}/models`
  (fonte `cli`; CLI fora → `source="erro"` + lista vazia, com "tentar de novo" —
  o erro não é cacheado). Para opencode a escolha é **obrigatória** (o default
  dele não funciona bem): os formulários bloqueiam submit sem modelo e o codex
  mantém a opção "padrão do codex" (`AUTOIA_CODEX_MODEL`/config.toml).
- **Orçamento**: custo por interação (`AUTOIA_COST_PER_INTERACTION`, kimi) ou custo real
  (opencode); estourou → `needs_review`. `RunEvent.cost` acumula em `Task.cost_spent`.
- **Janela de pico** (`Settings.in_peak_hours`, env `AUTOIA_PEAK_HOURS_WINDOWS`
  `HH:MM-HH:MM,...` em UTC, default `01:00-04:00,06:00-10:00` seg–sex; vazio desliga;
  `AUTOIA_PEAK_HOURS_WEEKDAYS_ONLY=1|0`): durante a janela os workers **não reclamam**
  novas execuções (steps de task, estágios de chamado, ações de chat) — o trabalho já em
  andamento termina, mas nada novo começa. Defaults seguem o horário de pico do DeepSeek
  (tarifa dobrada); trava aplicada nos loops `worker_loop`/`chamado_worker_loop`/
  `chat_worker_loop` antes do claim, com heartbeat mantido fresco durante a pausa.
- **Watchdogs de execução** (em `kimi_exec`/`opencode_exec`): o guardrail de comandos
  arriscados foi **removido** — a detecção era pós-emissão (o comando já rodava quando a
  `tool_call` chegava no stream), não impedia o dano e gerava falsos positivos que
  interrompiam trabalho legítimo (curl em loopback, `git push` em repo local de teste).
  A proteção real é o **sandbox de execução** (`backend/app/worker/sandbox.py`):
  os executores rodam a CLI dentro de um contêiner (`docker run`) com FS do host fora
  do checkout/estado das CLIs somente-leitura ou ausente, `--cap-drop ALL`,
  `no-new-privileges`, sem root, `--pids-limit`/`--memory`/`--cpus`, tmpfs `/tmp`
  (ou bind quando o checkout/fake fica sob `/tmp`) e `--workdir` = checkout (mesmo path
  absoluto, para o gitops do worker e o robô verem a mesma árvore). Modos (`Settings.sandbox`
  / `Repository.sandbox`, env `AUTOIA_SANDBOX`): `off` (spawn direto; default até validado
  em produção) | `fs` (isolamento de FS/privilégios, rede host — transitório) | `full`
  (rede bridge + `host.docker.internal` + proxy de egress allowlist no host, fail-closed,
  mesma lista de `config.DEFAULT_WHITELISTED_HOSTS`). No `full`, o container recebe
  `HTTP(S)_PROXY` apontando para o host **e** `NO_PROXY=localhost,127.0.0.1,::1` — o
  loopback é o do próprio container (ex.: emulador em `:8554`), nunca o do host, e por
  isso `127.0.0.1`/`localhost` **não** entram na allowlist do proxy (qualquer porta em
  loopback do host seria alcançável). Um túnel só é fechado após
  `AUTOIA_PROXY_IDLE_TIMEOUT` (default 600 s; 0 = nunca) de silêncio — os 30 s antigos
  matavam chamadas de LLM no meio (TTFB > 30 s em prompt grande) e o executor ficava em
  retry silencioso até o watchdog de 900 s. Fallback: `AUTOIA_SANDBOX_FAIL_CLOSED=1`
  faz a falha do sandbox (docker indisponível) falhar a execução; sem ele, cai para direto
  com aviso no log. Permanecem os watchdogs de progresso: loop de tool calls idênticas
  (`max_identical_calls`) → kill; timeout por fase (`AUTOIA_RUN_TIMEOUT`); watchdog de
  "sem progresso" (`AUTOIA_NO_PROGRESS_TIMEOUT`), que **não** conta a janela de bootstrap:
  o sandbox emite `AUTOIA_BOOTSTRAP_DONE` no stdout logo antes da CLI e o relógio recomeça
  (o boot do emulador escreve só em stderr e leva minutos). Kill/stop file matam o
  contêiner (SIGTERM via `--sig-proxy` + `docker rm -f` pelo `--cidfile` registrado em
  `exec_common._ACTIVE_PROCS`). `guardrails.py` mantém `GuardrailViolation` (usado pelo
  watchdog de loop) e as funções de análise — sem uso de enforcement por enquanto.
- **Chamadas LLM puras não sobem emulador**: tudo que é só ler/escrever JSON
  (resumo de fase, resumo da task, missão, decisão do PM, dispatcher do chat) chama
  `_run_executor(..., skip_device_bootstrap=True)` — sem isso cada um bootava um emulador
  Android completo no perfil `android-emulator-*` e o host entrava em contenção
  (load/swap) que dispara o watchdog de "sem progresso" (task 195).
- **Semáforo de emuladores Android** (`exec_common.acquire_device_slot`, env
  `AUTOIA_ANDROID_MAX_CONCURRENT`, default 2): execuções cujo sandbox sobe um emulador
  (`bootstrap_extra`) seguram um slot de `flock` em `workspaces/.device-slots/` durante o
  dispatch — semáforo entre TODOS os processos (workers forkados + chamado/chat), não um
  `threading.Semaphore`. A espera é segura (heartbeat e `.stop-*` rodam em outra
  thread), **visível** (`slot_wait`/`slot_ready` na timeline/log) e **interrompível**:
  `make_step_abort_check` aborta a espera quando pause/instrução/rewind devolvem a fase
  para `pending` ou a task é cancelada — o executor nem inicia (task-250 ficou ~14 min
  "rodando" atrás de um slot ignorando a instrução da UI; o `.stop-task-*` é um pulso
  removido pelo heartbeat em até 5s e não serve para a espera, onde não há subprocesso).
  `0` desliga os slots; perfis sem emulador (LLM pura, genérico) nunca bloqueiam.
- **Imagens do sandbox: receita VERSIONADA + rebuild automático** (`docker/<perfil>/Dockerfile`
  → `toolchains.build_context_dir` → `sandbox.ensure_image`): as imagens vivem **só** no
  Docker local — uma limpeza de disco apagou a `autoia-android-emu:2026-09` (cujo Dockerfile
  nunca tinha sido commitado) e TODAS as tasks do perfil `android-emulator-35` morriam em
  `infra_blocked: ... imagem indisponível (fail-closed)` sem como se recuperar. Agora são
  dois gatilhos: (1) **na subida do worker** `main._preflight_sandbox_images` (thread daemon,
  após o fork) confere as imagens dos repositórios configurados e reconstrói as faltantes;
  (2) **no fail-closed**, antes de bloquear a fase, `runner`/`subtask` chamam o mesmo
  `ensure_image`. Lock `flock` por imagem (workers concorrentes esperam em vez de duplicar o
  build), log em `data/logs/docker_build_<tag>.log`, desliga com
  `AUTOIA_SANDBOX_BUILD_IMAGE=0` e limite em `AUTOIA_SANDBOX_BUILD_TIMEOUT` (2400 s);
  `AUTOIA_SANDBOX_DOCKER_DIR` muda a pasta de receitas. Imagem **sem** receita no repo →
  fluxo antigo (bloqueia). Perfis permitidos: `docker/android-emulator-35` (emulador
  self-contained) e `docker/android-compose-37`.
- **Teto global de execuções LLM** (`exec_common.acquire_llm_slot`, env
  `AUTOIA_LLM_MAX_CONCURRENT`, default 2): TODO dispatch de executor (fases **e**
  missões/resumos/PM/dispatcher) segura um slot de `workspaces/.llm-slots/` — limita
  chamadas simultâneas ao provedor. Medido: 27 chamadas/min (normal 2–5) dispararam
  `429 Rate limit exceeded` em cascata e o retry imediato do opencode amplificou o
  pico (18 chamadas ↔ 17 erros no mesmo minuto). Ordem de aquisição é sempre
  **device → llm** (consistente em todos os caminhos, sem ciclo). A espera é
  **visível** (`slot_wait`/`slot_ready`) e **interrompível** nas fases de task
  (`slot_abort_check` — ver semáforo de emuladores). `0` = sem teto.
  As gerações LLM pura usam ainda `AUTOIA_LLM_BG_TIMEOUT` (default 300 s, via
  `runner._effective_run_timeout`) — sem o teto, uma missão/resumo travado segura
  o slot por `run_timeout` (5400 s no repo 4) e empurra fases de verdade na fila.
- **Push bloqueado durante o robô** (`gitops.lock_push`/`unlock_push`): antes de cada
  execução o worker força `remote.origin.pushurl` para `none://` + hook `pre-push` que
  falha (restaurado no `finally`); defesa em profundidade com a rede restrita do sandbox.
- **Serviços do host**: o robô acessa o host por `AUTOIA_HOST_SERVICES_BASE`
  (`http://host.docker.internal` no modo `full`, senão `http://127.0.0.1`) — orientação no
  prompt (`GUARDRAIL_INSTRUCTIONS`) e no `AGENTS.md` gerado (`worker/project.py`).
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
  `Task.resume_instruction` (separada do contexto original), reabre a MESMA fase e
  registra `user_intervention`/`execution_resumed` na timeline. **Ação humana = novo
  orçamento de tentativas**: instruction/retry/bounceback manual resetam `attempt` das
  fases para 1 (o `run` da timeline nunca reseta e mantém o histórico) — sem isso, uma
  retomada após esgotamento falhava na primeira reprovação seguinte. A instrução
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
  por `(step, run)` quando a fase termina `done` **ou falha** (a `run` é a execução real
  de `attempt_started`; o `attempt` se repete após bounce-back, ex.: QA — task-350) (gate
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

## Modo human-in-the-loop (pipeline aberta via chat)

- **Modo por tarefa, alternável em runtime** (`Task.mode`: `auto` | `manual`;
  `TaskStep.execution_mode`: `auto` | `manual`, null = herda a task). Em `manual`, o
  auto-worker **não** reclama as fases (`claim_next` filtra `Task.mode != manual` e
  `execution_mode != manual`); quando uma fase `auto` chega numa fase `manual` (ou no
  fim da pipeline em modo manual), `_decide` transiciona para `TASK_OPEN` (evento
  `pipeline_opened`) em vez de avançar/integrar — o merge deixa de ser automático.
- **Chat + dispatcher**: o humano manda mensagem em linguagem natural
  (`POST /api/tasks/{id}/chat` → `TaskMessage(kind=user)` + `pending_action=dispatch`).
  O **`autoia-chat-worker`** (processo separado, lock `chat-worker.lock`, heartbeat
  `chat-worker.heartbeat`, endpoint `/api/tasks/chat-worker/status`) roda o robô
  `dispatcher` (role `dispatcher`, novo no seed) contra o checkout read-only, que
  escreve `autoia_dispatch.json` (`action: run_agent | merge | chat | ask`). O worker
  valida, resolve o agente (qualquer robô do repo — global + local, por nome OU role)
  e encaminha `run_agent:<run_id>` | `merge` | resposta direta.
- **Rodada de agente** (`TaskRun`): roda o agente no papel dele (`_run_executor` +
  `build_prompt` + handoff da sessão) na branch da task, **commita por fase** e devolve
  o resultado ao humano (`TaskRun.status`: `concluida`/`falhou`/`bloqueada`). **Sem
  bounce-back**: reprovação de veredicto (review/verify/assess), timeout ou erro viram
  resultado no chat e a task volta a `open` (o humano decide o próximo passo).
  `autoia_blocked.json`/`autoia_decision.json` do agente viram prompt no chat.
- **Merge sob demanda**: só quando o humano chama (`POST /api/tasks/{id}/merge` ou o
  dispatcher decide `merge`) — roda o robô `merger` (se existir) e depois
  `gitops.merge_and_push`; sucesso → task `done`.
- Tabelas novas: `task_messages` (transcript da task, payload sempre completo, espelho
  do `ChamadoMessage`) e `task_runs` (histórico de rodadas de agente). O `WorkspaceOut`
  ganha `messages`/`runs`/`agents`. Testes em `tests/test_chat.py` usam `fake_kimi`
  escrevendo `autoia_dispatch.json`.

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
- **Fase NUNCA sai do worker `running`** (`runner._fail_step_hard` no `finally` do
  `worker_loop`): um `running` órfão congela a task para sempre (o `claim_next` pula
  a task inteira) e só o restart o recupera. A exceção também não pode escapar do
  loop — ela matava o processo filho (`finally: os._exit(0)` em `main.py`) e já
  deixou 2 de 3 workers mortos em silêncio (regressão das tasks 233/234, 07/10: clone
  falhou, o registro da falha morreu com `database is locked`, o `_fail_step_hard`
  morreu junto e a exceção saiu do loop). O motivo da exceção vai para
  `TaskStep.error` + evento `step_forced_failed`; fase já decidida (done/failed) não
  é sobrescrita. A rede é **amarrada ao token da execução** (`claim_next_with_token`
  devolve o `started_at` do claim; `_fail_step_hard(expected_started_at=...)`): o
  `finally` de uma execução ANTIGA (ex.: PM lento por limite do provedor enquanto o
  usuário clica "tenta novamente") não derruba a execução NOVA que já reclamou a
  fase (task-264). Os caminhos de ERRO do `execute_step` (clone/branch git) também
  gravam por esse caminho. **Gravação crítica com retry** (`runner._persist_with_retry`,
  4 tentativas com backoff): um `commit()` repetido na MESMA sessão NÃO serve — o
  flush que falhou descarta o estado pendente (objeto some de `session.new`) e o
  retry grava silenciosamente nada; por isso a unidade é **re-executada numa sessão
  NOVA** (as funções re-aplicam os campos). False = não gravou (nunca lança: quem
  chama é o caminho de recuperação).
- **Pai supervisor dos workers** (`main._supervise_worker_children`): filho que sai
  sozinho é logado e **substituído** (e reapado — antes viravam zumbi para sempre e
  a fila andava com menos capacidade, parecendo "starvation sem worker livre");
  morte rápida repetida aumenta a espera (anti fork bomb).
- **Observabilidade do lock do SQLite** (`db.install_sqlite_lock_probe`, limiar
  `AUTOIA_SQLITE_LOCK_WARN_SECONDS`, default 2 s): warning para a **vítima**
  (`SQLite: escrita levou Xs (espera pelo lock)`) e para o **candidato a holder**
  (`SQLite: transação de escrita durou Xs antes do commit/rollback)`) — é assim que
  se acha quem segurou o lock numa tempestade de `database is locked`.
- **Robustez contra hang do executor**: além do timeout total (`run_timeout`), há o
  watchdog de **sem progresso** (`AUTOIA_NO_PROGRESS_TIMEOUT`, default 300 s; 0 =
  desligado): se o kimi/opencode ficar N s sem emitir NENHUMA saída no stdout
  (`make_no_progress_watchdog` em `exec_common.py`), o processo é morto e tratado
  como timeout → bounce-back/retry. Perfis Android ganham 2400 s por padrão
  (`runner.ANDROID_NO_PROGRESS_TIMEOUT_DEFAULT`, sobrescrevível por
  `Repository.no_progress_timeout`) porque uma suíte instrumentada longa deixa o
  stdout mudo; a janela de **boot** não conta (sentinela `AUTOIA_BOOTSTRAP_DONE`).
  O motivo do kill distingue "nunca houve saída desde o spawn (CLI/bootstrap não
  subiu)" de silêncio posterior (`exec_common.stall_reason`). **Retomada de sessão**: o `kimi` emite
  `meta session.resume_hint` com `session_id` no stream-json; o worker guarda em
  `TaskStep.session_id` e, numa re-execução da MESMA fase que foi interrompida sem
  concluir (`_should_resume`), chama `kimi -S <id>` para **continuar a mesma conversa**
  (contexto do LLM preservado) em vez de começar do zero. A fase concluída (phase_done)
  não retoma. Como fallback, o handoff/prompt da retomada inclui a **atividade da
  execução anterior** da fase (`_step_prior_activity`, determinístico).
- **Cota do provedor não vira "timeout sem progresso"**: `Go usage limit exceeded`
  (e congêneres) nunca chega ao stdout/stderr do opencode — fica só no log próprio da
  conta (`$XDG_DATA_HOME/opencode/log/opencode.log`). Ao terminar uma execução morta
  por watchdog/saída não-zero, `opencode_exec.provider_limit_from_opencode_state`
  varre esse tail (só `level=error` com timestamp do run atual) e classifica como
  `provider_limit:` → o runner **rotaciona** para a próxima conta do roster ou agenda
  `retry_at`, sem consumir tentativa (task 195: fase presa ~40 min num limite que já
  era conhecido do provedor).
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
- O `opencode models` também é bloqueado pela suíte: o conftest tem uma guarda
  **autouse** que deixa `model_catalog.opencode_models_from_cli` vazio (e zera o
  cache); teste que precisa do catálogo faz `monkeypatch` do mesmo alvo (depois
  do guard) — ver `tests/test_model_catalog.py`.
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
autoia-chamado-worker       # worker dos CHAMADOS (processo separado; pip install -e após mudar)
autoia-chat-worker          # worker do human-in-the-loop de tasks (processo separado)
autoia-cleanup              # remove workspaces de tasks concluídas há +N dias (--days N, --dry-run)
autoia-stop                 # para TUDO: serviços + órfãos dos robôs (emuladores/daemons) + limpeza
cd frontend && npm install && npm run dev   # frontend dev :5173
cd frontend && npm run build                # atualiza dist (servido pela API)
python -m app.perf_bench    # benchmark de performance (task sintética com 500 RunEvent; baseline/pós)
```

**Liberação de disco de workspaces**: com `AUTOIA_KEEP_WORKSPACES=0`, o worker
remove `workspaces/<repo>/task_<id>` do disco quando a task atinge `done`/`failed`
(após gravar o último evento) — default `1` preserva. O comando `autoia-cleanup`
faz o mesmo para tasks concluídas há +N dias (`--days N`, default 7; `--dry-run`
apenas lista), reusando a detecção de `storage.py` e sem tocar tasks ativas ou
concluídas recentes. A rotação de logs (`.log` antigos, `AUTOIA_LOG_RETENTION_DAYS`)
é coberta por `clean_storage`/`scan_storage` (`storage.py`).

**Parada total (`autoia-stop`)**: SIGTERM nos serviços (os workers matam os
executores e saem sem gravar falha fake — o step fica `running` e o próximo start
o recupera como `pending`), varredura de `/proc` para órfãos que escapam do grupo
do executor (emuladores Android, gradle daemons, processos com cwd no workspace),
`docker rm -f` pelos cidfiles do sandbox e limpeza de `.stop-*`/heartbeats. Prefira
sempre `autoia-stop` ou Ctrl+C no worker — `kill -9` direto no worker deixa os
executores órfãos (o comando varre, mas a fase morre gravada como falha).

## O que NÃO fazer

- Não rodar `git commit`/`push` sem pedido explícito do usuário.
- Não adicionar bounce-back automático para fases pós-merge (é `needs_review` por
  design — reverter merge é arriscado).
- Não reverter o merge automaticamente em falha pós-merge (decisão do PM/humano).
- Não truncar conteúdo de eventos/log (requisito "textos exatos").
- Não rodar kimi real em testes nem adicionar dependências sem confirmar antes.
- Não modificar `data/`, `.venv`, `node_modules`, `frontend/dist` (ignorados).
