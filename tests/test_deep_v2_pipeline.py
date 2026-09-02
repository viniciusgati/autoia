"""Fluxo das pipelines deep-v2: QA rigoroso/lean + validador substitui o tester."""

from __future__ import annotations

from app.models import Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]

PIPELINE_BACKEND = "deep-v2-backend"


def _make_task_with_pipeline(flow, pipeline_name: str) -> dict:
    with flow["session_factory"]() as s:
        for task in s.query(Task).all():
            s.delete(task)
        s.commit()

    pipelines = flow["client"].get("/api/pipelines").json()
    pipeline = next(p for p in pipelines if p["name"] == pipeline_name)
    response = flow["client"].post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": pipeline["id"],
            "title": "t",
            "description": "d",
            "kind": "feature",
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    flow["client"].post(f"/api/tasks/{task['id']}/start")
    return task


def _state(flow, task_id: int) -> dict:
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        return {
            "status": t.status,
            "steps": [
                {
                    "robot": st.robot.name if st.robot else "?",
                    "role": st.robot.role if st.robot else "?",
                    "status": st.status,
                    "post_merge": st.post_merge,
                }
                for st in sorted(t.steps, key=lambda x: x.position)
            ],
        }


def test_deep_v2_backend_uses_validador_not_tester(flow, fake_kimi):
    """deep-v2-backend roda po → qa → developer → validador → avaliador → merger
    + deploy-tester pós-merge; o validador (role verify) substitui o tester."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0
    task = _make_task_with_pipeline(flow, PIPELINE_BACKEND)

    for _ in range(9):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    state = _state(flow, task["id"])
    assert state["status"] == "done"
    assert all(st["status"] == "done" for st in state["steps"])
    robots = [st["robot"] for st in state["steps"]]
    assert robots == [
        "po", "qa", "developer", "validador", "avaliador", "merger", "deploy-tester",
    ]
    assert "tester" not in robots
    validador = next(st for st in state["steps"] if st["robot"] == "validador")
    assert validador["role"] == "verify"
    assert validador["post_merge"] is False
    assert state["steps"][-1]["post_merge"] is True


def test_deep_v2_lean_pipeline_runs_to_done(flow, fake_kimi):
    """deep-v2-fullstack-lean usa qa-lean e conclui com browser-tester pós-merge."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0
    task = _make_task_with_pipeline(flow, "deep-v2-fullstack-lean")

    for _ in range(10):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    state = _state(flow, task["id"])
    assert state["status"] == "done"
    robots = [st["robot"] for st in state["steps"]]
    assert robots == [
        "po", "qa-lean", "developer", "validador", "avaliador", "merger",
        "deploy-tester", "browser-tester",
    ]
    post = [st["post_merge"] for st in state["steps"]]
    assert post == [False, False, False, False, False, False, True, True]
