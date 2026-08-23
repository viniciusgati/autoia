"""Testes do modo human-in-the-loop de tasks (chat + dispatcher + agentes).

Cobre: start em modo manual abre o chat (sem enfileirar fases), dispatch por
linguagem natural (autoia_dispatch.json), rodada de agente com commit por fase
(sem merge automático), reprovação de veredicto SEM bounce-back, merge sob demanda
e alternância de modo em runtime.
"""

from __future__ import annotations

import json
import stat

import pytest


@pytest.fixture
def env(settings, bare_repo):
    """App + session_factory + repositório registrado."""
    from fastapi.testclient import TestClient

    from app.db import make_engine, make_session_factory
    from app.main import create_app

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    response = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": response.json()["id"],
    }


def _make_chat_kimi(tmp_path, *, dispatch: dict | None, verdict: str | None = None) -> str:
    """Fake kimi que distingue dispatcher (escreve autoia_dispatch.json) de agente
    (escreve autoia_verdict.txt quando `verdict` é informado)."""
    dispatch_json = json.dumps(dispatch) if dispatch else "None"
    script = tmp_path / "fake_chat_kimi"
    body = f"""#!/usr/bin/env python3
import sys, json
prompt = ""
if "-p" in sys.argv:
    prompt = sys.argv[sys.argv.index("-p") + 1]
elif len(sys.argv) > 1 and sys.argv[1] == "run":
    prompt = sys.argv[2]
print(json.dumps({{"role": "assistant", "content": "texto final do executor"}}))
sys.stdout.flush()
if "Agentes disponíveis" in prompt:
    d = {dispatch_json}
    if d is not None:
        with open("autoia_dispatch.json", "w") as f:
            json.dump(d, f)
else:
    v = {verdict!r}
    if v:
        with open("autoia_verdict.txt", "w") as f:
            f.write(v)
"""
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _new_task(env, **extra):
    data = {
        "repository_id": env["repo_id"],
        "pipeline_id": 1,
        "title": "t",
        "description": "d",
        "kind": "feature",
    }
    data.update(extra)
    r = env["client"].post("/api/tasks", json=data)
    assert r.status_code == 201, r.text
    return r.json()


def _claim(env):
    from app.worker import chat_runner

    claimed = chat_runner.claim_next_chat(env["session_factory"])
    assert claimed is not None, "nenhuma ação de chat pendente"
    return claimed


def _run(env, task_id: int, action: str) -> None:
    from app.worker import chat_runner

    chat_runner.execute_chat_action(env["settings"], env["session_factory"], task_id, action)


def test_manual_start_opens_chat(env):
    t = _new_task(env)
    r = env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "manual"
    started = env["client"].post(f"/api/tasks/{t['id']}/start").json()
    assert started["status"] == "open"
    # Nenhuma fase é enfileirada/running no modo manual (ficam pending, sem claim).
    assert all(s["status"] != "running" for s in started["steps"])


def test_chat_requires_open_mode(env):
    t = _new_task(env)
    r = env["client"].post(f"/api/tasks/{t['id']}/chat", json={"text": "oi"})
    assert r.status_code == 400


def test_dispatch_run_agent_commit(env, tmp_path):
    t = _new_task(env)
    env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    env["client"].post(f"/api/tasks/{t['id']}/start")
    env["settings"].kimi_bin = _make_chat_kimi(
        tmp_path,
        dispatch={"action": "run_agent", "agent": "developer", "instruction": "implemente a rota X"},
    )

    r = env["client"].post(f"/api/tasks/{t['id']}/chat", json={"text": "cria a rota de login"})
    assert r.status_code == 200, r.text

    task_id, action = _claim(env)
    assert action == "dispatch"
    _run(env, task_id, action)

    task_id, action = _claim(env)
    assert action.startswith("run_agent:")
    _run(env, task_id, action)

    ws = env["client"].get(f"/api/tasks/{t['id']}/workspace").json()
    assert ws["task"]["status"] == "open"
    assert ws["task"]["pending_action"] is None
    assert len(ws["runs"]) == 1
    assert ws["runs"][0]["robot_name"] == "developer"
    assert ws["runs"][0]["status"] == "concluida"
    kinds = [m["kind"] for m in ws["messages"]]
    assert kinds[0] == "user"
    assert "dispatch" in kinds
    assert "system" in kinds


def test_dispatch_chat_replies_directly(env, tmp_path):
    t = _new_task(env)
    env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    env["client"].post(f"/api/tasks/{t['id']}/start")
    env["settings"].kimi_bin = _make_chat_kimi(
        tmp_path,
        dispatch={"action": "chat", "reply": "Olá! Como posso ajudar?"},
    )
    env["client"].post(f"/api/tasks/{t['id']}/chat", json={"text": "oi"})
    task_id, action = _claim(env)
    assert action == "dispatch"
    _run(env, task_id, action)
    ws = env["client"].get(f"/api/tasks/{t['id']}/workspace").json()
    assert ws["runs"] == []
    msgs = ws["messages"]
    assert msgs[-1]["kind"] == "assistant_text"
    assert "Olá" in msgs[-1]["payload"]["content"]


def test_fail_verdict_no_bounceback(env, tmp_path):
    t = _new_task(env)
    env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    env["client"].post(f"/api/tasks/{t['id']}/start")
    env["settings"].kimi_bin = _make_chat_kimi(
        tmp_path,
        dispatch={"action": "run_agent", "agent": "tester", "instruction": "teste a feature"},
        verdict="FAIL\nSUMMARY: testes falharam",
    )
    env["client"].post(f"/api/tasks/{t['id']}/chat", json={"text": "testa aí"})
    task_id, action = _claim(env)
    _run(env, task_id, action)  # dispatch
    task_id, action = _claim(env)
    _run(env, task_id, action)  # run_agent

    ws = env["client"].get(f"/api/tasks/{t['id']}/workspace").json()
    assert ws["task"]["status"] == "open"
    assert ws["runs"][0]["status"] == "falhou"
    assert ws["runs"][0]["verdict"] == "FAIL"


def test_merge_on_demand(env, tmp_path):
    t = _new_task(env)
    env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    env["client"].post(f"/api/tasks/{t['id']}/start")
    # dispatcher decide merge (não precisa de agente)
    env["settings"].kimi_bin = _make_chat_kimi(
        tmp_path,
        dispatch={"action": "merge", "reply": "vou integrar a branch"},
    )
    env["client"].post(f"/api/tasks/{t['id']}/chat", json={"text": "faz o merge"})
    task_id, action = _claim(env)
    assert action == "dispatch"
    _run(env, task_id, action)
    task_id, action = _claim(env)
    assert action == "merge"
    _run(env, task_id, action)

    ws = env["client"].get(f"/api/tasks/{t['id']}/workspace").json()
    assert ws["task"]["status"] == "done"
    assert any(m["kind"] == "system" and m["payload"].get("event") == "merged" for m in ws["messages"])


def test_switch_back_to_auto(env):
    t = _new_task(env)
    env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "manual"})
    started = env["client"].post(f"/api/tasks/{t['id']}/start").json()
    assert started["status"] == "open"
    back = env["client"].patch(f"/api/tasks/{t['id']}", json={"mode": "auto"}).json()
    assert back["status"] == "queued"
    assert back["mode"] == "auto"
    assert any(s["status"] == "pending" for s in back["steps"])
