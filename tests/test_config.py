"""Testes do carregamento de .env opcional (config.py)."""

from __future__ import annotations

import os
from datetime import datetime

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


def _utc(h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, 8, h, m)  # terça-feira (weekday 1)


def test_peak_hours_defaults():
    """Sem env, a janela de pico é a do DeepSeek (seg–sex 01-04 e 06-10 UTC)."""
    assert Settings().peak_hours_windows == "01:00-04:00,06:00-10:00"
    assert Settings().peak_hours_weekdays_only is True


def test_peak_hours_inside_window():
    s = Settings()
    assert s.in_peak_hours(_utc(1, 0))     # início da 1ª janela
    assert s.in_peak_hours(_utc(3, 59))    # fim da 1ª janela
    assert s.in_peak_hours(_utc(6, 0))     # início da 2ª janela
    assert s.in_peak_hours(_utc(9, 59))    # fim da 2ª janela


def test_peak_hours_outside_window():
    s = Settings()
    assert not s.in_peak_hours(_utc(0, 59))  # antes da 1ª janela
    assert not s.in_peak_hours(_utc(4, 0))   # o fim da janela é exclusivo
    assert not s.in_peak_hours(_utc(5, 59))
    assert not s.in_peak_hours(_utc(10, 0))  # após a 2ª janela
    assert not s.in_peak_hours(_utc(23, 59))


def test_peak_hours_weekend_ignores_window():
    """Fim de semana (sábado/domingo UTC) é fora de pico mesmo dentro da janela."""
    s = Settings()
    saturday = datetime(2026, 9, 12, 2, 0)   # sábado
    sunday = datetime(2026, 9, 13, 8, 0)     # domingo
    assert saturday.weekday() == 5
    assert sunday.weekday() == 6
    assert not s.in_peak_hours(saturday)
    assert not s.in_peak_hours(sunday)


def test_peak_hours_window_disabled_when_empty():
    s = Settings(peak_hours_windows="")
    assert not s.in_peak_hours(_utc(2, 0))


def test_peak_hours_custom_window(monkeypatch):
    monkeypatch.setenv("AUTOIA_PEAK_HOURS_WINDOWS", "22:00-02:00")
    monkeypatch.setenv("AUTOIA_PEAK_HOURS_WEEKDAYS_ONLY", "0")
    s = Settings()
    assert s.in_peak_hours(_utc(22, 0))   # noite (cruzando a meia-noite)
    assert s.in_peak_hours(_utc(0, 30))   # depois da meia-noite, ainda na janela
    assert s.in_peak_hours(_utc(1, 59))   # fim da janela
    assert not s.in_peak_hours(_utc(21, 59))
    assert not s.in_peak_hours(_utc(2, 0))
