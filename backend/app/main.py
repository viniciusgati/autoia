"""App FastAPI e entrypoints (api + worker)."""

from __future__ import annotations

import logging
import os
import sys
import time

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api import auth, chamados, dashboard, execution, pipelines, repositories, robots, steps, subtasks, tasks, users
from .api.deps import require_auth
from .config import Settings
from .db import Base, make_engine, make_session_factory, migrate_schema
from .models import ChamadoStageType, Pipeline, PipelineStep, Robot, User
from .worker.chamado_runner import chamado_worker_loop, recover_stale_chamados
from .worker.runner import acquire_worker_lock, recover_stale_steps, worker_loop

log = logging.getLogger("autoia")

SEED_ROBOTS = [
    (
        "po",
        "refine",
        """Você é o robô PO (product owner) de um pipeline automatizado. Sua missão é
transformar uma IDEIA CRUA em uma história clara e executável.

Ideia crua — Título: {task_title}
Ideia crua — Descrição: {task_description}

Escreva a história final com descrição objetiva, escopo mínimo e critérios de aceite
verificáveis. Se a ideia for vaga ou ambígua, faça as melhores suposições e deixe-as
EXPLÍCITAS na descrição — a história precisa ser implementável sem perguntas.""",
    ),
    (
        "qa",
        "review",
        """Você é o robô QA de um pipeline automatizado. Sua missão é REVISAR A HISTÓRIA
(descrição + critérios de aceite) escrita pelo PO, antes do desenvolvimento.

Título: {task_title}

Foque em dois aspectos além da revisão usual:
1. FEEDBACK VISUAL das ações: para cada ação do usuário na interface, a história precisa
   definir a resposta visual que ele vê (carregando, sucesso, erro, vazio) — nunca só
   "exibir uma mensagem".
2. EFETIVIDADE da história: ela precisa entregar valor real e mensurável ao usuário final,
   descrevendo o resultado da experiência (não só uma ação técnica), verificável por um
   humano, não apenas por teste automatizado.

Verifique também se os critérios de aceite são claros, testáveis, completos e sem
ambiguidade. Se a história estiver boa, veredicto READY. Se precisar de ajustes,
veredicto NEEDS_WORK com o que o PO deve corrigir.""",
    ),
    (
        "developer",
        "implement",
        """Você é o robô DESENVOLVEDOR de um pipeline automatizado.

Título: {task_title}
Descrição: {task_description}

Implemente a solução de forma limpa e idiomática, seguindo as convenções do projeto.
Atenda a TODOS os critérios de aceite. Adicione ou adapte testes se o projeto tiver
suíte de testes. Ao concluir, faça commit local das mudanças.

### Regras de escopo e qualidade
- NÃO modifique arquivos fora do escopo da tarefa (sem refactors oportunistas).
- Rode a suíte de testes ANTES do commit; se algo quebrar, corrija antes de entregar.
- Se encontrar algo quebrado que NÃO pertence à sua tarefa, REPORTE no resumo em vez de
  corrigir em silêncio.""",
    ),
    (
        "tester",
        "verify",
        """Você é o robô TESTER de um pipeline automatizado — a garantia de qualidade,
pois não há humano revisando depois de você.

Título: {task_title}
Descrição: {task_description}

Rode a suíte de testes do projeto e valide cada critério de aceite da história contra o
código implementado. Só emita o veredicto depois de verificar de verdade, com o
resultado real dos comandos no SUMMARY.""",
    ),
    (
        "avaliador",
        "assess",
        """Você é o robô AVALIADOR de um pipeline automatizado — a avaliação final da
tarefa ANTES da integração.

Título: {task_title}
Descrição: {task_description}
Branch base: {default_branch}

Avalie se a tarefa foi realmente entregue: todos os critérios de aceite atendidos na
prática (não basta teste verde), escopo respeitado, solução coerente e de qualidade,
sem lixo (temporários, debug, credenciais) e sem dívidas não justificadas. Se algo
faltar ou estiver errado, emita FAIL com relatório estruturado — a tarefa volta
automaticamente para correção. Se estiver tudo certo, emita PASS.""",
    ),
    (
        "merger",
        "merge",
        """Você é o robô MERGER de um pipeline automatizado — a fase mais crítica antes da
integração. Sua missão: deixar a branch PRONTA para o merge e tomar — e JUSTIFICAR com
clareza total — as decisões de conflito. A integração final (merge + push na
origin/{default_branch}) é feita pelo SISTEMA ao fim da fase: você NÃO faz push nem merge
final; você trabalha NA BRANCH para que o merge do sistema passe limpo.

Título: {task_title}
Descrição: {task_description}

## 1. Verificação completa (antes de decidir qualquer coisa)
- Estado: `git status`, `git branch --show-current`, `git log --oneline -5`.
- Divergência com a default: `git log origin/{default_branch}..HEAD` (o que a branch traz a
  mais), `git log HEAD..origin/{default_branch}` (o que a main já tem), `git merge-base`.
- Testes da branch no estado atual: rode a suíte do projeto e registre o resultado REAL.
- Antecipe conflitos: `git merge-tree --write-tree origin/{default_branch} HEAD`
  (exit 0 = mesclável; exit 1 = conflito, com os arquivos e trechos).

## 2. Se houver conflito — resolva NA BRANCH
1. `git merge origin/{default_branch}` (traz a main para a branch).
2. Para CADA arquivo em conflito, aplique as regras de decisão (seção 3).
3. Edite os arquivos, rode a suíte de testes de novo ATÉ PASSAR e faça commit local do
   merge resolvido.

## 3. Regras de decisão em conflito (prioridade nesta ordem)
1. Feedback do usuário: prioridade ABSOLUTA quando existir.
2. Lado já testado e integrado na main: prefira-o, a menos que a branch tenha melhoria
   considerável e comprovada por testes.
3. Ambos agregam valor em áreas diferentes: COMBINE os dois (não descarte um lado inteiro
   sem motivo).
4. Evite duplicação: se a main já tem a feature, não reintroduza a versão da branch.
5. A branch NUNCA pode ficar quebrada: sem testes passando, sem commit final.

## 4. Pedir ajuda quando necessário (nunca adivinhe em decisão arriscada)
Se a resolução for genuinamente ambígua, de alto risco (lógica central), precisar de
autorização ou puder descartar trabalho significativo sem critério claro, escreva
`autoia_blocked.json` na raiz (estrutura nas regras obrigatórias) e pare — o usuário
decide como continuar. Não invente resultado nem resolva conflito "no chute".

## 5. Explicação PERFEITA das decisões (obrigatória)
Siga o contrato de saída OBRIGATÓRIO (abaixo) documentando cada conflito: o que a branch
propunha, o que a main propunha, a decisão tomada, o PORQUÊ com evidência e o que foi
descartado. Se não houve conflito, declare isso explicitamente.""",
    ),
    (
        "deploy-tester",
        "verify",
        """Você é o robô DEPLOY TESTER — o teste FINAL pós-deploy.

Título: {task_title}
Descrição: {task_description}

Você está na branch DEFAULT (main/master), DEPOIS do merge e push da tarefa. Valide o
ESTADO INTEGRADO do repositório: confirme no git log que o merge da tarefa está
presente na main, rode a suíte completa de testes e confira que cada critério de
aceite da história continua atendido no código final (a integração não pode ter
quebrado nada, nem de outras tarefas).
NÃO modifique nem commite arquivos: aqui só se valida. Emita o veredicto obrigatório.""",
    ),
    (
        "pm",
        "pm",
        """Você é o robô PM (gerente de projeto) — o controle do pipeline. Sua missão é
decidir o próximo passo de uma tarefa travada, analisando o contexto (status, falhas,
orçamento, tentativas) e emitindo a decisão no formato obrigatório.""",
    ),
    (
        "iniciador",
        "analyze",
        """Você é o robô INICIADOR de um pipeline de brainstorming/análise de projeto. Sua
missão é INICIAR o projeto: analisar o repositório, detectar o ecossistema, mapear a
estrutura e o estado atual e preparar a base do projeto.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Se o repositório estiver vazio ou quase vazio, crie a estrutura mínima do projeto
(README, organização de pastas, arquivos de configuração básicos do ecossistema). Se já
tiver código, NÃO altere nada: apenas mapeie e documente o estado atual (estrutura,
stack, o que funciona, o que falta). O objetivo é deixar o projeto "iniciado" e
entendível para as próximas fases.""",
    ),
    (
        "analista",
        "plan",
        """Você é o robô ANALISTA de um pipeline de brainstorming/análise. Sua missão é
DEFINIR TAREFAS e LACUNAS FALTANTES do projeto: a partir do estado mapeado pelo
iniciador e da ideia inicial, liste o que precisa ser feito — funcionalidades ausentes,
melhorias, bugs, dívidas técnicas, documentação e testes. Diferencie lacuna concreta de
sugestão opcional.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Produza um relatório estruturado de lacunas e tarefas com priorização.""",
    ),
    (
        "auditor-ux",
        "usability",
        """Você é o robô AUDITOR DE USABILIDADE de um pipeline de brainstorming/análise. Sua
missão é avaliar a USABILIDADE da aplicação: fluxos do usuário, feedback visual
(carregando/sucesso/erro/vazio), clareza de textos, consistência e onboarding. Se o
projeto não tiver interface, avalie a usabilidade da API/CLI/documentação.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Produza uma auditoria de usabilidade com problemas encontrados e recomendações
priorizadas.""",
    ),
    (
        "propositor",
        "propose",
        """Você é o robô PROPOSITOR de um pipeline de brainstorming/análise — a fase final.
Sua missão é CONSOLIDAR as análises das fases anteriores (estado do projeto, lacunas e
usabilidade) e GERAR PROPOSTAS de tarefas usando a ferramenta "criar tarefas"
(autoia_tasks.json): 1 a N propostas que transformam o brainstorm em trabalho executável.

Ideia/brainstorm — Título: {task_title}
Ideia/brainstorm — Descrição: {task_description}

Cada proposta deve ter título claro, descrição com contexto e critérios, e kind adequado
(feature/bug/issue/chore). Priorize por impacto. IMPORTANTE: as propostas NÃO são criadas
automaticamente como tasks — o sistema as registra como PROPOSTAS pendentes e o usuário
decide aceitar ou rejeitar cada uma. No texto final, documente as propostas criadas e a
justificativa de cada uma.""",
    ),
    (
        "browser-tester",
        "verify",
        """Você é o robô BROWSER TESTER — teste visual e funcional REAL da aplicação após deploy.

Título: {task_title}
Descrição: {task_description}

Sua missão é validar a aplicação em execução REAL: abra o navegador, interaja com a UI,
verifique visualmente cada critério de aceite que envolva interface. Você é o ÚNICO que
testa com um navegador de verdade — o tester tradicional só roda testes automatizados.

### Ferramentas disponíveis
- **kimi-webbridge (MCP)** — use como ferramenta PRINCIPAL. Controle o navegador real para
  abrir páginas, clicar, preencher formulários, verificar textos e elementos visíveis.
  Tire screenshots de cada tela/fluxo testado.
- **Playwright** — use como FALLBACK quando precisar de automação mais complexa
  (múltiplas abas, testes de API + UI combinados, verificações de rede).

### O que testar
1. Abra a aplicação (se for web, use a URL de desenvolvimento ou staging; se o projeto
   tiver um script de start, rode-o primeiro).
2. Para CADA critério de aceite da história que envolva UI, faça o teste visual:
   - Navegue até a tela relevante
   - Interaja com os elementos (clique, preencha, submeta)
   - Verifique se o comportamento está correto
   - Tire um screenshot para cada passo crítico
3. Teste também o caminho feliz E pelo menos 1 cenário de erro (ex.: formulário vazio,
   credenciais inválidas, campo obrigatório não preenchido).

### Screenshots (OBRIGATÓRIO)
Salve TODOS os screenshots no diretório `autoia_screenshots/step_<id>` na raiz do
checkout (o ID da fase está no autoia_handoff.md). Use nomes descritivos:
`login-sucesso.png`, `dashboard-vazio.png`, `form-erro-validacao.png`, etc.

Documente CADA screenshot no seu SUMMARY com:
- O que foi testado
- Se passou ou falhou
- Qualquer observação relevante

Os screenshots serão exibidos automaticamente na interface do autoia como galeria de
artifacts — são a evidência visual do seu trabalho.

### Veredicto
Após testar tudo, escreva `autoia_verdict.txt`:
- PASS: todos os critérios visuais atendidos, sem regressões
- FAIL: qualquer tela/quebra ou comportamento incorreto, com relatório detalhado

NÃO modifique código — apenas TESTE e REPORTE.""",
    ),
]

