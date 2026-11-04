"""Catálogos de modelo dos executores — SEMPRE vindo do CLI.

Dois usos, a mesma fonte da verdade:

* `codex_models`/`opencode_models` — lista do dropdown da UI. Fonte `cli`
  quando o CLI respondeu; `erro` quando não (lista vazia). **Não existe mais
  fallback por env**: uma lista fixa de "modelos default" esconde que o CLI
  não respondeu e oferece modelo que não funciona bem na prática.
* `resolve_opencode_model` — fallback de EXECUÇÃO para task/chamado legado
  sem modelo escolhido: primeiro slug do catálogo do CLI (nunca um default
  fixo) e, se o CLI não responder, `None` (execução sem `-m`).

Cache: sucesso vale 60 s (não respawna o CLI a cada abertura do dropdown);
falha vale 15 s (retry rápido sem martelar o CLI). `reset_cache()` limpa
para os testes trocarem o binário.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

_TTL_OK_S = 60.0
_TTL_ERR_S = 15.0

# Slug de modelo não tem espaço; linhas fora do padrão (ajuda/banner) saem.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][\w./:@+-]*$")

_CACHE: dict[str, dict] = {
    "codex": {"ts": -1e9, "models": [], "source": "erro"},
    "opencode": {"ts": -1e9, "models": [], "source": "erro"},
}


def reset_cache() -> None:
    """Zera os caches (testes que trocam o binário do CLI)."""
    for entry in _CACHE.values():
        entry.update({"ts": -1e9, "models": [], "source": "erro"})


def _cached(key: str, read) -> tuple[list[str], str]:
    """Consulta o CLI com cache curto; devolve `(modelos, fonte)`."""
    entry = _CACHE[key]
    ttl = _TTL_OK_S if entry["source"] == "cli" else _TTL_ERR_S
    if time.monotonic() - entry["ts"] < ttl:
        return list(entry["models"]), entry["source"]
    models = read() or []
    entry.update({
        "ts": time.monotonic(),
        "models": models,
        "source": "cli" if models else "erro",
    })
    return list(entry["models"]), entry["source"]


def codex_models_from_cli(codex_bin: str) -> list[str]:
    """`codex debug models` → slugs visíveis. Qualquer erro → lista vazia.

    Tenta primeiro o catálogo remoto (o que a conta pode usar); se falhar
    (sem rede/auth), usa o `--bundled`.
    """
    if not codex_bin or not shutil.which(codex_bin):
        return []
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


def opencode_models_from_cli(opencode_bin: str) -> list[str]:
    """`opencode models` (um slug por linha, sem JSON) → slugs. Erro → []."""
    if not opencode_bin or not shutil.which(opencode_bin):
        return []
    try:
        proc = subprocess.run(
            [opencode_bin, "models"],
            capture_output=True,
            text=True,
            timeout=15,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if _SLUG_RE.match(line.strip())
    ]


def codex_models(codex_bin: str) -> tuple[list[str], str]:
    """Catálogo do codex para o dropdown: `(modelos, "cli" | "erro")`."""
    return _cached("codex", lambda: codex_models_from_cli(codex_bin))


def opencode_models(opencode_bin: str) -> tuple[list[str], str]:
    """Catálogo do opencode para o dropdown: `(modelos, "cli" | "erro")`."""
    return _cached("opencode", lambda: opencode_models_from_cli(opencode_bin))


def resolve_opencode_model(opencode_bin: str, model: str | None) -> str | None:
    """Modelo da execução opencode: escolhido (task/robô) > catálogo do CLI > None.

    Sem modelo escolhido NÃO cai num default fixo (o `AUTOIA_OPENCODE_MODEL`
    antigo não funcionava bem): o fallback é o primeiro slug do `opencode
    models` e, se o CLI não responder, a execução segue sem `-m` (a conta usa
    o modelo configurado nela).
    """
    chosen = (model or "").strip()
    if chosen:
        return chosen
    catalog, _source = opencode_models(opencode_bin)
    return catalog[0] if catalog else None
