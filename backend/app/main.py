"""App FastAPI e entrypoints (api + worker)."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api import dashboard, pipelines, repositories, robots, steps, tasks
from .config import Settings
from .db import Base, make_engine, make_session_factory, migrate_schema
from .models import Pipeline, PipelineStep, Robot
from .worker.runner import worker_loop

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

Verifique se os critérios de aceite são claros, testáveis, completos e sem ambiguidade.
Se a história estiver boa, veredicto READY. Se precisar de ajustes, veredicto NEEDS_WORK
com o que o PO deve corrigir.""",
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
        """Você é o robô MERGER de um pipeline automatizado.

Título: {task_title}
Descrição: {task_description}

Esta é a fase final antes da integração. Verifique o estado da branch (git status,
git log), confira que tudo está commitado e que a suíte de testes passa. Confira que a
branch NÃO divergiu da main sem motivo (git log origin/{default_branch}..HEAD) e resuma
o que está sendo integrado.
NÃO faça push nem merge: a integração final (merge + push na default) é feita
automaticamente pelo sistema. Resuma o estado final do trabalho.""",
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
]

SEED_PIPELINES = [
    (
        "po-qa-dev-tester-avaliador-merge",
        ["po", "qa", "developer", "tester", "avaliador", "merger"],
    ),
    (
        "po-qa-dev-tester-merge",
        ["po", "qa", "developer", "tester", "merger"],
    ),
    (
        "po-qa-dev-tester-deploytest",
        [
            ("po", False),
            ("qa", False),
            ("developer", False),
            ("tester", False),
            ("merger", False),
            ("deploy-tester", True),  # pós-merge: roda na default integrada
        ],
    ),
]


def seed(session_factory) -> None:
    with session_factory() as s:
        robots = {r.name: r for r in s.query(Robot).all()}
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
            if s.query(Pipeline).filter(Pipeline.name == pipeline_name).first():
                continue
            pipeline = Pipeline(name=pipeline_name)
            for position, spec in enumerate(steps_spec):
                robot_name, post_merge = spec if isinstance(spec, tuple) else (spec, False)
                robot = s.query(Robot).filter(Robot.name == robot_name).first()
                if robot:
                    pipeline.steps.append(
                        PipelineStep(
                            position=position,
                            robot_id=robot.id,
                            post_merge=post_merge,
                        )
                    )
            s.add(pipeline)
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
    app.include_router(steps.router)
    app.include_router(dashboard.router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

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
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings = Settings()
    settings.ensure_dirs()
    worker_loop(settings)


if __name__ == "__main__":
    run_api()
