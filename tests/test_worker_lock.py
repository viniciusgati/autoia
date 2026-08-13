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


def test_worker_lock_shared_permite_varios_e_bloqueia_avulso(tmp_path):
    """Workers multi-processo (lock compartilhado) coexistem; um `--workers 1`
    avulso (exclusivo) é recusado enquanto houver workers compartilhados."""
    lock_path = str(tmp_path / "worker.lock")

    a = acquire_worker_lock(lock_path, shared=True)
    b = acquire_worker_lock(lock_path, shared=True)
    assert a is not None and b is not None  # N workers compartilham

    avulso = acquire_worker_lock(lock_path)  # exclusivo
    assert avulso is None  # recusado com workers compartilhados ativos

    a.close()
    b.close()
    avulso = acquire_worker_lock(lock_path)
    assert avulso is not None
    avulso.close()


def test_worker_lock_exclusivo_recusa_shared(tmp_path):
    """Instância única (exclusivo) ativa → workers compartilhados são recusados."""
    lock_path = str(tmp_path / "worker.lock")

    first = acquire_worker_lock(lock_path)  # exclusivo
    assert first is not None

    shared = acquire_worker_lock(lock_path, shared=True)
    assert shared is None

    first.close()
    shared = acquire_worker_lock(lock_path, shared=True)
    assert shared is not None
    shared.close()


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


def test_multi_worker_processes_claim_different_tasks(flow, tmp_path):
    """Dois processos de worker reclamam fases de TASKS diferentes em paralelo
    (claim atômico nunca deixa duas fases da MESMA task running)."""
    import logging

    from app.db import make_engine, make_session_factory

    client = flow["client"]
    # segunda task no mesmo repo, iniciada
    task2 = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t2", "description": "d", "kind": "feature"},
    ).json()
    client.post(f"/api/tasks/{task2['id']}/start")

    def child(db_url):
        # cada processo de worker cria engine/sessão próprios (fork-safe)
        sf = make_session_factory(make_engine(db_url))
        from app.worker import runner as r

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            step_id = r.claim_next(sf)
            if step_id is not None:
                with sf() as s:
                    st = s.get(r.TaskStep, step_id)
                    st.status = "running"
                    s.commit()
                os._exit(0)
            time.sleep(0.05)
        os._exit(1)  # não conseguiu claim em tempo hábil

    pids = []
    for _ in range(2):
        pid = os.fork()
        if pid == 0:
            try:
                child(flow["settings"].database_url)
            finally:
                os._exit(1)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)

    # as duas tasks têm EXATAMENTE uma fase running cada (uma por worker)
    with flow["session_factory"]() as s:
        from app.models import Task

        running_by_task = {}
        for t in s.query(Task).all():
            running_by_task[t.id] = [
                st for st in t.steps if st.status == "running"
            ]
    assert len(running_by_task.get(flow["task"]["id"], [])) == 1
    assert len(running_by_task.get(task2["id"], [])) == 1


def test_worker_setup_recover_so_no_pai(flow, monkeypatch):
    """Regressão do double-claim com `--workers N`: a recuperação de órfãos roda
    SOMENTE no pai do grupo (`recover=True`); os filhos (`recover=False`) NÃO
    recuperam — se um filho recuperasse no startup, resetaria para `pending` a fase
    que outro filho acabou de reclamar → a MESMA fase roda 2× em paralelo."""
    import logging

    from app import main as app_main
    from app.main import _worker_setup

    calls = []
    monkeypatch.setattr(app_main, "recover_stale_steps", lambda sf: calls.append(1) or 0)

    engine, sf = _worker_setup(flow["settings"], recover=False, logger=logging.getLogger("t"))
    assert calls == []  # filho: sem recuperação
    engine.dispose()

    engine, sf = _worker_setup(flow["settings"], recover=True, logger=logging.getLogger("t"))
    assert len(calls) == 1  # pai: recupera exatamente uma vez
    engine.dispose()


def test_multi_worker_uma_task_muitos_steps_so_um_running(flow, tmp_path):
    """Vários workers contra UMA task com todos os steps pendentes: no máximo UMA
    fase running (claim atômico + sem recuperação nos filhos)."""
    import logging

    from app.db import make_engine, make_session_factory
    from app.models import TaskStep

    sf = flow["session_factory"]
    # deixa todos os steps da task pendentes
    with sf() as s:
        t = s.get(runner.Task, flow["task"]["id"])
        for st in t.steps:
            st.status = "pending"
        t.status = "queued"
        s.commit()

    def child(db_url):
        sf2 = make_session_factory(make_engine(db_url))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            step_id = runner.claim_next(sf2)
            if step_id is not None:
                with sf2() as s:
                    st = s.get(TaskStep, step_id)
                    st.status = "running"
                    s.commit()
                os._exit(0)
            time.sleep(0.05)
        os._exit(1)

    pids = []
    for _ in range(3):
        pid = os.fork()
        if pid == 0:
            try:
                child(flow["settings"].database_url)
            finally:
                os._exit(1)
        pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)

    # mesmo com 3 workers e a task cheia de pendentes: apenas UMA fase running
    with sf() as s:
        t = s.get(runner.Task, flow["task"]["id"])
        running = [st for st in t.steps if st.status == "running"]
        assert len(running) == 1
