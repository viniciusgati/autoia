"""Testes de integração do executor codex: backfill cmd→codex, endpoint de
modelos do CLI, resolução de modelo (task > robô > default)."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


# ---------- Backfill do banco (executor 'cmd' -> 'codex') ----------


def test_backfill_legacy_executors_converts_cmd(monkeypatch):
    from sqlalchemy import create_engine, text

    from app.db import backfill_legacy_executors

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for table in ("tasks", "chamados"):
            conn.exec_driver_sql(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, executor VARCHAR(20))"
            )
            conn.exec_driver_sql(
                f"INSERT INTO {table} (id, executor) VALUES (1, 'cmd'), (2, 'kimi'), (3, 'codex')"
            )

    backfill_legacy_executors(engine)

    with engine.connect() as conn:
        for table in ("tasks", "chamados"):
            rows = conn.execute(text(f"SELECT id, executor FROM {table} ORDER BY id")).fetchall()
            assert [r[1] for r in rows] == ["codex", "kimi", "codex"]


def test_backfill_legacy_executors_idempotent():
    from sqlalchemy import create_engine, text

    from app.db import backfill_legacy_executors

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE tasks (id INTEGER PRIMARY KEY, executor VARCHAR(20))")
        conn.exec_driver_sql("INSERT INTO tasks (id, executor) VALUES (1, 'codex')")
    backfill_legacy_executors(engine)
    backfill_legacy_executors(engine)
    with engine.connect() as conn:
        value = conn.execute(text("SELECT executor FROM tasks WHERE id=1")).scalar()
    assert value == "codex"


# ---------- Endpoint /api/system/codex/models ----------


def _codex_catalog_fake(tmp_path, models: list[dict]) -> str:
    script = tmp_path / "fake_codex_catalog"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        f"print(json.dumps({{'models': {json.dumps(models)}}}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _make_client(tmp_path, codex_bin: str):
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "ws"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        codex_bin=codex_bin,
        auth_enabled=False,
    )
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_models_cache():
    from app import model_catalog

    def reset():
        model_catalog.reset_cache()

    reset()
    yield
    reset()


def test_codex_models_endpoint_sem_fallback_de_config(tmp_path):
    """CLI do codex inexistente → lista VAZIA com `source="erro"`.

    Não existe mais a lista fixa por env (`AUTOIA_CODEX_MODELS`): um catálogo
    default esconde que o CLI está fora e oferece modelo que não existe.
    """
    client = _make_client(tmp_path, codex_bin="/bin/nonexistent-codex")
    resp = client.get("/api/system/codex/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == []
    assert body["source"] == "erro"


def test_codex_models_endpoint_reads_cli_catalog(tmp_path):
    catalog = _codex_catalog_fake(
        tmp_path,
        [
            {"slug": "gpt-5.6-luna", "visibility": "list", "priority": 1},
            {"slug": "codex-auto-review", "visibility": "hide", "priority": 9},
        ],
    )
    client = _make_client(tmp_path, codex_bin=catalog)
    resp = client.get("/api/system/codex/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "cli"
    assert body["models"] == ["gpt-5.6-luna"]


# ---------- Resolução de modelo (task > robô > default) ----------


def test_effective_model_precedence():
    from app.worker.runner import _effective_model

    task = SimpleNamespace(model="gpt-5.6-luna")
    robot = SimpleNamespace(model="robot-model")
    assert _effective_model(task, robot) == "gpt-5.6-luna"
    assert _effective_model(SimpleNamespace(model="  "), robot) == "robot-model"
    assert _effective_model(SimpleNamespace(model=None), robot) == "robot-model"
    assert _effective_model(SimpleNamespace(model=""), None) is None
    assert _effective_model(SimpleNamespace(model=""), SimpleNamespace(model="")) is None


def test_effective_model_subtask_mirrors():
    from app.worker.subtask import _task_effective_model

    assert _task_effective_model(SimpleNamespace(model="x"), SimpleNamespace(model="y")) == "x"
    assert _task_effective_model(SimpleNamespace(model=""), SimpleNamespace(model="y")) == "y"
    assert _task_effective_model(SimpleNamespace(model=None), None) is None
