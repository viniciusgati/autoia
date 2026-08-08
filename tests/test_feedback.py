"""Testes do feedback externo na task (nota do humano + retry com nota)."""

from __future__ import annotations

import os

from app.models import Task
from app.prompts import build_prompt
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_feedback_roundtrip(flow):
    task = flow["task"]
    client = flow["client"]

    resp = client.post(
        f"/api/tasks/{task['id']}/feedback", json={"text": "Railway ENOTFOUND host"}
    )
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "Railway ENOTFOUND host"

    got = client.get(f"/api/tasks/{task['id']}").json()
    assert got["feedback"] == "Railway ENOTFOUND host"

    resp = client.delete(f"/api/tasks/{task['id']}/feedback")
    assert resp.status_code == 200
    assert resp.json()["feedback"] is None


def test_feedback_reaches_handoff_and_prompt(flow, fake_kimi):
    """A nota externa entra no handoff (arquivo) e no prompt das próximas fases."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    with flow["session_factory"]() as s:
        checkout = s.get(Task, task["id"]).repository.local_path

    _execute(flow, _run_claim(flow))  # fase 0 (po) conclui
    resp = flow["client"].post(
        f"/api/tasks/{task['id']}/feedback", json={"text": "Railway: ENOTFOUND host"}
    )
    assert resp.status_code == 200
    _execute(flow, _run_claim(flow))  # fase 1 (qa) roda com o feedback no handoff

    md = open(os.path.join(checkout, "autoia_handoff.md"), encoding="utf-8").read()
    assert "Feedback externo" in md
    assert "ENOTFOUND host" in md

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        prompt = build_prompt(t.steps[1].robot, t, "", "main")
    assert "ENOTFOUND host" in prompt


def test_retry_done_step_with_note(flow, fake_kimi):
    """Retry de fase done (voltar pro developer) com nota grava feedback e reabre."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task = flow["task"]

    for _ in range(3):  # po, qa, developer
        _execute(flow, _run_claim(flow))

    resp = flow["client"].post(
        f"/api/tasks/{task['id']}/steps/2/retry",
        json={"note": "Railway: ENOTFOUND host — corrija o fallback"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feedback"] == "Railway: ENOTFOUND host — corrija o fallback"
    dev = next(st for st in data["steps"] if st["position"] == 2)
    assert dev["status"] == "pending"
    assert dev["attempt"] == 2
