"""Testes de responsável por tarefa e permissão de atuação (lista fechada).

Cobrem: default responsável = criador, reatribuição com upsert de
`repository_users`, o helper `_ensure_can_act` em TODAS as mutações da lista
fechada (403 para não autorizados; 2xx para responsável/admin do projeto/admin
global; qualquer autenticado quando não há responsável; qualquer um com auth
OFF), e o snapshot no worker (`task_steps.responsible_id`/`finished_by_id`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import SubTask, Task, TaskProposal, TaskStep
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "fase concluída"},
]


def _login(client, email, password="senha123"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def aflow(settings, bare_repo):
    """App com auth ON: admin global + membros r/outro/repo_admin + repo + task.

    A task é criada por `r` (responsible_id = r). `repoadmin` é admin do projeto;
    `outro` é apenas um usuário autenticado sem participação.
    """
    settings.auth_enabled = True
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)

    client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    for email in ("r@ex.com", "outro@ex.com", "repoadmin@ex.com"):
        resp = client.post(
            "/api/users",
            json={"name": email.split("@")[0], "email": email, "password": "senha123", "role": "member"},
        )
        assert resp.status_code == 201, resp.text
    assert client.post(
        "/api/repositories", json={"name": "repo", "url": bare_repo, "default_branch": "main"}
    ).status_code == 201

    client_r = TestClient(app)
    _login(client_r, "r@ex.com")
    client_outro = TestClient(app)
    _login(client_outro, "outro@ex.com")
    client_repo_admin = TestClient(app)
    _login(client_repo_admin, "repoadmin@ex.com")
    client_admin = TestClient(app)
    _login(client_admin, "admin@ex.com")

    ids = {u["email"]: u["id"] for u in client.get("/api/users").json()}
    # admin global adiciona repoadmin como membro e o promove a admin do projeto
    assert client.post(
        "/api/repositories/1/members", json={"user_id": ids["repoadmin@ex.com"]}
    ).status_code == 201
    assert client.patch(
        f"/api/repositories/1/members/{ids['repoadmin@ex.com']}", json={"role": "admin"}
    ).status_code == 200

    task = client_r.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    ).json()
    assert task["responsible_id"] == ids["r@ex.com"]

    return {
        "app": app,
        "session_factory": session_factory,
        "client": client,
        "client_r": client_r,
        "client_outro": client_outro,
        "client_repo_admin": client_repo_admin,
        "client_admin": client_admin,
        "task_id": task["id"],
        "ids": ids,
    }


def _prep(session_factory, task_id, name):
    """Prepara o estado do banco para a rota (chamada por cliente AUTORIZADO)."""
    with session_factory() as s:
        t = s.get(Task, task_id)
        st = t.steps[0]
        if name == "start":
            t.status = "created"
        elif name == "pause":
            t.status = "queued"
        elif name == "resume":
            t.status = "paused"
        elif name == "cancel":
            t.status = "queued"
        elif name == "review":
            t.status = "needs_review"
            t.error = "x"
        elif name == "bounceback":
            t.status = "needs_review"
            t.steps[0].status = "done"
            t.steps[1].status = "done"
        elif name == "retry_step":
            t.status = "queued"
            st.status = "failed"
            st.error = "x"
        elif name == "approve_step":
            t.status = "waiting_approval"
            st.status = "pending"
            st.pause_before = True
        elif name == "continue_blocked":
            t.status = "blocked"
            st.status = "blocked"
            st.error = "x"
        elif name == "instruction":
            t.status = "paused"
        elif name == "pm_decide":
            t.status = "needs_review"
            t.error = "x"
        elif name == "subtask_retry":
            t.status = "queued"
            if not t.subtasks:
                t.subtasks.append(SubTask(position=0, title="s1", status="failed", attempt=1))
            else:
                t.subtasks[0].status = "failed"
                t.subtasks[0].attempt = 1  # re-prepara: retries sucessivos incrementam
        elif name == "delete":
            t.status = "created"
        # feedback/PATCH/pm/instruction não precisam de estado especial
        s.commit()


# Rotas da lista fechada (todas as mutações de task).
MUTATIONS = [
    ("start", "POST", "/api/tasks/{id}/start", None),
    ("pause", "POST", "/api/tasks/{id}/pause", None),
    ("resume", "POST", "/api/tasks/{id}/resume", None),
    ("cancel", "POST", "/api/tasks/{id}/cancel", None),
    ("review", "POST", "/api/tasks/{id}/review", {"action": "approve", "extra_budget": 1}),
    ("bounceback", "POST", "/api/tasks/{id}/bounceback", {"target_position": 0}),
    ("retry_step", "POST", "/api/tasks/{id}/steps/0/retry", None),
    ("approve_step", "POST", "/api/tasks/{id}/approve-step", {"position": 0}),
    ("continue_blocked", "POST", "/api/tasks/{id}/blocked/continue", {"instruction": "x"}),
    ("instruction", "POST", "/api/tasks/{id}/instruction", {"instruction": "x"}),
    ("pm_decide", "POST", "/api/tasks/{id}/pm/decide", None),
    ("feedback", "POST", "/api/tasks/{id}/feedback", {"text": "x"}),
    ("feedback_clear", "DELETE", "/api/tasks/{id}/feedback", None),
    ("patch", "PATCH", "/api/tasks/{id}", {"details": "x"}),
    ("delete", "DELETE", "/api/tasks/{id}", None),
    ("subtask_retry", "POST", "/api/tasks/{id}/subtasks/0/retry", None),
    ("responsible", "PUT", "/api/tasks/{id}/responsible", {"user_id": 1}),
]


def test_create_task_defaults_to_creator(aflow):
    assert aflow["task_id"]
    task = aflow["client_r"].get(f"/api/tasks/{aflow['task_id']}").json()
    assert task["responsible_id"] == aflow["ids"]["r@ex.com"]
    assert task["responsible"]["email"] == "r@ex.com"


def test_mutations_by_non_responsible_are_403(aflow):
    """Usuário ≠ responsável (sem admin no projeto) leva 403 em TODA a lista."""
    task_id = aflow["task_id"]
    outro = aflow["client_outro"]
    for _name, method, path_tpl, payload in MUTATIONS:
        path = path_tpl.format(id=task_id)
        resp = outro.request(method, path, json=payload)
        assert resp.status_code == 403, f"{method} {path}: {resp.status_code} {resp.text}"
        assert "responsável" in resp.json()["detail"]


def test_responsible_repo_admin_and_global_admin_can_act(aflow, monkeypatch):
    """R, admin do projeto e admin global recebem 2xx em todas as mutações."""
    import app.api.tasks as tasks_api

    # pm/decide dispara o robô PM em background — neutraliza nos testes.
    monkeypatch.setattr(tasks_api, "_pm_decide", lambda *a, **k: None)

    task_id = aflow["task_id"]
    sf = aflow["session_factory"]
    # `delete` e `responsible` mudam a task de forma destrutiva — cobertos à parte.
    for name, method, path_tpl, payload in MUTATIONS:
        if name in ("delete", "responsible"):
            continue
        path = path_tpl.format(id=task_id)
        for label, client in (
            ("responsável", aflow["client_r"]),
            ("admin do projeto", aflow["client_repo_admin"]),
            ("admin global", aflow["client_admin"]),
        ):
            _prep(sf, task_id, name)  # re-prepara o estado para cada chamada
            resp = client.request(method, path, json=payload)
            assert resp.status_code in (200, 201, 204), (
                f"{name} via {label}: {resp.status_code} {resp.text}"
            )


def test_responsible_can_delete_task(aflow):
    task_id = aflow["task_id"]
    _prep(aflow["session_factory"], task_id, "delete")
    resp = aflow["client_r"].delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 204, resp.text


def test_no_responsible_any_authenticated_can_act(aflow):
    task_id = aflow["task_id"]
    with aflow["session_factory"]() as s:
        t = s.get(Task, task_id)
        t.responsible_id = None
        t.status = "created"
        s.commit()
    resp = aflow["client_outro"].post(f"/api/tasks/{task_id}/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"


def test_auth_off_anyone_can_act(settings, bare_repo):
    """Auth OFF (fixture padrão): sem responsável e sem sessão — comportamento atual."""
    app = create_app(settings)
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "repo", "url": bare_repo, "default_branch": "main"}
    )
    task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    ).json()
    assert task["responsible_id"] is None
    assert client.post(f"/api/tasks/{task['id']}/start").status_code == 200


def test_assign_responsible_upserts_membership(aflow):
    task_id = aflow["task_id"]
    outro_id = aflow["ids"]["outro@ex.com"]
    admin_id = aflow["ids"]["admin@ex.com"]

    # o responsável (r) reatribui para outro
    resp = aflow["client_r"].put(
        f"/api/tasks/{task_id}/responsible", json={"user_id": outro_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["responsible_id"] == outro_id

    # upsert idempotente de repository_users (outro passa a constar, sem duplicar)
    members = aflow["client_admin"].get("/api/repositories/1/members").json()
    assert sum(1 for m in members if m["user_id"] == outro_id) == 1
    outro_as_member = next(m for m in members if m["user_id"] == outro_id)
    assert outro_as_member["role"] == "member"
    # o responsável atual (outro) reatribui para si mesmo → 200, sem duplicar
    resp = aflow["client_outro"].put(
        f"/api/tasks/{task_id}/responsible", json={"user_id": outro_id}
    )
    assert resp.status_code == 200, resp.text
    members = aflow["client_admin"].get("/api/repositories/1/members").json()
    assert sum(1 for m in members if m["user_id"] == outro_id) == 1

    # outro (agora responsável) reatribui para o admin global
    resp = aflow["client_outro"].put(
        f"/api/tasks/{task_id}/responsible", json={"user_id": admin_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["responsible_id"] == admin_id


def test_assign_responsible_keeps_repo_admin_role(aflow):
    """Atribuir um admin do projeto como responsável NÃO rebaixa o papel dele."""
    task_id = aflow["task_id"]
    repoadmin_id = aflow["ids"]["repoadmin@ex.com"]
    resp = aflow["client_r"].put(
        f"/api/tasks/{task_id}/responsible", json={"user_id": repoadmin_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["responsible_id"] == repoadmin_id
    members = aflow["client_admin"].get("/api/repositories/1/members").json()
    repoadmin = next(m for m in members if m["user_id"] == repoadmin_id)
    assert repoadmin["role"] == "admin"


def test_assign_responsible_to_unknown_user_404(aflow):
    task_id = aflow["task_id"]
    resp = aflow["client_r"].put(
        f"/api/tasks/{task_id}/responsible", json={"user_id": 99999}
    )
    assert resp.status_code == 404


def test_member_management_requires_repo_admin(aflow):
    repo_admin = aflow["client_repo_admin"]
    outro = aflow["client_outro"]
    ids = aflow["ids"]
    # outro não é admin do projeto → não gerencia membros
    assert outro.post(
        "/api/repositories/1/members", json={"user_id": ids["outro@ex.com"]}
    ).status_code == 403
    # admin do projeto adiciona outro como membro
    assert repo_admin.post(
        "/api/repositories/1/members", json={"user_id": ids["outro@ex.com"]}
    ).status_code == 201
    # repetir → 409 (não duplica)
    assert repo_admin.post(
        "/api/repositories/1/members", json={"user_id": ids["outro@ex.com"]}
    ).status_code == 409
    # muda o papel e remove
    assert repo_admin.patch(
        "/api/repositories/1/members/" + str(ids["outro@ex.com"]), json={"role": "admin"}
    ).status_code == 200
    assert repo_admin.delete(
        "/api/repositories/1/members/" + str(ids["outro@ex.com"])
    ).status_code == 204


def test_worker_snapshot_responsible_and_finished_by(settings, bare_repo, tmp_path, fake_kimi):
    """claim_next grava o snapshot do responsável; ao concluir, finished_by_id."""
    settings.auth_enabled = True
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    r_user = client.post(
        "/api/users",
        json={"name": "r", "email": "r@ex.com", "password": "senha123", "role": "member"},
    ).json()
    client_r = TestClient(app)
    _login(client_r, "r@ex.com")
    client_r.post(
        "/api/repositories", json={"name": "repo", "url": bare_repo, "default_branch": "main"}
    )
    task = client_r.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    ).json()
    client_r.post(f"/api/tasks/{task['id']}/start")

    claimed = runner.claim_next(session_factory)
    assert claimed is not None
    with session_factory() as s:
        step = s.get(TaskStep, claimed)
        assert step.status == "running"
        assert step.responsible_id == r_user["id"]  # snapshot no claim

    runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        step = s.get(TaskStep, claimed)
        assert step.status == "done"
        assert step.finished_by_id == r_user["id"]


def test_child_task_inherits_responsible(aflow):
    """Tasks geradas por spawn herdam o responsible_id da task pai."""
    task_id = aflow["task_id"]
    r_id = aflow["ids"]["r@ex.com"]
    with aflow["session_factory"]() as s:
        parent = s.get(Task, task_id)
        proposal = TaskProposal(
            task_id=parent.id, position=0, title="filha", description="d", kind="feature"
        )
        s.add(proposal)
        s.commit()
        proposal_id = proposal.id
    resp = aflow["client_admin"].post(f"/api/tasks/{task_id}/proposals/{proposal_id}/accept")
    assert resp.status_code == 200, resp.text
    child = resp.json()["children"][0]
    assert child["responsible_id"] == r_id
