"""Medição e limpeza de armazenamento do sistema (configuração geral).

Lógica pura e testável (sem HTTP): `scan_storage` produz o `StorageReport` das
5 categorias de dados gerados pelo autoia e `clean_storage` remove apenas os
alvos explicitamente pedidos — logs `.log` antigos e lixo de teste
(`.pytest-tmp/`, `data/smoke/`, perfis de Chrome) dentro de workspaces de tasks
NÃO ativas (ou inexistentes no banco — órfãs).

Garantias inegociáveis:
- O banco (`autoia.db` + `-wal`/`-shm`) é APENAS medido, nunca apagado.
- Um diretório de workspace `task_*` inteiro nunca é removido — a limpeza age
  apenas no lixo interno.
- Workspaces de tasks ativas (`queued`/`in_progress`) são preservados
  integralmente (nem o lixo interno é tocado).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from sqlalchemy.orm import Session

from .config import Settings
from .models import TASK_IN_PROGRESS, TASK_QUEUED, Task
from .schemas import CleanResult, CleanTargetResult, StorageCategory, StorageReport

# Ids de alvos de limpeza aceitos no payload (desconhecido → 400).
CLEAN_TARGET_IDS = ("logs", "pytest_tmp", "smoke", "chrome_profiles")

# Lixo de teste que a limpeza pode remover DENTRO de um workspace `task_*`
# (caminhos relativos ao workspace; nunca o workspace inteiro).
TEST_JUNK_RELATIVES = (
    ".pytest-tmp",
    "data/smoke",
    "chrome-profile",
    ".smoke-chrome-extra",
)

# Nomes PT-BR das categorias (usados no relatório e no frontend).
CATEGORY_LABELS = {
    "database": "Banco de dados",
    "logs": "Logs",
    "workspaces": "Workspaces",
    "test_junk": "Lixo de teste",
    "skills": "Skills",
}


class InvalidTargetError(ValueError):
    """Alvo de limpeza desconhecido (payload inválido)."""


def _sqlite_paths(database_url: str) -> list[Path]:
    """Caminhos do banco sqlite (`<db>` + `-wal` + `-shm`) derivados da URL.

    Retorna lista vazia se a URL não for um sqlite local (ex.: postgres).
    """
    if not database_url.startswith("sqlite:///"):
        return []
    base = Path(database_url[len("sqlite:///"):])
    return [base, Path(str(base) + "-wal"), Path(str(base) + "-shm")]


def _dir_size(path: Path) -> int:
    """Soma recursiva dos tamanhos dos arquivos (0 para inexistente/erro)."""
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _workspace_tasks(workspace_dir: str) -> dict[str, int]:
    """Mapa workspace → task id: `workspace_dir/<repo_id>/task_<id>`.

    Retorna {caminho_do_workspace: id_da_task}; ignora diretórios fora do
    padrão `task_<digits>` (e `repo_id` não numérico continua valendo — a
    chave é o caminho completo, sem depender do formato do nome do repo).
    """
    result: dict[str, int] = {}
    root = Path(workspace_dir)
    if not root.is_dir():
        return result
    for repo_dir in root.iterdir():
        if not repo_dir.is_dir():
            continue
        for ws in repo_dir.iterdir():
            if not ws.is_dir() or not ws.name.startswith("task_"):
                continue
            suffix = ws.name[len("task_"):]
            if not suffix.isdigit():
                continue
            result[str(ws)] = int(suffix)
    return result


def _active_task_ids(session: Session, task_ids: set[int]) -> set[int]:
    """Ids de tasks ATIVAS (`queued`/`in_progress`) — workspaces preservados."""
    if not task_ids:
        return set()
    rows = (
        session.query(Task.id)
        .filter(
            Task.id.in_(task_ids),
            Task.status.in_((TASK_QUEUED, TASK_IN_PROGRESS)),
        )
        .all()
    )
    return {row[0] for row in rows}


def scan_storage(settings: Settings, session: Session) -> StorageReport:
    """Mede as 5 categorias de armazenamento (o filesystem como está).

    `session` é aceita para manter a assinatura uniforme com `clean_storage`
    (no scan não é necessária — a medição não consulta `Task.status`).
    """
    categories: list[StorageCategory] = []

    # database — apenas medido; item = cada arquivo existente entre
    # <db>, <db>-wal, <db>-shm (0 a 3 itens).
    db_size = 0
    db_items = 0
    for path in _sqlite_paths(settings.database_url):
        if path.is_file():
            db_size += path.stat().st_size
            db_items += 1
    categories.append(
        StorageCategory(
            id="database",
            label=CATEGORY_LABELS["database"],
            size_bytes=db_size,
            item_count=db_items,
            cleanable=False,
        )
    )

    # logs — arquivos `.log` no nível superior (não recursivo); subdiretórios
    # e arquivos não-`.log` não contam.
    log_size = 0
    log_items = 0
    log_dir = Path(settings.log_dir)
    if log_dir.is_dir():
        for entry in log_dir.iterdir():
            if entry.is_file() and entry.name.endswith(".log"):
                log_size += entry.stat().st_size
                log_items += 1
    categories.append(
        StorageCategory(
            id="logs",
            label=CATEGORY_LABELS["logs"],
            size_bytes=log_size,
            item_count=log_items,
            cleanable=True,
        )
    )

    # workspaces — 1 item por diretório `task_*`; tamanho total recursivo.
    workspace_map = _workspace_tasks(settings.workspace_dir)
    categories.append(
        StorageCategory(
            id="workspaces",
            label=CATEGORY_LABELS["workspaces"],
            size_bytes=sum(_dir_size(Path(ws)) for ws in workspace_map),
            item_count=len(workspace_map),
            cleanable=False,
        )
    )

    # test_junk — 1 item por artefato órfão DENTRO de um workspace `task_*`
    # (um `.pytest-tmp/`, um `data/smoke/`, um perfil de Chrome); medido
    # recursivamente.
    junk_items = 0
    junk_size = 0
    for ws in workspace_map:
        for relative in TEST_JUNK_RELATIVES:
            path = Path(ws) / relative
            if path.exists():
                junk_items += 1
                junk_size += _dir_size(path)
    categories.append(
        StorageCategory(
            id="test_junk",
            label=CATEGORY_LABELS["test_junk"],
            size_bytes=junk_size,
            item_count=junk_items,
            cleanable=True,
        )
    )

    # skills — 1 item por entrada (diretório ou arquivo) no nível superior;
    # apenas medida (sem regra de "órfão").
    skills_size = 0
    skills_items = 0
    skills_dir = Path(settings.skills_dir)
    if skills_dir.is_dir():
        for entry in skills_dir.iterdir():
            skills_items += 1
            skills_size += _dir_size(entry) if entry.is_dir() else entry.stat().st_size
    categories.append(
        StorageCategory(
            id="skills",
            label=CATEGORY_LABELS["skills"],
            size_bytes=skills_size,
            item_count=skills_items,
            cleanable=False,
        )
    )

    return StorageReport(
        categories=categories,
        total_bytes=sum(c.size_bytes for c in categories),
    )


def _clean_logs(settings: Settings) -> tuple[int, int]:
    """Remove arquivos `.log` do nível superior com mtime além da retenção.

    `log_retention_days <= 0` desliga a limpeza de logs. Retorna
    (itens removidos, bytes liberados).
    """
    retention = settings.log_retention_days
    if retention <= 0:
        return 0, 0
    cutoff = time.time() - retention * 86400
    items = 0
    freed = 0
    log_dir = Path(settings.log_dir)
    if not log_dir.is_dir():
        return 0, 0
    for entry in log_dir.iterdir():
        if not entry.is_file() or not entry.name.endswith(".log"):
            continue
        try:
            stat = entry.stat()
            if stat.st_mtime >= cutoff:
                continue
            freed += stat.st_size
            entry.unlink()
            items += 1
        except OSError:
            pass
    return items, freed


def _clean_junk(workspaces: list[str], relatives: tuple[str, ...]) -> tuple[int, int]:
    """Remove os artefatos `relatives` dentro de cada workspace não preservado.

    Retorna (itens removidos, bytes liberados). Nunca remove o workspace em si.
    """
    items = 0
    freed = 0
    for ws in workspaces:
        for relative in relatives:
            path = Path(ws) / relative
            if not path.exists():
                continue
            try:
                freed += _dir_size(path)
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                items += 1
            except OSError:
                pass
    return items, freed


def clean_storage(
    settings: Settings,
    session: Session,
    target_ids: list[str],
) -> CleanResult:
    """Remove os alvos pedidos e retorna bytes liberados + relatório atualizado.

    Alvos válidos: `logs`, `pytest_tmp`, `smoke`, `chrome_profiles`. A limpeza
    de lixo de teste age apenas em workspaces de tasks NÃO ativas ou
    inexistentes no banco; o banco e os workspaces inteiros jamais são alvo.
    """
    unknown = [t for t in target_ids if t not in CLEAN_TARGET_IDS]
    if unknown:
        raise InvalidTargetError(unknown[0])

    workspace_map = _workspace_tasks(settings.workspace_dir)
    active_ids = _active_task_ids(session, set(workspace_map.values()))
    cleanable_workspaces = [
        ws for ws, task_id in workspace_map.items() if task_id not in active_ids
    ]

    requested = list(dict.fromkeys(target_ids))  # preserva ordem, sem duplicados
    results: list[CleanTargetResult] = []
    for target in requested:
        if target == "logs":
            items, freed = _clean_logs(settings)
        elif target == "pytest_tmp":
            items, freed = _clean_junk(cleanable_workspaces, (".pytest-tmp",))
        elif target == "smoke":
            items, freed = _clean_junk(cleanable_workspaces, ("data/smoke",))
        else:  # chrome_profiles
            items, freed = _clean_junk(
                cleanable_workspaces, ("chrome-profile", ".smoke-chrome-extra")
            )
        results.append(
            CleanTargetResult(target=target, item_count=items, bytes_freed=freed)
        )

    return CleanResult(
        targets=results,
        total_bytes_freed=sum(r.bytes_freed for r in results),
        report=scan_storage(settings, session),
    )
