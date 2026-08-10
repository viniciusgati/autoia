"""Testes do sistema de artifacts (screenshots gerados pelos robôs)."""

from __future__ import annotations

import os
from pathlib import Path

from app.models import StepArtifact, TaskStep
from app.worker import runner


HARMLESS = [
    {"role": "assistant", "content": "tarefa concluída"},
]


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _task_checkout(flow) -> Path:
    """Retorna o diretório de checkout isolado da task (workspace/task-<id>)."""
    settings = flow["settings"]
    task_id = flow["task"]["id"]
    return Path(settings.workspace_dir) / "1" / f"task_{task_id}"


def test_scan_artifacts_registra_screenshots(flow, fake_kimi):
    """Worker escaneia autoia_screenshots/ após execução bem-sucedida e registra no banco."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]

    # Avança po (pos 0), qa (pos 1), developer (pos 2)
    for _ in range(3):
        _execute(flow, _run_claim(flow))

    # Pega o step do tester (pos 3) para saber o ID antes de executar
    with flow["session_factory"]() as s:
        tester_step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id, TaskStep.position == 3)
            .first()
        )
        assert tester_step is not None
        tester_step_id = tester_step.id
        checkout = _task_checkout(flow)
        screens_dir = checkout / "autoia_screenshots" / f"step_{tester_step_id}"
        screens_dir.mkdir(parents=True, exist_ok=True)
        (screens_dir / "smoke-login.png").write_text("fake png content")
        (screens_dir / "dashboard.jpg").write_text("fake jpg content")
        (screens_dir / "ignorado.txt").write_text("não é imagem")  # deve ser ignorado

    # Executa o tester (pos 3) — o kimi fake emite PASS
    _execute(flow, _run_claim(flow))

    # Verifica que os artifacts foram registrados
    with flow["session_factory"]() as s:
        artifacts = (
            s.query(StepArtifact)
            .filter(StepArtifact.step_id == tester_step_id)
            .order_by(StepArtifact.filename)
            .all()
        )
        assert len(artifacts) == 2
        assert artifacts[0].filename == "dashboard.jpg"
        assert artifacts[1].filename == "smoke-login.png"
        assert "autoia_screenshots" in artifacts[0].filepath


def test_api_artifacts_list_and_serve(flow, fake_kimi):
    """GET /api/steps/{id}/artifacts retorna os artifacts; GET .../file serve a imagem."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    task_id = flow["task"]["id"]
    cli = flow["client"]

    for _ in range(3):
        _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        tester_step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id, TaskStep.position == 3)
            .first()
        )
        tester_step_id = tester_step.id
        checkout = _task_checkout(flow)
        screens_dir = checkout / "autoia_screenshots" / f"step_{tester_step_id}"
        screens_dir.mkdir(parents=True, exist_ok=True)
        (screens_dir / "tela.png").write_text("conteudo binario simulado")

    _execute(flow, _run_claim(flow))

    # GET artifacts
    resp = cli.get(f"/api/steps/{tester_step_id}/artifacts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["filename"] == "tela.png"
    artifact_id = data[0]["id"]

    # GET arquivo
    resp2 = cli.get(f"/api/steps/artifacts/{artifact_id}/file")
    assert resp2.status_code == 200
    assert resp2.content == b"conteudo binario simulado"


def test_api_delete_artifacts(flow, fake_kimi):
    """DELETE /api/steps/{id}/artifacts remove arquivos e registros."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    cli = flow["client"]

    for _ in range(3):
        _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        tester_step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.position == 3)
            .first()
        )
        tester_step_id = tester_step.id
        checkout = _task_checkout(flow)
        screens_dir = checkout / "autoia_screenshots" / f"step_{tester_step_id}"
        screens_dir.mkdir(parents=True, exist_ok=True)
        fpath = screens_dir / "removivel.png"
        fpath.write_text("remova-me")

    _execute(flow, _run_claim(flow))

    # Confirma que o artifact existe
    resp = cli.get(f"/api/steps/{tester_step_id}/artifacts")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # DELETE
    resp = cli.delete(f"/api/steps/{tester_step_id}/artifacts")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1

    # Confirma que foi removido do banco
    resp = cli.get(f"/api/steps/{tester_step_id}/artifacts")
    assert resp.json() == []

    # Confirma que o arquivo foi removido do disco
    assert not fpath.exists()


def test_taskstep_out_inclui_artifacts(flow, fake_kimi):
    """TaskStepOut inclui o campo `artifacts` com os artifacts do step."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    cli = flow["client"]

    for _ in range(3):
        _execute(flow, _run_claim(flow))

    with flow["session_factory"]() as s:
        tester_step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.position == 3)
            .first()
        )
        checkout = _task_checkout(flow)
        screens_dir = checkout / "autoia_screenshots" / f"step_{tester_step.id}"
        screens_dir.mkdir(parents=True, exist_ok=True)
        (screens_dir / "a.png").write_text("a")

    _execute(flow, _run_claim(flow))

    resp = cli.get(f"/api/tasks/{flow['task']['id']}")
    assert resp.status_code == 200
    task_data = resp.json()
    tester_out = next(st for st in task_data["steps"] if st["position"] == 3)
    assert len(tester_out["artifacts"]) == 1
    assert tester_out["artifacts"][0]["filename"] == "a.png"
