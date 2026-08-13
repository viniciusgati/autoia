"""Testes da timeline cronológica da execução (eventos determinísticos)."""

from __future__ import annotations

from app.worker import runner

STREAM = [
    {"role": "assistant", "content": "analisando a estrutura do projeto"},
    {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "Read", "arguments": "{\"path\": \"src/services/OrderService.ts\"}"}}
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "conteudo do arquivo OrderService.ts"},
    {"role": "assistant", "content": "implementação concluída"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_timeline_derives_events(flow, fake_kimi):
    """Tool calls viram eventos com name/input/output/status/duração; o início do
    desenvolvimento abre a timeline."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    resp = flow["client"].get(f"/api/tasks/{task_id}/timeline")
    assert resp.status_code == 200
    events = resp.json()
    assert events, "timeline não deveria estar vazia"
    assert events[0]["type"] == "development_started"
    assert events[0]["summary"] == "Desenvolvimento iniciado"

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) >= 1
    tc = tool_calls[0]
    assert tc["name"] == "Read"
    assert tc["input"] == {"path": "src/services/OrderService.ts"}
    assert tc["status"] == "completed"
    assert tc["output"] and "OrderService.ts" in tc["output"]["content"]
    assert tc["duration_ms"] is not None
    assert tc["step_id"] is not None

    # A fase que rodou tem evento de conclusão (fase 0 do po).
    assert any(e["type"] == "phase_done" for e in events)


def test_timeline_complete_with_guardrail(flow, fake_kimi):
    """Timeline não quebra com eventos de guardrail/erro e os expõe como `blocked`."""
    from sqlalchemy import func

    from app.models import RunEvent, Task

    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 (po) executa

    # Guardrail removido da execução: o evento é simulado (determinístico) para
    # validar a derivação da timeline com eventos `blocked`.
    with flow["session_factory"]() as s:
        step = sorted(s.get(Task, task_id).steps, key=lambda x: x.position)[0]
        max_seq = (
            s.query(func.max(RunEvent.seq)).filter(RunEvent.step_id == step.id).scalar() or 0
        )
        s.add(RunEvent(
            step_id=step.id,
            seq=max_seq + 1,
            kind="guardrail_blocked",
            payload={"pattern": "sudo", "detail": "sudo rm -rf /etc"},
        ))
        s.commit()

    resp = flow["client"].get(f"/api/tasks/{task_id}/timeline")
    assert resp.status_code == 200
    blocked = [e for e in resp.json() if e["type"] == "blocked"]
    assert any("guardrail" in e["summary"].lower() for e in blocked)


def test_timeline_evento_sandbox_tem_resumo(flow, fake_kimi):
    """O evento de observabilidade `sandbox` (modo, contêiner, overhead) aparece na
    timeline com resumo humano."""
    from sqlalchemy import func

    from app.models import RunEvent, Task

    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        step = sorted(s.get(Task, task_id).steps, key=lambda x: x.position)[0]
        max_seq = (
            s.query(func.max(RunEvent.seq)).filter(RunEvent.step_id == step.id).scalar() or 0
        )
        s.add(RunEvent(
            step_id=step.id,
            seq=max_seq + 1,
            kind="sandbox",
            payload={"mode": "fs", "container_id": "abc123def456", "wall_ms": 820},
        ))
        s.add(RunEvent(
            step_id=step.id,
            seq=max_seq + 2,
            kind="secrets_scan",
            payload={"mounts": ["/home/x/.ssh (expoe segredo: /home/x/.ssh)"], "mode": "fs"},
        ))
        s.commit()

    resp = flow["client"].get(f"/api/tasks/{task_id}/timeline")
    assert resp.status_code == 200
    sandbox = [e for e in resp.json() if e["name"] == "sandbox"]
    assert sandbox
    assert any("fs" in e["summary"] for e in sandbox)
    assert any("abc123def" in e["summary"] for e in sandbox)
    assert any("820 ms" in e["summary"] for e in sandbox)
    scans = [e for e in resp.json() if e["name"] == "varredura de segredos"]
    assert scans
    assert any(".ssh" in e["summary"] for e in scans)


def test_timeline_milestones_resumo(flow, fake_kimi):
    """A visão compacta (Nível 1) é um subconjunto de marcos — a derivada no backend
    mantém tool_calls como eventos próprios, mas o frontend filtra."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))

    events = flow["client"].get(f"/api/tasks/{task_id}/timeline").json()
    types = {e["type"] for e in events}
    assert "tool_call" in types
    assert "development_started" in types
    assert "phase_done" in types


def test_timeline_reexecution_at_end(flow, fake_kimi):
    """Voltar para uma fase re-executa a partir dela e a nova execução aparece no
    FIM da timeline (histórico anterior preservado, re-execução identificável)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(STREAM, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 roda (tentativa 1)
    resp = flow["client"].post(f"/api/tasks/{task_id}/steps/0/retry")
    assert resp.status_code == 200, resp.text
    _execute(flow, _run_claim(flow))  # re-execução da fase 0 (tentativa 2)

    events = flow["client"].get(f"/api/tasks/{task_id}/timeline").json()
    reruns = [e for e in events if "re-execução da fase 0" in e["summary"]]
    assert len(reruns) == 1

    first_done = next(e for e in events if e["type"] == "phase_done")
    assert events.index(reruns[0]) > events.index(first_done)
    assert len(events) >= 6  # tentativa 1 + re-execução com tool_calls
