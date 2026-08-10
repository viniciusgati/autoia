"""Testes de robôs e pipelines por projeto (escopo repository_id)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(settings, bare_repo):
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture
def repos(client, bare_repo):
    """Cria dois repositórios de teste."""
    ids = []
    for name in ("repo-a", "repo-b"):
        r = client.post(
            "/api/repositories",
            json={"name": name, "url": bare_repo, "default_branch": "main"},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


def test_list_robots_sem_filtro_retorna_so_globais(client):
    robots = client.get("/api/robots").json()
    assert len(robots) > 0
    assert all(r["repository_id"] is None for r in robots)


def test_criar_robô_de_projeto_e_listar_com_filtro(client, repos):
    repo_a, repo_b = repos

    # robô próprio do projeto A
    r = client.post(
        "/api/robots",
        json={"name": "copywriter", "mission": "escrever textos", "repository_id": repo_a},
    )
    assert r.status_code == 201, r.text
    assert r.json()["repository_id"] == repo_a

    # projeto A: globais + o do projeto
    lista_a = client.get(f"/api/robots?repository_id={repo_a}").json()
    names_a = {x["name"] for x in lista_a}
    assert "copywriter" in names_a
    assert "pm" in names_a  # global

    # projeto B: NÃO deve ver o robô do projeto A
    lista_b = client.get(f"/api/robots?repository_id={repo_b}").json()
    assert "copywriter" not in {x["name"] for x in lista_b}


def test_unicidade_por_escopo(client, repos):
    repo_a, repo_b = repos

    r = client.post(
        "/api/robots",
        json={"name": "helper", "mission": "m1", "repository_id": repo_a},
    )
    assert r.status_code == 201, r.text

    # mesmo nome em projeto diferente: OK
    r = client.post(
        "/api/robots",
        json={"name": "helper", "mission": "m1", "repository_id": repo_b},
    )
    assert r.status_code == 201, r.text

    # mesmo nome no MESMO projeto: 409
    r = client.post(
        "/api/robots",
        json={"name": "helper", "mission": "m1", "repository_id": repo_a},
    )
    assert r.status_code == 409, r.text

    # mesmo nome global que já existe (seed): 409
    r = client.post("/api/robots", json={"name": "pm", "mission": "m1"})
    assert r.status_code == 409, r.text


def test_pipeline_de_projeto_com_robo_do_projeto_e_global(client, repos):
    repo_a, _ = repos

    # robô do projeto
    r = client.post(
        "/api/robots",
        json={"name": "copywriter", "mission": "escrever", "repository_id": repo_a},
    )
    copywriter_id = r.json()["id"]

    # robô global (developer do seed)
    globals_list = client.get("/api/robots").json()
    dev_id = next(r for r in globals_list if r["name"] == "developer")["id"]

    r = client.post(
        "/api/pipelines",
        json={
            "name": "pipeline-copy",
            "repository_id": repo_a,
            "steps": [
                {"position": 0, "robot_id": copywriter_id},
                {"position": 1, "robot_id": dev_id, "post_merge": False},
            ],
        },
    )
    assert r.status_code == 201, r.text
    pipeline = r.json()
    assert pipeline["repository_id"] == repo_a
    assert len(pipeline["steps"]) == 2

    # lista filtrada: globais + do projeto
    lista = client.get(f"/api/pipelines?repository_id={repo_a}").json()
    assert any(p["name"] == "pipeline-copy" for p in lista)
    # outro projeto não vê
    outra = client.get(f"/api/pipelines?repository_id={repos[1]}").json()
    assert all(p["name"] != "pipeline-copy" for p in outra)


def test_create_task_valida_pipeline_do_projeto(client, repos):
    repo_a, repo_b = repos

    dev_id = next(r for r in client.get("/api/robots").json() if r["name"] == "developer")["id"]

    # pipeline do projeto A
    r = client.post(
        "/api/pipelines",
        json={
            "name": "pipeline-a",
            "repository_id": repo_a,
            "steps": [
                {"position": 0, "robot_id": dev_id},
            ],
        },
    )
    assert r.status_code == 201, r.text
    pipeline_a_id = r.json()["id"]

    # task no projeto A com pipeline do A: OK
    r = client.post(
        "/api/tasks",
        json={
            "repository_id": repo_a,
            "pipeline_id": pipeline_a_id,
            "title": "t1",
            "description": "d",
        },
    )
    assert r.status_code == 201, r.text

    # task no projeto B com pipeline do A: 400
    r = client.post(
        "/api/tasks",
        json={
            "repository_id": repo_b,
            "pipeline_id": pipeline_a_id,
            "title": "t2",
            "description": "d",
        },
    )
    assert r.status_code == 400, r.text
    assert "projeto" in r.json()["detail"]


def test_seed_robos_e_pipelines_globais(client, repos):
    """Seed cria robôs/pipelines globais com repository_id None (não por projeto)."""
    robots = client.get("/api/robots").json()
    assert all(r["repository_id"] is None for r in robots)

    pipelines = client.get("/api/pipelines").json()
    assert all(p["repository_id"] is None for p in pipelines)
