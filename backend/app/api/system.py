"""Configuração geral do sistema (nível global, fora do escopo de um repositório):
medição do armazenamento e limpeza de arquivos órfãos. Restrito a admin global
(`require_admin` — padrão de `api/users.py`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import User
from ..schemas import CleanRequest, CleanResult, CodexModelsOut, StorageReport
from ..storage import InvalidTargetError, clean_storage, scan_storage
from .deps import get_session, get_settings, require_admin

router = APIRouter(prefix="/api/system", tags=["system"])

# Cache curto do catálogo de modelos do codex (o `codex debug models` remoto
# pode consultar a rede — não vale repetir a cada abertura do dropdown).
_CODEX_MODELS_CACHE: dict = {"ts": 0.0, "models": [], "source": "config"}
_CODEX_MODELS_TTL_S = 60.0


def _codex_models_from_cli(codex_bin: str) -> list[str]:
    """Lê o catálogo do codex (`codex debug models`) e devolve os slugs visíveis.

    Tenta primeiro o catálogo remoto (o que a conta pode usar); se falhar
    (sem rede/auth), usa o `--bundled`. Qualquer erro → lista vazia (a UI cai
    na lista configurável por env).
    """
    attempts = [
        [codex_bin, "debug", "models"],
        [codex_bin, "debug", "models", "--bundled"],
    ]
    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                start_new_session=True,
            )
            if proc.returncode != 0:
                continue
            catalog = json.loads(proc.stdout)
            slugs = []
            for model in catalog.get("models", []):
                if (model.get("visibility") or "list") != "list":
                    continue
                slug = (model.get("slug") or "").strip()
                if slug:
                    slugs.append(slug)
            if slugs:
                return slugs
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
    return []


@router.get("/codex/models", response_model=CodexModelsOut)
def codex_models(settings: Settings = Depends(get_settings)):
    """Modelos disponíveis para o executor codex (dropdown de seleção).

    Fonte primária: `codex debug models` (catálogo real do CLI, cache curto);
    fallback (sem binário/erro): lista fixa de `AUTOIA_CODEX_MODELS`.
    """
    now = time.monotonic()
    cached = _CODEX_MODELS_CACHE
    if now - cached["ts"] > _CODEX_MODELS_TTL_S:
        models = []
        source = "config"
        if shutil.which(settings.codex_bin):
            cli_models = _codex_models_from_cli(settings.codex_bin)
            if cli_models:
                models, source = cli_models, "cli"
        if not models:
            models = list(settings.codex_models)
        cached.update({"ts": now, "models": models, "source": source})
    return CodexModelsOut(models=list(cached["models"]), source=cached["source"])


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
