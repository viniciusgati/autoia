"""Testes dos endpoints REST de skills de projeto (`/api/repositories/{id}/skills`).

Cobre: upload válido (admin global e admin do projeto), validações 400 com
mensagem específica, nome duplicado 409, 403 para não-admin em todos os
endpoints, `GET .../skills/{id}/file`, exclusão (disco + banco) e o
comportamento legado com `auth_enabled=False`.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.skills import MAX_SKILL_ZIP_BYTES

SKILL_MD_OK = "---\nname: minha-skill\ndescription: Skill de exemplo\n---\n# Conteúdo\n"


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def upload_files(zip_bytes: bytes, filename: str = "skill.zip") -> dict:
    return {"file": (filename, zip_bytes, "application/zip")}


@pytest.fixture
def admin_client(settings, bare_repo):
    """Auth ON: Ana (admin global bootstrap) + repositório criado."""
    settings.auth_enabled = True
    app = create_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"name": "Ana", "email": "ana@ex.com", "password": "senha123"},
    )
    assert resp.status_code == 201, resp.text
    repo = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert repo.status_code == 201, repo.text
    return client, repo.json()


def _create_user(client, name, email, role="member") -> dict:
    resp = client.post(
        "/api/users",
        json={"name": name, "email": email, "password": "senha456", "role": role},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_as(app, email) -> TestClient:
    client = TestClient(app)
    resp = client.post(
        "/api/auth/login",
        json={"email": email, "password": "senha456"},
    )
    assert resp.status_code == 200, resp.text
    return client


def _upload_skill(client, repo_id, zip_bytes, filename="skill.zip"):
    return client.post(
        f"/api/repositories/{repo_id}/skills",
        files=upload_files(zip_bytes, filename),
    )


# ---------------------------------------------------------------------------
# Caminho feliz: upload → 201 + arquivos no disco (admin global e do projeto)
# ---------------------------------------------------------------------------


def test_upload_valid_zip_returns_201_and_files_on_disk(settings, admin_client):
    client, repo = admin_client
    resp = _upload_skill(client, repo["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode(), "docs/guia.md": b"# guia\n"}))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "minha-skill"
    assert body["description"] == "Skill de exemplo"
    assert body["file_count"] == 2
    assert body["size_bytes"] == len(SKILL_MD_OK.encode()) + len(b"# guia\n")
    assert body["repository_id"] == repo["id"]

    skill_dir = Path(settings.skills_dir) / str(repo["id"]) / str(body["id"])
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "docs" / "guia.md").is_file()
    assert (skill_dir / "SKILL.md").read_text() == SKILL_MD_OK


def test_upload_by_repo_admin_not_global(settings, admin_client):
    """Member global com papel `admin` no projeto pode enviar skills."""
    client, repo = admin_client
    carlos = _create_user(client, "Carlos", "carlos@ex.com")
    member = client.post(
        f"/api/repositories/{repo['id']}/members",
        json={"user_id": carlos["id"], "role": "admin"},
    )
    assert member.status_code == 201, member.text

    carlos_client = _login_as(client.app, "carlos@ex.com")
    resp = _upload_skill(carlos_client, repo["id"], make_zip({"SKILL.md": b"---\nname: skill-carlos\n---\n"}), "carlos.zip")
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "skill-carlos"


def test_skills_endpoints_legacy_with_auth_off(settings, bare_repo):
    """`auth_enabled=False` (fixture padrão) preserva o comportamento legado."""
    app = create_app(settings)
    client = TestClient(app)
    repo = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert repo.status_code == 201, repo.text
    resp = _upload_skill(client, repo.json()["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode()}))
    assert resp.status_code == 201, resp.text
    skill_id = resp.json()["id"]
    assert client.get(f"/api/repositories/{repo.json()['id']}/skills").status_code == 200
    assert client.get(f"/api/repositories/{repo.json()['id']}/skills/{skill_id}/file").status_code == 200


# ---------------------------------------------------------------------------
# Validações de upload → 400 com mensagem específica
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zip_bytes", "message", "case_id"),
    [
        pytest.param(
            make_zip({"README.md": b"# sem skill"}),
            "ZIP inválido: falta SKILL.md na raiz",
            "sem-skill-md",
            id="sem-skill-md",
        ),
        pytest.param(
            make_zip({"SKILL.md": b"# x\n", "../evil.txt": b"x"}),
            "caminho inválido no zip",
            "path-traversal",
            id="path-traversal",
        ),
        pytest.param(
            make_zip({"SKILL.md": b"# x\n", "/etc/passwd": b"x"}),
            "caminho inválido no zip",
            "caminho-absoluto",
            id="caminho-absoluto",
        ),
        pytest.param(
            b"x" * (MAX_SKILL_ZIP_BYTES + 1),
            "arquivo muito grande (máx. 5 MB)",
            "muito-grande",
            id="muito-grande",
        ),
        pytest.param(
            b"not a zip",
            "ZIP inválido: arquivo não é um .zip válido",
            "nao-e-zip",
            id="nao-e-zip",
        ),
    ],
)
def test_upload_invalid_zip_returns_400_with_message(settings, admin_client, zip_bytes, message, case_id):
    client, repo = admin_client
    resp = _upload_skill(client, repo["id"], zip_bytes)
    assert resp.status_code == 400, resp.text
    assert message in resp.json()["detail"]
    # nada foi extraído nem registrado (o diretório do repo nem chega a ser criado)
    assert client.get(f"/api/repositories/{repo['id']}/skills").json() == []
    assert not (Path(settings.skills_dir) / str(repo["id"])).exists()


def test_upload_duplicate_name_returns_409(admin_client):
    client, repo = admin_client
    zip_bytes = make_zip({"SKILL.md": SKILL_MD_OK.encode()})
    assert _upload_skill(client, repo["id"], zip_bytes).status_code == 201
    resp = _upload_skill(client, repo["id"], zip_bytes)
    assert resp.status_code == 409, resp.text
    assert "já existe neste projeto" in resp.json()["detail"]
    # a skill original permanece intacta
    assert len(client.get(f"/api/repositories/{repo['id']}/skills").json()) == 1


# ---------------------------------------------------------------------------
# Permissão: não-admin do projeto (e sem admin global) → 403 em todos
# ---------------------------------------------------------------------------


@pytest.fixture
def non_admin_client(settings, admin_client):
    """Bob: member global + papel `member` no projeto, autenticado."""
    client, repo = admin_client
    bob = _create_user(client, "Bob", "bob@ex.com")
    member = client.post(
        f"/api/repositories/{repo['id']}/members",
        json={"user_id": bob["id"], "role": "member"},
    )
    assert member.status_code == 201, member.text
    # uma skill já existente (criada por Ana) para testar GET file e DELETE
    skill = _upload_skill(client, repo["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode()}))
    assert skill.status_code == 201
    bob_client = _login_as(client.app, "bob@ex.com")
    return bob_client, repo, skill.json()["id"]


def test_non_admin_forbidden_on_all_skill_endpoints(non_admin_client):
    bob_client, repo, skill_id = non_admin_client
    zip_bytes = make_zip({"SKILL.md": b"---\nname: outra\n---\n"})
    for method, url, kwargs in (
        ("get", f"/api/repositories/{repo['id']}/skills", {}),
        ("post", f"/api/repositories/{repo['id']}/skills", {"files": upload_files(zip_bytes)}),
        ("get", f"/api/repositories/{repo['id']}/skills/{skill_id}/file", {}),
        ("delete", f"/api/repositories/{repo['id']}/skills/{skill_id}", {}),
    ):
        resp = getattr(bob_client, method)(url, **kwargs)
        assert resp.status_code == 403, f"{method} {url} → {resp.status_code}"
        assert "admin" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET .../skills/{id}/file e DELETE (disco + banco)
# ---------------------------------------------------------------------------


def test_get_skill_file_returns_skill_md_content(settings, admin_client):
    client, repo = admin_client
    skill = _upload_skill(client, repo["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode()}))
    assert skill.status_code == 201
    resp = client.get(f"/api/repositories/{repo['id']}/skills/{skill.json()['id']}/file")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == SKILL_MD_OK


def test_get_skill_file_404_for_unknown_or_other_repo(settings, admin_client):
    client, repo = admin_client
    skill = _upload_skill(client, repo["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode()}))
    skill_id = skill.json()["id"]
    assert client.get(f"/api/repositories/{repo['id']}/skills/999999/file").status_code == 404
    assert client.get(f"/api/repositories/999999/skills/{skill_id}/file").status_code == 404


def test_delete_skill_removes_dir_and_row(settings, admin_client):
    client, repo = admin_client
    skill = _upload_skill(client, repo["id"], make_zip({"SKILL.md": SKILL_MD_OK.encode(), "extra.txt": b"x"}))
    skill_id = skill.json()["id"]
    skill_dir = Path(settings.skills_dir) / str(repo["id"]) / str(skill_id)
    assert skill_dir.is_dir()

    resp = client.delete(f"/api/repositories/{repo['id']}/skills/{skill_id}")
    assert resp.status_code == 204, resp.text
    assert not skill_dir.exists()  # disco removido
    assert client.get(f"/api/repositories/{repo['id']}/skills/{skill_id}/file").status_code == 404
    assert client.get(f"/api/repositories/{repo['id']}/skills").json() == []
    # excluir de novo → 404 (registro já removido do banco)
    assert client.delete(f"/api/repositories/{repo['id']}/skills/{skill_id}").status_code == 404
