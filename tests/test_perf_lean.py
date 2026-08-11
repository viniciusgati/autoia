"""Desempenho de polling: payload "lean" nas listas e respostas condicionais (ETag/304)."""

from __future__ import annotations


def test_list_tasks_lean(flow):
    """A listagem é leve: sem resumo LLM, children, propostas/subtasks; fases só com preview."""
    client = flow["client"]
    tasks = client.get("/api/tasks").json()
    assert len(tasks) == 1
    t = tasks[0]
    assert "summary" not in t
    assert "children" not in t
    assert "proposals" not in t
    assert "subtasks" not in t
    step = t["steps"][0]
    assert "summary_preview" in step
    assert "summary" not in step
    assert step["robot"] is not None  # nome/role ainda presentes (stepper/cards)


def test_get_task_still_full(flow):
    """O detalhe continua completo (TaskDetail depende de summary/children/proposals)."""
    client = flow["client"]
    task = client.get(f"/api/tasks/{flow['task']['id']}").json()
    assert "summary" in task
    assert "children" in task
    assert "proposals" in task
    assert task["steps"][0]["summary"] is None  # nunca executada


def test_tasks_etag_304_e_depois_200(flow):
    """ETag de /api/tasks: 304 sem mudanças, 200 quando algo muda."""
    client = flow["client"]
    r1 = client.get("/api/tasks")
    etag = r1.headers.get("etag")
    assert etag
    assert r1.status_code == 200

    r2 = client.get("/api/tasks", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""  # sem corpo no 304

    # muda algo → token muda → 200 com corpo novo
    client.post(f"/api/tasks/{flow['task']['id']}/pause")
    r3 = client.get("/api/tasks", headers={"If-None-Match": etag})
    assert r3.status_code == 200
    assert r3.json()


def test_execution_e_dashboard_etag(flow):
    """Execução e Dashboard também respondem 304 para o mesmo If-None-Match."""
    client = flow["client"]
    for path in ("/api/execution", "/api/dashboard"):
        r1 = client.get(path)
        etag = r1.headers.get("etag")
        assert etag, path
        r2 = client.get(path, headers={"If-None-Match": etag})
        assert r2.status_code == 304, path
