"""Testes do carregamento de .env opcional (config.py)."""

from __future__ import annotations

import os

from app.config import Settings, _load_dotenv


def test_load_dotenv_reads_values_and_escapes(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "AUTOIA_TESTE=linha um \\nlinha dois\n"
        "AUTOIA_EXISTENTE=nao deve sobrescrever\n"
        "# comentário\n"
        "SEM_IGUAL\n"
    )
    monkeypatch.setenv("AUTOIA_EXISTENTE", "original")

    _load_dotenv(str(tmp_path))

    assert os.environ["AUTOIA_TESTE"] == "linha um \nlinha dois"
    assert os.environ["AUTOIA_EXISTENTE"] == "original"  # env já setada prevalece
    # limpa a var criada pelo helper (fora do monkeypatch) para não vazar nos testes
    os.environ.pop("AUTOIA_TESTE", None)


def test_load_dotenv_missing_dir_is_noop(tmp_path, monkeypatch):
    _load_dotenv(str(tmp_path / "nao_existe"))
    assert "AUTOIA_TESTE" not in os.environ


def test_settings_auth_defaults(monkeypatch):
    """Sem env, auth fica ON por padrão: sessão 30 dias, cookie sem Secure."""
    for key in ("AUTOIA_AUTH_ENABLED", "AUTOIA_SESSION_DAYS", "AUTOIA_COOKIE_SECURE"):
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.auth_enabled is True
    assert s.session_days == 30
    assert s.cookie_secure is False


def test_settings_auth_env_overrides(monkeypatch):
    monkeypatch.setenv("AUTOIA_AUTH_ENABLED", "0")
    monkeypatch.setenv("AUTOIA_SESSION_DAYS", "7")
    monkeypatch.setenv("AUTOIA_COOKIE_SECURE", "1")
    s = Settings()
    assert s.auth_enabled is False
    assert s.session_days == 7
    assert s.cookie_secure is True


def test_settings_step_context_recent_phases_default(monkeypatch):
    """Sem env, a janela de fases recentes do contexto é 1 (default)."""
    monkeypatch.delenv("AUTOIA_STEP_CONTEXT_RECENT_PHASES", raising=False)
    assert Settings().step_context_recent_phases == 1


def test_settings_step_context_recent_phases_env_overrides(monkeypatch):
    """`AUTOIA_STEP_CONTEXT_RECENT_PHASES` é respeitado (0 = compactar todas)."""
    monkeypatch.setenv("AUTOIA_STEP_CONTEXT_RECENT_PHASES", "0")
    assert Settings().step_context_recent_phases == 0
    monkeypatch.setenv("AUTOIA_STEP_CONTEXT_RECENT_PHASES", "3")
    assert Settings().step_context_recent_phases == 3
