"""Pipeline Android SEM emulador (`android-noemu`).

Objetivo: um fluxo específico para projetos Android que NÃO podem depender de
emulador/dispositivo (boot + qemu causam timeout/infra_blocked). Todos os robôs do
pipeline precisam carregar a diretriz e o worker precisa executar o fluxo completo.
"""

from __future__ import annotations

from app.models import Robot, Task
from app.prompts import CONTRACT_VERIFY, build_prompt
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]

PIPELINE = "android-noemu"
ROBOTS = [
    "android-noemu-po",
    "android-noemu-qa",
    "android-noemu-dev",
    "android-noemu-validador",
    "android-noemu-avaliador",
    "android-noemu-merger",
    "android-noemu-deploy-tester",
]


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


def test_android_noemu_pipeline_runs_to_done(flow, fake_kimi):
    """Fluxo completo termina em done com as fases do pipeline, sem tester/browser."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0
    task = _make_task_with_pipeline(flow, PIPELINE)

    for _ in range(10):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    state = _state(flow, task["id"])
    assert state["status"] == "done"
    assert all(st["status"] == "done" for st in state["steps"])
    assert [st["robot"] for st in state["steps"]] == ROBOTS
    assert [st["post_merge"] for st in state["steps"]] == [
        False, False, False, False, False, False, True,
    ]
    assert "tester" not in [st["robot"] for st in state["steps"]]
    assert "browser-tester" not in [st["robot"] for st in state["steps"]]


def test_android_noemu_missions_forbid_emulator(flow):
    """Todos os robôs do pipeline proíbem emulador/connected tests explicitamente."""
    with flow["session_factory"]() as s:
        robots = {
            r.name: r
            for r in s.query(Robot).filter(Robot.name.in_(ROBOTS)).all()
        }
    assert set(robots) == set(ROBOTS)
    for robot in robots.values():
        mission = robot.mission
        assert "SEM EMULADOR/DISPOSITIVO" in mission
        assert "connectedAndroidTest" in mission
        assert "NUNCA inicie emulador" in mission
        assert "src/test" in mission
        assert "NÃO VERIFICADO:" in mission

    task = Task(title="t", description="d")
    dev_prompt = build_prompt(robots["android-noemu-dev"], task, "", "main")
    assert "NUNCA execute teste instrumentado/conectado" in dev_prompt
    assert "./gradlew test" in dev_prompt
    assert "assembleDebug" in dev_prompt

    # O contrato genérico de verify precisa ceder à diretriz da pipeline.
    assert "FORA DE ESCOPO" in CONTRACT_VERIFY
    assert "PULE esta seção inteira" in CONTRACT_VERIFY
    validador_prompt = build_prompt(robots["android-noemu-validador"], task, "", "main")
    assert "FORA DE ESCOPO" in validador_prompt
    assert "PREVALECE" in validador_prompt
