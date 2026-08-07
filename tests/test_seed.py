"""Testes do seed (robôs com papéis + pipeline default)."""

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

        pipeline = (
            s.query(Pipeline).filter(Pipeline.name == "po-qa-dev-tester-deploytest").one()
        )
        order = [st.robot.name for st in sorted(pipeline.steps, key=lambda x: x.position)]
        assert order == ["po", "qa", "developer", "tester", "merger", "deploy-tester"]
        post = [st.post_merge for st in sorted(pipeline.steps, key=lambda x: x.position)]
        assert post == [False, False, False, False, False, True]

        default = (
            s.query(Pipeline).filter(Pipeline.name == "po-qa-dev-tester-avaliador-merge").one()
        )
        order = [st.robot.name for st in sorted(default.steps, key=lambda x: x.position)]
        assert order == ["po", "qa", "developer", "tester", "avaliador", "merger"]

    # seed é idempotente
    create_app(settings)
    with session_factory() as s:
        assert s.query(Robot).count() == 8
