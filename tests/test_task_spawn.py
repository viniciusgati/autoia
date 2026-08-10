"""Testes da ferramenta de criação de tarefas filhas durante a execução
(autoia_tasks.json → _spawn_tasks → task filha aguardando aprovação humana)."""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task
from app.worker import runner

HARMLESS = [{"role": "assistant", "content": "tarefa concluída"}]


@pytest.fixture
def spawn_flow(settings, bare_repo, request):
    """App + session_factory + repo (allow_auto_tasks conforme o teste)."""
    allow_auto = getattr(request, "param", False)
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={
            "name": "r",
            "url": bare_repo,
            "default_branch": "main",
            "allow_auto_tasks": allow_auto,
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


@pytest.mark.parametrize("spawn_flow", [True], indirect=True)
def test_robo_cria_task_filha_durante_execucao(spawn_flow, fake_kimi, tmp_path):
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

    # po executa e escreve autoia_tasks.json → worker spawna a filha
    _claim_and_execute(spawn_flow)

    tasks = _all_tasks(spawn_flow)
    assert len(tasks) == 2
    child = next(t for t in tasks if t["id"] != parent_id)
    assert child["status"] == "created"  # aguarda aprovação humana
    assert child["title"] == "filha"
    assert child["kind"] == "bug"
    assert child["description"] == "desc da filha"
    assert child["parent_task_id"] == parent_id
    assert len(child["steps"]) == 2  # copiou o pipeline da task pai
    assert child["branch"] is None

    # evento de auditoria no step do pai
    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        step = next(st for st in parent.steps if st.position == 0)
        spawned = [e for e in step.events if e.kind == "task_spawned"]
        assert len(spawned) == 1
        assert spawned[0].payload["count"] == 1
        assert "filha" in spawned[0].payload["titles"]


@pytest.mark.parametrize("spawn_flow", [True], indirect=True)
def test_filha_pode_ser_aprovada_e_executa(spawn_flow, fake_kimi, tmp_path):
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(
        tmp_path, [{"title": "filha", "description": "", "kind": "feature"}]
    )
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    # pipeline de UMA fase (po): o pai termina sozinho e o worker fica livre
    # para a filha (sem interferência FIFO de fases pendentes do pai)
    robots = client.get(f"/api/robots?repository_id={spawn_flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "one-phase",
            "repository_id": spawn_flow["repo_id"],
            "steps": [{"position": 0, "robot_id": by_name["po"]}],
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": resp.json()["id"],
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")
    _claim_and_execute(spawn_flow)

    child = next(t for t in _all_tasks(spawn_flow) if t["id"] != parent_id)

    # humano aprova e inicia a filha (mesmo fluxo do TaskDetail/RepoTasks)
    resp = client.post(f"/api/tasks/{child['id']}/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    # troca o fake para não re-espawnar (filha não deve gerar netas)
    settings.kimi_bin = fake_kimi(HARMLESS)

    _claim_and_execute(spawn_flow)  # po da filha

    with spawn_flow["session_factory"]() as s:
        child_db = s.get(Task, child["id"])
        assert child_db.status == "done"
        po = next(st for st in child_db.steps if st.position == 0)
        assert po.status == "done"
        assert po.summary  # rodou de verdade

    # nenhuma neta foi criada
    assert len(_all_tasks(spawn_flow)) == 2


@pytest.mark.parametrize("spawn_flow", [False], indirect=True)
def test_sem_allow_auto_tasks_nao_spawna(spawn_flow, tmp_path):
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
    _claim_and_execute(spawn_flow)

    assert len(_all_tasks(spawn_flow)) == 1  # só o pai; o arquivo foi ignorado


@pytest.mark.parametrize("spawn_flow", [True], indirect=True)
def test_spawn_tambem_em_task_com_subtarefas(spawn_flow, tmp_path):
    """Regressão: _spawn_tasks também roda após implement/verify de subtarefas
    (antes o arquivo só era lido depois de uma fase 'normal')."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(
        tmp_path, [{"title": "netinha", "kind": "chore"}]
    )
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

    tasks = _all_tasks(spawn_flow)
    assert len(tasks) == 2
    child = next(t for t in tasks if t["id"] != parent_id)
    assert child["title"] == "netinha"
    assert child["parent_task_id"] == parent_id
