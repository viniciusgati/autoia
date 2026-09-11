"""Retomada automática após limite do provedor (usage/rate limit).

Quando o LLM atinge o limite de uso, a fase NÃO deve falhar (nem consumir
tentativas): volta a `pending` com `retry_at` no futuro e o worker a reclama
SOZINHA quando o horário passa. Cobre `_parse_provider_retry_at` e o filtro do
`claim_next`.
"""

from __future__ import annotations

from datetime import timedelta

from app.db import utcnow
from app.models import STEP_PENDING, STEP_RUNNING, TaskStep
from app.worker import runner


def test_parse_provider_retry_at_sem_hora_usa_backoff():
    """Sem hora explícita, retoma em ~5 min (não imediatamente)."""
    retry_at = runner._parse_provider_retry_at(
        "provider_limit: You've hit your usage limit. Upgrade to Pro."
    )
    now = utcnow()
    assert now < retry_at <= now + timedelta(minutes=6)


def test_parse_provider_retry_at_extrai_hora_utc():
    """'try again at HH:MM PM' é hora UTC do container (executores rodam com
    TZ=UTC) — vira UTC direto, sem deslocamento pelo fuso do host (erro de 3h
    que atrasava a retomada em -3)."""
    from datetime import datetime, timezone

    # força um horário no futuro para não depender da hora atual
    future_utc = datetime.now(timezone.utc) + timedelta(hours=3)
    label = future_utc.strftime("%-I:%M %p")
    retry_at = runner._parse_provider_retry_at(
        f"provider_limit: usage limit, try again at {label}"
    )
    assert retry_at > utcnow()
    assert retry_at.hour == future_utc.hour
    assert retry_at.minute == future_utc.minute


def test_claim_next_ignora_step_com_retry_at_no_futuro(flow):
    """Fase re-enfileirada por limite do provedor NÃO é reclamada antes do horário."""
    with flow["session_factory"]() as s:
        for step in s.query(TaskStep).filter(TaskStep.task_id == flow["task"]["id"]):
            if step.status == STEP_PENDING:
                step.retry_at = utcnow() + timedelta(hours=3)
        s.commit()

    assert runner.claim_next(flow["session_factory"]) is None


def test_claim_next_reclama_step_com_retry_at_no_passado(flow):
    """Passado o retry_at, o worker volta a reclamar a fase — retomada automática."""
    with flow["session_factory"]() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"], TaskStep.status == STEP_PENDING)
            .order_by(TaskStep.position)
            .first()
        )
        step.retry_at = utcnow() - timedelta(minutes=1)
        s.commit()

    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == STEP_RUNNING


def test_claim_next_nao_pula_fase_anterior_pendente(flow):
    """Pipeline não anda fora de ordem: com a fase anterior pendente (retry_at
    futuro de limite do provedor), a fase SEGUINTE pendente não é reclamada."""
    from app.models import STEP_DONE

    with flow["session_factory"]() as s:
        steps = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"])
            .order_by(TaskStep.position)
            .all()
        )
        for st in steps:
            st.status = STEP_DONE
        steps[0].status = STEP_PENDING
        steps[0].retry_at = utcnow() + timedelta(hours=3)
        steps[1].status = STEP_PENDING
        steps[1].retry_at = None
        ids = (steps[0].id, steps[1].id)
        s.commit()

    # A fase 1 está pendente e sem retry_at, mas a 0 (anterior) ainda não
    # terminou — nada é reclamado (antes, a 1 era reclamada fora de ordem).
    assert runner.claim_next(flow["session_factory"]) is None

    # Vencido o retry_at da fase 0, ELA é reclamada (não a 1).
    with flow["session_factory"]() as s:
        s.get(TaskStep, ids[0]).retry_at = utcnow() - timedelta(minutes=1)
        s.commit()
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id == ids[0]