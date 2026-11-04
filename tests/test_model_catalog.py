"""Catálogo de modelos do executor — SEMPRE via CLI (`model_catalog`).

Regras cobertas aqui (regressão do seletor de modelo do opencode):

* o endpoint devolve o catálogo do CLI e NUNCA uma lista fixa de default;
* CLI fora → lista vazia com `source="erro"` (a UI mostra o estado e tenta de novo);
* o fallback de execução (task/chamado legado sem modelo) vem do catálogo do CLI;
* cache: sucesso 60 s / falha 15 s — não respawna o CLI a cada consulta.
"""

from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient

# Capturado no import (ANTES do monkeypatch autouse do conftest que impede o
# `opencode models` real na suíte): a implementação verdadeira da CLI.
from app.model_catalog import opencode_models_from_cli as _real_opencode_models_from_cli


def _fake_cli(tmp_path, body: str, name: str = "fake_cli") -> str:
    script = tmp_path / name
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _client(
    tmp_path, *, opencode_bin: str, codex_bin: str = "/bin/nonexistent-codex"
) -> TestClient:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/autoia.db",
        workspace_dir=str(tmp_path / "ws"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        opencode_bin=opencode_bin,
        codex_bin=codex_bin,
        auth_enabled=False,
    )
    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _cli_verdadeira_e_cache_zerado(monkeypatch):
    """Restaura a CLI verdadeira para ESTE teste e zera o cache compartilhado."""
    from app import model_catalog

    model_catalog.reset_cache()
    monkeypatch.setattr(
        model_catalog, "opencode_models_from_cli", _real_opencode_models_from_cli
    )
    yield
    model_catalog.reset_cache()


def test_endpoint_opencode_vem_do_cli(tmp_path):
    """O dropdown recebe os slugs do `opencode models` (linhas inválidas saem)."""
    bin_ = _fake_cli(
        tmp_path,
        "print('opencode-go/deepseek-v4-flash')\n"
        "print('google/gemini-3.5-flash')\n"
        "print('linha com espaco que nao e slug')\n"
        "print('')\n",
    )
    resp = _client(tmp_path, opencode_bin=bin_).get("/api/system/opencode/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "cli"
    assert body["models"] == ["opencode-go/deepseek-v4-flash", "google/gemini-3.5-flash"]


def test_endpoint_opencode_sem_cli_nao_tem_fallback(tmp_path):
    """CLI inexistente → lista vazia + `source="erro"`: zero lista default."""
    resp = _client(tmp_path, opencode_bin=str(tmp_path / "opencode-que-nao-existe")).get(
        "/api/system/opencode/models"
    )
    assert resp.status_code == 200
    assert resp.json() == {"models": [], "source": "erro"}


def test_endpoint_opencode_cli_com_falha(tmp_path):
    """CLI que sai com erro (código != 0) também vira `source="erro"`."""
    bin_ = _fake_cli(tmp_path, "import sys; sys.exit(3)\n")
    resp = _client(tmp_path, opencode_bin=bin_).get("/api/system/opencode/models")
    assert resp.json() == {"models": [], "source": "erro"}


def test_cache_nao_respawna_cli(monkeypatch):
    """Sucesso vale 60 s / falha 15 s: 2 consultas seguidas = UMA chamada ao CLI."""
    from app import model_catalog

    calls: list[str] = []

    def counting(opencode_bin: str) -> list[str]:
        calls.append(opencode_bin)
        return ["prov/modelo-a"] if "quebrado" not in opencode_bin else []

    model_catalog.reset_cache()
    monkeypatch.setattr(model_catalog, "opencode_models_from_cli", counting)

    assert model_catalog.opencode_models("opencode") == (["prov/modelo-a"], "cli")
    assert model_catalog.opencode_models("outro-bin") == (["prov/modelo-a"], "cli")
    assert calls == ["opencode"]  # cache por executor, não por invocação

    model_catalog.reset_cache()
    assert model_catalog.opencode_models("opencode-quebrado") == ([], "erro")
    assert model_catalog.opencode_models("opencode-quebrado") == ([], "erro")
    assert calls[-1] == "opencode-quebrado"
    assert len(calls) == 2  # falha também é cacheada (TTL curto)


def test_resolve_opencode_model_prefere_o_escolhido(monkeypatch):
    """Modelo escolhido (task/robô) ganha do catálogo — sem nem ler o CLI."""
    from app import model_catalog

    def nao_deve_chamar(_bin: str) -> list[str]:  # pragma: no cover — defeito
        raise AssertionError("CLI não deveria ser consultada com modelo escolhido")

    model_catalog.reset_cache()
    monkeypatch.setattr(model_catalog, "opencode_models_from_cli", nao_deve_chamar)
    assert model_catalog.resolve_opencode_model("opencode", "prov/escolhido") == "prov/escolhido"
    assert (
        model_catalog.resolve_opencode_model("opencode", "  prov/escolhido  ")
        == "prov/escolhido"
    )


def test_resolve_opencode_model_sem_escolha_usa_o_catalogo(tmp_path, monkeypatch):
    """Task legada sem modelo → primeiro slug do `opencode models` (nunca um
    default fixo de config) e, com o CLI fora, `None` (execução sem `-m`)."""
    from app import model_catalog

    bin_ = _fake_cli(tmp_path, "print('prov/primeiro')\nprint('prov/segundo')\n")
    model_catalog.reset_cache()
    assert model_catalog.resolve_opencode_model(bin_, None) == "prov/primeiro"
    assert model_catalog.resolve_opencode_model(bin_, "") == "prov/primeiro"

    model_catalog.reset_cache()
    monkeypatch.setattr(model_catalog, "opencode_models_from_cli", lambda _b: [])
    assert model_catalog.resolve_opencode_model(str(tmp_path / "nao-existe"), None) is None
