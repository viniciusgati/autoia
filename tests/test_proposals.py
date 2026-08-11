"""Testes das propostas de tasks filhas e da aprovação humana (aceitar/rejeitar)."""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task, TaskStep
from app.worker import runner


@pytest.fixture
def proposals_flow(settings, bare_repo):
    """App + session_factory + repo + task pai cuja fase po gera a proposta."""
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


def _kimi_spawn(tmp_path, tasks: list[dict]) -> str:
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


def _prepare(flow, tmp_path, phases: list[str] | None = None) -> dict:
    """Cria pipeline (po→qa por padrão), task pai com a proposta e executa a fase po.

    Retorna a task pai e a proposta pendente.
    """
    phases = phases or ["po", "qa"]
    settings = flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, [{"title": "filha", "description": "desc", "kind": "bug"}])
    settings.task_budget = 100.0
    client = flow["client"]

    robots = client.get(f"/api/robots?repository_id={flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "p",
            "repository_id": flow["repo_id"],
            "steps": [{"position": i, "robot_id": by_name[name]} for i, name in enumerate(phases)],
        },
    )
    pipeline_id = resp.json()["id"]
    resp = client.post(
        "/api/tasks",
        json={"repository_id": flow["repo_id"], "pipeline_id": pipeline_id, "title": "pai", "description": "d"},
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)

    proposals = client.get(f"/api/tasks/{parent_id}/proposals").json()
    assert len(proposals) == 1
    return {"parent_id": parent_id, "proposal": proposals[0]}


def test_accept_cria_task_filha_e_marca_aceita(proposals_flow, tmp_path):
    client = proposals_flow["client"]
    prep = _prepare(proposals_flow, tmp_path)
    parent_id, proposal = prep["parent_id"], prep["proposal"]

    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposal['id']}/accept")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # task filha criada: parent, steps copiados, executor herdado, aguardando humano
    child = next(t for t in data["children"] if t["parent_task_id"] == parent_id)
    assert child["title"] == "filha"
    assert child["kind"] == "bug"
    assert child["status"] == "created"
    assert child["executor"] == "kimi"
    assert len(child["steps"]) == 2
    assert child["branch"] is None

    # proposta marcada aceita + link para a task criada
    prop_after = next(p for p in data["proposals"] if p["id"] == proposal["id"])
    assert prop_after["status"] == "accepted"
    assert prop_after["accepted_task_id"] == child["id"]

    # evento de auditoria
    with proposals_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        anchor = sorted(parent.steps, key=lambda x: x.position)[0]
        accepted = [e for e in anchor.events if e.kind == "proposal_accepted"]
        assert len(accepted) == 1
        assert accepted[0].payload["child_task_id"] == child["id"]


def test_accept_filha_pode_ser_iniciada_e_executa(proposals_flow, tmp_path, fake_kimi):
    client = proposals_flow["client"]
    # pipeline de UMA fase (po) para o pai: sem fases pendentes do pai sobrando
    # para interferir no FIFO do worker quando a filha for executada
    prep = _prepare(proposals_flow, tmp_path, phases=["po"])
    parent_id, proposal = prep["parent_id"], prep["proposal"]

    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposal['id']}/accept")
    child = next(c for c in resp.json()["children"] if c["parent_task_id"] == parent_id)

    # troca o fake para a filha executar sem re-propor
    proposals_flow["settings"].kimi_bin = fake_kimi([{"role": "assistant", "content": "ok"}])

    resp = client.post(f"/api/tasks/{child['id']}/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    step_id = runner.claim_next(proposals_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(proposals_flow["settings"], proposals_flow["session_factory"], step_id)

    with proposals_flow["session_factory"]() as s:
        child_db = s.get(Task, child["id"])
        assert child_db.status == "done"


def test_reject_marca_rejeitada(proposals_flow, tmp_path):
    client = proposals_flow["client"]
    prep = _prepare(proposals_flow, tmp_path)
    parent_id, proposal = prep["parent_id"], prep["proposal"]

    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposal['id']}/reject")
    assert resp.status_code == 200, resp.text
    prop_after = next(p for p in resp.json()["proposals"] if p["id"] == proposal["id"])
    assert prop_after["status"] == "rejected"
    assert prop_after["accepted_task_id"] is None
    assert resp.json()["children"] == []  # nenhuma task criada

    with proposals_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        anchor = sorted(parent.steps, key=lambda x: x.position)[0]
        rejected = [e for e in anchor.events if e.kind == "proposal_rejected"]
        assert len(rejected) == 1


def test_aceitar_duas_vezes_falha(proposals_flow, tmp_path):
    client = proposals_flow["client"]
    prep = _prepare(proposals_flow, tmp_path)
    parent_id, proposal = prep["parent_id"], prep["proposal"]

    assert client.post(f"/api/tasks/{parent_id}/proposals/{proposal['id']}/accept").status_code == 200
    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposal['id']}/accept")
    assert resp.status_code == 400
    assert "já foi" in resp.json()["detail"]


def test_propostas_aparecem_no_taskout(proposals_flow, tmp_path):
    client = proposals_flow["client"]
    prep = _prepare(proposals_flow, tmp_path)
    parent_id = prep["parent_id"]

    data = client.get(f"/api/tasks/{parent_id}").json()
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["status"] == "pending"


def test_accept_cross_repo_requer_allow_external_tasks(proposals_flow, tmp_path, bare_repo):
    """Proposta mirando outro repo: aceitar só funciona se o alvo aceitar tasks
    externas (`allow_external_tasks`)."""
    client = proposals_flow["client"]
    # repo alvo "docs" sem allow_external_tasks
    docs = client.post(
        "/api/repositories",
        json={"name": "docs", "url": bare_repo, "default_branch": "main"},
    )
    assert docs.status_code == 201, docs.text

    settings = proposals_flow["settings"]
    settings.kimi_bin = _kimi_spawn(
        tmp_path,
        [{"title": "doc da feature", "kind": "chore", "repository": "docs"}],
    )
    settings.task_budget = 100.0

    robots = client.get(f"/api/robots?repository_id={proposals_flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "p",
            "repository_id": proposals_flow["repo_id"],
            "steps": [{"position": 0, "robot_id": by_name["po"]}],
        },
    )
    resp = client.post(
        "/api/tasks",
        json={"repository_id": proposals_flow["repo_id"], "pipeline_id": resp.json()["id"], "title": "pai"},
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    step_id = runner.claim_next(proposals_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(proposals_flow["settings"], proposals_flow["session_factory"], step_id)

    proposals = client.get(f"/api/tasks/{parent_id}/proposals").json()
    assert len(proposals) == 1
    assert proposals[0]["target_repository_id"] == docs.json()["id"]

    # alvo não aceita tasks externas → aceitar falha
    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposals[0]['id']}/accept")
    assert resp.status_code == 400
    assert "externas" in resp.json()["detail"]

    # habilitando no alvo, passa a funcionar
    client.put(f"/api/repositories/{docs.json()['id']}", json={"allow_external_tasks": True})
    resp = client.post(f"/api/tasks/{parent_id}/proposals/{proposals[0]['id']}/accept")
    assert resp.status_code == 200, resp.text
    child = next(
        t for t in resp.json()["children"] if t["parent_task_id"] == parent_id
    )
    assert child["repository_id"] == docs.json()["id"]
