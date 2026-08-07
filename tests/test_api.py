"""Testes de API + fluxo do worker (com kimi fake) ponta a ponta.

Cobrem: registro de repo, seed, criação/start de tarefa, avanço de fases,
merge final, orçamento -> needs_review -> revisão, e guardrail -> bloqueio.
O pipeline default agora é po-qa-dev-tester-merge (5 fases).
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import RunEvent, Task, TaskStep
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "tarefa concluída com sucesso"},
]

RISKY = [
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"rm -rf /"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
]

ONLY_TEXT = [
    {"role": "assistant", "content": "resposta única"},
]

PIPELINE_STEPS = 6


@pytest.fixture
def app_client(settings, bare_repo):
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture
def registered_repo(app_client, bare_repo):
    response = app_client.post(
        "/api/repositories",
        json={"name": "repo-teste", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_and_start_task(app_client, title="implementar hello"):
    response = app_client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": title,
            "description": "criar um hello.py",
            "kind": "feature",
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "created"
    assert len(task["steps"]) == PIPELINE_STEPS

    response = app_client.post(f"/api/tasks/{task['id']}/start")
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "queued"
    assert task["branch"] == "autoia/task-1"
    assert task["steps"][0]["status"] == "pending"
    return task


# ---------- API básica ----------

def test_register_repository(app_client, bare_repo, settings):
    repo = app_client.post(
        "/api/repositories",
        json={"name": "r1", "url": bare_repo, "default_branch": "main"},
    ).json()
    assert repo["default_branch"] == "main"
    assert repo["local_path"].startswith(settings.workspace_dir)
    assert app_client.get("/api/repositories").status_code == 200


def test_seed_robots_and_pipeline(app_client):
    robots = app_client.get("/api/robots").json()
    names = {r["name"] for r in robots}
    assert {"po", "qa", "developer", "tester", "avaliador", "merger", "pm"} <= names
    roles = {r["name"]: r["role"] for r in robots}
    assert roles["po"] == "refine"
    assert roles["qa"] == "review"
    assert roles["tester"] == "verify"
    assert roles["avaliador"] == "assess"
    assert roles["pm"] == "pm"

    pipelines = app_client.get("/api/pipelines").json()
    # pipeline default (primeiro): com a fase de avaliação entre tester e merger
    default = next(p for p in pipelines if p["name"] == "po-qa-dev-tester-avaliador-merge")
    assert len(default["steps"]) == 6
    order = [st["robot"]["name"] for st in default["steps"]]
    assert order == ["po", "qa", "developer", "tester", "avaliador", "merger"]

    main = next(p for p in pipelines if p["name"] == "po-qa-dev-tester-merge")
    assert len(main["steps"]) == 5
    order = [st["robot"]["name"] for st in main["steps"]]
    assert order == ["po", "qa", "developer", "tester", "merger"]


def test_create_and_start_task(app_client, registered_repo):
    task = _create_and_start_task(app_client)
    assert task["repository_id"] == 1


def test_retry_step(app_client, registered_repo, settings):
    task = _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        step = s.get(TaskStep, task["steps"][0]["id"])
        step.status = "failed"
        step.error = "algum erro"
        s.commit()

    response = app_client.post(f"/api/tasks/{task['id']}/steps/0/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"][0]["attempt"] == 2
    assert body["steps"][0]["status"] == "pending"
    assert body["status"] == "queued"


def test_review_approve_and_cancel(app_client, registered_repo):
    task = _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        t = s.get(Task, task["id"])
        t.status = "needs_review"
        t.error = "orçamento estourado"
        s.commit()

    response = app_client.post(
        f"/api/tasks/{task['id']}/review",
        json={"action": "approve", "extra_budget": 5.0},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "in_progress"
    assert body["budget_limit"] == pytest.approx(6.0)

    with app_client.app.state.Session() as s:
        t = s.get(Task, task["id"])
        t.status = "needs_review"
        s.commit()

    response = app_client.post(
        f"/api/tasks/{task['id']}/review",
        json={"action": "cancel", "note": "não aprovado"},
    )
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "não aprovado"


def test_dashboard(app_client, registered_repo):
    _create_and_start_task(app_client)
    data = app_client.get("/api/dashboard").json()
    assert data["total_tasks"] >= 1
    assert "queued" in data["tasks_by_status"]


def test_dashboard_notices(app_client, registered_repo):
    """Avisos do dashboard: guardrail, needs_review, blocked, custo alto e arquitetura."""
    _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        t1 = s.get(Task, 1)
        t1.status = "in_progress"
        t1.cost_spent = 0.9 * (t1.budget_limit or 1.0)  # custo alto (>= 80%)
        step = t1.steps[0]
        step.status = "guardrail_blocked"
        step.error = "guardrail: rm -rf"
        s.add(
            RunEvent(
                step_id=step.id,
                seq=99,
                kind="arch_metric",
                payload={"score": 80, "level": "alto", "reasons": ["A Dockerfile"]},
            )
        )
        s.add_all(
            [
                Task(
                    repository_id=1, pipeline_id=1, title="t2", kind="issue",
                    status="needs_review", error="orçamento estourado",
                ),
                Task(
                    repository_id=1, pipeline_id=1, title="t3", kind="issue",
                    status="blocked", error="conflito de merge",
                ),
            ]
        )
        s.commit()

    data = app_client.get("/api/dashboard").json()
    kinds = {n["kind"] for n in data["notices"]}
    assert kinds >= {"guardrail", "arch", "needs_review", "blocked", "budget_high"}
    by_kind = {n["kind"]: n for n in data["notices"]}
    assert by_kind["guardrail"]["level"] == "critical"
    assert by_kind["arch"]["level"] == "critical"
    assert by_kind["needs_review"]["level"] == "warning"
    # críticos ordenados antes dos warnings
    levels = [n["level"] for n in data["notices"]]
    assert levels == sorted(levels, key=lambda l: 0 if l == "critical" else 1)


# ---------- Fluxo do worker (kimi fake) ----------

def test_worker_advances_phases_and_merges(settings, bare_repo, tmp_path, fake_kimi):
    """po -> qa -> developer -> tester -> merger com kimi fake; no fim, merge+push."""
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    for _ in range(PIPELINE_STEPS + 2):  # margem
        claimed = runner.claim_next(session_factory)
        if claimed is None:
            break
        runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done"
        assert all(st.status == "done" for st in t.steps)
        total_events = sum(len(st.events) for st in t.steps)
        assert total_events > 0

    # merge chegou no bare
    dest = tmp_path / "verify"
    import subprocess

    subprocess.run(["git", "clone", bare_repo, str(dest)], check=True, capture_output=True)
    assert (dest / "README.md").exists()


def test_worker_budget_hits_needs_review(settings, bare_repo, tmp_path, fake_kimi):
    settings.kimi_bin = fake_kimi(ONLY_TEXT)
    settings.task_budget = 0.01  # primeira interação já estoura
    settings.cost_per_interaction = 0.01
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "needs_review"
        assert "orçamento" in (t.error or "")
        assert t.steps[0].status == "pending"
        kinds = [e.kind for e in t.steps[0].events]
        assert "budget_hit" in kinds


def test_worker_guardrail_blocks(settings, bare_repo, tmp_path, fake_kimi):
    settings.kimi_bin = fake_kimi(RISKY)
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "failed"
        step = t.steps[0]
        assert step.status == "guardrail_blocked"
        assert "rm -rf" in (step.error or "")
        kinds = [e.kind for e in step.events]
        assert "guardrail_blocked" in kinds


def test_worker_arch_metric_event(settings, bare_repo, tmp_path, fake_kimi):
    """Task que adiciona Dockerfile gera evento arch_metric 'alto' e aviso no dashboard."""
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="Dockerfile")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    for _ in range(PIPELINE_STEPS + 2):  # margem
        claimed = runner.claim_next(session_factory)
        if claimed is None:
            break
        runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done"
        merger = max(t.steps, key=lambda st: st.position)  # fase de integração
        events = {e.kind: e for e in merger.events}
        assert "arch_metric" in events
        payload = events["arch_metric"].payload
        assert payload["level"] == "alto"
        assert any("Dockerfile" in r for r in payload["reasons"])

    data = client.get("/api/dashboard").json()
    kinds = {n["kind"] for n in data["notices"]}
    assert "arch" in kinds
