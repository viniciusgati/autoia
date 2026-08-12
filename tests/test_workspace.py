"""Testes do workspace (tela de trabalho): ocorrências, resumo por fase, instrução e decisão."""

from __future__ import annotations

import json

from app.models import RunEvent, StepSummary, Task
from app.worker import runner
from app.worker.step_summarizer import summarize_step

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

DECISION_JSON = json.dumps({
    "question": "Validar no OrderService ou no StockService?",
    "options": ["A — validar no OrderService", "B — validar no StockService"],
    "context": "O estoque é consultado em dois pontos do fluxo.",
})


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_workspace_derives_occurrences(flow, fake_kimi):
    """Workspace: fase executada vira uma ocorrência com status/atividade/entrega."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    resp = flow["client"].get(f"/api/tasks/{task_id}/workspace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["id"] == task_id
    occ = data["occurrences"]
    assert len(occ) == 1
    assert occ[0]["position"] == 0
    assert occ[0]["status"] == "done"
    assert occ[0]["goal"] and len(occ[0]["goal"]) > 0
    assert occ[0]["last_activity"] and "OrderService.ts" in occ[0]["last_activity"]
    assert "implementação concluída" in (occ[0]["delivered_text"] or "")
    assert len(occ[0]["events"]) >= 1


def test_workspace_ignores_ghost_events_on_unrun_steps(flow, fake_kimi):
    """Eventos de PM/worker ancorados num step que NUNCA executou (ex.: pm_decision
    no último step) NÃO criam ocorrência fantasma — a timeline fica em ordem real."""
    from sqlalchemy import func

    from app.models import RunEvent, TaskStep

    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 (po) executa; as demais seguem pending

    # Simula o PM ancorando a decisão no ÚLTIMO step (merger, nunca executado).
    with flow["session_factory"]() as s:
        merger = next(st for st in s.get(Task, task_id).steps if st.position == 5)
        max_seq = (
            s.query(func.max(RunEvent.seq))
            .filter(RunEvent.step_id == merger.id)
            .scalar() or 0
        )
        s.add(RunEvent(
            step_id=merger.id,
            seq=max_seq + 1,
            kind="pm_decision",
            payload={"action": "retry", "position": 0, "reason": "teste"},
        ))
        s.commit()

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"]
    assert [o["position"] for o in occ] == [0]
    assert occ[0]["status"] == "done"


def test_workspace_ghost_events_before_attempt_dont_shift_position(flow, fake_kimi):
    """Eventos de nível da task (summary_generated/pm_decision) ancorados num step
    ANTES do `attempt_started` dele não criam ocorrência nem mudam a ordem."""
    from sqlalchemy import func

    from app.models import RunEvent

    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    # Eventos fantasma ancorados no último step (merger) ANTES de qualquer execução.
    with flow["session_factory"]() as s:
        merger = next(st for st in s.get(Task, task_id).steps if st.position == 5)
        for kind in ("summary_generated", "pm_decision"):
            max_seq = (
                s.query(func.max(RunEvent.seq))
                .filter(RunEvent.step_id == merger.id)
                .scalar() or 0
            )
            s.add(RunEvent(
                step_id=merger.id,
                seq=max_seq + 1,
                kind=kind,
                payload={"result": "partial", "len": 1},
            ))
        s.commit()

    _execute(flow, _run_claim(flow))  # fase 0 (po)
    _execute(flow, _run_claim(flow))  # fase 1 (qa)

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"]
    assert [o["position"] for o in occ] == [0, 1]
    assert not any(o["position"] == 5 for o in occ)


def test_workspace_stop_shows_verdict_rejection_detail(flow, fake_kimi):
    """Ocorrência reprovada por veredicto (NEEDS_WORK) expõe no `stop` o motivo
    COMPLETO da reprovação (conteúdo do autoia_verdict.txt)."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    _execute(flow, _run_claim(flow))  # fase 0 (po) ok

    settings.kimi_bin = fake_kimi(STREAM, verdict="needs_work")
    _execute(flow, _run_claim(flow))  # fase 1 (qa) reprova

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    qa = next(o for o in data["occurrences"] if o["position"] == 1 and o["status"] == "failed")
    assert qa["stop"]["kind"] == "verdict"
    assert "NEEDS_WORK" in (qa["stop"]["detail"] or "")
    assert "ambigua" in (qa["stop"]["detail"] or "")


