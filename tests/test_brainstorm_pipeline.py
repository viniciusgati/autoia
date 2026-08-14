"""Testes da pipeline de brainstorm: iniciador → analista → auditor-ux → propositor.

O propositor escreve autoia_tasks.json (tool_call) → o worker grava as propostas
pendentes de decisão humana (TaskProposal) e a task finaliza com merge na default.
O usuário decide aceitar/rejeitar cada proposta (ver test_proposals.py).
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task, TaskProposal
from app.worker import runner

HARMLESS = [{"role": "assistant", "content": "relatório ok"}]


@pytest.fixture
def brainstorm_flow(settings, bare_repo):
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": resp.json()["id"],
    }


def _kimi_brainstorm(tmp_path, tasks: list[dict]) -> str:
    """Fake kimi que escreve autoia_tasks.json apenas na fase do propositor (o
    prompt contém o contrato 'propostas de tarefas'). Nas demais, só responde."""
    script = tmp_path / f"kimi_brain_{len(list(tmp_path.glob('kimi_brain_*')))}"
    payload = json.dumps(tasks)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'relatório ok'}))\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
        "if 'propostas de tarefas' in prompt.lower():\n"
        f"    with open('autoia_tasks.json', 'w') as f:\n"
        f"        f.write({payload!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _pipeline_id(flow) -> int:
    pipelines = flow["client"].get(f"/api/pipelines?repository_id={flow['repo_id']}").json()
    bsp = next(p for p in pipelines if p["name"] == "iniciador-analista-ux-propositor")
    return bsp["id"]


def _run_until_done(flow) -> None:
    """Executa as fases até a task terminar (claim_next retorna None)."""
    for _ in range(20):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_brainstorm_pipeline_genera_propostas_e_merge(settings, bare_repo, tmp_path):
    """Pipeline completa: propositor gera 2 propostas → pendentes; task done com merge."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    repo_id = resp.json()["id"]
    flow = {"settings": settings, "session_factory": session_factory, "client": client, "repo_id": repo_id}

    settings.kimi_bin = _kimi_brainstorm(
        tmp_path,
        [
            {"title": "proposta 1", "description": "desc 1", "kind": "feature"},
            {"title": "proposta 2", "description": "desc 2", "kind": "bug"},
        ],
    )
    settings.task_budget = 100.0

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": repo_id,
            "pipeline_id": _pipeline_id(flow),
            "title": "brainstorm do projeto",
            "description": "analisar e propor",
            "kind": "issue",
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")

    _run_until_done(flow)

    # 4 fases executadas + merge → done (nenhuma fase exige veredicto)
    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "done"
        assert [st.robot.name for st in sorted(task.steps, key=lambda x: x.position)] == [
            "iniciador", "analista", "auditor-ux", "propositor",
        ]
        assert all(st.status == "done" for st in task.steps)

    proposals = client.get(f"/api/tasks/{task_id}/proposals").json()
    assert {p["title"] for p in proposals} == {"proposta 1", "proposta 2"}
    assert all(p["status"] == "pending" for p in proposals)
    assert all(p["accepted_task_id"] is None for p in proposals)

    # merge aconteceu: a branch da task está na default
    with session_factory() as s:
        task = s.get(Task, task_id)
        merged = [
            e for st in task.steps for e in st.events if e.kind == "merged"
        ]
        assert merged, "esperava evento merged (integração na default)"


def test_brainstorm_sem_propostas_termina_done(settings, bare_repo):
    """Se o propositor não escrever autoia_tasks.json, a task ainda termina done
    sem propostas (nenhuma fase exige veredicto — pipeline de análise)."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    repo_id = resp.json()["id"]
    flow = {"settings": settings, "session_factory": session_factory, "client": client, "repo_id": repo_id}

    import os

    script = os.path.join(str(settings.workspace_dir), "kimi_plain")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        )
    os.chmod(script, 0o755)
    settings.kimi_bin = script
    settings.task_budget = 100.0

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": repo_id,
            "pipeline_id": _pipeline_id(flow),
            "title": "brainstorm vazio",
            "description": "d",
            "kind": "issue",
        },
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")

    _run_until_done(flow)

    with session_factory() as s:
        task = s.get(Task, task_id)
        assert task.status == "done"
    assert client.get(f"/api/tasks/{task_id}/proposals").json() == []


def test_propostas_aparecem_no_dashboard(settings, bare_repo, tmp_path):
    """Dashboard lista propostas não-rejeitadas; rejeitar a tira da lista."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    repo_id = resp.json()["id"]
    flow = {"settings": settings, "session_factory": session_factory, "client": client, "repo_id": repo_id}

    settings.kimi_bin = _kimi_brainstorm(
        tmp_path,
        [
            {"title": "a", "description": "d a", "kind": "feature"},
            {"title": "b", "description": "d b", "kind": "chore"},
        ],
    )
    settings.task_budget = 100.0

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": repo_id,
            "pipeline_id": _pipeline_id(flow),
            "title": "brainstorm dashboard",
            "description": "d",
            "kind": "issue",
        },
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")
    _run_until_done(flow)

    dash = client.get("/api/dashboard").json()
    titles = {p["title"] for p in dash["proposals"]}
    assert {"a", "b"} <= titles

    # rejeita "b" → sai do dashboard (aceita/ pendente permanecem)
    prop_b = next(p for p in client.get(f"/api/tasks/{task_id}/proposals").json() if p["title"] == "b")
    client.post(f"/api/tasks/{task_id}/proposals/{prop_b['id']}/reject")

    dash2 = client.get("/api/dashboard").json()
    titles2 = {p["title"] for p in dash2["proposals"]}
    assert "b" not in titles2
    assert "a" in titles2
