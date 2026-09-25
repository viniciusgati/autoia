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


def test_settings_proxy_idle_timeout_default(monkeypatch):
    """Ociosidade do túnel do proxy: 600s por padrão (muito acima dos 30s que
    matavam chamadas de LLM no meio — regressão da task 195)."""
    monkeypatch.delenv("AUTOIA_PROXY_IDLE_TIMEOUT", raising=False)
    assert Settings().sandbox_proxy_idle_timeout == 600


def test_settings_proxy_idle_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AUTOIA_PROXY_IDLE_TIMEOUT", "1800")
    assert Settings().sandbox_proxy_idle_timeout == 1800
    monkeypatch.setenv("AUTOIA_PROXY_IDLE_TIMEOUT", "0")  # 0 = sem limite
    assert Settings().sandbox_proxy_idle_timeout == 0


def test_settings_android_max_concurrent_default(monkeypatch):
    """Slots de emulador Android: 2 execuções com qemu ao mesmo tempo por padrão."""
    monkeypatch.delenv("AUTOIA_ANDROID_MAX_CONCURRENT", raising=False)
    assert Settings().android_max_concurrent == 2


def test_settings_android_max_concurrent_env_override(monkeypatch):
    monkeypatch.setenv("AUTOIA_ANDROID_MAX_CONCURRENT", "1")
    assert Settings().android_max_concurrent == 1
    monkeypatch.setenv("AUTOIA_ANDROID_MAX_CONCURRENT", "0")  # 0 = sem slots
    assert Settings().android_max_concurrent == 0


def test_settings_llm_max_concurrent_default(monkeypatch):
    """Teto global de execuções LLM simultâneas: 2 por padrão (medido: 27
    chamadas/min causaram 429 em cascata; 2–5/min funcionam)."""
    monkeypatch.delenv("AUTOIA_LLM_MAX_CONCURRENT", raising=False)
    assert Settings().llm_max_concurrent == 2


def test_settings_llm_max_concurrent_env_override(monkeypatch):
    monkeypatch.setenv("AUTOIA_LLM_MAX_CONCURRENT", "1")
    assert Settings().llm_max_concurrent == 1
    monkeypatch.setenv("AUTOIA_LLM_MAX_CONCURRENT", "0")  # 0 = sem teto
    assert Settings().llm_max_concurrent == 0


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


def test_settings_llm_bg_timeout_default(monkeypatch):
    """Teto de timeout das gerações LLM pura: 300 s (chamada única de JSON)."""
    monkeypatch.delenv("AUTOIA_LLM_BG_TIMEOUT", raising=False)
    assert Settings().llm_bg_timeout == 300


def test_settings_llm_bg_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AUTOIA_LLM_BG_TIMEOUT", "120")
    assert Settings().llm_bg_timeout == 120
    monkeypatch.setenv("AUTOIA_LLM_BG_TIMEOUT", "0")  # 0 = usar run_timeout
    assert Settings().llm_bg_timeout == 0
