"""Testes do resumo do desenvolvimento (LLM dedicada) e do campo details."""

from __future__ import annotations

import json
import time

from app.models import Repository, Task, TaskSummary
from app.worker import runner
from app.worker.summarizer import summarize_task

HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]

SUMMARY_JSON = json.dumps({
    "summary": "Implementada validação de estoque no processo de pedido, incluindo "
               "bloqueio da confirmação quando a quantidade excede o estoque.",
    "request": "Validar o estoque antes da confirmação do pedido.",
    "implementation": "Validação de quantidade adicionada antes da confirmação.",
    "changes": [
        "Validação adicionada antes da confirmação",
        "Tratamento de erro ajustado",
    ],
    "result": "completed",
    "issues": [],
    "files": ["src/services/OrderService.ts"],
    "tasks_summary": "4 tarefas concluídas · 1 pendente",
})


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_summary_empty_by_default(flow):
    """Sem resumo gerado ainda, GET /summary retorna null."""
    resp = flow["client"].get(f"/api/tasks/{flow['task']['id']}/summary")
    assert resp.status_code == 200
    assert resp.json() is None


def test_summarize_persists_and_get(flow, fake_kimi):
    """A LLM dedicada (via executor) gera autoia_summary.json → persistido no banco."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=SUMMARY_JSON)
    task_id = flow["task"]["id"]

    ok = summarize_task(settings, flow["session_factory"], task_id)
    assert ok

    with flow["session_factory"]() as s:
        summary = s.query(TaskSummary).filter_by(task_id=task_id).first()
        assert summary is not None
        assert summary.summary.startswith("Implementada")
        assert summary.result == "completed"
        assert any("OrderService.ts" in f for f in summary.files)
        assert summary.changes == ["Validação adicionada antes da confirmação", "Tratamento de erro ajustado"]

    resp = flow["client"].get(f"/api/tasks/{task_id}/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "completed"
    assert data["request"] == "Validar o estoque antes da confirmação do pedido."

    # O resumo também aparece embutido no TaskOut.
    got = flow["client"].get(f"/api/tasks/{task_id}").json()
    assert got["summary"]["result"] == "completed"


def test_summarize_overwrites_with_newest(flow, fake_kimi):
    """Regenerar cria uma versão nova; a API retorna sempre a mais recente."""
    settings = flow["settings"]
    task_id = flow["task"]["id"]

    v1 = json.loads(SUMMARY_JSON)
    v1["summary"] = "primeira versão"
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=json.dumps(v1))
    assert summarize_task(settings, flow["session_factory"], task_id)

    v2 = json.loads(SUMMARY_JSON)
    v2["summary"] = "segunda versão (pós-intervenção)"
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=json.dumps(v2))
    assert summarize_task(settings, flow["session_factory"], task_id)

    resp = flow["client"].get(f"/api/tasks/{task_id}/summary")
    assert resp.json()["summary"] == "segunda versão (pós-intervenção)"
    with flow["session_factory"]() as s:
        assert s.query(TaskSummary).filter_by(task_id=task_id).count() == 2


def test_summary_failure_does_not_break(flow, fake_kimi):
    """Executor sem autoia_summary.json → geração falha silenciosamente, task intacta."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS)  # não escreve o resumo
    task_id = flow["task"]["id"]

    ok = summarize_task(settings, flow["session_factory"], task_id)
    assert not ok

    resp = flow["client"].get(f"/api/tasks/{task_id}/summary")
    assert resp.json() is None
    got = flow["client"].get(f"/api/tasks/{task_id}").json()
    assert got["status"] == "queued"


def test_details_field_editable_any_status(flow):
    """O campo 'detalhes da implementação' (contexto adicional do usuário) é editável
    a qualquer momento e entra no TaskOut."""
    client = flow["client"]
    task_id = flow["task"]["id"]

    resp = client.patch(f"/api/tasks/{task_id}", json={"details": "Utilizar a abordagem B; não alterar a interface."})
    assert resp.status_code == 200, resp.text
    assert resp.json()["details"] == "Utilizar a abordagem B; não alterar a interface."

    got = client.get(f"/api/tasks/{task_id}").json()
    assert got["details"] == "Utilizar a abordagem B; não alterar a interface."


def test_details_reaches_handoff(flow, fake_kimi):
    """Os detalhes do usuário entram no handoff das próximas fases."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    _execute(flow, _run_claim(flow))  # fase 0 (po) conclui
    resp = flow["client"].patch(f"/api/tasks/{task_id}", json={"details": "Regra do banco: usar PostgreSQL local."})
    assert resp.status_code == 200
    _execute(flow, _run_claim(flow))  # fase 1 (qa) roda com os detalhes no handoff

    with flow["session_factory"]() as s:
        t = s.get(Task, task_id)
        checkout = f"{settings.workspace_dir}/{t.repository.id}/task_{t.id}"
    md = open(f"{checkout}/autoia_handoff.md", encoding="utf-8").read()
    assert "Detalhes adicionados pelo usuário" in md
    assert "PostgreSQL local" in md


def test_auto_summary_off_does_not_generate(flow, fake_kimi):
    """Sem auto_summary no repo, avançar uma fase não gera resumo."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=SUMMARY_JSON)
    task_id = flow["task"]["id"]

    step_id = _run_claim(flow)
    _execute(flow, step_id)
    runner._maybe_auto_summary(settings, flow["session_factory"], step_id)
    time.sleep(0.5)

    with flow["session_factory"]() as s:
        assert s.query(TaskSummary).filter_by(task_id=task_id).count() == 0


def test_auto_summary_generates_on_step(flow, fake_kimi):
    """Com auto_summary ligado no repo, o resumo é gerado automaticamente após a
    decisão de uma fase (cada step avançado)."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=SUMMARY_JSON)
    task_id = flow["task"]["id"]

    with flow["session_factory"]() as s:
        repo = s.query(Repository).filter_by(name="r").first()
        repo.auto_summary = True
        s.commit()

    step_id = _run_claim(flow)
    _execute(flow, step_id)
    runner._maybe_auto_summary(settings, flow["session_factory"], step_id)

    for _ in range(100):
        with flow["session_factory"]() as s:
            n = s.query(TaskSummary).filter_by(task_id=task_id).count()
        if n > 0:
            break
        time.sleep(0.1)
    with flow["session_factory"]() as s:
        summary = s.query(TaskSummary).filter_by(task_id=task_id).first()
        assert summary is not None
        assert summary.result == "completed"


def test_auto_summary_does_not_duplicate_inflight(flow, fake_kimi):
    """Chamar o auto-resumo duas vezes seguidas não gera resumos sobrepostos."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(HARMLESS, write_file="autoia_summary.json", write_content=SUMMARY_JSON)
    task_id = flow["task"]["id"]
    with flow["session_factory"]() as s:
        repo = s.query(Repository).filter_by(name="r").first()
        repo.auto_summary = True
        s.commit()

    step_id = _run_claim(flow)
    _execute(flow, step_id)
    runner._maybe_auto_summary(settings, flow["session_factory"], step_id)
    runner._maybe_auto_summary(settings, flow["session_factory"], step_id)

    for _ in range(100):
        with flow["session_factory"]() as s:
            n = s.query(TaskSummary).filter_by(task_id=task_id).count()
        if n > 0:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    with flow["session_factory"]() as s:
        assert s.query(TaskSummary).filter_by(task_id=task_id).count() == 1
