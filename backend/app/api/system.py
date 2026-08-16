"""Configuração geral do sistema (nível global, fora do escopo de um repositório):
medição do armazenamento e limpeza de arquivos órfãos. Restrito a admin global
(`require_admin` — padrão de `api/users.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import User
from ..schemas import CleanRequest, CleanResult, StorageReport
from ..storage import InvalidTargetError, clean_storage, scan_storage
from .deps import get_session, get_settings, require_admin

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/storage", response_model=StorageReport)
def get_storage(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _admin: User | None = Depends(require_admin),
):
    """Relatório de armazenamento das 5 categorias de dados gerados pelo autoia.

    Apenas medição: banco de dados, workspaces inteiros e skills nunca são
    limpáveis (o relatório marca `cleanable` por categoria).
    """
    return scan_storage(settings, session)


@router.post("/storage/clean", response_model=CleanResult)
def clean_orphan_data(
    data: CleanRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _admin: User | None = Depends(require_admin),
):
    """Remove os alvos pedidos (logs antigos e lixo de teste em workspaces de
    tasks não ativas) e retorna o espaço liberado + relatório atualizado.

    Alvos válidos: `logs`, `pytest_tmp`, `smoke`, `chrome_profiles`.
    O banco e os workspaces inteiros jamais são removidos.
    """
    try:
        return clean_storage(settings, session, data.targets)
    except InvalidTargetError as exc:
        raise HTTPException(400, f"alvo de limpeza desconhecido: {exc}") from exc