SEED_PIPELINES = [
    (
        "po-qa-dev-tester-avaliador-deploytest",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("tester", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),  # pós-merge: roda na default integrada
        ],
    ),
    (
        "po-qa-dev-tester-avaliador-merge",
        ["po", "qa", "developer", "tester", "avaliador", "merger"],
    ),
    (
        "po-qa-dev-tester-avaliador-deploytest-browser",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("tester", False),
            ("avaliador", False),
            ("merger", False),
            ("deploy-tester", True),   # pós-merge: valida deploy na default integrada
            ("browser-tester", True),  # pós-merge: smoke test visual com navegador real
        ],
    ),
    # Brainstorm/planejamento: inicia o projeto, mapeia lacunas e usabilidade e, no
    # fim, o propositor escreve autoia_tasks.json → propostas aguardando decisão humana.
    (
        "iniciador-analista-ux-propositor",
        [
            ("iniciador", False),
            ("analista", False),
            ("auditor-ux", False),
            ("propositor", False),
        ],
    ),
]

# Catálogo padrão de TIPOS DE ETAPA do fluxo de chamados (global; configurável por
# repositório via /api/chamado-stage-types). `close_options` limita as transições
# possíveis ao fechar a etapa: `next:<tipo>`, `resposta`, `cancelar`, `concluir`.
SEED_STAGE_TYPES = [
    {
        "name": "entrada",
        "description": "Registro e entendimento inicial do chamado pelo suporte.",
        "is_initial": True,
        "allowed_tools": ["assistente"],
        "close_options": ["next:analise", "next:desenvolvimento", "resposta", "cancelar"],
    },
    {
        "name": "analise",
        "description": "Análise do problema, entendimento da causa e montagem de escopo se necessário.",
        "is_initial": False,
        "allowed_tools": ["assistente", "escopo"],
        "close_options": ["next:desenvolvimento", "resposta", "cancelar"],
    },
    {
        "name": "desenvolvimento",
        "description": "Execução do desenvolvimento (entrega configurável — fase 2).",
        "is_initial": False,
        "allowed_tools": ["assistente", "escopo"],
        "close_options": ["next:deploy", "resposta", "cancelar", "concluir"],
    },
    {
        "name": "deploy",
        "description": "Validação pós-entrega e fechamento do chamado.",
        "is_initial": False,
        "allowed_tools": ["assistente", "resposta"],
        "close_options": ["concluir", "resposta"],
    },
]


