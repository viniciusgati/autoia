"""Testes da allowlist de criação de tarefas cross-projeto (`task_targets`) e do
contexto externo (`external_context`) injetado nos robôs.

- `task_targets` vazio = restritivo: proposta para outro repo é recusada.
- `task_targets` com nomes = permitido só para aqueles repos.
- `external_context` entra no prompt e no AGENTS.md gerado.
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task
from app.prompts import build_prompt
from app.worker import runner
from app.worker import project as project_mod


def _new_app(settings, bare_repo):
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    r1 = client.post(
        "/api/repositories",
        json={"name": "api", "url": bare_repo, "default_branch": "main"},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/repositories",
        json={"name": "docs", "url": bare_repo, "default_branch": "main"},
    )
    assert r2.status_code == 201, r2.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
    }


def _kimi_cross(tmp_path, tasks: list[dict]) -> str:
    """Fake kimi que escreve autoia_tasks.json (sem veredicto — fase de análise)."""
    script = tmp_path / f"kimi_cross_{len(list(tmp_path.glob('kimi_cross_*')))}"
    payload = json.dumps(tasks)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        "with open('autoia_tasks.json', 'w') as f:\n"
        f"    f.write({payload!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _simple_pipeline(flow, repo_id: int, robot: str) -> int:
    robots = flow["client"].get(f"/api/robots?repository_id={repo_id}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = flow["client"].post(
        "/api/pipelines",
        json={
            "name": f"p-{robot}",
            "repository_id": repo_id,
            "steps": [{"position": 0, "robot_id": by_name[robot]}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_task_targets_vazio_recusa_cross_repo(settings, bare_repo, tmp_path):
    """Restritivo: sem task_targets, proposta para outro repo é recusada."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]
    settings.kimi_bin = _kimi_cross(
        tmp_path,
        [{"title": "doc", "description": "d", "kind": "chore", "repository": "docs"}],
    )
    settings.task_budget = 100.0

    pipeline_id = _simple_pipeline(flow, 1, "iniciador")
    resp = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": pipeline_id, "title": "brain", "description": "d"},
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")

    step_id = runner.claim_next(flow["session_factory"])
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)

    proposals = client.get(f"/api/tasks/{task_id}/proposals").json()
    assert proposals == []

    with flow["session_factory"]() as s:
        task = s.get(Task, task_id)
        blocked = [
            e for st in task.steps for e in st.events if e.kind == "task_spawn_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0].payload["target"] == "docs"
        assert "task_targets" in blocked[0].payload["reason"]


def test_task_targets_permitido_cria_proposta(settings, bare_repo, tmp_path):
    """Com task_targets incluindo o repo, a proposta cross-repo é gravada."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]

    # api (id 1) permite criar tarefas em docs (id 2)
    resp = client.put(
        "/api/repositories/1",
        json={"task_targets": ["docs"], "external_context": "Deploy: https://app.exemplo.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_targets"] == ["docs"]
    assert resp.json()["external_context"] == "Deploy: https://app.exemplo.com"

    settings.kimi_bin = _kimi_cross(
        tmp_path,
        [{"title": "doc", "description": "d", "kind": "chore", "repository": "docs"}],
    )
    settings.task_budget = 100.0

    pipeline_id = _simple_pipeline(flow, 1, "iniciador")
    resp = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": pipeline_id, "title": "brain", "description": "d"},
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")

    step_id = runner.claim_next(flow["session_factory"])
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)

    proposals = client.get(f"/api/tasks/{task_id}/proposals").json()
    assert len(proposals) == 1
    assert proposals[0]["target_repository_id"] == 2


def test_task_targets_fora_da_lista_recusado(settings, bare_repo, tmp_path):
    """Repo não está na allowlist → proposta recusada (mesmo existindo)."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]

    # api (1) só permite criar em "docs"; robô mira um terceiro inexistente na lista
    client.put("/api/repositories/1", json={"task_targets": ["docs"]})

    settings.kimi_bin = _kimi_cross(
        tmp_path,
        [{"title": "x", "description": "d", "kind": "chore", "repository": "api"}],
    )
    settings.task_budget = 100.0

    pipeline_id = _simple_pipeline(flow, 1, "iniciador")
    resp = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": pipeline_id, "title": "t", "description": "d"},
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")

    step_id = runner.claim_next(flow["session_factory"])
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)

    assert client.get(f"/api/tasks/{task_id}/proposals").json() == []


