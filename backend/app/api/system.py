"""Configuração geral do sistema (nível global, fora do escopo de um repositório):
medição do armazenamento e limpeza de arquivos órfãos. Restrito a admin global
(`require_admin` — padrão de `api/users.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import model_catalog
from ..config import Settings
from ..models import OpenCodeAccount, Repository, User
from ..opencode_accounts import (
    InvalidAccountName,
    materialize_auth,
    remove_account_dir,
    sanitize_name,
)
from ..schemas import (
    CleanRequest,
    CleanResult,
    CodexModelsOut,
    OpenCodeAccountCreate,
    OpenCodeAccountOut,
    OpenCodeAccountUpdate,
    OpenCodeModelsOut,
    StorageReport,
)
from ..storage import InvalidTargetError, clean_storage, scan_storage
from .deps import get_session, get_settings, require_admin

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/codex/models", response_model=CodexModelsOut)
def codex_models(settings: Settings = Depends(get_settings)):
    """Modelos disponíveis para o executor codex (dropdown de seleção).

    Fonte ÚNICA: `codex debug models` (cache curto). Sem fallback de lista fixa
    — CLI fora → `models=[]` e `source="erro"` (a UI mostra o estado e tenta de
    novo) em vez de modelos default que não correspondem ao catálogo real.
    """
    models, source = model_catalog.codex_models(settings.codex_bin)
    return CodexModelsOut(models=models, source=source)


@router.get("/opencode/models", response_model=OpenCodeModelsOut)
def opencode_models(settings: Settings = Depends(get_settings)):
    """Modelos disponíveis para o executor opencode (dropdown de seleção).

    Fonte ÚNICA: `opencode models` (um slug por linha, cache curto). Sem
    fallback de lista fixa — CLI fora → `models=[]` e `source="erro"`.
    """
    models, source = model_catalog.opencode_models(settings.opencode_bin)
    return OpenCodeModelsOut(models=models, source=source)


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


# ---------- Contas opencode-go (roster global de credenciais) ----------

@router.get("/opencode-accounts", response_model=list[OpenCodeAccountOut])
def list_opencode_accounts(
    session: Session = Depends(get_session),
):
    """Lista o roster de contas opencode-go (sem tokens; `has_token` indica se a
    conta tem credencial válida). Acessível a qualquer autenticado: os admins de
    projeto precisam do roster para fixar uma conta no repositório."""
    return (
        session.query(OpenCodeAccount)
        .order_by(OpenCodeAccount.name)
        .all()
    )


@router.post("/opencode-accounts", response_model=OpenCodeAccountOut, status_code=201)
def create_opencode_account(
    data: OpenCodeAccountCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _admin: User | None = Depends(require_admin),
):
    """Cria uma conta opencode-go e materializa a credencial em disco.

    Nome vira a pasta da conta (`data/opencode-accounts/<nome>`); `is_default=True`
    remove o default de todas as demais (default é exclusivo)."""
    try:
        sanitize_name(data.name)
    except InvalidAccountName as exc:
        raise HTTPException(400, str(exc)) from exc

    exists = (
        session.query(OpenCodeAccount)
        .filter(OpenCodeAccount.name == data.name)
        .one_or_none()
    )
    if exists is not None:
        raise HTTPException(400, f"já existe uma conta opencode-go chamada {data.name!r}")
    if data.is_default:
        session.query(OpenCodeAccount).update(
            {OpenCodeAccount.is_default: False}
        )
    account = OpenCodeAccount(
        name=data.name,
        token=data.token,
        description=data.description or "",
        is_default=data.is_default,
    )
    session.add(account)
    session.commit()
    try:
        materialize_auth(settings, account.name, account.token)
    except InvalidAccountName as exc:  # pragma: no cover — sanitize já validou
        session.delete(account)
        session.commit()
        raise HTTPException(400, str(exc)) from exc
    session.refresh(account)
    return account


@router.put("/opencode-accounts/{account_id}", response_model=OpenCodeAccountOut)
def update_opencode_account(
    account_id: int,
    data: OpenCodeAccountUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _admin: User | None = Depends(require_admin),
):
    """Edita uma conta. `token` ausente mantém o atual (informativo: o token de
    atualização é requisito). `is_default=None` não mexe no flag; `is_default=True`
    remove o default das demais."""
    account = session.get(OpenCodeAccount, account_id)
    if account is None:
        raise HTTPException(404, "conta opencode-go não encontrada")

    if data.is_default is not None and data.is_default:
        session.query(OpenCodeAccount).update(
            {OpenCodeAccount.is_default: False}
        )
        account.is_default = True
    elif data.is_default is not None:
        account.is_default = data.is_default
    if data.description is not None:
        account.description = data.description
    if data.token:
        account.token = data.token
    session.commit()

    if data.token:
        try:
            materialize_auth(settings, account.name, account.token)
        except InvalidAccountName as exc:  # pragma: no cover — sanitize já validou
            raise HTTPException(400, str(exc)) from exc
    session.refresh(account)
    return account


@router.delete("/opencode-accounts/{account_id}", status_code=204)
def delete_opencode_account(
    account_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _admin: User | None = Depends(require_admin),
):
    """Remove a conta do roster, desfixa dos repositórios que a usam e apaga o
    diretório materializado em disco."""
    account = session.get(OpenCodeAccount, account_id)
    if account is None:
        raise HTTPException(404, "conta opencode-go não encontrada")
    name = account.name
    session.delete(account)
    session.query(Repository).filter(Repository.opencode_account == name).update(
        {Repository.opencode_account: None}
    )
    session.commit()
    remove_account_dir(settings, name)
