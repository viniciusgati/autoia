"""Edição da dependência de execução via PATCH /api/tasks/{id}.

A dependência é um metadado de ORDEM (não entra no handoff/prompt). O PATCH
distingue campo ausente (não altera) de `null` explícito (remove), rejeita
auto-dependência e ciclo.
"""

from __future__ import annotations


def _create_task(client, title: str) -> int:
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": title,
            "description": "d",
            "kind": "feature",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_patch_dependency_set_and_clear(flow):
    client = flow["client"]
    first = flow["task"]["id"]
    second = _create_task(client, "segunda")

    resp = client.patch(
        f"/api/tasks/{second}", json={"depends_on_task_id": first}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_task_id"] == first

    # Campo ausente não altera a dependência existente.
    resp = client.patch(f"/api/tasks/{second}", json={"details": "contexto"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_task_id"] == first

    # `null` explícito remove.
    resp = client.patch(
        f"/api/tasks/{second}", json={"depends_on_task_id": None}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_task_id"] is None


def test_patch_dependency_rejects_self_and_cycle(flow):
    client = flow["client"]
    first = flow["task"]["id"]
    second = _create_task(client, "segunda")

    resp = client.patch(
        f"/api/tasks/{first}", json={"depends_on_task_id": first}
    )
    assert resp.status_code == 400
    assert "si mesma" in resp.text

    # first → second; depois second → first fecharia um ciclo.
    resp = client.patch(
        f"/api/tasks/{first}", json={"depends_on_task_id": second}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["depends_on_task_id"] == second

    resp = client.patch(
        f"/api/tasks/{second}", json={"depends_on_task_id": first}
    )
    assert resp.status_code == 400
    assert "cíclica" in resp.text


def test_patch_dependency_missing_target(flow):
    resp = flow["client"].patch(
        f"/api/tasks/{flow['task']['id']}", json={"depends_on_task_id": 999999}
    )
    assert resp.status_code == 404
