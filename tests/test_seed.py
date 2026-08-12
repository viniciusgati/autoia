"""Testes do seed (robôs com papéis + pipelines: com e sem deploy pós-merge)."""

from __future__ import annotations

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Pipeline, Robot


def test_seed_roles_and_pipeline(settings):
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))

    with session_factory() as s:
        roles = {r.name: r.role for r in s.query(Robot).all()}
        assert roles["po"] == "refine"
        assert roles["qa"] == "review"
        assert roles["developer"] == "implement"
        assert roles["tester"] == "verify"
        assert roles["avaliador"] == "assess"
        assert roles["merger"] == "merge"
        assert roles["deploy-tester"] == "verify"
        assert roles["pm"] == "pm"
        assert roles["browser-tester"] == "verify"

        assert s.query(Pipeline).count() == 3

        # com deploy: avaliador pré-merge + deploy-tester pós-merge (7 fases)
        deploy = (
            s.query(Pipeline)
            .filter(Pipeline.name == "po-qa-dev-tester-avaliador-deploytest")
            .one()
        )
        order = [st.robot.name for st in sorted(deploy.steps, key=lambda x: x.position)]
        assert order == ["po", "qa", "developer", "tester", "avaliador", "merger", "deploy-tester"]
        post = [st.post_merge for st in sorted(deploy.steps, key=lambda x: x.position)]
        assert post == [False, False, False, False, False, False, True]

        # com deploy + browser: deploy-tester e browser-tester pós-merge (8 fases)
        browser = (
            s.query(Pipeline)
            .filter(Pipeline.name == "po-qa-dev-tester-avaliador-deploytest-browser")
            .one()
        )
        order = [st.robot.name for st in sorted(browser.steps, key=lambda x: x.position)]
        assert order == ["po", "qa", "developer", "tester", "avaliador", "merger", "deploy-tester", "browser-tester"]
        post = [st.post_merge for st in sorted(browser.steps, key=lambda x: x.position)]
        assert post == [False, False, False, False, False, False, True, True]

        # sem deploy: termina no merger (6 fases, todas pré-merge)
        sem_deploy = (
            s.query(Pipeline)
            .filter(Pipeline.name == "po-qa-dev-tester-avaliador-merge")
            .one()
        )
        order = [st.robot.name for st in sorted(sem_deploy.steps, key=lambda x: x.position)]
        assert order == ["po", "qa", "developer", "tester", "avaliador", "merger"]
        assert all(not st.post_merge for st in sem_deploy.steps)

    # seed é idempotente
    create_app(settings)
    with session_factory() as s:
        assert s.query(Robot).count() == 9
        assert s.query(Pipeline).count() == 3


def test_migrate_schema_creates_auth_tables_and_new_columns(settings):
    """Migração aditiva: tabelas de auth e as 3 colunas novas existem após
    `create_all` + `migrate_schema` (sem drop/rename de nada existente)."""
    from sqlalchemy import inspect

    from app.db import Base, make_engine, migrate_schema

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    migrate_schema(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"users", "sessions", "repository_users"} <= tables

    users_cols = {c["name"] for c in inspector.get_columns("users")}
    assert {"name", "email", "password_hash", "role", "active"} <= users_cols

    tasks_cols = {c["name"] for c in inspector.get_columns("tasks")}
    assert "responsible_id" in tasks_cols

    step_cols = {c["name"] for c in inspector.get_columns("task_steps")}
    assert {"responsible_id", "finished_by_id"} <= step_cols