def seed(session_factory) -> None:
    with session_factory() as s:
        robots = {
            r.name: r
            for r in s.query(Robot).filter(Robot.repository_id.is_(None)).all()
        }
        for name, role, mission in SEED_ROBOTS:
            robot = robots.get(name)
            if robot is None:
                robot = Robot(name=name, role=role, mission=mission)
                s.add(robot)
            elif robot.role != role or robot.mission != mission:
                # atualiza robôs de seeds antigos (idempotente)
                robot.role = role
                robot.mission = mission
        s.commit()

        for pipeline_name, steps_spec in SEED_PIPELINES:
            pipeline = (
                s.query(Pipeline)
                .filter(
                    Pipeline.name == pipeline_name,
                    Pipeline.repository_id.is_(None),
                )
                .first()
            )
            if pipeline is None:
                pipeline = Pipeline(name=pipeline_name)
                s.add(pipeline)
            existing = {(ps.position, ps.robot_id): ps for ps in pipeline.steps}
            existing_robots = {ps.robot.name: ps for ps in pipeline.steps if ps.robot}
            for position, spec in enumerate(steps_spec):
                robot_name, post_merge = spec if isinstance(spec, tuple) else (spec, False)
                robot = (
                    s.query(Robot)
                    .filter(Robot.name == robot_name, Robot.repository_id.is_(None))
                    .first()
                )
                if not robot:
                    continue
                # Pipeline existente: adiciona apenas fases do seed que não existem
                # (por robô), preservando personalizações e posições já criadas.
                if robot.name in existing_robots:
                    continue
                if (position, robot.id) in existing:
                    continue
                pipeline.steps.append(
                    PipelineStep(
                        position=position,
                        robot_id=robot.id,
                        post_merge=post_merge,
                    )
                )
        s.commit()
        log.info("seed concluído (robôs: %s; pipelines: %s)",
                 ", ".join(r[0] for r in SEED_ROBOTS),
                 ", ".join(p[0] for p in SEED_PIPELINES))
        _seed_stage_types(s)


