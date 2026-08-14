"""Testes da associação Projeto > Épico em Tasks (metadados organizacionais).

Cobre: migração aditiva das colunas `project_id`/`epic_id` em `tasks`, criação de
tarefa com associação válida (projeto+épico e épico derivando o projeto), erros de
associação inválida no POST (400/404 sem criar a tarefa), PATCH em qualquer status
(atualização/remoção, distinção ausente × `null`, épico prevalece, limpeza do épico
ao trocar só o projeto) e erros de associação inválida no PATCH (associação
permanece inalterada). Regras espelhadas no fluxo de chamados (`update_chamado`).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def env(settings, bare_repo):
    """App + session_factory + 2 repositórios; repo1 com 2 projetos (épicos)."""
    from fastapi.testclient import TestClient

    from app.db import make_engine, make_session_factory
    from app.main import create_app

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    r1 = client.post(
        "/api/repositories",
        json={"name": "r1", "url": bare_repo, "default_branch": "main"},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/repositories",
        json={"name": "r2", "url": bare_repo, "default_branch": "main"},
    )
    assert r2.status_code == 201, r2.text
    repo_id = r1.json()["id"]
    repo2_id = r2.json()["id"]
    p1 = client.post("/api/projects", json={"repository_id": repo_id, "name": "App"}).json()
    p1b = client.post("/api/projects", json={"repository_id": repo_id, "name": "App2"}).json()
    p2 = client.post("/api/projects", json={"repository_id": repo2_id, "name": "Outro"}).json()
    e1 = client.post("/api/epics", json={"project_id": p1["id"], "name": "Auth"}).json()
    e1b = client.post("/api/epics", json={"project_id": p1b["id"], "name": "Billing"}).json()
    e2 = client.post("/api/epics", json={"project_id": p2["id"], "name": "E2"}).json()
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": repo_id,
        "repo2_id": repo2_id,
        "project": p1,
        "project_same_repo": p1b,
        "project2": p2,
        "epic": e1,
        "epic_same_repo": e1b,
        "epic2": e2,
    }


def _new_task(env, **extra) -> dict:
    data = {
        "repository_id": env["repo_id"],
        "pipeline_id": 1,
        "title": "t",
        "description": "d",
        "kind": "feature",
    }
    data.update(extra)
    r = env["client"].post("/api/tasks", json=data)
    assert r.status_code == 201, r.text
    return r.json()


# ── Migração aditiva ─────────────────────────────────────────────────────────

def test_migrate_schema_adds_task_project_epic(settings):
    """`migrate_schema()` é aditivo e garante `project_id`/`epic_id` em `tasks`
    (INTEGER, FK para `projects`/`epics`) sem criar/alterar mais nada."""
    from app.db import Base, make_engine, migrate_schema
    from app import models  # noqa: F401 — registra o metadata

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    migrate_schema(engine)  # no-op: schema já tem as colunas (idempotente)

    with engine.begin() as conn:
        before = [r for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")]
        fks = {(r[2], r[3], r[4]) for r in conn.exec_driver_sql("PRAGMA foreign_key_list(tasks)")}
    # migrate de novo não altera nada (aditivo de verdade)
    migrate_schema(engine)
    with engine.begin() as conn:
        after = [r for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")]
    assert after == before

    info = {r[1]: r for r in after}
    assert "project_id" in info and "epic_id" in info
    assert info["project_id"][2] == "INTEGER" and info["epic_id"][2] == "INTEGER"
    assert ("projects", "project_id", "id") in fks
    assert ("epics", "epic_id", "id") in fks


def test_migrate_schema_upgrades_old_db(settings):
    """Banco antigo (tasks sem as colunas): o migrate adiciona exatamente as
    colunas aditivas registradas, sem tocar nas já existentes."""
    from app.db import make_engine, migrate_schema

    engine = make_engine(settings.database_url)
    # Tabelas "antigas" do ADDITIVE_COLUMNS com UMA coluna existente (`id`) —
    # o migrate deve só acrescentar as colunas registradas (inclusive as novas
    # project_id/epic_id em tasks) e preservar `id`.
    with engine.begin() as conn:
        for table in [
            "repositories", "robots", "pipelines", "tasks",
            "task_steps", "pipeline_steps", "step_artifacts", "task_proposals",
        ]:
            conn.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

    migrate_schema(engine)

    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")}
        fks = {(r[2], r[3], r[4]) for r in conn.exec_driver_sql("PRAGMA foreign_key_list(tasks)")}
    assert "id" in cols  # coluna existente preservada
    assert {"project_id", "epic_id"} <= cols
    assert ("projects", "project_id", "id") in fks
    assert ("epics", "epic_id", "id") in fks


# ── Criação (POST) ───────────────────────────────────────────────────────────

def test_task_create_with_project_and_epic(env):
    t = _new_task(env, project_id=env["project"]["id"], epic_id=env["epic"]["id"])
    assert t["project_id"] == env["project"]["id"]
    assert t["epic_id"] == env["epic"]["id"]

    got = env["client"].get(f"/api/tasks/{t['id']}").json()
    assert got["project_id"] == env["project"]["id"]
    assert got["epic_id"] == env["epic"]["id"]


def test_task_create_epic_derives_project(env):
    # só o épico → project_id derivado dele (épico prevalece)
    t = _new_task(env, epic_id=env["epic"]["id"])
    assert t["project_id"] == env["project"]["id"]
    assert t["epic_id"] == env["epic"]["id"]

    # épico + projeto conflitante do MESMO repo → o épico prevalece e deriva
    t2 = _new_task(env, project_id=env["project_same_repo"]["id"], epic_id=env["epic"]["id"])
    assert t2["project_id"] == env["project"]["id"]
    assert t2["epic_id"] == env["epic"]["id"]

    # épico + projeto de OUTRO repositório → o épico prevalece também (deriva o
    # projeto antes da validação — mesmo fluxo do `create_chamado`)
    t3 = _new_task(env, project_id=env["project2"]["id"], epic_id=env["epic"]["id"])
    assert t3["project_id"] == env["project"]["id"]
    assert t3["epic_id"] == env["epic"]["id"]


def test_task_create_without_association(env):
    t = _new_task(env)
    assert t["project_id"] is None and t["epic_id"] is None
    got = env["client"].get(f"/api/tasks/{t['id']}").json()
    assert got["project_id"] is None and got["epic_id"] is None


def test_task_create_invalid_association(env):
    client = env["client"]
    total_before = len(client.get("/api/tasks").json())

    # projeto de OUTRO repositório → 400 com mensagem
    r = client.post(
        "/api/tasks",
        json={
            "repository_id": env["repo_id"],
            "pipeline_id": 1,
            "title": "x",
            "project_id": env["project2"]["id"],
        },
    )
    assert r.status_code == 400
    assert "projeto" in r.json()["detail"]

    # épico inexistente → 404
    r = client.post(
        "/api/tasks",
        json={"repository_id": env["repo_id"], "pipeline_id": 1, "title": "x", "epic_id": 99999},
    )
    assert r.status_code == 404
    assert "épico" in r.json()["detail"]

    # épico de OUTRO repositório → 400
    r = client.post(
        "/api/tasks",
        json={"repository_id": env["repo_id"], "pipeline_id": 1, "title": "x", "epic_id": env["epic2"]["id"]},
    )
    assert r.status_code == 400
    assert "épico" in r.json()["detail"]

    # nenhuma tarefa foi criada nos casos inválidos
    total_after = len(client.get("/api/tasks").json())
    assert total_after == total_before


# ── Edição (PATCH) ───────────────────────────────────────────────────────────

def test_task_patch_association_any_status(env):
    """Associação editável em qualquer status (fora do gate da história)."""
    t = _new_task(env)
    # start → queued (execução): editar história seria 400, associação funciona.
    client = env["client"]
    assert client.post(f"/api/tasks/{t['id']}/start").status_code == 200
    got = client.get(f"/api/tasks/{t['id']}").json()
    assert got["status"] == "queued"

    # epic_id deriva o project_id
    r = client.patch(f"/api/tasks/{t['id']}", json={"epic_id": env["epic"]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project"]["id"]
    assert r.json()["epic_id"] == env["epic"]["id"]

    # epic_id: null remove APENAS o épico
    r = client.patch(f"/api/tasks/{t['id']}", json={"epic_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project"]["id"]
    assert r.json()["epic_id"] is None

    # project_id: null remove projeto E épico
    r = client.patch(f"/api/tasks/{t['id']}", json={"project_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] is None and r.json()["epic_id"] is None

    # persiste no GET seguinte
    got = client.get(f"/api/tasks/{t['id']}").json()
    assert got["project_id"] is None and got["epic_id"] is None


def test_task_patch_field_absent_does_not_change(env):
    t = _new_task(env)
    client = env["client"]
    client.patch(f"/api/tasks/{t['id']}", json={"epic_id": env["epic"]["id"]})

    # PATCH sem os campos de associação → não altera nada deles
    r = client.patch(f"/api/tasks/{t['id']}", json={})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project"]["id"]
    assert r.json()["epic_id"] == env["epic"]["id"]


def test_task_patch_epic_prevales_over_project(env):
    """epic_id (com ou sem project_id) prevalece e deriva o project_id."""
    t = _new_task(env)
    client = env["client"]
    # projeto conflitante do MESMO repo + épico → o épico prevalece
    r = client.patch(
        f"/api/tasks/{t['id']}",
        json={"project_id": env["project"]["id"], "epic_id": env["epic_same_repo"]["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project_same_repo"]["id"]
    assert r.json()["epic_id"] == env["epic_same_repo"]["id"]

    # só epic_id → deriva o projeto
    r = client.patch(f"/api/tasks/{t['id']}", json={"epic_id": env["epic"]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project"]["id"]
    assert r.json()["epic_id"] == env["epic"]["id"]


def test_task_patch_switch_project_clears_epic(env):
    """Trocar apenas o project_id para um projeto ao qual o épico atual não
    pertence limpa o épico (sem erro) — mesmo comportamento de `update_chamado`."""
    t = _new_task(env, project_id=env["project"]["id"], epic_id=env["epic"]["id"])
    client = env["client"]
    r = client.patch(f"/api/tasks/{t['id']}", json={"project_id": env["project_same_repo"]["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == env["project_same_repo"]["id"]
    assert r.json()["epic_id"] is None


def test_task_patch_invalid_association_keeps_unchanged(env):
    t = _new_task(env, project_id=env["project"]["id"], epic_id=env["epic"]["id"])
    client = env["client"]

    # projeto de outro repositório → 400 e associação inalterada
    r = client.patch(f"/api/tasks/{t['id']}", json={"project_id": env["project2"]["id"]})
    assert r.status_code == 400
    assert "projeto" in r.json()["detail"]

    # épico inexistente → 404
    r = client.patch(f"/api/tasks/{t['id']}", json={"epic_id": 99999})
    assert r.status_code == 404
    assert "épico" in r.json()["detail"]

    # épico de outro repositório → 400
    r = client.patch(f"/api/tasks/{t['id']}", json={"epic_id": env["epic2"]["id"]})
    assert r.status_code == 400
    assert "épico" in r.json()["detail"]

    got = client.get(f"/api/tasks/{t['id']}").json()
    assert got["project_id"] == env["project"]["id"]
    assert got["epic_id"] == env["epic"]["id"]
