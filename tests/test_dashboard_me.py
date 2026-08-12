"""Testes do dashboard pessoal: /api/me/tasks, /api/me/projects e o filtro
por projetos do usuário no GET /api/dashboard (auth ON) vs. global (auth OFF)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task


def _login(client, email, password="senha123"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def dflow(settings, bare_repo):
    """Auth ON: admin + usuários A/B; 3 repos; A participa de r1/r2, B de r2/r3.

    Tasks: t1 (A, r1), t2 (A, r2), t3 (B, r2), t4 (B, r3).
    """
    settings.auth_enabled = True
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    for email in ("a@ex.com", "b@ex.com"):
        client.post(
            "/api/users",
            json={"name": email.split("@")[0], "email": email, "password": "senha123", "role": "member"},
        )
    ids = {u["email"]: u["id"] for u in client.get("/api/users").json()}
    for name in ("r1", "r2", "r3"):
        resp = client.post(
            "/api/repositories", json={"name": name, "url": bare_repo, "default_branch": "main"}
        )
        assert resp.status_code == 201, resp.text
    # participações: A em r1/r2; B em r2/r3
    client.post("/api/repositories/1/members", json={"user_id": ids["a@ex.com"]})
    client.post("/api/repositories/2/members", json={"user_id": ids["a@ex.com"]})
    client.post("/api/repositories/2/members", json={"user_id": ids["b@ex.com"]})
    client.post("/api/repositories/3/members", json={"user_id": ids["b@ex.com"]})

    client_a = TestClient(app)
    _login(client_a, "a@ex.com")
    client_b = TestClient(app)
    _login(client_b, "b@ex.com")

    def _task(c, repo_id, title):
        resp = c.post(
            "/api/tasks",
            json={"repository_id": repo_id, "pipeline_id": 1, "title": title, "description": "d", "kind": "feature"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {
        "app": app,
        "session_factory": session_factory,
        "client": client,
        "client_a": client_a,
        "client_b": client_b,
        "ids": ids,
        "tasks": {
            "t1": _task(client_a, 1, "t1"),
            "t2": _task(client_a, 2, "t2"),
            "t3": _task(client_b, 2, "t3"),
            "t4": _task(client_b, 3, "t4"),
        },
    }


def test_me_tasks_returns_only_mine_with_repo_name(dflow):
    mine = dflow["client_a"].get("/api/me/tasks").json()
    assert {t["title"] for t in mine} == {"t1", "t2"}
    by_title = {t["title"]: t for t in mine}
    assert by_title["t1"]["repository_name"] == "r1"
    assert by_title["t1"]["id"] == dflow["tasks"]["t1"]

    mine_b = dflow["client_b"].get("/api/me/tasks").json()
    assert {t["title"] for t in mine_b} == {"t3", "t4"}
    by_title_b = {t["title"]: t for t in mine_b}
    assert by_title_b["t4"]["repository_name"] == "r3"


def test_me_projects_with_role_and_counts(dflow):
    projects = dflow["client_a"].get("/api/me/projects").json()
    by_name = {p["name"]: p for p in projects}
    assert set(by_name) == {"r1", "r2"}  # A não participa do r3
    assert by_name["r1"]["role"] == "member"
    assert by_name["r1"]["my_tasks_total"] == 1
    assert by_name["r2"]["my_tasks_total"] == 1

    # status de ação humana conta como pendente (selo "aguardando você")
    with dflow["session_factory"]() as s:
        t1 = s.get(Task, dflow["tasks"]["t1"])
        t1.status = "needs_review"
        t1.error = "x"
        s.commit()
    projects = dflow["client_a"].get("/api/me/projects").json()
    by_name = {p["name"]: p for p in projects}
    assert by_name["r1"]["my_tasks_pending"] == 1
    assert by_name["r1"]["my_tasks_active"] == 0


def test_dashboard_auth_on_is_personal_and_scoped(dflow):
    # t1 (r1, do A) e t4 (r3, do B) precisam de ação → aviso
    with dflow["session_factory"]() as s:
        for task_id in (dflow["tasks"]["t1"], dflow["tasks"]["t4"]):
            t = s.get(Task, task_id)
            t.status = "needs_review"
            t.error = "x"
        s.commit()

    dash = dflow["client_a"].get("/api/dashboard").json()
    assert dash["user"]["email"] == "a@ex.com"
    # dashboard pessoal: minhas tasks e projetos preenchidos
    assert {t["title"] for t in dash["my_tasks"]} == {"t1", "t2"}
    assert {p["name"] for p in dash["projects"]} == {"r1", "r2"}
    # avisos e métricas filtrados aos projetos de A (r3 fora)
    assert {n["task_id"] for n in dash["notices"]} == {dflow["tasks"]["t1"]}
    assert dash["total_tasks"] == 3  # t1, t2, t3 (t4 do r3 não conta)

    dash_b = dflow["client_b"].get("/api/dashboard").json()
    assert {p["name"] for p in dash_b["projects"]} == {"r2", "r3"}
    assert {n["task_id"] for n in dash_b["notices"]} == {dflow["tasks"]["t4"]}


def test_dashboard_auth_off_remains_global(settings, bare_repo):
    app = create_app(settings)  # auth OFF (fixture padrão)
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r1", "url": bare_repo, "default_branch": "main"}
    )
    client.post(
        "/api/repositories", json={"name": "r2", "url": bare_repo, "default_branch": "main"}
    )
    for repo_id in (1, 2):
        client.post(
            "/api/tasks",
            json={"repository_id": repo_id, "pipeline_id": 1, "title": f"t{repo_id}", "description": "d", "kind": "feature"},
        )
    dash = client.get("/api/dashboard").json()
    assert dash["user"] is None
    assert dash["my_tasks"] == []
    assert dash["projects"] == []
    assert dash["total_tasks"] == 2  # global (todos os projetos)


def test_me_endpoints_require_session(settings):
    settings.auth_enabled = True
    app = create_app(settings)
    fresh = TestClient(app)
    assert fresh.get("/api/me/tasks").status_code == 401
    assert fresh.get("/api/me/projects").status_code == 401