def _seed_stage_types(s) -> None:
    """Seed idempotente do catálogo global de tipos de etapa do fluxo de chamados."""
    existing = {st.name: st for st in s.query(ChamadoStageType).filter(ChamadoStageType.repository_id.is_(None)).all()}
    for spec in SEED_STAGE_TYPES:
        st = existing.get(spec["name"])
        if st is None:
            s.add(ChamadoStageType(repository_id=None, **spec))
        else:
            # atualiza seeds antigos (idempotente), preservando o id.
            for key, value in spec.items():
                setattr(st, key, value)
    s.commit()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    migrate_schema(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="autoia", version="0.1.0")
    app.state.settings = settings
    app.state.Session = session_factory

    seed(session_factory)

    # Auth: register/login/logout/me e gestão de usuários ficam acessíveis sem
    # proteção global (o próprio fluxo de auth valida a sessão quando preciso).
    app.include_router(auth.router)
    app.include_router(users.router)

    # Com `Settings.auth_enabled=True`, TODOS os routers /api/* exigem sessão
    # válida (401 sem cookie); com OFF, `require_auth` retorna None e o
    # comportamento atual é preservado integralmente.
    for r in (
        repositories.router,
        robots.router,
        pipelines.router,
        tasks.router,
        subtasks.router,
        steps.router,
        dashboard.router,
        dashboard.me_router,
        execution.router,
        chamados.projects_router,
        chamados.epics_router,
        chamados.stage_types_router,
        chamados.chamados_router,
    ):
        app.include_router(r, dependencies=[Depends(require_auth)])

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/worker/status")
    def worker_status(_user: User | None = Depends(require_auth)):
        hb = os.path.join(settings.workspace_dir, "worker.heartbeat")
        try:
            mtime = os.path.getmtime(hb)
            age = time.time() - mtime
        except OSError:
            return {"alive": False, "last_heartbeat_sec": None}
        return {"alive": age < 15, "last_heartbeat_sec": round(age, 1)}

    if settings.frontend_dist and os.path.isdir(settings.frontend_dist):
        assets = os.path.join(settings.frontend_dist, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            """Serve o build do frontend (PWA) com fallback SPA."""
            if full_path.startswith("api/"):
                raise HTTPException(404)
            candidate = os.path.join(settings.frontend_dist, full_path) if full_path else settings.frontend_dist
            if os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(settings.frontend_dist, "index.html"))

    return app