def test_task_targets_invalidos_rejeitados(settings, bare_repo):
    """Alvo inexistente ou o próprio projeto → 400."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]

    resp = client.put("/api/repositories/1", json={"task_targets": ["inexistente"]})
    assert resp.status_code == 400

    resp = client.put("/api/repositories/1", json={"task_targets": ["api"]})
    assert resp.status_code == 400

    # alvo válido + contexto passam
    resp = client.put(
        "/api/repositories/1",
        json={"task_targets": ["docs"], "external_context": "x"},
    )
    assert resp.status_code == 200


def test_external_context_entra_no_prompt_e_no_agents_md(settings, bare_repo):
    """external_context e task_targets aparecem no prompt e no AGENTS.md gerado."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]
    client.put(
        "/api/repositories/1",
        json={"task_targets": ["docs"], "external_context": "Deploy DNS: https://app.exemplo.com"},
    )

    robots = client.get("/api/robots?repository_id=1").json()
    po = next(r for r in robots if r["name"] == "iniciador")

    # unidade: build_prompt injeta repo_context
    from types import SimpleNamespace

    from app.models import Robot as RobotModel

    robot = RobotModel(name="iniciador", role="analyze", mission="m.")
    task = SimpleNamespace(
        title="t", description="d", acceptance_criteria="",
        feedback="", details="", resume_instruction="", executor="kimi",
    )
    repo_context = project_mod.build_repo_context(["docs"], "Deploy DNS: https://app.exemplo.com")
    prompt = build_prompt(robot, task, "ctx", "main", repo_context=repo_context)
    assert "docs" in prompt
    assert "Deploy DNS: https://app.exemplo.com" in prompt
    assert "criar tarefas" in prompt

    # AGENTS.md: a seção entra no template gerado
    md = project_mod.build_agents_md("python", repo_context=repo_context)
    assert "Deploy DNS: https://app.exemplo.com" in md
    assert "Repositórios onde este projeto pode criar tarefas" in md


def test_repo_proposals_endpoint_lista_do_projeto(settings, bare_repo, tmp_path):
    """GET /api/repositories/{id}/proposals lista propostas do projeto (pendentes +
    aceitas; rejeitadas saem), isolando entre projetos."""
    flow = _new_app(settings, bare_repo)
    client = flow["client"]
    client.put("/api/repositories/1", json={"task_targets": ["docs"]})
    settings.kimi_bin = _kimi_cross(
        tmp_path,
        [
            {"title": "doc a", "description": "d", "kind": "chore", "repository": "docs"},
            {"title": "doc b", "description": "d", "kind": "chore", "repository": "docs"},
        ],
    )
    settings.task_budget = 100.0

    pipeline_id = _simple_pipeline(flow, 1, "iniciador")
    resp = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": pipeline_id, "title": "brain", "description": "d"},
    )
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")
    step_id = runner.claim_next(flow["session_factory"])
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)

    props = client.get("/api/repositories/1/proposals").json()
    assert {p["title"] for p in props} == {"doc a", "doc b"}
    # projeto 2 (docs) não tem propostas próprias
    assert client.get("/api/repositories/2/proposals").json() == []

    # rejeita uma → sai da lista do projeto
    b = next(p for p in client.get(f"/api/tasks/{task_id}/proposals").json() if p["title"] == "doc b")
    client.post(f"/api/tasks/{task_id}/proposals/{b['id']}/reject")
    props2 = client.get("/api/repositories/1/proposals").json()
    assert {p["title"] for p in props2} == {"doc a"}
