"""Testes do módulo `app.storage` (medição e limpeza de armazenamento).

Usam `Settings` apontando para `tmp_path` (fixture do conftest): árvore fake
nas 5 categorias, tasks semeadas num sqlite real para a leitura de
`Task.status`, e assertivas exatas de tamanho/contagem conforme a definição de
item da história (banco com/sem `-wal`/`-shm`, logs não recursivos, 1 item por
workspace `task_*`, 1 item por artefato órfão, 1 item por entrada de skills).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import (
    TASK_DONE,
    TASK_IN_PROGRESS,
    TASK_QUEUED,
    Pipeline,
    Repository,
    Task,
)
from app.storage import (
    CLEAN_TARGET_IDS,
    InvalidTargetError,
    _workspace_tasks,
    clean_storage,
    scan_storage,
)


def _memory_session():
    """Sessão num sqlite em memória (para leitura de Task.status quando o
    `autoia.db` é um arquivo fake de teste — o scan nunca o consulta)."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _session(settings):
    """Sessão no MESMO banco de `settings` (para seeds reais de Task)."""
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _seed_task(session, task_id: int, status: str) -> None:
    """Insere repo + pipeline + task com o id/status pedidos (sem rodar o worker)."""
    repo = Repository(
        name=f"repo-{task_id}",
        url=f"file:///repo-{task_id}",
        default_branch="main",
    )
    session.add(repo)
    session.flush()
    pipe = Pipeline(name=f"pipeline-{task_id}")
    session.add(pipe)
    session.flush()
    session.add(
        Task(
            id=task_id,
            repository_id=repo.id,
            pipeline_id=pipe.id,
            title="t",
            description="d",
            status=status,
        )
    )
    session.commit()


def _build_tree(tmp_path, settings, with_db_files: bool = True) -> None:
    """Árvore fake determinística nas 5 categorias.

    Tamanhos esperados (bytes): database 175 (3 itens), logs 10 (1 item),
    workspaces 110 (2 itens), test_junk 70 (4 itens), skills 67 (2 itens).
    """
    if with_db_files:
        (tmp_path / "autoia.db").write_bytes(b"d" * 100)
        (tmp_path / "autoia.db-wal").write_bytes(b"w" * 50)
        (tmp_path / "autoia.db-shm").write_bytes(b"s" * 25)

    # logs: só arquivos `.log` do nível superior contam.
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "app.log").write_bytes(b"a" * 10)
    (log_dir / "notes.txt").write_text("nota")
    (log_dir / "sub").mkdir(exist_ok=True)
    (log_dir / "sub" / "inner.log").write_bytes(b"i")

    # workspaces task_10 e task_20, cada um com lixo de teste.
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

    # skills: entrada-diretório + entrada-arquivo.
    skills = Path(settings.skills_dir)
    (skills / "skill_a").mkdir(parents=True, exist_ok=True)
    (skills / "skill_a" / "SKILL.md").write_bytes(b"m" * 60)
    (skills / "skill_b.md").write_bytes(b"b" * 7)


# ---------- scan_storage ----------


def test_scan_measures_all_categories(settings, tmp_path):
    _build_tree(tmp_path, settings)
    report = scan_storage(settings, _memory_session())

    assert [c.id for c in report.categories] == [
        "database",
        "logs",
        "workspaces",
        "test_junk",
        "skills",
    ]
    by_id = {c.id: c for c in report.categories}

    assert by_id["database"].item_count == 3
    assert by_id["database"].size_bytes == 175
    assert by_id["database"].cleanable is False

    assert by_id["logs"].item_count == 1  # `sub/inner.log` e `notes.txt` não contam
    assert by_id["logs"].size_bytes == 10
    assert by_id["logs"].cleanable is True

    assert by_id["workspaces"].item_count == 2
    assert by_id["workspaces"].size_bytes == 110  # soma recursiva dos 2 checkouts
    assert by_id["workspaces"].cleanable is False

    assert by_id["test_junk"].item_count == 4  # 1 item por artefato órfão
    assert by_id["test_junk"].size_bytes == 70
    assert by_id["test_junk"].cleanable is True

    assert by_id["skills"].item_count == 2
    assert by_id["skills"].size_bytes == 67
    assert by_id["skills"].cleanable is False

    assert report.total_bytes == 175 + 10 + 110 + 70 + 67


def test_scan_db_without_wal_shm_counts_one(settings, tmp_path):
    (tmp_path / "autoia.db").write_bytes(b"x" * 5)
    report = scan_storage(settings, _memory_session())
    by_id = {c.id: c for c in report.categories}
    assert by_id["database"].item_count == 1
    assert by_id["database"].size_bytes == 5


def test_scan_empty_dirs_yield_zero(settings):
    report = scan_storage(settings, _memory_session())
    assert report.total_bytes == 0
    assert all(c.item_count == 0 for c in report.categories)


def test_scan_counts_orphan_workspace_without_task_in_db(settings, tmp_path):
    ws = Path(settings.workspace_dir) / "3" / "task_99"
    (ws / ".pytest-tmp").mkdir(parents=True)
    (ws / ".pytest-tmp" / "o").write_bytes(b"o" * 12)

    report = scan_storage(settings, _memory_session())
    by_id = {c.id: c for c in report.categories}
    assert by_id["workspaces"].item_count == 1
    assert by_id["workspaces"].size_bytes == 12
    assert by_id["test_junk"].item_count == 1
    assert by_id["test_junk"].size_bytes == 12


# ---------- clean_storage ----------


