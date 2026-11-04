"""Testes da espera por slot de execução (visibilidade + interrupção).

Contexto (task-250): a fase era reclamada e ficava `running` aguardando o teto
global de LLM (`AUTOIA_LLM_MAX_CONCURRENT`) sem NENHUM sinal na timeline — parecia
travada — e a instrução/pause do usuário só era processada quando um slot liberava
(o `.stop-task-<id>` é um pulso removido pelo heartbeat; sem subprocesso para
matar, a ação humana se perdia).

A correção tem três partes:
- a espera emite `slot_wait`/`slot_ready` (visível no log e na timeline);
- a espera reage à ação do usuário via `make_step_abort_check` (status durável do
  step/task no banco), abortando SEM iniciar o executor;
- o executor nem chega a subir nesse caso (nenhum `tool_call`/`assistant_text`).
"""

from __future__ import annotations

import threading
import time

import pytest

from app.models import STEP_PENDING, RunEvent, TaskStep
from app.worker import exec_common, runner

HARMLESS = [{"role": "assistant", "content": "fase concluída"}]


# ---------- exec_common: espera de slot ----------


def test_acquire_slot_espera_visivel_e_abortavel(tmp_path):
    """Com todos os slots ocupados, a espera avisa (`on_wait_start`) e aborta
    quando `should_abort` devolve um motivo (`SlotWaitAborted`)."""
    ws = str(tmp_path / "ws")
    held = exec_common.acquire_llm_slot(ws, 1)
    assert held is not None
    started: list[tuple[str, int]] = []
    calls = {"n": 0}

    def _should_abort() -> str | None:
        calls["n"] += 1
        return "pausa do usuário" if calls["n"] >= 2 else None

    with pytest.raises(exec_common.SlotWaitAborted) as exc:
        exec_common.acquire_llm_slot(
            ws,
            1,
            should_abort=_should_abort,
            on_wait_start=lambda kind, limit: started.append((kind, limit)),
        )
    assert exc.value.reason == "pausa do usuário"
    assert started == [("llm", 1)]
    exec_common.release_slot(held)


def test_acquire_slot_avisa_quando_libera(tmp_path):
    """Liberado um slot, a espera emite `on_wait_end` com o tempo aguardado."""
    ws = str(tmp_path / "ws")
    held = exec_common.acquire_llm_slot(ws, 1)
    assert held is not None
    timer = threading.Timer(0.6, exec_common.release_slot, args=(held,))
    timer.start()
    waited: list[tuple] = []

    handle = exec_common.acquire_llm_slot(
        ws,
        1,
        on_wait_start=lambda kind, limit: waited.append(("start", kind, limit)),
        on_wait_end=lambda kind, seconds: waited.append(("end", kind, round(seconds))),
    )
    timer.join()
    assert handle is not None
    assert waited[0] == ("start", "llm", 1)
    assert waited[-1][0] == "end" and waited[-1][1] == "llm"
    exec_common.release_slot(handle)


# ---------- runner: visibilidade na timeline ----------


def test_make_step_abort_check_le_status_do_banco(flow):
    """O check usa o status DURÁVEL do step: `running` = segue, fora = aborta."""
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    check = exec_common.make_step_abort_check(flow["session_factory"], step_id)
    assert check() is None  # fase running: a espera continua

    with flow["session_factory"]() as s:
        s.get(TaskStep, step_id).status = STEP_PENDING
        s.commit()

    reason = check()
    assert reason is not None
    assert "interrompida" in reason


def test_execucao_espera_slot_emite_eventos(flow, fake_kimi):
    """A fase que espera slot grava `slot_wait`/`slot_ready` e depois executa."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    settings.llm_max_concurrent = 1

    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    held = exec_common.acquire_llm_slot(settings.workspace_dir, 1)
    assert held is not None

    def _release_when_waiting() -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            with flow["session_factory"]() as s:
                if (
                    s.query(RunEvent)
                    .filter(RunEvent.step_id == step_id, RunEvent.kind == "slot_wait")
                    .first()
                    is not None
                ):
                    break
            time.sleep(0.1)
        exec_common.release_slot(held)

    releaser = threading.Thread(target=_release_when_waiting, daemon=True)
    releaser.start()
    runner.execute_step(settings, flow["session_factory"], step_id)
    releaser.join(timeout=5)

    with flow["session_factory"]() as s:
        kinds = [
            r.kind
            for r in s.query(RunEvent)
            .filter(RunEvent.step_id == step_id)
            .order_by(RunEvent.id)
        ]
        step = s.get(TaskStep, step_id)
    assert "slot_wait" in kinds
    assert "slot_ready" in kinds
    # depois do slot liberado, o executor rodou de verdade (fase concluiu).
    assert step.status == "done"


# ---------- runner: instrução durante a espera ----------


def test_instrucao_durante_espera_aborta_sem_iniciar_executor(flow, fake_kimi):
    """Ação do usuário durante a espera de slot: o worker retorna na hora, a fase
    continua `pending` e o executor NUNCA é iniciado (sem eventos de execução)."""
    settings = flow["settings"]
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    settings.llm_max_concurrent = 1

    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    held = exec_common.acquire_llm_slot(settings.workspace_dir, 1)
    assert held is not None

    result: list = []

    def _run() -> None:
        try:
            result.append(runner.execute_step(settings, flow["session_factory"], step_id))
        except Exception as exc:  # pragma: no cover - falha do teste
            result.append(exc)

    worker_thread = threading.Thread(target=_run, daemon=True)
    worker_thread.start()

    # Espera a fase entrar na espera do slot (ainda com o slot ocupado).
    deadline = time.time() + 20
    waiting = False
    while time.time() < deadline:
        with flow["session_factory"]() as s:
            waiting = (
                s.query(RunEvent)
                .filter(RunEvent.step_id == step_id, RunEvent.kind == "slot_wait")
                .first()
                is not None
            )
        if waiting:
            break
        time.sleep(0.1)
    assert waiting, "a fase não chegou a esperar pelo slot"

    # Ação do usuário (espelha `_force_stop_running` da API: pause/instrução
    # devolve a fase para `pending` e o stop file é o pulso do executor).
    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        step.status = STEP_PENDING
        step.started_at = None
        s.commit()

    worker_thread.join(timeout=10)
    assert not worker_thread.is_alive(), "execute_step não retornou após a instrução"
    assert result and result[0] is None  # _decide não decide: ação do usuário manda

    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        kinds = [
            r.kind
            for r in s.query(RunEvent)
            .filter(RunEvent.step_id == step_id)
            .order_by(RunEvent.id)
        ]
    assert step.status == STEP_PENDING
    assert "slot_wait" in kinds
    # O executor nunca iniciou: nenhum evento de execução do robô.
    assert "tool_call" not in kinds
    assert "assistant_text" not in kinds
    assert "phase_done" not in kinds
    exec_common.release_slot(held)
