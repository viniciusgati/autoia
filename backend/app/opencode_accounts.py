"""Contas opencode-go: resolução e materialização das credenciais em disco.

Uma conta do roster global (`OpenCodeAccount`) é fixada em repositórios
(`Repository.opencode_account`) em vez de ser trocada em runtime por tarefa. A
credencial é materializada no formato legado que o opencode CLI lê:
`<dir>/opencode/auth.json` = `{"opencode-go": {"type": "api", "key": <token>}}`.

O worker entrega o diretório da conta ao executor opencode via `XDG_DATA_HOME`
(redireciona auth + estado/sessões para o diretório isolado da conta). Quando não
há conta (sem pin, sem default), o opencode usa a conta do host — nenhuma env é
injetada.

Resolução: `Repository.opencode_account` (pin) > conta `is_default` > None.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .models import OpenCodeAccount, Repository

# Diretórios permitidos no nome da conta (usados como nome de pasta em disco).
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class InvalidAccountName(ValueError):
    """Nome de conta inválido para uso como diretório em disco."""


@dataclass(frozen=True)
class AccountInfo:
    """Conta resolvida para um repositório (None em todos os campos = host)."""

    name: str | None
    token: str | None
    # Diretório materializado que será usado como XDG_DATA_HOME do opencode.
    dir: str | None


def sanitize_name(name: str) -> str:
    """Valida o nome da conta (usado como pasta em disco) e devolve limpo.

    Apenas letras, números, `.`, `_` e `-`; sem `/`, espaços ou caracteres que
    poderiam quebrar paths/sandbox. Lança `InvalidAccountName` se inválido.
    """
    name = (name or "").strip()
    if not _SAFE_NAME_RE.match(name) or name in (".", ".."):
        raise InvalidAccountName(
            "o nome da conta só pode ter letras, números, ponto, sublinhado e hífen"
        )
    return name


def account_dir(settings: Settings, name: str) -> str:
    """Diretório em disco da conta (onde vive `opencode/auth.json`)."""
    return os.path.abspath(os.path.join(settings.opencode_accounts_dir, sanitize_name(name)))


def auth_json_path(settings: Settings, name: str) -> str:
    """Caminho do `auth.json` legado do opencode para a conta."""
    return os.path.join(account_dir(settings, name), "opencode", "auth.json")


def materialize_auth(settings: Settings, name: str, token: str) -> str:
    """Grava a credencial da conta no formato legado do opencode (idempotente).

    Escreve `auth.json` apenas quando o conteúdo muda. Retorna o diretório da conta
    (para uso como `XDG_DATA_HOME`). Pode lançar `InvalidAccountName`.
    """
    dir = account_dir(settings, name)
    auth = {"opencode-go": {"type": "api", "key": token}}
    path = os.path.join(dir, "opencode", "auth.json")
    payload = json.dumps(auth, ensure_ascii=False, indent=2)
    existing = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        pass
    if existing != payload:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    return dir


def remove_account_dir(settings: Settings, name: str) -> None:
    """Remove o diretório materializado da conta do disco (após apagar o roster).

    Best-effort: conta sem materialização não é erro.
    """
    import shutil

    dir = account_dir(settings, name)
    if os.path.isdir(dir):
        shutil.rmtree(dir, ignore_errors=True)


def effective_account_dir(
    settings: Settings, session: Session, repo: Repository | None
) -> str | None:
    """Resolve a conta aberta para o repositório e materializa a credencial.

    Precedência: pin do repositório (`Repository.opencode_account`) > conta global
    `is_default` > None (host). Retorna o diretório da conta materializado (a ser
    usado como `XDG_DATA_HOME` do opencode) ou None para usar a conta do host.
    Pin inexistente → warning via logging e fallback para default/host.
    """
    import logging

    log = logging.getLogger(__name__)
    pinned = repo.opencode_account if repo is not None else None

    account: OpenCodeAccount | None = None
    if pinned:
        account = (
            session.query(OpenCodeAccount)
            .filter(OpenCodeAccount.name == pinned)
            .one_or_none()
        )
        if account is None:
            log.warning(
                "conta opencode-go fixada em repositório não existe no roster: %r",
                pinned,
            )
    if account is None:
        account = (
            session.query(OpenCodeAccount)
            .filter(OpenCodeAccount.is_default.is_(True))
            .one_or_none()
        )
    if account is None:
        return None
    return materialize_auth(settings, account.name, account.token)


def account_chain(session: Session, repo: Repository | None) -> list[str]:
    """Candidatas opencode-go na ordem de preferência para um repositório.

    Ordem determinística: pin do repositório (`Repository.opencode_account`) >
    conta global `is_default` > demais contas do roster (alfabética). É a cadeia
    usada pela rotação automática de conta quando a em uso esgota (`provider_limit`
    no executor opencode): a fase tenta a PRÓXIMA da cadeia em vez de esperar.
    """
    names = [n for (n,) in session.query(OpenCodeAccount.name).order_by(OpenCodeAccount.name)]
    chain: list[str] = []
    if repo is not None and repo.opencode_account:
        chain.append(repo.opencode_account)
    default = (
        session.query(OpenCodeAccount)
        .filter(OpenCodeAccount.is_default.is_(True))
        .one_or_none()
    )
    if default is not None and default.name not in chain:
        chain.append(default.name)
    for name in names:
        if name not in chain:
            chain.append(name)
    return chain


def next_account(session: Session, repo: Repository | None, current: str) -> str | None:
    """Próxima conta da cadeia após `current` (None = `current` é a última/inexistente)."""
    chain = account_chain(session, repo)
    try:
        idx = chain.index(current)
    except ValueError:
        return None
    return chain[idx + 1] if idx + 1 < len(chain) else None


def resolve_account(
    settings: Settings,
    session: Session,
    repo: Repository | None,
    override: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a conta efetiva (nome, diretório) materializando a credencial.

    `override` (rotação fixada na fase via `TaskStep.opencode_account`) usa EXATAMENTE
    essa conta, se ainda existir no roster; sem override (ou override inexistente),
    cai para a primeira da cadeia `account_chain` (pin > default). Sem contas →
    `(None, None)` = conta do host (nenhuma env injetada).
    """
    if override:
        account = (
            session.query(OpenCodeAccount)
            .filter(OpenCodeAccount.name == override)
            .one_or_none()
        )
        if account is not None:
            return account.name, materialize_auth(settings, account.name, account.token)
    chain = account_chain(session, repo)
    if not chain:
        return None, None
    account = (
        session.query(OpenCodeAccount)
        .filter(OpenCodeAccount.name == chain[0])
        .one_or_none()
    )
    if account is None:
        return None, None
    return account.name, materialize_auth(settings, account.name, account.token)