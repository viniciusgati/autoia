"""Testes da página global "Execução" (GET /api/execution)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import RunEvent, Task, TaskProposal, TaskStep
from app.worker.runner import _system_event


def _setup(settings, bare_repo, statuses=("queued",)):
    """App + repo + N tasks (uma por status), retornando os objetos de apoio."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text
    repo_id = resp.json()["id"]
    task_ids = []
    for i, status in enumerate(statuses):
        resp = client.post(
            "/api/tasks",
            json={"repository_id": repo_id, "pipeline_id": 1, "title": f"t{i}", "description": "d"},
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["id"]
        with session_factory() as s:
            task = s.get(Task, task_id)
            task.status = status
            if status in ("queued", "in_progress"):
                task.steps[0].status = "pending"
            s.commit()
        task_ids.append(task_id)
    return {
        "client": client,
        "session_factory": session_factory,
        "repo_id": repo_id,
        "task_ids": task_ids,
    }


def test_execution_retorna_tasks_ativas(settings, bare_repo):
    flow = _setup(settings, bare_repo, statuses=("in_progress", "done", "failed"))
    data = flow["client"].get("/api/execution").json()

    ids = [t["id"] for t in data["tasks"]]
    assert flow["task_ids"][0] in ids  # in_progress aparece
    assert flow["task_ids"][1] not in ids  # done não é ativa
    assert flow["task_ids"][2] not in ids  # failed não é ativa


def test_execution_filtra_por_repositorio(settings, bare_repo):
    flow = _setup(settings, bare_repo, statuses=("queued",))
    other = flow["client"].post(
        "/api/repositories",
        json={"name": "outro", "url": bare_repo, "default_branch": "main"},
    ).json()
    flow["client"].post(
        "/api/tasks",
        json={"repository_id": other["id"], "pipeline_id": 1, "title": "de outro repo", "description": "d"},
    )

    data = flow["client"].get(f"/api/execution?repository_id={flow['repo_id']}").json()
    assert all(t["repository_id"] == flow["repo_id"] for t in data["tasks"])
    assert all(p["task_id"] in flow["task_ids"] for p in data["proposals"])


def test_execution_current_events_da_fase_running(settings, bare_repo):
    flow = _setup(settings, bare_repo, statuses=("in_progress",))
    task_id = flow["task_ids"][0]

    with flow["session_factory"]() as s:
        task = s.get(Task, task_id)
        step = task.steps[0]
        step.status = "running"
        _system_event(s, step, "attempt_started", {"attempt": 1, "robot": "po"})
        _system_event(s, step, "assistant_text", {"content": "trabalhando…"})
        s.commit()

    data = flow["client"].get("/api/execution").json()
    events = data["current_events"].get(str(step.id))
    assert events is not None
    kinds = [e["kind"] for e in events]
    assert "assistant_text" in kinds
    assert events[0]["kind"] == "assistant_text"  # mais recente primeiro


def test_execution_propostas_e_notices(settings, bare_repo):
    flow = _setup(settings, bare_repo, statuses=("queued",))
    task_id = flow["task_ids"][0]

    with flow["session_factory"]() as s:
        task = s.get(Task, task_id)
        s.add(
            TaskProposal(
                task_id=task.id,
                step_id=task.steps[0].id,
                position=0,
                title="proposta pendente",
                description="desc",
                kind="feature",
                status="pending",
            )
        )
        # task aguardando revisão vira notice
        s.add(Task(repository_id=flow["repo_id"], pipeline_id=1, title="precisa de humano",
                   description="", status="needs_review", error="falha pós-merge"))
        s.commit()

    data = flow["client"].get("/api/execution").json()
    assert [p["title"] for p in data["proposals"]] == ["proposta pendente"]
    assert any(n["kind"] == "needs_review" for n in data["notices"])


def test_execution_worker_status(settings, bare_repo, tmp_path):
    flow = _setup(settings, bare_repo, statuses=("queued",))
    data = flow["client"].get("/api/execution").json()
    assert data["worker"]["alive"] is False  # sem heartbeat no workspace de teste
