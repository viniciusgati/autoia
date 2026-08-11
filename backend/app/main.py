"""App FastAPI e entrypoints (api + worker)."""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api import dashboard, execution, pipelines, repositories, robots, steps, subtasks, tasks
from .config import Settings
from .db import Base, make_engine, make_session_factory, migrate_schema
from .models import Pipeline, PipelineStep, Robot
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

    app.include_router(repositories.router)
    app.include_router(robots.router)
    app.include_router(pipelines.router)
    app.include_router(tasks.router)
    app.include_router(subtasks.router)
    app.include_router(steps.router)
    app.include_router(dashboard.router)
    app.include_router(execution.router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/worker/status")
    def worker_status():
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


def run_worker() -> None:
    import argparse
    import signal
    import sys
    import threading

    parser = argparse.ArgumentParser(description="autoia worker")
    parser.add_argument("--workers", type=int, default=1, help="número de workers (threads)")
    args = parser.parse_known_args()[0]  # ignora args desconhecidos (uvicorn pode injetar)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger("autoia.worker")
    settings = Settings()
    settings.ensure_dirs()

    # Instância única: um segundo `autoia-worker` se recusa a iniciar (evita
    # dois workers disputando as mesmas tasks → fases rodando em paralelo).
    lock = acquire_worker_lock(os.path.join(settings.workspace_dir, "worker.lock"))
    if lock is None:
        try:
            pid = open(
                os.path.join(settings.workspace_dir, "worker.lock"), encoding="utf-8"
            ).read().strip()
        except OSError:
            pid = "?"
        print(
            f"worker já está rodando (PID {pid}); não é possível iniciar outro.",
            file=sys.stderr,
        )
        sys.exit(1)

    # No shutdown, mata os subprocessos ativos: os robôs rodam em sessão própria
    # (start_new_session=True) e, sem isso, virariam órfãos continuando a trabalhar
    # na mesma branch (corrompendo estado) após restart do worker.
    def _on_shutdown(signum, _frame):
        logger.info("worker recebeu sinal %s — encerrando subprocessos", signum)
        from app.worker import exec_common

        exec_common.kill_all_procs()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT, _on_shutdown)

    # Engine/session compartilhados e recuperação de steps órfãos UMA única vez
    # ANTES de spawnar as threads. Rodar dentro de cada thread (como antes) tinha
    # corrida: duas threads recuperavam o mesmo step running ao mesmo tempo e ambas
    # o reclamavam → duas execuções da mesma fase em paralelo.
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # não depende da API ter subido antes
    migrate_schema(engine)
    session_factory = make_session_factory(engine)
    recovered = recover_stale_steps(session_factory)
    if recovered:
        logger.info("worker recuperou %s step(s) running órfão(s) para re-execução", recovered)

    if args.workers <= 1:
        worker_loop(settings, session_factory, settings.workspace_dir)
    else:
        threads = []
        for i in range(args.workers):
            t = threading.Thread(
                target=worker_loop, args=(settings, session_factory, settings.workspace_dir),
                daemon=True, name=f"worker-{i}",
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()


if __name__ == "__main__":
    run_api()