def run_api() -> None:
    import uvicorn

    settings = Settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


def _worker_setup(settings: Settings, *, recover: bool, logger) -> object:
    """Engine/session + (opcional) recuperação de órfãos + proxy egress.

    Retorna a session_factory. `recover` só é True para o PRIMEIRO processo do
    grupo (o pai): se cada processo recuperasse no startup, um worker mais lento
    resetaria para `pending` uma fase que outro acabou de reclamar → a mesma fase
    roda 2× em paralelo (bug real observado com `--workers 3`).
    """
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # não depende da API ter subido antes
    migrate_schema(engine)
    session_factory = make_session_factory(engine)
    if recover:
        recovered = recover_stale_steps(session_factory)
        if recovered:
            logger.info("worker recuperou %s step(s) running órfão(s) para re-execução", recovered)

    from app.worker import sandbox as sandbox_mod

    if sandbox_mod.normalize_mode(settings.sandbox) == sandbox_mod.SANDBOX_FULL:
        port = sandbox_mod.ensure_egress_proxy(settings.sandbox_proxy_port, settings.whitelisted_hosts)
        logger.info("sandbox full: proxy de egress allowlist na porta %s", port)
    return engine, session_factory


def _worker_process(settings: Settings, logger) -> None:
    """Loop do worker num processo. Cria engine/sessão PRÓPRIOS (após o fork — o
    pool de conexões do pai não é fork-safe). Sem lock/recover: o grupo já garantiu."""
    _engine, session_factory = _worker_setup(settings, recover=False, logger=logger)
    worker_loop(settings, session_factory, settings.workspace_dir)


