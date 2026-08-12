"""Testes da missão de execução de fase ("por que esta execução existe").

A missão é o texto humano principal do card de etapa: vem de LLM dedicada
(`StepMission`, chaveada por (step, run)) e, enquanto não está pronta (ou se
falhar), de um fallback determinístico derivado dos eventos da execução.
"""

from __future__ import annotations

import json

from app.models import StepMission
from app.worker import runner
from app.worker.step_mission import generate_mission

STREAM = [
    {"role": "assistant", "content": "analisando a estrutura do projeto"},
    {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "Read", "arguments": "{\"path\": \"src/OrderService.ts\"}"}}
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "conteudo"},
    {"role": "assistant", "content": "implementação concluída com sucesso"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_mission_fallback_first_execution(flow, fake_kimi):
    """Primeira execução sem missão LLM: fallback determinístico por papel do robô."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 (po)

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"][0]
    assert occ["position"] == 0
    assert occ["mission_source"] == "fallback"
    assert occ["mission"]
    assert "história" in occ["mission"]  # missão do papel refine (PO)
    assert "robô" not in occ["mission"].lower()


def test_mission_persisted_and_exposed(flow, fake_kimi):
    """Missão LLM: autoia_step_mission.json → StepMission (step, run) → ocorrência."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    step_id = _run_claim(flow)
    _execute(flow, step_id)

    settings.kimi_bin = fake_kimi([], write_file="autoia_step_mission.json", write_content=json.dumps({
        "mission": "Implementar a validação de estoque antes da confirmação do pedido, "
                   "conforme os requisitos da tarefa.",
    }))
    assert generate_mission(settings, flow["session_factory"], step_id, 1)

    with flow["session_factory"]() as s:
        m = s.query(StepMission).filter_by(step_id=step_id).first()
        assert m is not None
        assert m.run == 1
        assert m.source == "llm"
        assert "validação de estoque" in m.mission

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"][0]
    assert occ["mission_source"] == "llm"
    assert "validação de estoque" in occ["mission"]


def test_mission_fallback_after_previous_failure(flow, fake_kimi):
    """Re-execução: fallback deriva a missão da parada da tentativa ANTERIOR da mesma
    fase (reprovação com motivo) — não repete o objetivo original."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    _execute(flow, _run_claim(flow))  # fase 0 (po) ok

    settings.kimi_bin = fake_kimi(STREAM, verdict="needs_work")
    _execute(flow, _run_claim(flow))  # fase 1 (qa) reprova → failed; bounce-back reabre po

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    _execute(flow, _run_claim(flow))  # po re-executada (run 2)
    _execute(flow, _run_claim(flow))  # qa re-executada (run 2) → done

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    qa = [o for o in data["occurrences"] if o["position"] == 1]
    assert [o["run"] for o in qa] == [1, 2]
    assert qa[0]["status"] == "failed"
    rerun = qa[1]
    assert rerun["status"] == "done"
    assert rerun["mission_source"] == "fallback"
    assert "tentativa anterior" in rerun["mission"]
    assert "ambigua" in rerun["mission"]  # motivo da reprovação (autoia_verdict.txt)


def test_mission_fallback_uses_user_instruction(flow, fake_kimi):
    """Re-execução por instrução do usuário: fallback cita a mensagem que motivou."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": "não consigo continuar"}],
        write_file="autoia_blocked.json",
        write_content=json.dumps({"reason_type": "ambiguity", "reason": "duas abordagens", "question": "qual?"}),
    )
    _execute(flow, _run_claim(flow))  # fase 0 bloqueia

    flow["client"].post(
        f"/api/tasks/{task_id}/instruction",
        json={"instruction": "Use a abordagem B."},
    )
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    _execute(flow, _run_claim(flow))  # fase 0 retomada

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    rerun = [o for o in data["occurrences"] if o["position"] == 0][-1]
    assert rerun["mission_source"] == "fallback"
    assert "abordagem B" in rerun["mission"]


def test_mission_run_unique_across_reexecution(flow, fake_kimi):
    """A missão é chaveada por (step, run): re-execução tem missão própria, não
    reaproveita a da execução anterior."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    step_id = _run_claim(flow)
    _execute(flow, step_id)  # run 1

    flow["client"].post(f"/api/tasks/{task_id}/steps/0/retry")
    _execute(flow, _run_claim(flow))  # run 2

    settings.kimi_bin = fake_kimi([], write_file="autoia_step_mission.json", write_content=json.dumps({
        "mission": "Missão da segunda execução.",
    }))
    assert generate_mission(settings, flow["session_factory"], step_id, 2)

    with flow["session_factory"]() as s:
        rows = s.query(StepMission).filter_by(step_id=step_id).all()
        assert {r.run for r in rows} == {2}  # só a missão da execução 2 (a 1 não foi gerada)

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occs = data["occurrences"]
    assert occs[0]["run"] == 1 and occs[0]["mission_source"] == "fallback"
    assert occs[1]["run"] == 2 and occs[1]["mission_source"] == "llm"
    assert "segunda execução" in occs[1]["mission"]