def test_clean_removes_requested_targets_and_reports(settings, tmp_path):
    _build_tree(tmp_path, settings, with_db_files=False)
    session = _session(settings)
    _seed_task(session, 10, TASK_DONE)
    _seed_task(session, 20, TASK_DONE)

    result = clean_storage(settings, session, ["pytest_tmp", "smoke", "chrome_profiles"])

    # tasks não ativas → lixo removido; resto do workspace preservado.
    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    assert not (ws1 / ".pytest-tmp").exists()
    assert not (ws1 / "data" / "smoke").exists()
    assert (ws1 / "src" / "main.py").is_file()
    ws2 = Path(settings.workspace_dir) / "2" / "task_20"
    assert not (ws2 / "chrome-profile").exists()
    assert not (ws2 / ".smoke-chrome-extra").exists()
    assert ws1.is_dir() and ws2.is_dir()  # workspaces inteiros nunca somem

    targets = {r.target: r for r in result.targets}
    assert targets["pytest_tmp"].item_count == 1
    assert targets["pytest_tmp"].bytes_freed == 20
    assert targets["smoke"].item_count == 1
    assert targets["smoke"].bytes_freed == 30
    assert targets["chrome_profiles"].item_count == 2
    assert targets["chrome_profiles"].bytes_freed == 15 + 5
    assert result.total_bytes_freed == 70

    # relatório atualizado reflete a remoção.
    by_id = {c.id: c for c in result.report.categories}
    assert by_id["test_junk"].item_count == 0
    assert by_id["test_junk"].size_bytes == 0
    assert by_id["workspaces"].item_count == 2
    assert by_id["workspaces"].size_bytes == 40  # só main.py restou (20+30 foram)


def test_clean_preserves_active_task_workspaces(settings, tmp_path):
    _build_tree(tmp_path, settings, with_db_files=False)
    session = _session(settings)
    _seed_task(session, 10, TASK_QUEUED)
    _seed_task(session, 20, TASK_IN_PROGRESS)

    result = clean_storage(settings, session, ["pytest_tmp", "smoke", "chrome_profiles"])

    assert result.total_bytes_freed == 0  # preservação integral
    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    assert (ws1 / ".pytest-tmp").is_dir()
    assert (ws1 / "data" / "smoke").is_dir()
    ws2 = Path(settings.workspace_dir) / "2" / "task_20"
    assert (ws2 / "chrome-profile").is_dir()
    assert (ws2 / ".smoke-chrome-extra").is_dir()

    # o relatório pós-clean ainda mostra o lixo preservado.
    by_id = {c.id: c for c in result.report.categories}
    assert by_id["test_junk"].item_count == 4
    assert by_id["test_junk"].size_bytes == 70


def test_clean_orphan_workspace(settings, tmp_path):
    """Workspace `task_*` sem task no banco é órfão → lixo removível."""
    _build_tree(tmp_path, settings, with_db_files=False)
    result = clean_storage(settings, _memory_session(), ["pytest_tmp"])
    assert result.targets[0].item_count == 1
    ws1 = Path(settings.workspace_dir) / "1" / "task_10"
    assert not (ws1 / ".pytest-tmp").exists()
    assert (ws1 / "src" / "main.py").is_file()


def test_clean_never_touches_database_or_workspace_dirs(settings, tmp_path):
    _build_tree(tmp_path, settings, with_db_files=True)
    session = _memory_session()  # banco de testes à parte — não abre o autoia.db

    result = clean_storage(settings, session, list(CLEAN_TARGET_IDS))

    assert (tmp_path / "autoia.db").read_bytes() == b"d" * 100
    assert (tmp_path / "autoia.db-wal").read_bytes() == b"w" * 50
    assert (tmp_path / "autoia.db-shm").read_bytes() == b"s" * 25
    # nenhum diretório `task_*` inteiro foi removido (lixo interno sim).
    assert all(Path(ws).is_dir() for ws in _workspace_tasks(settings.workspace_dir))
    assert result.report.categories[0].id == "database"  # categoria medida segue lá


def _touch_old(path: Path, days: int) -> None:
    path.write_bytes(b"log")
    past = time.time() - days * 86400
    os.utime(path, (past, past))


def test_clean_logs_retention(settings, tmp_path):
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True)
    old = log_dir / "old.log"
    _touch_old(old, 40)  # mais antigo que 30 dias → remove
    recent = log_dir / "recent.log"
    recent.write_bytes(b"new")
    (log_dir / "readme.txt").write_text("x")  # não-`.log` preservado
    (log_dir / "sub").mkdir()
    (log_dir / "sub" / "inner.log").write_text("y")  # subdiretório não conta

    result = clean_storage(settings, _memory_session(), ["logs"])

    assert not old.exists()
    assert recent.exists()
    assert (log_dir / "readme.txt").exists()
    assert (log_dir / "sub" / "inner.log").exists()
    r = result.targets[0]
    assert r.target == "logs"
    assert r.item_count == 1
    assert r.bytes_freed == 3


def test_clean_logs_disabled_when_retention_zero(settings, tmp_path):
    settings.log_retention_days = 0  # desliga a limpeza de logs
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True)
    old = log_dir / "old.log"
    _touch_old(old, 400)

    result = clean_storage(settings, _memory_session(), ["logs"])

    assert old.exists()
    assert result.targets[0].item_count == 0
    assert result.total_bytes_freed == 0


def test_clean_invalid_target_raises(settings, tmp_path):
    with pytest.raises(InvalidTargetError):
        clean_storage(settings, _memory_session(), ["banana"])


def test_clean_unknown_target_among_valid_raises(settings, tmp_path):
    with pytest.raises(InvalidTargetError):
        clean_storage(settings, _memory_session(), ["pytest_tmp", "nope"])