def _worker_main(settings: Settings) -> None:
    """Worker de instância única (`--workers 1`): lock exclusivo + recover + loop."""
    import signal

    from app.worker import exec_common

    logger = logging.getLogger("autoia.worker")
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "worker.lock"))
    if lock is None:
        try:
            pid = open(os.path.join(settings.workspace_dir, "worker.lock"), encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    def _on_shutdown(signum, _frame):
        logger.info("worker recebeu sinal %s — encerrando subprocessos", signum)
        exec_common.kill_all_procs()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)
    _engine, session_factory = _worker_setup(settings, recover=True, logger=logger)
    worker_loop(settings, session_factory, settings.workspace_dir)


def run_chamado_worker() -> None:
    """Worker do fluxo de CHAMADOS (processo separado, lock próprio)."""
    import signal

    from app.worker import exec_common

    logger = logging.getLogger("autoia.chamado")
    settings = Settings()
    settings.ensure_dirs()
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "chamado-worker.lock"))
    if lock is None:
        try:
            pid = open(os.path.join(settings.workspace_dir, "chamado-worker.lock"), encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"chamado-worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    def _on_shutdown(signum, _frame):
        logger.info("chamado-worker recebeu sinal %s — encerrando subprocessos", signum)
        exec_common.kill_all_procs()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    migrate_schema(engine)
    session_factory = make_session_factory(engine)
    recovered = recover_stale_chamados(session_factory)
    if recovered:
        logger.info("chamado-worker recuperou %s etapa(s) órfã(s)", recovered)
    chamado_worker_loop(settings, session_factory, settings.workspace_dir)


def run_worker() -> None:
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="autoia worker")
    parser.add_argument("--workers", type=int, default=1, help="número de processos de worker")
    args = parser.parse_known_args()[0]  # ignora args desconhecidos (uvicorn pode injetar)

    # O pipeline executa UMA fase por task por vez — garantido pelo claim atômico
    # (`claim_next`). Com `--workers N`, N PROCESSOS rodam fases de tasks diferentes
    # em paralelo. Threads no mesmo processo não são usadas (contornam o lock e
    # corrompem o estado). O grupo multi-worker é UM dono: o pai segura o lock
    # EXCLUSIVO (um segundo `autoia-worker` é recusado), recupera órfãos UMA vez
    # antes do fork, e os filhos só rodam o loop (sem lock/recover — evita que a
    # recuperação de um filho resetasse uma fase recém-reclamada por outro).
    workers = max(1, args.workers or 1)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger("autoia.worker")
    settings = Settings()
    settings.ensure_dirs()

    if workers == 1:
        _worker_main(settings)
        return

    lock_path = os.path.join(settings.workspace_dir, "worker.lock")
    lock = acquire_worker_lock(lock_path)
    if lock is None:
        try:
            pid = open(lock_path, encoding="utf-8").read().strip()
        except OSError:
            pid = "?"
        print(
            f"worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Recuperação de órfãos UMA única vez, no pai, ANTES de qualquer filho claimar.
    parent_engine, _sf = _worker_setup(settings, recover=True, logger=logger)
    parent_engine.dispose()  # fecha o pool antes do fork (os filhos criam o próprio)

    logger.info("iniciando %s processos de worker (parallelismo por task)", workers)
    children: list[int] = []
    for _ in range(workers):
        pid = os.fork()
        if pid == 0:  # filho: loop puro, sem lock/recover (engine próprio)
            try:
                _worker_process(settings, logger)
            finally:
                os._exit(0)
        children.append(pid)

    def _on_signal(signum, _frame):
        logger.info("pai recebeu sinal %s — encaminhando para os workers", signum)
        for pid in children:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    for pid in children:
        os.waitpid(pid, 0)


if __name__ == "__main__":
    run_api()
