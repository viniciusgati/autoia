"""Claim durante a janela de pico: só modelos isentos (opencode-go) continuam."""

from __future__ import annotations

from app.models import Task, TaskStep
from app.worker import runner


def test_claim_peak_exempt_only_ignores_model_with_peak(flow):
    """Com `peak_exempt_only`, a task DeepSeek fica parada e a opencode-go roda."""
    client = flow["client"]
    sf = flow["session_factory"]

    # A task do fixture (primeira na fila) usa um modelo com tarifa de pico.
    with sf() as s:
        first = s.get(Task, flow["task"]["id"])
        first.executor = "opencode"
        first.model = "deepseek/deepseek-chat"
        s.commit()

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": "task opencode-go",
            "description": "d",
            "kind": "feature",
            "executor": "opencode",
            "model": "opencode-go/mimo-v2.6-flash",
        },
    )
    assert resp.status_code == 201, resp.text
    exempt_id = resp.json()["id"]
    assert client.post(f"/api/tasks/{exempt_id}/start").status_code == 200

    claimed = runner.claim_next_with_token(
        sf,
        peak_exempt_only=True,
        peak_exempt_prefixes=("opencode-go",),
    )
    assert claimed is not None
    with sf() as s:
        step = s.get(TaskStep, claimed[0])
        assert step is not None
        assert step.task_id == exempt_id


def test_claim_without_peak_filter_claims_first_task(flow):
    """Fora do pico o claim normal não filtra por modelo."""
    claimed = runner.claim_next_with_token(flow["session_factory"])
    assert claimed is not None
    with flow["session_factory"]() as s:
        step = s.get(TaskStep, claimed[0])
        assert step is not None
        assert step.task_id == flow["task"]["id"]
