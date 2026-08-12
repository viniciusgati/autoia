"""Testes de autenticação: bootstrap, login/logout/me e a flag `auth_enabled`.

Com `settings(auth_enabled=False)` (fixture padrão do conftest) a suíte antiga
permanece sem sessão; estes testes ativam a flag explicitamente para validar o
fluxo de cookie, exceto onde o objetivo é justamente o comportamento OFF.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Session as AuthSession


@pytest.fixture
def auth_client(settings):
    """Client com auth ON (usuário bootstrap já registrado)."""
    settings.auth_enabled = True
    app = create_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"name": "Ana", "email": "ana@ex.com", "password": "senha123"},
    )
    assert resp.status_code == 201, resp.text
    return client


def _login(client, email="ana@ex.com", password="senha123"):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )


# ---------- Bootstrap (register) ----------


def test_register_bootstrap_creates_admin_and_cookie(settings):
    settings.auth_enabled = True
    app = create_app(settings)
    client = TestClient(app)

    resp = client.post(
        "/api/auth/register",
        json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Admin"
    assert body["email"] == "admin@ex.com"
    assert body["role"] == "admin"
    assert body["active"] is True

    cookie = resp.headers.get("set-cookie", "")
    assert "autoia_session=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()

    # a sessão criada está no banco e a senha foi hasheada (nunca em texto puro)
    with app.state.Session() as s:
        assert s.query(AuthSession).count() == 1
        from app.models import User

        u = s.query(User).filter(User.email == "admin@ex.com").first()
        assert u is not None
        assert u.password_hash != "senha123"
        assert u.password_hash.startswith("pbkdf2_sha256$200000$")


def test_register_with_existing_users_returns_403(auth_client):
    resp = auth_client.post(
        "/api/auth/register",
        json={"name": "Outro", "email": "outro@ex.com", "password": "senha456"},
    )
    assert resp.status_code == 403
    assert "bootstrap" in resp.json()["detail"]


# ---------- Login / me / logout ----------


def test_login_wrong_password_returns_401(auth_client):
    resp = _login(auth_client, password="errada")
    assert resp.status_code == 401
    assert "inválidos" in resp.json()["detail"]


def test_login_unknown_email_returns_401(auth_client):
    resp = _login(auth_client, email="ninguem@ex.com")
    assert resp.status_code == 401


def test_login_ok_returns_user_and_cookie(auth_client):
    resp = _login(auth_client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "ana@ex.com"
    assert "password" not in body
    cookie = resp.headers.get("set-cookie", "")
    assert "autoia_session=" in cookie
    assert "HttpOnly" in cookie


def test_me_validates_session(auth_client):
    # sem cookie → 401
    fresh = TestClient(auth_client.app)
    assert fresh.get("/api/auth/me").status_code == 401

    # com cookie (login) → usuário
    resp = _login(auth_client)
    me = auth_client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "ana@ex.com"


def test_logout_removes_session_and_cookie(auth_client):
    # client limpo: login cria UMA sessão nova (a do register do auth_client fica)
    fresh = TestClient(auth_client.app)
    with fresh.app.state.Session() as s:
        before = s.query(AuthSession).count()
    resp = _login(fresh)
    assert resp.status_code == 200, resp.text
    assert fresh.get("/api/auth/me").status_code == 200

    resp = fresh.post("/api/auth/logout")
    assert resp.status_code == 204, resp.text

    # sessão de login apagada do banco (count volta ao baseline), cookie limpo
    # (delete_cookie → valor vazio) e /me sem sessão → 401
    with fresh.app.state.Session() as s:
        assert s.query(AuthSession).count() == before
    assert 'autoia_session=""' in resp.headers.get("set-cookie", "")
    assert fresh.get("/api/auth/me").status_code == 401


def test_inactive_user_cannot_login(settings):
    settings.auth_enabled = True
    app = create_app(settings)
    client = TestClient(app)
    client.post(
        "/api/auth/register",
        json={"name": "Ana", "email": "ana@ex.com", "password": "senha123"},
    )
    from app.models import User

    with app.state.Session() as s:
        u = s.query(User).filter(User.email == "ana@ex.com").first()
        u.active = False
        s.commit()

    resp = _login(client)
    assert resp.status_code == 403


# ---------- Flag auth_enabled: proteção das rotas /api/* ----------


def test_auth_on_requires_session_on_all_api_routes(auth_client):
    """Com auth ON, rota /api/* sem cookie responde 401."""
    fresh = TestClient(auth_client.app)  # sem cookies
    for path in ("/api/repositories", "/api/dashboard", "/api/tasks", "/api/execution"):
        assert fresh.get(path).status_code == 401, path
    # login + cookie → 200
    _login(auth_client)
    assert auth_client.get("/api/repositories").status_code == 200


def test_auth_off_allows_anonymous(settings, bare_repo):
    """Com auth OFF (fixture padrão), as rotas funcionam sem sessão."""
    app = create_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    # auth config expõe o estado para o frontend decidir a tela inicial
    assert client.get("/api/auth/config").json() == {"enabled": False}


def test_auth_config_reports_enabled(auth_client):
    assert auth_client.get("/api/auth/config").json() == {"enabled": True}


# ---------- Gestão de usuários (admin global) ----------


def test_users_management_requires_admin(auth_client):
    # cria um usuário COMUM via admin (o bootstrap é admin) e loga como ele
    bob = auth_client.post(
        "/api/users",
        json={"name": "Bob", "email": "bob@ex.com", "password": "senha456", "role": "member"},
    )
    assert bob.status_code == 201, bob.text
    member_client = TestClient(auth_client.app)
    login = member_client.post(
        "/api/auth/login",
        json={"email": "bob@ex.com", "password": "senha456"},
    )
    assert login.status_code == 200, login.text

    # usuário comum não pode gerenciar usuários
    resp = member_client.get("/api/users")
    assert resp.status_code == 403, resp.text
    assert "admin" in resp.json()["detail"]


def test_admin_creates_lists_and_updates_users(auth_client):
    # o bootstrap é admin global; o endpoint de criação aceita member/admin
    created = auth_client.post(
        "/api/users",
        json={"name": "Bob", "email": "bob@ex.com", "password": "senha456", "role": "member"},
    )
    assert created.status_code == 201, created.text
    bob = created.json()
    assert bob["role"] == "member"

    users = auth_client.get("/api/users").json()
    emails = {u["email"] for u in users}
    assert {"ana@ex.com", "bob@ex.com"} <= emails

    patched = auth_client.patch(
        f"/api/users/{bob['id']}",
        json={"role": "admin", "active": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["role"] == "admin"
    assert patched.json()["active"] is False

    # e-mail duplicado → 409
    dup = auth_client.post(
        "/api/users",
        json={"name": "Cópia", "email": "bob@ex.com", "password": "senha456"},
    )
    assert dup.status_code == 409
