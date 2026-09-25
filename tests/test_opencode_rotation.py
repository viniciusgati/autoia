"""Rotação automática de conta opencode-go após estouro (`provider_limit`).

Quando a conta em uso esgota (evento de erro do opencode com marca de limite), a
fase NÃO espera `retry_at`: com executor opencode e mais contas no roster, o worker
fixa a PRÓXIMA conta da cadeia (`TaskStep.opencode_account`) e re-enfileira na hora.
Só quando a cadeia esgota é que a fase aguarda (retry_at) como antes.
"""

from __future__ import annotations

import stat
from datetime import timedelta

import pytest

from app.config import Settings
from app.db import make_session_factory, make_engine, utcnow
from app.main import create_app
from app.models import STEP_PENDING, OpenCodeAccount, TaskStep
from app.worker import runner


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "workspaces"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        opencode_accounts_dir=str(tmp_path / "opencode-accounts"),
        kimi_bin="kimi",
        opencode_bin="opencode",
        run_timeout=30,
        max_identical_calls=3,
        max_attempts=3,
        task_budget=1.0,
        cost_per_interaction=0.01,
        pm_budget_topup=5.0,
        max_pm_decisions=0,
        step_mission=False,
        auth_enabled=False,
    )


def _quota_fake(tmp_path, xdg_log) -> str:
    """Fake do opencode: SEMPRE emite erro de limite de uso (conta esgotada) e
    grava o XDG_DATA_HOME usado (conta) em `xdg_log`."""
    script = tmp_path / "fake_opencode_limit"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, json, sys\n"
        f"with open({str(xdg_log)!r}, 'a') as f:\n"
        "    f.write(os.environ.get('XDG_DATA_HOME', 'HOST') + '\\n')\n"
        "print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': 'x'}}))\n"
        "sys.stdout.flush()\n"
        "print(json.dumps({'type': 'error', 'part': {'type': 'error', "
        "\"message\": \"You've hit your usage limit. Upgrade to Pro.\"}}))\n"
        "sys.stdout.flush()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _flow(settings, bare_repo, fake_bin, *, extra_accounts: list[str]) -> dict:
    from fastapi.testclient import TestClient

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)

    client.post(
        "/api/system/opencode-accounts",
        json={"name": "conta-a", "token": "tk-a", "is_default": True},
    )
    for name in extra_accounts:
        client.post(
            "/api/system/opencode-accounts",
            json={"name": name, "token": f"tk-{name}", "is_default": False},
        )

    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text

    task = client.post(
        "/api/tasks",
        json={
            "repository_id": resp.json()["id"],
            "pipeline_id": 1,
            "title": "t",
            "description": "d",
            "kind": "feature",
            "executor": "opencode",
        },
    ).json()
    client.post(f"/api/tasks/{task['id']}/start")

    settings.opencode_bin = fake_bin
    return {"settings": settings, "session_factory": session_factory, "task": task, "client": client}


def _step(session_factory, task_id: int) -> TaskStep:
    with session_factory() as s:
        st = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).first()
        s.expunge(st)
        return st


def _delete_accounts(session_factory, *names: str) -> None:
    with session_factory() as s:
        n = s.query(OpenCodeAccount).filter(OpenCodeAccount.name.in_(names)).delete()
        s.commit()
        assert n == len(names)


def _run_claim(flow) -> int | None:
    return runner.claim_next(flow["session_factory"])


def _execute(flow, step_id) -> None:
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def test_rotaciona_para_proxima_conta_e_depois_aguarda(settings, bare_repo, tmp_path):
    """provider_limit na conta default (conta-a) → fase re-enfileira para conta-b
    (reclamada imediatamente). Esgotada a cadeia, aguarda `retry_at`."""
    xdg_log = tmp_path / "xdg.txt"
    flow = _flow(settings, bare_repo, _quota_fake(tmp_path, xdg_log), extra_accounts=["conta-b"])

    # 1ª execução: conta default (conta-a) esgota → rota para conta-b, sem retry_at.
    _execute(flow, _run_claim(flow))
    st = _step(flow["session_factory"], flow["task"]["id"])
    assert st.status == STEP_PENDING
    assert st.retry_at is None
    assert st.opencode_account == "conta-b"
    assert "tentando conta conta-b" in (st.error or "")

    # 2ª execução: usa conta-b (override) → também esgota → cadeia acabou → aguarda.
    _execute(flow, _run_claim(flow))
    st = _step(flow["session_factory"], flow["task"]["id"])
    assert st.status == STEP_PENDING
    assert st.opencode_account is None  # volta à cadeia na retomada futura
    assert st.retry_at is not None and st.retry_at > utcnow()
    assert st.retry_at <= utcnow() + timedelta(minutes=6)

    # As execuções usaram a conta default e depois a próxima da cadeia.
    used = xdg_log.read_text().splitlines()
    base = settings.opencode_accounts_dir.rstrip("/")
    assert used == [f"{base}/conta-a", f"{base}/conta-b"]

    # Com retry_at no futuro, nada é reclamado.
    assert _run_claim(flow) is None


