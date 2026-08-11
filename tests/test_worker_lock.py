"""Testes das travas de concorrência do worker (lock de instância única + claim)."""

from __future__ import annotations

import os
import threading
import time

from app.worker import runner
from app.worker.runner import acquire_worker_lock, _heartbeat_loop, _touch_heartbeat


def test_worker_lock_single_instance(tmp_path):
    """Segundo worker não adquire o lock; após liberar, adquire de novo."""
    lock_path = str(tmp_path / "worker.lock")

    first = acquire_worker_lock(lock_path)
    assert first is not None

    second = acquire_worker_lock(lock_path)
    assert second is None  # já existe um worker

    first.close()
    again = acquire_worker_lock(lock_path)
    assert again is not None
    again.close()


def test_claim_never_runs_two_phases_of_same_task(flow):
    """Com um step já running, o claim não pega outra fase da MESMA task.

    Guarda de concorrência: mesmo com um segundo worker por engano, nunca
    duas fases da mesma task rodam ao mesmo tempo (bug de workers duplicados).
    """
    session_factory = flow["session_factory"]
    client = flow["client"]
    task = flow["task"]

    # primeira fase pendente → claim ok
    step_id = runner.claim_next(session_factory)
    assert step_id is not None

    # segunda fase da mesma task está pending, mas não pode ser reclamada
    assert runner.claim_next(session_factory) is None

    with session_factory() as s:
        t = s.get(runner.Task, task["id"])
        running = [st for st in t.steps if st.status == "running"]
        assert len(running) == 1

    # finaliza a fase (simula execução) → próxima fase já pode ser reclamada
    with session_factory() as s:
        t = s.get(runner.Task, task["id"])
        steps = sorted(t.steps, key=lambda st: st.position)
        steps[0].status = "done"
        steps[0].finished_at = None
        steps[1].status = "pending"
        s.commit()
    assert runner.claim_next(session_factory) is not None


def test_claim_skips_task_with_running_phase_and_claims_others(flow):
    """Com uma task com fase running, o claim pula para OUTRA task pendente
    (paralelismo `--workers N`) em vez de travar na próxima fase da mesma task."""
    session_factory = flow["session_factory"]
    client = flow["client"]

    first = runner.claim_next(session_factory)
    assert first is not None  # task 1, fase 1 running

    # segunda task no mesmo repo
    task2 = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t2", "description": "d", "kind": "feature"},
    ).json()
    client.post(f"/api/tasks/{task2['id']}/start")

    claimed = runner.claim_next(session_factory)
    assert claimed is not None
    with session_factory() as s:
        st = s.get(runner.TaskStep, claimed)
        assert st.task_id == task2["id"]


def test_heartbeat_keeps_fresh_during_execution(tmp_path):
    """O heartbeat é tocado periodicamente enquanto uma fase roda (a UI usa
    `alive = age < 15`; sem isso o worker "apareceria offline" durante fases
    longas, já que o loop principal fica bloqueado no subprocess)."""
    hb_path = str(tmp_path / "worker.heartbeat")
    _touch_heartbeat(hb_path)
    first_mtime = os.path.getmtime(hb_path)

    stop = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop, args=(hb_path, stop, 0.1), daemon=True
    )
    thread.start()
    try:
        time.sleep(0.7)
        assert os.path.getmtime(hb_path) > first_mtime
    finally:
        stop.set()
        thread.join(timeout=1)
