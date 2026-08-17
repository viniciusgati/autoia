"""Testes da API de configuração geral do sistema (`/api/system/storage`).

Cobertura: relatório via HTTP, limpeza via HTTP (fixture `settings(tmp_path)` +
tasks semeadas), preservação integral de workspaces de tasks ativas, 403 para
não-admin, 401 sem sessão com auth ON e 400 para alvo desconhecido (PT-BR).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import TASK_DONE, TASK_IN_PROGRESS, TASK_QUEUED, Pipeline, Repository, Task


def _build_fs(settings, tmp_path) -> None:
    """Árvore fake (logs/workspaces/junk/skills) — o banco é criado pelo app.

    Tamanhos esperados: logs 1 item/10 B; workspaces 2 itens/110 B;
    test_junk 4 itens/70 B; skills 2 itens/67 B.
    """
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "app.log").write_bytes(b"a" * 10)
    (log_dir / "notes.txt").write_text("nota")
    (log_dir / "sub").mkdir(exist_ok=True)
    (log_dir / "sub" / "inner.log").write_bytes(b"i")

    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    (ws1 / "src").mkdir(parents=True, exist_ok=True)
    (ws1 / "src" / "main.py").write_bytes(b"x" * 40)
    (ws1 / ".pytest-tmp").mkdir(exist_ok=True)
    (ws1 / ".pytest-tmp" / "f.bin").write_bytes(b"j" * 20)
    (ws1 / "data" / "smoke").mkdir(parents=True, exist_ok=True)
    (ws1 / "data" / "smoke" / "s.png").write_bytes(b"s" * 30)
    ws2 = Path(settings.workspace_dir) / "2" / "task_20"
    (ws2 / "chrome-profile").mkdir(parents=True, exist_ok=True)
    (ws2 / "chrome-profile" / "c").write_bytes(b"c" * 15)
    (ws2 / ".smoke-chrome-extra").mkdir(exist_ok=True)
    (ws2 / ".smoke-chrome-extra" / "e").write_bytes(b"e" * 5)

    skills = Path(settings.skills_dir)
    (skills / "skill_a").mkdir(parents=True, exist_ok=True)
    (skills / "skill_a" / "SKILL.md").write_bytes(b"m" * 60)
    (skills / "skill_b.md").write_bytes(b"b" * 7)


def _seed_task(app, task_id: int, status: str) -> None:
    """Insere repo + pipeline + task no banco do app (sem rodar o worker)."""
    with app.state.Session() as s:
        repo = Repository(
            name=f"repo-{task_id}",
            url=f"file:///repo-{task_id}",
            default_branch="main",
        )
        s.add(repo)
        s.flush()
        pipe = Pipeline(name=f"pipeline-{task_id}")
        s.add(pipe)
        s.flush()
        s.add(
            Task(
                id=task_id,
                repository_id=repo.id,
                pipeline_id=pipe.id,
                title="t",
                description="d",
                status=status,
            )
        )
        s.commit()


# ---------- GET /api/system/storage ----------


def test_get_storage_report_via_http(settings, tmp_path):
    _build_fs(settings, tmp_path)
    client = TestClient(create_app(settings))

    resp = client.get("/api/system/storage")

    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body["categories"]] == [
        "database",
        "logs",
        "workspaces",
        "test_junk",
        "skills",
    ]
    by_id = {c["id"]: c for c in body["categories"]}

    # banco: medido (o app cria um sqlite real em settings.database_url),
    # nunca limpável.
    assert by_id["database"]["cleanable"] is False
    assert by_id["database"]["item_count"] >= 1
    assert by_id["database"]["size_bytes"] > 0

    assert by_id["logs"] == {
        "id": "logs",
        "label": "Logs",
        "size_bytes": 10,
        "item_count": 1,
        "cleanable": True,
    }
    assert by_id["workspaces"] == {
        "id": "workspaces",
        "label": "Workspaces",
        "size_bytes": 110,
        "item_count": 2,
        "cleanable": False,
    }
    assert by_id["test_junk"] == {
        "id": "test_junk",
        "label": "Lixo de teste",
        "size_bytes": 70,
        "item_count": 4,
        "cleanable": True,
    }
    assert by_id["skills"] == {
        "id": "skills",
        "label": "Skills",
        "size_bytes": 67,
        "item_count": 2,
        "cleanable": False,
    }
    assert body["total_bytes"] == sum(c["size_bytes"] for c in body["categories"])


def test_get_storage_empty_state(settings):
    """Sem dados além do próprio banco: categorias não-database zeradas."""
    client = TestClient(create_app(settings))
    resp = client.get("/api/system/storage")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body["categories"]] == [
        "database",
        "logs",
        "workspaces",
        "test_junk",
        "skills",
    ]
    by_id = {c["id"]: c for c in body["categories"]}
    assert by_id["database"]["item_count"] >= 1  # o sqlite existe sempre
    for cat_id in ("logs", "workspaces", "test_junk", "skills"):
        assert by_id[cat_id]["item_count"] == 0
        assert by_id[cat_id]["size_bytes"] == 0


# ---------- POST /api/system/storage/clean ----------


def test_clean_via_http(settings, tmp_path):
    _build_fs(settings, tmp_path)
    app = create_app(settings)
    _seed_task(app, 10, TASK_DONE)
    _seed_task(app, 20, TASK_DONE)
    client = TestClient(app)

    resp = client.post(
        "/api/system/storage/clean",
        json={"targets": ["pytest_tmp", "smoke", "chrome_profiles"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    targets = {t["target"]: t for t in body["targets"]}
    assert targets["pytest_tmp"] == {"target": "pytest_tmp", "item_count": 1, "bytes_freed": 20}
    assert targets["smoke"] == {"target": "smoke", "item_count": 1, "bytes_freed": 30}
    assert targets["chrome_profiles"] == {
        "target": "chrome_profiles",
        "item_count": 2,
        "bytes_freed": 20,
    }
    assert body["total_bytes_freed"] == 70

    # artefatos realmente removidos; resto do workspace preservado.
    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    assert not (ws1 / ".pytest-tmp").exists()
    assert not (ws1 / "data" / "smoke").exists()
    assert (ws1 / "src" / "main.py").is_file()
    assert ws1.is_dir()

    # relatório atualizado reflete a remoção.
    by_id = {c["id"]: c for c in body["report"]["categories"]}
    assert by_id["test_junk"]["item_count"] == 0
    assert by_id["test_junk"]["size_bytes"] == 0
    assert by_id["workspaces"]["item_count"] == 2
    assert by_id["workspaces"]["size_bytes"] == 40  # só main.py restou


def test_clean_via_http_includes_logs(settings, tmp_path):
    app = create_app(settings)
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    old = log_dir / "old.log"
    old.write_bytes(b"log")
    past = 40 * 86400
    import os
    import time

    os.utime(old, (time.time() - past, time.time() - past))
    client = TestClient(app)

    resp = client.post("/api/system/storage/clean", json={"targets": ["logs"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["targets"] == [{"target": "logs", "item_count": 1, "bytes_freed": 3}]
    assert not old.exists()


def test_clean_preserves_active_task_workspace_via_http(settings, tmp_path):
    _build_fs(settings, tmp_path)
    app = create_app(settings)
    _seed_task(app, 10, TASK_QUEUED)
    _seed_task(app, 20, TASK_IN_PROGRESS)
    client = TestClient(app)

    resp = client.post(
        "/api/system/storage/clean",
        json={"targets": ["pytest_tmp", "smoke", "chrome_profiles"]},
    )

    assert resp.status_code == 200
    assert resp.json()["total_bytes_freed"] == 0  # preservação integral
    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    assert (ws1 / ".pytest-tmp").is_dir()
    assert (ws1 / "data" / "smoke").is_dir()
    ws2 = Path(settings.workspace_dir) / "2" / "task_20"
    assert (ws2 / "chrome-profile").is_dir()
    assert (ws2 / ".smoke-chrome-extra").is_dir()
    # o relatório pós-clean ainda mostra o lixo preservado.
    by_id = {c["id"]: c for c in resp.json()["report"]["categories"]}
    assert by_id["test_junk"]["item_count"] == 4
    assert by_id["test_junk"]["size_bytes"] == 70


def test_clean_never_removes_database_via_http(settings, tmp_path):
    app = create_app(settings)
    client = TestClient(app)
    db = Path(tmp_path / "autoia.db")

    resp = client.post(
        "/api/system/storage/clean",
        json={"targets": ["pytest_tmp", "smoke", "chrome_profiles", "logs"]},
    )

    assert resp.status_code == 200
    assert db.is_file()  # banco jamais removido
    assert resp.json()["report"]["categories"][0]["id"] == "database"


# ---------- Autorização e validação ----------


def test_clean_unknown_target_400_ptbr(settings, tmp_path):
    client = TestClient(create_app(settings))

    resp = client.post("/api/system/storage/clean", json={"targets": ["banana"]})

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "desconhecido" in detail
    assert "banana" in detail


def test_clean_requires_admin_403(settings):
    settings.auth_enabled = True
    client = TestClient(create_app(settings))
    assert (
        client.post(
            "/api/auth/register",
            json={"name": "Admin", "email": "admin@ex.com", "password": "senha123"},
        ).status_code
        == 201
    )
    # cria um membro (não-admin) via API como admin
    assert (
        client.post(
            "/api/users",
            json={"name": "Ana", "email": "ana@ex.com", "password": "senha123", "role": "member"},
        ).status_code
        == 201
    )
    login = client.post(
        "/api/auth/login", json={"email": "ana@ex.com", "password": "senha123"}
    )
    assert login.status_code == 200

    resp = client.post("/api/system/storage/clean", json={"targets": ["logs"]})
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]


def test_get_storage_requires_auth_401(settings):
    settings.auth_enabled = True
    client = TestClient(create_app(settings))

    resp = client.get("/api/system/storage")
    assert resp.status_code == 401