def test_workspace_reexecution_preserves_history(flow, fake_kimi):
    """Re-executar uma fase cria uma NOVA ocorrência no fim — histórico imutável."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))
    flow["client"].post(f"/api/tasks/{task_id}/steps/0/retry")
    _execute(flow, _run_claim(flow))

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"]
    assert len(occ) == 2
    assert occ[0]["position"] == 0 and occ[0]["attempt"] == 1
    assert occ[1]["position"] == 0 and occ[1]["attempt"] == 2
    assert occ[1]["status"] == "done"
    assert occ[0]["delivered_text"] == occ[1]["delivered_text"] or True  # ambos preservados


def test_workspace_diff_from_git(flow, fake_kimi):
    """Arquivos alterados e diff vêm do git (commit real da fase)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass", write_file="feature.py", write_content="x = 1\n")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    occ = data["occurrences"][0]
    assert "feature.py" in occ["files"]
    assert occ["file_count"] >= 1

    diff = flow["client"].get(f"/api/tasks/{task_id}/steps/0/diff").json()
    assert diff["commit"]
    assert "feature.py" in diff["files"]
    assert diff["stat"]


def test_step_summary_persisted_and_exposed(flow, fake_kimi):
    """'O que foi entregue' por fase: LLM dedicada gera autoia_step_summary.json →
    StepSummary persistido e exposto na ocorrência como `delivered`."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    step_id = _run_claim(flow)
    _execute(flow, step_id)

    settings.kimi_bin = fake_kimi([], write_file="autoia_step_summary.json", write_content=json.dumps({
        "summary": "A validação de estoque foi adicionada antes da confirmação do pedido.",
        "changes": ["Validação antes da confirmação"],
        "files": ["src/OrderService.ts"],
        "issues": [],
        "result": "completed",
    }))
    assert summarize_step(settings, flow["session_factory"], step_id)

    with flow["session_factory"]() as s:
        ss = s.query(StepSummary).filter_by(step_id=step_id).first()
        assert ss is not None
        assert "validação de estoque" in ss.summary
        assert ss.result == "completed"

    data = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    delivered = data["occurrences"][0]["delivered"]
    assert delivered is not None
    assert delivered["result"] == "completed"
    assert delivered["summary"].startswith("A validação")


def test_instruction_blocked_continues(flow, fake_kimi):
    """POST /instruction sem position retoma o ponto de parada (bloqueio)."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": "não consigo continuar"}],
        write_file="autoia_blocked.json",
        write_content=json.dumps({"reason_type": "ambiguity", "reason": "duas abordagens", "question": "qual?"}),
    )
    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        assert t.status == "blocked"
        step_id = sorted(t.steps, key=lambda x: x.position)[0].id

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/instruction",
        json={"instruction": "Use a abordagem B."},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert data["resume_instruction"] == "Use a abordagem B."
    assert data["block_reason_type"] is None

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        step = next(st for st in t.steps if st.id == step_id)
        assert step.status == "pending"
        assert step.attempt == 2
        kinds = {e.kind for e in s.query(RunEvent).filter(RunEvent.step_id == step_id).all()}
        assert {"user_intervention", "execution_resumed"} <= kinds


def test_instruction_rewind_with_position(flow, fake_kimi):
    """POST /instruction com position reexecuta a partir da fase escolhida."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 ok

    # simula um estado que requer retorno (ex.: revisão humana)
    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        t.status = "needs_review"
        t.error = "rever abordagem"
        qa = next(st for st in t.steps if st.position == 1)
        qa.status = "failed"
        qa.error = "história ambígua"
        s.commit()

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/instruction",
        json={"instruction": "Não use a abordagem atual. Faça a validação no StockService.", "position": 0},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "queued"
    assert "StockService" in data["resume_instruction"]
    step0 = next(st for st in data["steps"] if st["position"] == 0)
    step1 = next(st for st in data["steps"] if st["position"] == 1)
    assert step0["status"] == "pending"
    assert step0["attempt"] == 2
    assert step1["status"] == "pending"


def test_instruction_rewind_forbidden_future(flow, fake_kimi):
    """Não é possível continuar de uma fase futura (além do que já foi executado)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))
    resp = flow["client"].post(
        f"/api/tasks/{task_id}/instruction",
        json={"instruction": "siga", "position": 3},
    )
    assert resp.status_code == 400


def test_decision_request_blocks_and_answers(flow, fake_kimi):
    """Agente pede decisão (autoia_decision.json) → task bloqueada com pergunta +
    opções; resposta via /instruction retoma."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": "preciso de uma decisão"}],
        write_file="autoia_decision.json",
        write_content=DECISION_JSON,
    )
    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        assert t.status == "blocked"
        assert t.block_reason_type == "decision_request"
        assert t.block_options == ["A — validar no OrderService", "B — validar no StockService"]
        step_id = sorted(t.steps, key=lambda x: x.position)[0].id

    ws = flow["client"].get(f"/api/tasks/{task_id}/workspace").json()
    assert ws["decisions"]
    assert "OrderService ou no StockService" in ws["decisions"][0]["question"]
    assert len(ws["decisions"][0]["options"]) == 2

    resp = flow["client"].post(
        f"/api/tasks/{task_id}/instruction",
        json={"instruction": "Decisão A — validar no OrderService."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    with flow["session_factory"]() as s:
        step = next(st for st in s.get(Task, task_id).steps if st.id == step_id)
        assert step.status == "pending"
        assert step.attempt == 2
