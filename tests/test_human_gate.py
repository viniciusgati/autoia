"""Testes do gate de aprovação humana (pause_before configurado no pipeline)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task
from app.worker import runner

HARMLESS = [{"role": "assistant", "content": "tarefa concluída"}]


@pytest.fixture
def gate_flow(settings, bare_repo):
    """App + worker session_factory + repo criado (SEM task preexistente — o
    fixture `flow` cria uma task iniciada que o claim pegaria antes das nossas)."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    response = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": response.json()["id"],
    }


def _make_gate_pipeline(flow) -> int:
    """Cria pipeline com gate no developer (pos 2) via API e retorna o id."""
    client = flow["client"]
    robots = client.get(f"/api/robots?repository_id={flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "gate-pipeline",
            "repository_id": flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["po"]},
                {"position": 1, "robot_id": by_name["qa"]},
                {"position": 2, "robot_id": by_name["developer"], "pause_before": True},
                {"position": 3, "robot_id": by_name["merger"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_gate_task(flow) -> dict:
    pipeline_id = _make_gate_pipeline(flow)
    client = flow["client"]
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "gate",
            "description": "d",
            "kind": "feature",
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    client.post(f"/api/tasks/{task['id']}/start")
    return task


def _run_po_qa(flow) -> None:
    """Executa po (0) e qa (1) com o kimi fake (ready_pass)."""
    for _ in range(2):
        step_id = runner.claim_next(flow["session_factory"])
        assert step_id is not None
        runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _state(flow, task_id) -> dict:
    """Snapshot serializado da task (sessão já fechada — sem lazy-load fora dela)."""
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        return {
            "status": t.status,
            "feedback": t.feedback,
            "description": t.description,
            "acceptance_criteria": t.acceptance_criteria,
            "steps": [
                {
                    "position": st.position,
                    "status": st.status,
                    "pause_before": st.pause_before,
                    "events": [e.kind for e in st.events],
                }
                for st in sorted(t.steps, key=lambda x: x.position)
            ],
        }


def _step(state: dict, position: int) -> dict:
    return next(st for st in state["steps"] if st["position"] == position)


def test_gate_persistido_no_pipeline_e_copiado_para_task(gate_flow):
    pipeline_id = _make_gate_pipeline(gate_flow)

    # pipeline criado com pause_before no developer
    pipes = gate_flow["client"].get(f"/api/pipelines?repository_id={gate_flow['repo_id']}").json()
    gate = next(p for p in pipes if p["name"] == "gate-pipeline")
    dev = next(st for st in gate["steps"] if st["position"] == 2)
    assert dev["pause_before"] is True
    assert all(not st["pause_before"] for st in gate["steps"] if st["position"] != 2)

    # task copia a flag para o TaskStep
    resp = gate_flow["client"].post(
        "/api/tasks",
        json={
            "repository_id": gate_flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "gate",
            "description": "d",
            "kind": "feature",
        },
    )
    assert resp.status_code == 201, resp.text
    body = gate_flow["client"].get(f"/api/tasks/{resp.json()['id']}").json()
    dev = next(st for st in body["steps"] if st["position"] == 2)
    assert dev["pause_before"] is True


def test_gate_para_a_task_antes_do_developer(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)

    _run_po_qa(gate_flow)

    # próxima fase é o developer (com gate) → o worker NÃO reclama
    assert runner.claim_next(gate_flow["session_factory"]) is None

    state = _state(gate_flow, task["id"])
    assert state["status"] == "waiting_approval"
    dev = _step(state, 2)
    assert dev["status"] == "pending"
    gate_events = [e for e in dev["events"] if e == "human_gate"]
    assert len(gate_events) == 1
    # o robô NÃO executou: sem attempt_started no step gated
    assert "attempt_started" not in dev["events"]

    # claim repetido: sem evento duplicado, sem execução
    assert runner.claim_next(gate_flow["session_factory"]) is None
    state = _state(gate_flow, task["id"])
    dev = _step(state, 2)
    assert len([e for e in dev["events"] if e == "human_gate"]) == 1


def test_aprovar_libera_a_fase(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)
    client = gate_flow["client"]

    _run_po_qa(gate_flow)
    assert runner.claim_next(gate_flow["session_factory"]) is None

    resp = client.post(
        f"/api/tasks/{task['id']}/approve-step",
        json={"position": 2, "note": "confirmar nomenclatura das rotas"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["feedback"] == "confirmar nomenclatura das rotas"

    # o worker agora libera o developer
    step_id = runner.claim_next(gate_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(gate_flow["settings"], gate_flow["session_factory"], step_id)

    state = _state(gate_flow, task["id"])
    dev = _step(state, 2)
    assert dev["status"] == "done"
    assert "human_gate_approved" in dev["events"]


def test_aprovar_sem_gate_rejeita(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)
    client = gate_flow["client"]

    resp = client.post(f"/api/tasks/{task['id']}/approve-step", json={"position": 0})
    assert resp.status_code == 400  # po não tem gate

    _run_po_qa(gate_flow)
    resp = client.post(f"/api/tasks/{task['id']}/approve-step", json={"position": 1})
    assert resp.status_code == 400  # qa não tem gate


def test_voltar_fase_anterior_a_partir_do_gate(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)
    client = gate_flow["client"]

    _run_po_qa(gate_flow)
    assert runner.claim_next(gate_flow["session_factory"]) is None

    # humano volta para o qa com a nota (reusa o retry de fase)
    resp = client.post(
        f"/api/tasks/{task['id']}/steps/1/retry",
        json={"note": "revisar critérios de aceite"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    qa = next(st for st in body["steps"] if st["position"] == 1)
    assert qa["status"] == "pending"

    # qa re-executa e o pipeline volta a parar no gate do developer
    step_id = runner.claim_next(gate_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(gate_flow["settings"], gate_flow["session_factory"], step_id)
    assert runner.claim_next(gate_flow["session_factory"]) is None
    assert _state(gate_flow, task["id"])["status"] == "waiting_approval"


def test_editar_historia_so_em_gate_ou_created(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)
    client = gate_flow["client"]

    # durante a execução (po rodando) → 400
    step_id = runner.claim_next(gate_flow["session_factory"])
    assert step_id is not None
    resp = client.patch(f"/api/tasks/{task['id']}", json={"description": "x"})
    assert resp.status_code == 400
    runner.execute_step(gate_flow["settings"], gate_flow["session_factory"], step_id)

    # qa executa e o pipeline para no gate
    step_id = runner.claim_next(gate_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(gate_flow["settings"], gate_flow["session_factory"], step_id)
    assert runner.claim_next(gate_flow["session_factory"]) is None

    resp = client.patch(
        f"/api/tasks/{task['id']}",
        json={"description": "nova desc", "acceptance_criteria": "critério 1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "nova desc"
    assert body["acceptance_criteria"] == "critério 1"

    # a alteração entra no prompt da fase aprovada (aprova e executa o developer)
    resp = client.post(
        f"/api/tasks/{task['id']}/approve-step",
        json={"position": 2},
    )
    assert resp.status_code == 200, resp.text
    step_id = runner.claim_next(gate_flow["session_factory"])
    assert step_id is not None
    runner.execute_step(gate_flow["settings"], gate_flow["session_factory"], step_id)


def test_dashboard_notice_para_gate(gate_flow, fake_kimi):
    settings = gate_flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = _create_gate_task(gate_flow)
    _run_po_qa(gate_flow)
    assert runner.claim_next(gate_flow["session_factory"]) is None

    notices = gate_flow["client"].get("/api/dashboard").json()["notices"]
    gates = [n for n in notices if n["kind"] == "human_gate"]
    assert len(gates) == 1
    assert gates[0]["task_id"] == task["id"]
    assert "developer" in gates[0]["message"]