def test_roda_todas_as_contas_do_roster(settings, bare_repo, tmp_path):
    """Cadeia completa (default + contas restantes do roster em ordem alfabética):
    cada provider_limit avança uma conta até esgotar."""
    xdg_log = tmp_path / "xdg.txt"
    flow = _flow(settings, bare_repo, _quota_fake(tmp_path, xdg_log), extra_accounts=["conta-b", "conta-c"])

    for _ in range(3):  # conta-a, conta-b, conta-c
        _execute(flow, _run_claim(flow))

    used = xdg_log.read_text().splitlines()
    base = settings.opencode_accounts_dir.rstrip("/")
    assert used == [f"{base}/conta-a", f"{base}/conta-b", f"{base}/conta-c"]

    st = _step(flow["session_factory"], flow["task"]["id"])
    assert st.opencode_account is None and st.retry_at is not None
    assert _run_claim(flow) is None


def test_sem_segunda_conta_aguarda_como_antes(settings, bare_repo, tmp_path):
    """Só uma conta no roster (e nenhuma outra): provider_limit aguarda `retry_at`
    direto, mantendo o comportamento original (sem rotação)."""
    xdg_log = tmp_path / "xdg.txt"
    flow = _flow(settings, bare_repo, _quota_fake(tmp_path, xdg_log), extra_accounts=["conta-b", "conta-c"])
    _delete_accounts(flow["session_factory"], "conta-b", "conta-c")  # roster só com conta-a

    _execute(flow, _run_claim(flow))
    st = _step(flow["session_factory"], flow["task"]["id"])
    assert st.status == STEP_PENDING
    assert st.retry_at is not None and st.retry_at > utcnow()
    assert st.opencode_account is None
    used = xdg_log.read_text().splitlines()
    assert used[0] == settings.opencode_accounts_dir.rstrip("/") + "/conta-a"


def test_pin_do_repo_eh_rotacionado_depois_que_esgota(settings, bare_repo, tmp_path):
    """Repo com pin `conta-b` (is_default false): a PRIMEIRA execução já usa a
    conta fixada. A cadeia é [pin, default, demais]: depois do pin vem a default
    (conta-a) e por fim conta-c — cada provider_limit avança uma etapa."""
    xdg_log = tmp_path / "xdg.txt"
    from fastapi.testclient import TestClient

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post("/api/system/opencode-accounts", json={"name": "conta-a", "token": "tk-a", "is_default": True})
    for name in ("conta-b", "conta-c"):
        client.post("/api/system/opencode-accounts", json={"name": name, "token": f"tk-{name}", "is_default": False})
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main", "opencode_account": "conta-b"},
    )
    assert resp.status_code == 201, resp.text
    task = client.post(
        "/api/tasks",
        json={"repository_id": resp.json()["id"], "pipeline_id": 1, "title": "t", "description": "d",
              "kind": "feature", "executor": "opencode"},
    ).json()
    client.post(f"/api/tasks/{task['id']}/start")
    settings.opencode_bin = _quota_fake(tmp_path, xdg_log)
    flow = {"settings": settings, "session_factory": session_factory, "task": task}

    _execute(flow, _run_claim(flow))  # pin conta-b
    st = _step(flow["session_factory"], task["id"])
    assert st.opencode_account == "conta-a"  # próximo da cadeia depois do pin
    assert st.retry_at is None

    _execute(flow, _run_claim(flow))  # default conta-a
    st = _step(flow["session_factory"], task["id"])
    assert st.opencode_account == "conta-c"
    assert st.retry_at is None

    _execute(flow, _run_claim(flow))  # conta-c
    used = xdg_log.read_text().splitlines()
    base = settings.opencode_accounts_dir.rstrip("/")
    assert used == [f"{base}/conta-b", f"{base}/conta-a", f"{base}/conta-c"]
    st = _step(flow["session_factory"], task["id"])
    assert st.opencode_account is None and st.retry_at is not None


def test_kimi_nao_rotaciona_conta_opencode(settings, bare_repo):
    """O guard de rotação pertence ao executor opencode: `_next_opencode_account`
    retorna None para task com executor kimi (fase kimi não tem conta opencode)."""
    from fastapi.testclient import TestClient

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post("/api/system/opencode-accounts", json={"name": "conta-a", "token": "tk-a", "is_default": True})
    client.post("/api/system/opencode-accounts", json={"name": "conta-b", "token": "tk-b", "is_default": False})
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text
    task = client.post(
        "/api/tasks",
        json={"repository_id": resp.json()["id"], "pipeline_id": 1, "title": "t", "description": "d",
              "kind": "feature", "executor": "kimi"},
    ).json()

    with session_factory() as s:
        from app.models import Task

        t = s.get(Task, task["id"])
        eff = runner.EffectiveSettings(
            max_attempts=3, max_pm_decisions=0, run_timeout=30, task_budget=1.0,
            cost_per_interaction=0.01, pm_budget_topup=5.0, risky_patterns=[],
            whitelisted_hosts=[], db_rule="", kimi_bin="kimi", opencode_bin="",
            opencode_model=None, codex_bin="", codex_model=None, log_dir="",
            branch_prefix="autoia", workspace_dir="", max_identical_calls=3,
            no_progress_timeout=300, max_repeated_searches=5, verify_retries=2,
            keep_workspaces=True, sandbox=None,
            opencode_account="conta-a", opencode_account_dir="/tmp/nao-usado",
        )
        assert runner._next_opencode_account(s, eff, t) is None  # kimi não rotaciona

        # sanity: mesma task com executor opencode e accounts disponíveis rotaciona
        t.executor = "opencode"
        assert runner._next_opencode_account(s, eff, t) == "conta-b"