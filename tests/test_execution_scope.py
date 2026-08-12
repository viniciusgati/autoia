"""Testes do escopo de visibilidade do GET /api/execution (auth ON).

Cenários: participação em projeto, responsável sem participação, task sem
responsável, não-vazamento de tasks de terceiros, filtro explícito por projeto,
auth OFF global e o mesmo escopo para proposals/notices/current_events + ETag.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task, TaskProposal
from app.worker.runner import _system_event


def _login(client, email, password="senha123"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


_KEEP_RESPONSIBLE = object()


@pytest.fixture
def eflow(settings, bare_repo):
    """Auth ON: admin + usuários A/B; repos r1/r2/r3; só A participa de r1.

    Tasks ativas: t_part (A resp., r1), t_resp (A resp., r2), t_noresp (sem
    responsável, r3) e t_other (B resp., r3) — cada uma cobre uma regra de
    visibilidade do escopo. A criação de task NÃO upserta participação, então
    A é responsável de t_resp (r2) sem ser membro do r2.
    """
    settings.auth_enabled = True
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    for email in ("a@ex.com", "b@ex.com"):
        client.post(
            "/api/users",
            json={"name": email.split("@")[0], "email": email, "password": "senha123", "role": "member"},
        )
    ids = {u["email"]: u["id"] for u in client.get("/api/users").json()}
    for name in ("r1", "r2", "r3"):
        resp = client.post(
            "/api/repositories", json={"name": name, "url": bare_repo, "default_branch": "main"}
        )
        assert resp.status_code == 201, resp.text
    client.post("/api/repositories/1/members", json={"user_id": ids["a@ex.com"]})

    client_a = TestClient(app)
    _login(client_a, "a@ex.com")
    client_b = TestClient(app)
    _login(client_b, "b@ex.com")

    def _task(c, repo_id, title):
        resp = c.post(
            "/api/tasks",
            json={"repository_id": repo_id, "pipeline_id": 1, "title": title, "description": "d", "kind": "feature"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def _activate(task_id, responsible=_KEEP_RESPONSIBLE):
        with session_factory() as s:
            task = s.get(Task, task_id)
            task.status = "queued"
            task.steps[0].status = "pending"
            if responsible is not _KEEP_RESPONSIBLE:
                task.responsible_id = responsible
            s.commit()

    tasks = {
        "t_part": _task(client_a, 1, "t_part"),
        "t_resp": _task(client_a, 2, "t_resp"),
        "t_noresp": _task(client_a, 3, "t_noresp"),
        "t_other": _task(client_b, 3, "t_other"),
    }
    _activate(tasks["t_part"])
    _activate(tasks["t_resp"])
    _activate(tasks["t_noresp"], responsible=None)  # sem responsável (regra c)
    _activate(tasks["t_other"])
    return {
        "app": app,
        "session_factory": session_factory,
        "client_a": client_a,
        "client_b": client_b,
        "ids": ids,
        "tasks": tasks,
    }


def test_scope_participacao_ve_task_do_projeto(eflow):
    """(a) Participa do projeto → vê a task ativa mesmo sem repository_id."""
    data = eflow["client_a"].get("/api/execution").json()
    ids = {t["id"] for t in data["tasks"]}
    assert eflow["tasks"]["t_part"] in ids


def test_scope_responsavel_sem_participacao_ve_task(eflow):
    """(b) É o responsible_id → vê a task ativa mesmo sem participar do projeto."""
    data = eflow["client_a"].get("/api/execution").json()
    ids = {t["id"] for t in data["tasks"]}
    assert eflow["tasks"]["t_resp"] in ids


def test_scope_task_sem_responsavel_visivel(eflow):
    """(c) Task sem responsável → qualquer autenticado vê."""
    data = eflow["client_a"].get("/api/execution").json()
    ids = {t["id"] for t in data["tasks"]}
    assert eflow["tasks"]["t_noresp"] in ids


def test_scope_nao_vaza_task_de_terceiros(eflow):
    """Task de terceiro (responsável B, sem participação de A) não aparece."""
    data = eflow["client_a"].get("/api/execution").json()
    ids = {t["id"] for t in data["tasks"]}
    assert eflow["tasks"]["t_other"] not in ids
    assert ids == {
        eflow["tasks"]["t_part"],
        eflow["tasks"]["t_resp"],
        eflow["tasks"]["t_noresp"],
    }


def test_scope_sem_participacao_vê_responsaveis_e_sem_responsavel(eflow):
    """B não participa de nenhum projeto (repo_ids=[]): vê tasks de que é
    responsável e sem responsável; não vê tasks de A (participação/responsável)."""
    data = eflow["client_b"].get("/api/execution").json()
    ids = {t["id"] for t in data["tasks"]}
    assert eflow["tasks"]["t_other"] in ids  # responsible == B
    assert eflow["tasks"]["t_noresp"] in ids  # sem responsável
    assert eflow["tasks"]["t_part"] not in ids  # A resp., r1 (B não participa)
    assert eflow["tasks"]["t_resp"] not in ids  # A resp., r2


def test_scope_sem_participacao_sem_tasks_nao_ve_nada(settings, bare_repo):
    """Usuário autenticado sem participação e sem tasks de que seja responsável
    não vê tasks de terceiros: tasks == [] (in_([]) não casa nada)."""
    settings.auth_enabled = True
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    for email in ("a@ex.com", "c@ex.com"):
        client.post(
            "/api/users",
            json={"name": email.split("@")[0], "email": email, "password": "senha123", "role": "member"},
        )
    client.post(
        "/api/repositories", json={"name": "r1", "url": bare_repo, "default_branch": "main"}
    )
    client_a = TestClient(app)
    _login(client_a, "a@ex.com")
    resp = client_a.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    )
    assert resp.status_code == 201, resp.text
    with session_factory() as s:
        t = s.get(Task, resp.json()["id"])
        t.status = "queued"
        t.steps[0].status = "pending"
        s.commit()

    client_c = TestClient(app)
    _login(client_c, "c@ex.com")
    data = client_c.get("/api/execution").json()
    assert data["tasks"] == []


def test_scope_repository_id_explicito_preserva_filtro(eflow):
    """?repository_id=X → só tasks do projeto X (mesmo sem participação/responsável)."""
    data = eflow["client_a"].get("/api/execution?repository_id=3").json()
    ids = {t["id"] for t in data["tasks"]}
    assert ids == {eflow["tasks"]["t_noresp"], eflow["tasks"]["t_other"]}


def test_scope_auth_off_global(settings, bare_repo):
    """Auth OFF → sem repository_id permanece global (tasks de todos os projetos)."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    ids = []
    for i in (1, 2):
        resp = client.post(
            "/api/repositories", json={"name": f"r{i}", "url": bare_repo, "default_branch": "main"}
        )
        assert resp.status_code == 201, resp.text
        resp = client.post(
            "/api/tasks",
            json={"repository_id": i, "pipeline_id": 1, "title": f"t{i}", "description": "d", "kind": "feature"},
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["id"]
        with session_factory() as s:
            task = s.get(Task, task_id)
            task.status = "queued"
            task.steps[0].status = "pending"
            s.commit()
        ids.append(task_id)
    data = client.get("/api/execution").json()
    assert {t["id"] for t in data["tasks"]} == set(ids)


def test_scope_proposals_do_escopo(eflow):
    """Propostas pendentes: só as de tasks visíveis aparecem."""
    with eflow["session_factory"]() as s:
        t_resp = s.get(Task, eflow["tasks"]["t_resp"])
        s.add(TaskProposal(
            task_id=t_resp.id, step_id=t_resp.steps[0].id, position=0,
            title="proposta visível", description="d", kind="feature", status="pending",
        ))
        t_other = s.get(Task, eflow["tasks"]["t_other"])
        s.add(TaskProposal(
            task_id=t_other.id, step_id=t_other.steps[0].id, position=0,
            title="proposta invisível", description="d", kind="feature", status="pending",
        ))
        s.commit()

    data = eflow["client_a"].get("/api/execution").json()
    assert [p["title"] for p in data["proposals"]] == ["proposta visível"]


def test_scope_notices_do_escopo(eflow):
    """Avisos: tasks visíveis geram notice; de tasks não visíveis, não."""
    with eflow["session_factory"]() as s:
        for task_id in (eflow["tasks"]["t_resp"], eflow["tasks"]["t_other"]):
            t = s.get(Task, task_id)
            t.status = "needs_review"
            t.error = "falha"
        s.commit()

    data = eflow["client_a"].get("/api/execution").json()
    assert {n["task_id"] for n in data["notices"]} == {eflow["tasks"]["t_resp"]}


def test_scope_current_events_das_tasks_visiveis(eflow):
    """Eventos ao vivo: só fases running de tasks visíveis aparecem."""
    with eflow["session_factory"]() as s:
        t_resp = s.get(Task, eflow["tasks"]["t_resp"])
        step_resp = t_resp.steps[0]
        step_resp.status = "running"
        _system_event(s, step_resp, "attempt_started", {"attempt": 1})
        _system_event(s, step_resp, "assistant_text", {"content": "rodando…"})
        t_other = s.get(Task, eflow["tasks"]["t_other"])
        step_other = t_other.steps[0]
        step_other.status = "running"
        _system_event(s, step_other, "attempt_started", {"attempt": 1})
        _system_event(s, step_other, "assistant_text", {"content": "invisível"})
        s.commit()
        resp_step_id = step_resp.id
        other_step_id = step_other.id

    data = eflow["client_a"].get("/api/execution").json()
    assert str(resp_step_id) in data["current_events"]
    assert str(other_step_id) not in data["current_events"]


def test_scope_etag_invalida_ao_atualizar_task_do_escopo(eflow):
    """304 → atualizar task do escopo → próxima requisição não é mais 304."""
    first = eflow["client_a"].get("/api/execution")
    assert first.status_code == 200
    etag = first.headers["ETag"]
    assert (
        eflow["client_a"].get("/api/execution", headers={"If-None-Match": etag}).status_code
        == 304
    )

    with eflow["session_factory"]() as s:
        t = s.get(Task, eflow["tasks"]["t_resp"])
        t.status = "in_progress"
        s.commit()

    second = eflow["client_a"].get("/api/execution", headers={"If-None-Match": etag})
    assert second.status_code == 200
