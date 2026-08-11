"""Testes do bloqueio aguardando instrução e da retomada por instrução."""

from __future__ import annotations

import json
import os

from app.models import RunEvent, Task
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "content": "não consigo continuar com segurança"},
]

BLOCKED_JSON = json.dumps({
    "status": "blocked",
    "reason_type": "decision_required",
    "reason": "Existem duas abordagens possíveis para implementar a integração.",
    "question": "Deve ser utilizada a API existente ou criada uma nova camada de integração?",
})


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_agent_block_declares_blocked(flow, fake_kimi):
    """Agente escreve autoia_blocked.json → worker marca fase e task como `blocked`
    com motivo estruturado (não é falha nem bounce-back)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_blocked.json", write_content=BLOCKED_JSON)
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        assert t.status == "blocked"
        assert t.block_reason_type == "decision_required"
        assert "abordagens" in t.block_reason
        assert "API existente" in t.block_question
        step = sorted(t.steps, key=lambda x: x.position)[0]
        assert step.status == "blocked"
        assert step.error == t.block_reason
        ev = (
            s.query(RunEvent)
            .filter(RunEvent.step_id == step.id, RunEvent.kind == "task_blocked")
            .first()
        )
        assert ev is not None
        assert ev.payload["reason_type"] == "decision_required"

    got = flow["client"].get(f"/api/tasks/{task_id}").json()
    assert got["status"] == "blocked"
    assert got["block_reason_type"] == "decision_required"


def test_continue_blocked_resumes_in_place(flow, fake_kimi):
    """POST /blocked/continue: reabre a MESMA fase (attempt+1), grava a instrução
    separadamente e registra a intervenção na timeline (RunEvent)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_blocked.json", write_content=BLOCKED_JSON)
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        blocked_step = sorted(t.steps, key=lambda x: x.position)[0]
        blocked_step_id = blocked_step.id
        attempt_before = blocked_step.attempt

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/blocked/continue",
        json={"instruction": "Utilize a abordagem B e mantenha a interface atual."},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["resume_instruction"] == "Utilize a abordagem B e mantenha a interface atual."
    assert data["error"] is None

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        step = next(st for st in t.steps if st.id == blocked_step_id)
        assert step.status == "pending"
        assert step.attempt == attempt_before + 1
        evs = (
            s.query(RunEvent)
            .filter(RunEvent.step_id == blocked_step_id)
            .filter(RunEvent.kind.in_(["user_intervention", "execution_resumed"]))
            .all()
        )
        assert len(evs) == 2
        user_ev = next(e for e in evs if e.kind == "user_intervention")
        assert "abordagem B" in user_ev.payload["instruction"]


def test_continue_requires_blocked_status(flow):
    """Só permite retomar uma task realmente bloqueada."""
    resp = flow["client"].post(
        f"/api/tasks/{flow['task']['id']}/blocked/continue",
        json={"instruction": "siga"},
    )
    assert resp.status_code == 400


def test_resume_instruction_enters_handoff(flow, fake_kimi):
    """Na retomada, a instrução do usuário entra no handoff da fase re-executada."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_blocked.json", write_content=BLOCKED_JSON)
    _execute(flow, _run_claim(flow))

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/blocked/continue",
        json={"instruction": "Utilize a abordagem B. Não altere a estrutura atual."},
    )
    assert resp.status_code == 200

    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    _execute(flow, _run_claim(flow))  # a fase bloqueada re-executa com a instrução

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        checkout = os.path.join(settings.workspace_dir, str(t.repository.id), f"task_{t.id}")
    md = open(os.path.join(checkout, "autoia_handoff.md"), encoding="utf-8").read()
    assert "Intervenção do usuário" in md
    assert "abordagem B" in md


def test_retry_unlocked_when_blocked(flow, settings):
    """Em task bloqueada (ex.: conflito de merge), o retry de fase falha com tentativas
    máximas fica LIBERADO: o usuário instrui (feedback) e re-executa para resolver."""
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        t.status = "blocked"
        t.error = "conflito de merge"
        merger = next(st for st in t.steps if st.position == 5)
        merger.status = "failed"
        merger.attempt = settings.max_attempts
        merger.error = "Auto-merging ... CONFLICT (content) ..."
        s.commit()

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/steps/5/retry",
        json={"note": "resolva o conflito dando prioridade ao que já está testado"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["feedback"] == "resolva o conflito dando prioridade ao que já está testado"
    merger = next(st for st in data["steps"] if st["position"] == 5)
    assert merger["status"] == "pending"
    assert merger["attempt"] == settings.max_attempts + 1


def test_retry_manual_ignora_limite_em_qualquer_estado(flow, settings):
    """O retry é ação MANUAL: não fica preso ao `max_attempts` (limite do bounce-back
    automático), mesmo com a task fora do estado 'blocked'."""
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        t.status = "needs_review"  # ex.: falha pós-merge, não está 'blocked'
        t.error = "deploy falhou"
        merger = next(st for st in t.steps if st.position == 5)
        merger.status = "failed"
        merger.attempt = settings.max_attempts  # já no teto automático
        merger.error = "erro no deploy"
        s.commit()

    resp = flow["client"].post(f"/api/tasks/{task_id}/steps/5/retry")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    merger = next(st for st in data["steps"] if st["position"] == 5)
    assert merger["status"] == "pending"
    assert merger["attempt"] == settings.max_attempts + 1
