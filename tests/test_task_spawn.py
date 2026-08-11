"""Testes da ferramenta de propostas de tasks filhas durante a execução
(autoia_tasks.json → _spawn_tasks → propostas pendentes de aprovação humana).

O worker NUNCA cria a task automaticamente: grava a proposta `pending` (dedup por
task_id + title) e o humano decide aceitar/rejeitar (ver test_proposals.py).
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task, TaskStep
from app.worker import runner

HARMLESS = [{"role": "assistant", "content": "tarefa concluída"}]


@pytest.fixture
def spawn_flow(settings, bare_repo):
    """App + session_factory + repo (allow_auto_tasks é obsoleto/ignorado)."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={
            "name": "r",
            "url": bare_repo,
            "default_branch": "main",
            "allow_auto_tasks": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": resp.json()["id"],
    }


def _kimi_spawn(tmp_path, tasks: list[dict]) -> str:
    """Fake kimi que emite JSONL e, quando o prompt NÃO pede veredicto (fases de
    implementação/refino), escreve autoia_tasks.json no cwd. Em fases verify
    escreve apenas o veredicto PASS — reproduz o caso real (arquivo escrito uma
    vez e consumido pelo worker no fim da fase)."""
    script = tmp_path / f"kimi_spawn_{len(list(tmp_path.glob('kimi_spawn_*')))}"
    payload = json.dumps(tasks)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
        "if 'VEREDICTO' not in prompt.upper():\n"
        f"    with open('autoia_tasks.json', 'w') as f:\n"
        f"        f.write({payload!r})\n"
        "if 'VEREDICTO' in prompt.upper() and 'PASS' in prompt:\n"
        "    with open('autoia_verdict.txt', 'w') as f:\n"
        "        f.write('PASS\\nSUMMARY: ok')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _simple_pipeline(flow) -> int:
    """Pipeline po → qa (2 fases) e retorna o id."""
    client = flow["client"]
    robots = client.get(f"/api/robots?repository_id={flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "spawn-pipeline",
            "repository_id": flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["po"]},
                {"position": 1, "robot_id": by_name["qa"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _claim_and_execute(flow) -> None:
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _all_tasks(flow) -> list[dict]:
    return flow["client"].get(f"/api/tasks?repository_id={flow['repo_id']}").json()


def _proposals(flow, task_id: int) -> list[dict]:
    return flow["client"].get(f"/api/tasks/{task_id}/proposals").json()


def test_robo_cria_proposta_de_task_filha(spawn_flow, tmp_path):
    """po escreve autoia_tasks.json → o worker grava uma proposta `pending`, sem
    criar a task filha automaticamente."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(
        tmp_path,
        [{"title": "filha", "description": "desc da filha", "kind": "bug"}],
    )
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    pipeline_id = _simple_pipeline(spawn_flow)
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)

    # Nenhuma task filha foi criada — só a proposta ficou pendente
    assert len(_all_tasks(spawn_flow)) == 1

    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["status"] == "pending"
    assert prop["title"] == "filha"
    assert prop["kind"] == "bug"
    assert prop["description"] == "desc da filha"
    assert prop["target_repository_id"] is None
    assert prop["accepted_task_id"] is None

    # evento de auditoria no step do pai
    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        step = next(st for st in parent.steps if st.position == 0)
        spawned = [e for e in step.events if e.kind == "task_spawned"]
        assert len(spawned) == 1
        assert spawned[0].payload["count"] == 1
        assert "filha" in spawned[0].payload["titles"]


def test_reexecucao_nao_duplica_proposta(spawn_flow, tmp_path):
    """Dedup por task_id + title: se a fase re-executa e o robô propõe a mesma
    tarefa, não cria uma segunda proposta pendente."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, [{"title": "filha", "kind": "feature"}])
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    pipeline_id = _simple_pipeline(spawn_flow)
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)  # po (escreve autoia_tasks.json)
    with spawn_flow["session_factory"]() as s:
        s.query(TaskStep).update({"status": "pending"})  # re-executa a fase
    _claim_and_execute(spawn_flow)  # po de novo

    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending"


def test_propostas_independentes_de_allow_auto_tasks(settings, bare_repo, tmp_path):
    """allow_auto_tasks é obsoleto: propostas são gravadas sempre, independente do
    flag do repositório."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main", "allow_auto_tasks": False},
    )
    assert resp.status_code == 201, resp.text
    repo_id = resp.json()["id"]

    settings.kimi_bin = _kimi_spawn(tmp_path, [{"title": "filha", "kind": "feature"}])
    settings.task_budget = 100.0

    robots = client.get(f"/api/robots?repository_id={repo_id}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "p",
            "repository_id": repo_id,
            "steps": [{"position": 0, "robot_id": by_name["po"]}],
        },
    )
    resp = client.post(
        "/api/tasks",
        json={"repository_id": repo_id, "pipeline_id": resp.json()["id"], "title": "pai", "description": "d"},
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    flow = {"settings": settings, "session_factory": session_factory, "client": client, "repo_id": repo_id}
    _claim_and_execute(flow)

    proposals = _proposals(flow, parent_id)
    assert len(proposals) == 1
    assert proposals[0]["title"] == "filha"


def test_spawn_tambem_em_task_com_subtarefas(spawn_flow, tmp_path):
    """Regressão: _spawn_tasks também roda após implement/verify de subtarefas
    (antes o arquivo só era lido depois de uma fase 'normal')."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, [{"title": "netinha", "kind": "chore"}])
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    robots = client.get(f"/api/robots?repository_id={spawn_flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "subtask-pipeline",
            "repository_id": spawn_flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["developer"]},
                {"position": 1, "robot_id": by_name["tester"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": resp.json()["id"],
            "title": "pai-com-subtarefas",
            "description": "d",
            "kind": "feature",
            "subtasks": [
                {"title": "sub 1", "description": "d1", "acceptance_criteria": "c1"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)  # implement (ciclo de subtarefa)
    _claim_and_execute(spawn_flow)  # verify (ciclo de subtarefa) → spawn

    assert len(_all_tasks(spawn_flow)) == 1  # só o pai; nada criado automaticamente
    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 1
    assert proposals[0]["title"] == "netinha"
