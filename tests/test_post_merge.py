"""Testes das fases pós-merge (teste final no estado integrado)."""

from __future__ import annotations

from app.models import Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]

PIPELINE_DEPLOY = "po-qa-dev-tester-avaliador-deploytest"  # 7 fases, última post_merge


def _make_task_with_deploy_pipeline(flow) -> dict:
    # limpa tarefas criadas pela fixture flow (para o claim pegar só a nossa)
    with flow["session_factory"]() as s:
        for task in s.query(Task).all():
            s.delete(task)
        s.commit()

    pipelines = flow["client"].get("/api/pipelines").json()
    pipeline = next(p for p in pipelines if p["name"] == PIPELINE_DEPLOY)
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
            "error": t.error,
            "steps": [
                {
                    "position": st.position,
                    "robot": st.robot.name if st.robot else "?",
                    "status": st.status,
                    "attempt": st.attempt,
                    "post_merge": st.post_merge,
                    "error": st.error,
                }
                for st in sorted(t.steps, key=lambda x: x.position)
            ],
        }


def test_happy_path_merges_then_runs_post_merge_on_main(flow, fake_kimi):
    """Pré-merge passa -> merge acontece -> deploy-tester roda NA MAIN e conclui."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0
    task = _make_task_with_deploy_pipeline(flow)

    # 7 fases: po, qa, developer, tester, avaliador, merger (pré) + deploy-tester (pós)
    for _ in range(9):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    state = _state(flow, task["id"])
    assert state["status"] == "done"
    assert all(st["status"] == "done" for st in state["steps"])
    assert state["steps"][-1]["post_merge"] is True

    # a fase pós-merge rodou na main: o arquivo da tarefa existe no checkout (main)
    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = t.repository.local_path
        branch_now = runner_git_branch(checkout)
        assert branch_now == "main"
        # o merge chegou: o commit da task está na main local
        log = runner_git_log(checkout)
        assert "autoia: merge autoia/" in log


def test_post_merge_failure_goes_to_review_no_bounce(flow, fake_kimi):
    """deploy-tester FAIL -> SEM bounce-back; task needs_review + evento; PM decide."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0  # sem PM automático neste teste
    task = _make_task_with_deploy_pipeline(flow)

    for _ in range(6):  # po..merger (6 pré-merge) passam; deploy-tester (7ª) roda em seguida
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    # troca o fake para o deploy-tester FALHAR
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    runner.execute_step(settings, flow["session_factory"], runner.claim_next(flow["session_factory"]))

    state = _state(flow, task["id"])
    deploy = state["steps"][-1]
    assert deploy["status"] == "failed"
    assert state["status"] == "needs_review"  # sem bounce, sem failed
    assert "FAIL" in (deploy["error"] or "")
    assert "código já integrado" in (state["error"] or "")

    # evento post_merge_failed gravado
    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        events = [e.kind for e in t.steps[-1].events]
        assert "post_merge_failed" in events
        # nenhuma fase anterior foi reaberta
        assert all(st.status == "done" for st in t.steps[:-1])


def test_pm_can_retry_post_merge_phase(flow, fake_kimi):
    """Falha pós-merge -> PM retry re-roda a fase pós-merge na main."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 2
    task = _make_task_with_deploy_pipeline(flow)

    for _ in range(6):  # po..merger (pré-merge) passam; deploy-tester fica pendente
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    # deploy-tester falha
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="fail")
    runner.execute_step(settings, flow["session_factory"], runner.claim_next(flow["session_factory"]))

    state = _state(flow, task["id"])
    assert state["status"] == "needs_review"

    # PM decide retry da fase pós-merge (posição 6)
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="pm_retry_post")
    runner._pm_decide(flow["session_factory"], settings, task["id"], "teste")

    state = _state(flow, task["id"])
    assert state["status"] == "in_progress"
    deploy = state["steps"][-1]
    assert deploy["status"] == "pending"
    assert deploy["attempt"] == 2


def test_no_commit_in_post_merge_phase(flow, fake_kimi):
    """Fase pós-merge não commita nada na main local."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="feature.txt")
    settings.task_budget = 100.0
    settings.max_pm_decisions = 0
    task = _make_task_with_deploy_pipeline(flow)

    for _ in range(8):
        step_id = runner.claim_next(flow["session_factory"])
        if step_id is None:
            break
        runner.execute_step(settings, flow["session_factory"], step_id)

    with flow["session_factory"]() as s:
        t = s.get(Task, task["id"])
        checkout = t.repository.local_path
    log = runner_git_log(checkout)
    # não há commit adicional da fase pós-merge além do merge
    assert log.count("autoia: merge autoia/") == 1


def runner_git_branch(checkout: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", checkout, "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def runner_git_log(checkout: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", checkout, "log", "--oneline", "-10"],
        capture_output=True, text=True, check=True,
    ).stdout
