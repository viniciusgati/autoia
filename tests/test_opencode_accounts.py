"""Testes das contas opencode-go: roster global CRUD + pin por repositório +
resolução e materialização (auth.json legado + XDG_DATA_HOME no executor opencode)."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import OpenCodeAccount, Repository
from app.opencode_accounts import (
    account_dir,
    auth_json_path,
    effective_account_dir,
    sanitize_name,
)
from app.worker import sandbox as sb
from app.worker.exec_common import build_spawn_command


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "workspaces"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        opencode_accounts_dir=str(tmp_path / "opencode-accounts"),
        kimi_bin="kimi",
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


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def session_factory(settings):
    from app.db import ensure_schema

    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    return make_session_factory(engine)


def _create_account(client, name="conta-a", token="tk-123", is_default=False, **kw):
    resp = client.post(
        "/api/system/opencode-accounts",
        json={"name": name, "token": token, "is_default": is_default, **kw},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_crud_opencode_account(client, settings):
    acc = _create_account(client)
    assert acc["name"] == "conta-a"
    assert acc["has_token"] is True
    assert "token" not in acc  # o token nunca é exposto

    # Lista sem token e com has_token.
    listed = client.get("/api/system/opencode-accounts").json()
    assert [a["name"] for a in listed] == ["conta-a"]
    assert all("token" not in a for a in listed)

    # Atualiza token + descrição.
    upd = client.put(
        f"/api/system/opencode-accounts/{acc['id']}",
        json={"token": "tk-novo", "description": "conta de produção"},
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["description"] == "conta de produção"
    assert body["has_token"] is True

    # Delete apaga o roster e o disco.
    assert client.delete(f"/api/system/opencode-accounts/{acc['id']}").status_code == 204
    assert client.get("/api/system/opencode-accounts").json() == []


def test_create_conta_invalida_400(client):
    # Espaço no nome seria usado como pasta em disco — recusado.
    resp = client.post(
        "/api/system/opencode-accounts",
        json={"name": "conta invalida", "token": "tk"},
    )
    assert resp.status_code == 400
    assert "só pode ter letras" in resp.text


def test_create_duplicada_400(client):
    _create_account(client, name="dup")
    resp = client.post(
        "/api/system/opencode-accounts",
        json={"name": "dup", "token": "tk"},
    )
    assert resp.status_code == 400


def test_default_exclusivo(client):
    a = _create_account(client, name="a", is_default=True)
    b = _create_account(client, name="b", is_default=True)
    # Marcar b como default remove o default de a.
    names = {x["name"]: x["is_default"] for x in client.get("/api/system/opencode-accounts").json()}
    assert names == {"a": False, "b": True}
    # Voltar a default remove de b.
    put = client.put(f"/api/system/opencode-accounts/{a['id']}", json={"is_default": True})
    assert put.status_code == 200
    names = {x["name"]: x["is_default"] for x in client.get("/api/system/opencode-accounts").json()}
    assert names == {"a": True, "b": False}


def test_materializacao_auth_json(client, settings):
    _create_account(client, name="prod", token="tk-prod")
    path = auth_json_path(settings, "prod")
    assert os.path.isfile(path)
    data = json.load(open(path))
    assert data == {"opencode-go": {"type": "api", "key": "tk-prod"}}
    # Atualizar o token regrava o arquivo.
    acc = client.get("/api/system/opencode-accounts").json()[0]
    client.put(f"/api/system/opencode-accounts/{acc['id']}", json={"token": "tk-2"})
    data = json.load(open(path))
    assert data["opencode-go"]["key"] == "tk-2"
    # Delete remove o diretório materializado.
    client.delete(f"/api/system/opencode-accounts/{acc['id']}")
    assert not os.path.isdir(account_dir(settings, "prod"))


def test_repository_pin_requer_conta_existente(client, bare_repo):
    _create_account(client, name="acct")
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main", "opencode_account": "acct"},
    )
    assert resp.status_code == 201, resp.text
    repo_id = resp.json()["id"]

    # Pin inexistente no update → 400.
    bad = client.put(
        f"/api/repositories/{repo_id}",
        json={"opencode_account": "nao-existe"},
    )
    assert bad.status_code == 400
    assert "não existe no roster" in bad.text

    # Pin inválido no create → 400.
    bad_create = client.post(
        "/api/repositories",
        json={"name": "r2", "url": bare_repo, "default_branch": "main", "opencode_account": "ghost"},
    )
    assert bad_create.status_code == 400


def test_delete_conta_desfixa_repositorios(client, bare_repo, settings, session_factory):
    acc = _create_account(client, name="acct")
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main", "opencode_account": "acct"},
    )
    assert resp.status_code == 201, resp.text

    client.delete(f"/api/system/opencode-accounts/{acc['id']}")
    with session_factory() as s:
        repo = s.get(Repository, resp.json()["id"])
        assert repo is not None
        assert repo.opencode_account is None


def test_effective_account_dir_resolucao(settings, session_factory):
    with session_factory() as s:
        s.add(OpenCodeAccount(name="default", token="tk-d", description="", is_default=True))
        s.add(OpenCodeAccount(name="pinada", token="tk-p", description="", is_default=False))
        s.flush()
        repo_sem_pin = Repository(
            name="rs", url="http://nohost/x.git", default_branch="main", local_path=None,
        )
        repo_pinada = Repository(
            name="rp", url="http://nohost/x.git", default_branch="main", local_path=None,
            opencode_account="pinada",
        )
        s.add_all([repo_sem_pin, repo_pinada])
        s.commit()

        # Sem pin → default materializada.
        d = effective_account_dir(settings, s, repo_sem_pin)
        assert d == account_dir(settings, "default")
        with open(auth_json_path(settings, "default")) as fh:
            assert json.load(fh)["opencode-go"]["key"] == "tk-d"

        # Com pin → conta pinada vence a default.
        d = effective_account_dir(settings, s, repo_pinada)
        assert d == account_dir(settings, "pinada")
        with open(auth_json_path(settings, "pinada")) as fh:
            assert json.load(fh)["opencode-go"]["key"] == "tk-p"

        # Sem contas → None (conta do host).
        s.query(OpenCodeAccount).delete()
        s.flush()
        assert effective_account_dir(settings, s, repo_pinada) is None


def test_effective_runner_define_opencode_account_dir(settings, session_factory):
    """`_effective` do runner resolve a conta para o repositório e a entrega como
    `opencode_account_dir` (input do executor)."""
    from app.worker.runner import _effective

    with session_factory() as s:
        s.add(OpenCodeAccount(name="acct", token="tk", description="", is_default=True))
        repo = Repository(
            name="r", url="http://nohost/x.git", default_branch="main", local_path=None,
        )
        s.add(repo)
        s.commit()
        eff = _effective(settings, repo, s)
        assert eff.opencode_account_dir == account_dir(settings, "acct")
        assert os.path.isfile(auth_json_path(settings, "acct"))
        # Sem sessão → sem resolução (rotas de leitura não tocam o disco).
        eff2 = _effective(settings, repo)
        assert eff2.opencode_account_dir is None


def test_spawn_off_injeta_xdg_data_home(tmp_path):
    """Modo off: o env do Popen ganha XDG_DATA_HOME da conta."""
    adir = str(tmp_path / "accounts" / "prod")
    os.makedirs(os.path.join(adir, "opencode"), exist_ok=True)
    sandbox = sb.SandboxConfig(mode="off", opencode_account_dir=adir)
    cmd, env = build_spawn_command(
        ["opencode", "run", "x"],
        cwd=str(tmp_path),
        sandbox=sandbox,
        cli_bin="opencode",
        workspace_dir=str(tmp_path),
        extra_env={"XDG_DATA_HOME": adir},
    )
    assert env is not None
    assert env.get("XDG_DATA_HOME") == adir


def test_spawn_sandbox_monta_conta_e_env(tmp_path):
    """Modo fs: o conta é montada rw e XDG_DATA_HOME entra no env do container."""
    adir = str(tmp_path / "accounts" / "prod")
    os.makedirs(os.path.join(adir, "opencode"), exist_ok=True)
    checkout = str(tmp_path / "checkout")
    os.makedirs(checkout, exist_ok=True)
    sandbox = sb.SandboxConfig(mode="fs", image="img", opencode_account_dir=adir)
    cmd, env = build_spawn_command(
        ["opencode", "run", "x"],
        cwd=checkout,
        sandbox=sandbox,
        cli_bin="opencode",
        workspace_dir=str(tmp_path / "ws"),
        extra_env={"XDG_DATA_HOME": adir},
    )
    assert cmd[0].endswith("docker")
    assert f"{adir}:{adir}:rw" in cmd
    assert f"XDG_DATA_HOME={adir}" in cmd


def test_bwrap_monta_conta_e_env(tmp_path):
    adir = str(tmp_path / "accounts" / "prod")
    os.makedirs(os.path.join(adir, "opencode"), exist_ok=True)
    checkout = str(tmp_path / "checkout")
    os.makedirs(checkout, exist_ok=True)
    cmd = sb.build_bwrap_command(
        ["opencode", "run", "x"],
        checkout=checkout,
        workspace_dir=str(tmp_path / "ws"),
        cli_bin="opencode",
        home=str(tmp_path / "home"),
        extra_env={"XDG_DATA_HOME": adir},
        opencode_account_dir=adir,
    )
    assert cmd[0] == "bwrap"
    # A conta entra como bind rw na árvore de mounts (o primeiro --bind é o checkout).
    binds = [(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == "--bind"]
    assert (adir, adir) in binds
    envs = {(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == "--setenv"}
    assert ("XDG_DATA_HOME", adir) in envs


def test_sanitize_name():
    assert sanitize_name("conta-prod_1") == "conta-prod_1"
    for bad in ("conta /x", "a/b", "", "..", "conta com espaço"):
        with pytest.raises(ValueError):
            sanitize_name(bad)