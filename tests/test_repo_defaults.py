"""Testes dos defaults de criação de tarefa por projeto.

- `Repository.default_executor`/`default_model`: executor/modelo usados quando a
  task é criada sem escolhê-los; o modelo só acompanha o executor padrão.
- `Repository.default_pipeline_id`: usado quando a task é criada sem pipeline.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client(settings, bare_repo) -> TestClient:
    app = create_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text
    return client


def test_create_repository_com_defaults(settings, bare_repo):
    """Defaults também valem na criação do projeto (POST /api/repositories)."""
    app = create_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={
            "name": "r",
            "url": bare_repo,
            "default_branch": "main",
            "default_pipeline_id": 1,
            "default_executor": "opencode",
            "default_model": "opencode-go/kimi-k2",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["default_pipeline_id"] == 1
    assert body["default_executor"] == "opencode"
    assert body["default_model"] == "opencode-go/kimi-k2"


def test_defaults_roundtrip_e_validacao(settings, bare_repo):
    client = _client(settings, bare_repo)
    resp = client.put(
        "/api/repositories/1",
        json={"default_executor": "opencode", "default_model": "opencode-go/kimi-k2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_executor"] == "opencode"
    assert resp.json()["default_model"] == "opencode-go/kimi-k2"
    repo = client.get("/api/repositories").json()[0]
    assert repo["default_executor"] == "opencode"
    assert repo["default_model"] == "opencode-go/kimi-k2"

    # Executor fora do catálogo é recusado pelo schema; null limpa os defaults.
    assert (
        client.put("/api/repositories/1", json={"default_executor": "nope"}).status_code
        == 422
    )
    cleared = client.put(
        "/api/repositories/1", json={"default_executor": None, "default_model": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_executor"] is None
    assert cleared.json()["default_model"] is None


def test_create_task_usa_defaults_do_projeto(settings, bare_repo):
    client = _client(settings, bare_repo)
    resp = client.put(
        "/api/repositories/1",
        json={
            "default_pipeline_id": 1,
            "default_executor": "codex",
            "default_model": "gpt-5-codex",
        },
    )
    assert resp.status_code == 200, resp.text
    task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "title": "t", "description": "d", "kind": "feature"},
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["pipeline_id"] == 1
    assert body["executor"] == "codex"
    assert body["model"] == "gpt-5-codex"


def test_executor_explicito_diferente_nao_herda_modelo(settings, bare_repo):
    client = _client(settings, bare_repo)
    client.put(
        "/api/repositories/1",
        json={
            "default_pipeline_id": 1,
            "default_executor": "codex",
            "default_model": "gpt-5-codex",
        },
    )
    task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "title": "t", "kind": "feature", "executor": "kimi"},
    )
    assert task.status_code == 201, task.text
    body = task.json()
    assert body["executor"] == "kimi"
    assert body["model"] is None
    # O pipeline padrão continua valendo — só o executor foi escolhido.
    assert body["pipeline_id"] == 1


def test_pipeline_explicito_vence_default(settings, bare_repo):
    client = _client(settings, bare_repo)
    pipelines = client.get("/api/pipelines").json()
    assert len(pipelines) >= 2
    default_id, other_id = pipelines[0]["id"], pipelines[1]["id"]
    client.put("/api/repositories/1", json={"default_pipeline_id": default_id})
    task = client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": other_id,
            "title": "t",
            "kind": "feature",
        },
    )
    assert task.status_code == 201, task.text
    assert task.json()["pipeline_id"] == other_id


def test_task_sem_pipeline_e_sem_default_recusada(settings, bare_repo):
    client = _client(settings, bare_repo)
    resp = client.post(
        "/api/tasks",
        json={"repository_id": 1, "title": "t", "kind": "feature"},
    )
    assert resp.status_code == 400
    assert "pipeline padrão" in resp.json()["detail"]
