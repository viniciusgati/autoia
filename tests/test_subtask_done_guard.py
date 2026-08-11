"""Guarda anti-laço: `autoia_subtasks_done.json` não pode "carimbar" como pronta uma
subtarefa que JÁ reprovou na verificação, se o developer não alterar o código.

Reproduz o caso do itagfm (task-20): developer re-declara "já implementada" sem
nenhum commit novo, tester re-falha, e o laço queima tentativas. Com a guarda, a
re-declaração sem mudança de código falha a subtarefa na hora (needs_review).
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.worker import runner


@pytest.fixture
def sub_flow(settings, bare_repo):
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


def _kimi_guard(tmp_path) -> str:
    """Fake kimi: em fases sem veredicto escreve `autoia_subtasks_done.json` ([1])
    SEM tocar no código; em fases verify escreve FAIL."""
    script = tmp_path / f"kimi_guard_{len(list(tmp_path.glob('kimi_guard_*')))}"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        "sys.stdout.flush()\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
        "if 'VEREDICTO' in prompt.upper():\n"
        "    with open('autoia_verdict.txt', 'w') as f:\n"
        "        f.write('FAIL\\nSUMMARY: testes falharam')\n"
        "else:\n"
        "    with open('autoia_subtasks_done.json', 'w') as f:\n"
        "        f.write('[1]')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _kimi_guard_infra_fail(tmp_path) -> str:
    """Fake kimi: implement escreve `autoia_subtasks_done.json` ([1]); verify FALHA
    por INFRAESTRUTURA (exit 1, sem veredicto) — como um guardrail/timeout."""
    script = tmp_path / f"kimi_guard_infra_{len(list(tmp_path.glob('kimi_guard_infra_*')))}"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        "sys.stdout.flush()\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
        "if 'VEREDICTO' in prompt.upper():\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    with open('autoia_subtasks_done.json', 'w') as f:\n"
        "        f.write('[1]')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _claim_and_execute(flow) -> None:
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _task_state(flow, task_id) -> dict:
    from app.models import Task

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        return {
            "status": t.status,
            "error": t.error,
            "subtasks": [
                {
                    "position": st.position,
                    "title": st.title,
                    "status": st.status,
                    "attempt": st.attempt,
                    "error": st.error,
                }
                for st in sorted(t.subtasks, key=lambda x: x.position)
            ],
            "steps": [
                {"position": st.position, "status": st.status, "error": st.error}
                for st in sorted(t.steps, key=lambda x: x.position)
            ],
        }


def _start_task(flow, pipeline_id: int) -> int:
    client = flow["client"]
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai-com-subtarefa",
            "description": "d",
            "kind": "feature",
            "subtasks": [
                {"title": "sub 1", "description": "d1", "acceptance_criteria": "c1"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    client.post(f"/api/tasks/{task_id}/start")
    return task_id


def _subtask_pipeline(flow) -> int:
    """Pipeline developer → tester e retorna o id."""
    client = flow["client"]
    robots = client.get(f"/api/robots?repository_id={flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "guard-pipeline",
            "repository_id": flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["developer"]},
                {"position": 1, "robot_id": by_name["tester"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_rededaracao_sem_fix_vai_direto_para_needs_review(sub_flow, tmp_path):
    """Subtarefa já reprovou (verify FAIL); developer re-declara 'já implementada'
    sem alterar código → guard rejeita → task needs_review (sem queimar tester)."""
    settings = sub_flow["settings"]
    settings.kimi_bin = _kimi_guard(tmp_path)
    settings.task_budget = 100.0
    task_id = _start_task(sub_flow, _subtask_pipeline(sub_flow))

    # 1ª execução do developer: declaração aceita (nunca falhou antes) → implementada
    _claim_and_execute(sub_flow)
    # tester: FAIL → bounce-back (subtarefa volta a pending + evento de falha verify)
    _claim_and_execute(sub_flow)
    state = _task_state(sub_flow, task_id)
    assert state["subtasks"][0]["status"] == "pending"
    assert state["steps"][0]["status"] == "pending"

    # 2ª execução do developer: re-declara sem mudança de código → guard rejeita
    _claim_and_execute(sub_flow)

    state = _task_state(sub_flow, task_id)
    assert state["status"] == "needs_review"
    assert "subtask_done_rejected" in (state["error"] or "")
    assert state["subtasks"][0]["status"] == "failed"
    assert "sem alterar o código" in (state["subtasks"][0]["error"] or "")

    # auditoria: evento subtask_done_rejected registrado no step
    from app.models import Task

    with sub_flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        step = sorted(t.steps, key=lambda x: x.position)[0]
        kinds = {e.kind for e in step.events}
        assert "subtask_done_rejected" in kinds


def test_rededaracao_apos_falha_de_infra_nao_e_rejeitada(sub_flow, tmp_path):
    """Falha verify por INFRAESTRUTURA (exit 1, sem veredicto — ex.: guardrail/timeout)
    não conta como 'defeito anterior': após release manual, o developer pode
    re-declarar a subtarefa sem alterar código sem ser rejeitado (caso da task 23)."""
    settings = sub_flow["settings"]
    settings.kimi_bin = _kimi_guard_infra_fail(tmp_path)
    settings.task_budget = 100.0
    task_id = _start_task(sub_flow, _subtask_pipeline(sub_flow))

    # developer: declaração aceita → implementada
    _claim_and_execute(sub_flow)
    # tester: falha por infra (exit 1, sem veredicto) → task needs_review
    _claim_and_execute(sub_flow)
    state = _task_state(sub_flow, task_id)
    assert state["status"] == "needs_review"

    # humano libera via retry da subtarefa (reabre o implement)
    resp = sub_flow["client"].post(f"/api/tasks/{task_id}/subtasks/0/retry")
    assert resp.status_code == 200, resp.text

    # developer re-declara sem mudança de código → guard NÃO rejeita (infra ≠ defeito)
    _claim_and_execute(sub_flow)
    state = _task_state(sub_flow, task_id)
    assert state["subtasks"][0]["status"] == "implemented"
    assert "subtask_done_rejected" not in (state["error"] or "")
