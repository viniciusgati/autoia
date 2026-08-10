"""Testes das travas de concorrência do worker (lock de instância única + claim)."""

from __future__ import annotations

from app.worker import runner
from app.worker.runner import acquire_worker_lock


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
