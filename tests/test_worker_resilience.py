"""Resiliência do worker: fase nunca fica `running` órfã e o loop nunca morre.

Regressão das tasks 233/234 (07/10): o clone falhou (timeout de rede), o
registro da falha morreu com `database is locked`, o `_fail_step_hard` de
segurança MORREU TAMBÉM, a exceção escapou do `worker_loop` e 2 dos 3 processos
worker saíram em silêncio — as fases ficaram `running` para sempre e as tasks
viraram starvation (só o restart do worker as recuperou).
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.exc import OperationalError

from app.models import (
    STEP_DONE,
    STEP_FAILED,
    STEP_RUNNING,
    TASK_FAILED,
    RunEvent,
    TaskStep,
)


class _FlakySessionFactory:
    """session_factory que faz os PRIMEIROS `fails` commits falharem com
    `database is locked` (a sessão quebrada é descartada; a seguinte grava)."""

    def __init__(self, real, fails: int = 1):
        self._real = real
        self._fails = fails

    def __call__(self):
        session = self._real()
        if self._fails > 0:
            self._fails -= 1

            def _locked_commit():
                raise OperationalError("INSERT", {}, Exception("database is locked"))

            session.commit = _locked_commit  # type: ignore[method-assign]
        return session


@pytest.fixture(autouse=True)
def _sem_espera_entre_tentativas(monkeypatch):
    """Retry sem backoff real: o teste não fica 1+2+4 s dormindo."""
    from app.worker import runner

    monkeypatch.setattr(runner, "_LOCK_RETRY_BASE_S", 0.0)


def _first_step(session_factory, task_id: int) -> int:
    with session_factory() as s:
        return (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id)
            .order_by(TaskStep.position)
            .first()
            .id
        )


def test_fail_step_hard_regrava_quando_o_sqlite_esta_ocupado(flow):
    """1º commit morre com lock → a unidade é RE-EXECUTADA e o estado grava."""
    from app.worker import runner

    session_factory = flow["session_factory"]
    with session_factory() as s:
        step = s.query(TaskStep).filter(TaskStep.task_id == flow["task"]["id"]).first()
        step.status = STEP_RUNNING
        s.commit()
        step_id = step.id

    flaky = _FlakySessionFactory(session_factory, fails=1)
    ok = runner._fail_step_hard(flaky, step_id, reason="clone falhou: rede")

    assert ok is True
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == STEP_FAILED
        assert "clone falhou: rede" in step.error
        assert step.finished_at is not None
        assert step.task.status == TASK_FAILED
        kinds = [e.kind for e in s.query(RunEvent).filter(RunEvent.step_id == step_id)]
        assert "step_forced_failed" in kinds


def test_fail_step_hard_nao_lanca_mesmo_sem_solver_o_lock(flow):
    """Se o SQLite nunca liberar, NÃO se lança: o chamador é o caminho de
    recuperação e não pode morrer junto com o problema."""
    from app.worker import runner

    session_factory = flow["session_factory"]
    with session_factory() as s:
        step = s.query(TaskStep).filter(TaskStep.task_id == flow["task"]["id"]).first()
        step.status = STEP_RUNNING
        s.commit()
        step_id = step.id

    forever_locked = _FlakySessionFactory(session_factory, fails=99)
    ok = runner._fail_step_hard(forever_locked, step_id, reason="x")

    assert ok is False
    with session_factory() as s:
        # Continua `running` (não gravou), mas nada lançou e o loop segue vivo.
        assert s.get(TaskStep, step_id).status == STEP_RUNNING


def test_fail_step_hard_nao_sobrescreve_fase_ja_decidida(flow):
    """Fase já concluída/reprovada nunca é tocada pela rede de proteção."""
    from app.worker import runner

    session_factory = flow["session_factory"]
    with session_factory() as s:
        step = s.query(TaskStep).filter(TaskStep.task_id == flow["task"]["id"]).first()
        step.status = STEP_DONE
        step.error = None
        s.commit()
        step_id, task_status = step.id, step.task.status

    assert runner._fail_step_hard(session_factory, step_id, reason="de qualquer jeito") is True

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == STEP_DONE
        assert step.task.status == task_status


def test_worker_loop_nao_morre_e_fecha_a_fase_que_errou(flow, monkeypatch, tmp_path):
    """Exceção no meio da fase NÃO mata o loop: a fase é fechada com o motivo e
    o próximo claim acontece (antes, a exceção escapava e o processo saía com
    `os._exit(0)` deixando a fase `running` para sempre)."""
    from app.models import Task
    from app.worker import runner

    settings = flow["settings"]
    settings.peak_hours_windows = ""  # janela de pico desligada no teste
    session_factory = flow["session_factory"]
    task_id = flow["task"]["id"]

    calls = {"claim": 0}

    def fake_claim(_sf):
        calls["claim"] += 1
        if calls["claim"] > 1:
            raise SystemExit  # BaseException: sai do loop sem ser engolida
        with session_factory() as s:
            step = (
                s.query(TaskStep)
                .filter(TaskStep.task_id == task_id)
                .order_by(TaskStep.position)
                .first()
            )
            step.status = STEP_RUNNING
            s.commit()
            return step.id, step.started_at

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom do executor")

    monkeypatch.setattr(runner, "claim_next_with_token", fake_claim)
    monkeypatch.setattr(runner, "execute_step", boom)
    # Gerações LLM de pós-fase não participam deste teste.
    monkeypatch.setattr(runner, "_maybe_auto_summary", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_step_summary", lambda *a, **k: None)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(SystemExit):
        runner.worker_loop(settings, session_factory, str(workspace))

    with session_factory() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id)
            .order_by(TaskStep.position)
            .first()
        )
        assert step.status == STEP_FAILED
        assert "boom do executor" in step.error
        assert s.get(Task, task_id).status == TASK_FAILED
        kinds = [e.kind for e in s.query(RunEvent).filter(RunEvent.step_id == step.id)]
        assert "step_forced_failed" in kinds
    assert calls["claim"] == 2  # o loop continuou vivo depois do erro


def test_worker_loop_deixa_fase_pendente_depois_de_decidir(flow, monkeypatch, tmp_path):
    """Quando a fase é decidida normalmente, a rede de proteção é um no-op."""
    from app.worker import runner

    settings = flow["settings"]
    settings.peak_hours_windows = ""
    session_factory = flow["session_factory"]
    task_id = flow["task"]["id"]

    def claim_and_finish(_sf):
        with session_factory() as s:
            step = (
                s.query(TaskStep)
                .filter(TaskStep.task_id == task_id)
                .order_by(TaskStep.position)
                .first()
            )
            step.status = STEP_RUNNING
            s.commit()
            step_id = step.id
        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            step.status = STEP_DONE
            s.commit()
        raise SystemExit

    monkeypatch.setattr(runner, "claim_next_with_token", claim_and_finish)
    monkeypatch.setattr(runner, "execute_step", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_auto_summary", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_step_summary", lambda *a, **k: None)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SystemExit):
        runner.worker_loop(settings, session_factory, str(workspace))

    with session_factory() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == task_id)
            .order_by(TaskStep.position)
            .first()
        )
        assert step.status == STEP_DONE
        kinds = [e.kind for e in s.query(RunEvent).filter(RunEvent.step_id == step.id)]
        assert "step_forced_failed" not in kinds


def test_supervisor_repor_filho_morto():
    """Pai supervisor: filho que sai é logado e substituído (sem zumbi, sem
    capacidade perdida em silêncio)."""
    from app.main import _supervise_worker_children

    spawned: list[int] = []
    probes = {"n": 0}

    def spawn() -> int:
        spawned.append(1)
        return 4242

    def waitpid(_pid, _flags):
        return (111, 256)  # filho saiu com status != 0

    def is_stopping() -> bool:
        probes["n"] += 1
        return probes["n"] > 2  # deixa UM ciclo rodar

    children = [111]
    result = _supervise_worker_children(
        children,
        spawn,
        logging.getLogger("test.supervisor"),
        is_stopping,
        poll_s=0,
        waitpid=waitpid,
    )

    assert spawned == [1]
    assert children == [4242]
    assert result is children


def test_supervisor_ignora_filho_vivo():
    from app.main import _supervise_worker_children

    spawned: list[int] = []
    probes = {"n": 0}

    def is_stopping() -> bool:
        probes["n"] += 1
        return probes["n"] > 2

    children = [111]
    _supervise_worker_children(
        children,
        lambda: spawned.append(1) or 999,
        logging.getLogger("test.supervisor"),
        is_stopping,
        poll_s=0,
        waitpid=lambda pid, flags: (0, 0),  # WNOHANG: ainda vivo
    )

    assert spawned == []
    assert children == [111]


def test_lock_probe_registra_vitima_e_holder(monkeypatch, tmp_path, caplog):
    """Instrumentação do lock: escrita que esperou + transação de escrita longa
    viram warning — é assim que a próxima tempestade de `database is locked`
    revela quem SEGURA o lock."""
    from sqlalchemy import create_engine, text

    from app import db as db_mod

    engine = create_engine(f"sqlite:///{tmp_path}/probe.db")
    db_mod.install_sqlite_lock_probe(engine, 1.0)

    class _SlowTime:
        """Cada leitura 'passa' 10 s — simula escrita que esperou pelo lock."""

        def __init__(self):
            self._now = 0.0

        def monotonic(self) -> float:
            self._now += 10.0
            return self._now

    monkeypatch.setattr(db_mod, "time", _SlowTime())

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with caplog.at_level(logging.WARNING, logger="autoia.db"):
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO t (id) VALUES (1)")

    messages = [r.getMessage() for r in caplog.records]
    assert any("escrita levou" in m and "espera pelo lock" in m for m in messages)
    assert any("transação de escrita durou" in m for m in messages)
