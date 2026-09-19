"""Detector de loop semântico + correções de contexto (prior activity / handoff).

Regressões da task 127: o developer ficou horas caçando um relatório de QA
inexistente (buscas repetidas pelo mesmo arquivo) e o handoff não dizia o motivo
da falha ("- None" x12 na atividade anterior, [FALHOU] sem motivo).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.guardrails import SemanticLoopTracker
from app.worker.runner import _step_prior_activity, _subtask_progress_summary


# ---------------------------------------------------------------------------
# SemanticLoopTracker (busca repetida da MESMA intenção)
# ---------------------------------------------------------------------------


def test_tracker_detecta_busca_repetida_do_mesmo_alvo():
    tracker = SemanticLoopTracker(max_repeats=3)
    # find com caminhos diferentes mas MESMO arquivo procurado (caso task 127)
    assert tracker.register("bash", {"command": 'find /home/vinicius -name "autoia_verdict.txt"'}) is None
    assert tracker.register("bash", {"command": 'find / -path /proc -prune -o -name "autoia_verdict.txt" -print'}) is None
    violation = tracker.register("bash", {"command": 'find data -name "autoia_verdict.txt" 2>/dev/null'})
    assert violation is not None
    assert violation.pattern == "repeated-search"
    assert "autoia_verdict.txt" in violation.detail
    assert "3x" in violation.detail


def test_tracker_alvos_diferentes_nao_violam():
    tracker = SemanticLoopTracker(max_repeats=3)
    for f in ("a.py", "b.py", "c.py"):
        assert tracker.register("read", {"filePath": f"src/{f}"}) is None


def test_tracker_leitura_repetida_do_mesmo_caminho():
    tracker = SemanticLoopTracker(max_repeats=2)
    assert tracker.register("read", {"filePath": "/x/report.pdf"}) is None
    violation = tracker.register("read", {"path": "/x/report.pdf"})
    assert violation is not None


def test_tracker_homonimos_em_diretorios_diferentes_nao_violam():
    """Regressão task 143: Next.js com vários `page.tsx` — ler arquivos
    DIFERENTES de mesmo nome não pode virar "busca repetida"."""
    tracker = SemanticLoopTracker(max_repeats=3)
    for f in (
        "src/app/dashboard/page.tsx",
        "src/app/registro/page.tsx",
        "src/app/cadastro/page.tsx",
        "src/app/historia/page.tsx",
        "src/app/exportar/page.tsx",
        "src/app/login/page.tsx",
    ):
        assert tracker.register("read", {"filePath": f}) is None


def test_tracker_mesmo_arquivo_absoluto_e_relativo_viola():
    """O MESMO arquivo lido por caminho absoluto e relativo colapsa no mesmo
    fingerprint (kimi usa relativo, opencode absoluto)."""
    tracker = SemanticLoopTracker(max_repeats=2)
    assert tracker.register("read", {"filePath": "/w/1/task_143/src/app/login/page.tsx"}) is None
    violation = tracker.register("read", {"path": "src/app/login/page.tsx"})
    assert violation is not None


def test_tracker_mesmo_page_tsx_repetido_viola():
    """Repetir leitura do MESMO page.tsx continua sendo detectado."""
    tracker = SemanticLoopTracker(max_repeats=2)
    assert tracker.register("read", {"filePath": "src/app/login/page.tsx"}) is None
    violation = tracker.register("read", {"path": "src/app/login/page.tsx"})
    assert violation is not None


def test_tracker_grep_repetido_do_mesmo_termo():
    tracker = SemanticLoopTracker(max_repeats=2)
    assert tracker.register("grep", {"pattern": "FALHOU"}) is None
    violation = tracker.register("grep", {"query": "FALHOU"})
    assert violation is not None


def test_tracker_desligado_nunca_viola():
    tracker = SemanticLoopTracker(max_repeats=0)
    for _ in range(20):
        assert tracker.register("bash", {"command": 'find / -name "x.txt"'}) is None


def test_tracker_ignora_nao_busca():
    tracker = SemanticLoopTracker(max_repeats=2)
    for _ in range(5):
        assert tracker.register("bash", {"command": "./gradlew test"}) is None
    for _ in range(5):
        assert tracker.register("write", {"filePath": "a.py"}) is None


# ---------------------------------------------------------------------------
# _step_prior_activity (atividade da tentativa anterior — sem "- None")
# ---------------------------------------------------------------------------


def test_prior_activity_opencode_sem_none(flow):
    """Tool calls no formato opencode (`{"tool": ..., "input": ...}`) não podem
    virar "- None" no handoff; e a atividade mostrada é da ÚLTIMA tentativa com
    atividade (a atual, recém-iniciada no claim, ainda não tem eventos)."""
    from app.models import RunEvent, TaskStep

    with flow["session_factory"]() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"])
            .order_by(TaskStep.position)
            .first()
        )
        # tentativa 1 (kimi): leitura + texto
        s.add(RunEvent(step_id=step.id, seq=1, kind="attempt_started", payload={"attempt": 1}))
        s.add(RunEvent(step_id=step.id, seq=2, kind="tool_call", payload={
            "tool_call": {"function": {"name": "Read", "arguments": '{"path": "a.py"}'}}
        }))
        s.add(RunEvent(step_id=step.id, seq=3, kind="assistant_text", payload={"content": "li o arquivo"}))
        # tentativa 2 (atual, recém-iniciada): attempt_started sem atividade ainda
        s.add(RunEvent(step_id=step.id, seq=4, kind="attempt_started", payload={"attempt": 2}))
        s.commit()
        text = _step_prior_activity(s, step)

    assert text, "deveria mostrar a atividade da tentativa anterior"
    assert "None" not in text
    assert "Read: a.py" in text
    assert "li o arquivo" in text


def test_prior_activity_ultima_tentativa_com_atividade(flow):
    """Quando a última tentativa TEM atividade, só ela aparece (a anterior não)."""
    from app.models import RunEvent, TaskStep

    with flow["session_factory"]() as s:
        step = (
            s.query(TaskStep)
            .filter(TaskStep.task_id == flow["task"]["id"])
            .order_by(TaskStep.position)
            .first()
        )
        s.add(RunEvent(step_id=step.id, seq=1, kind="attempt_started", payload={"attempt": 1}))
        s.add(RunEvent(step_id=step.id, seq=2, kind="tool_call", payload={
            "tool_call": {"function": {"name": "Read", "arguments": '{"path": "a.py"}'}}
        }))
        s.add(RunEvent(step_id=step.id, seq=3, kind="attempt_started", payload={"attempt": 2}))
        s.add(RunEvent(step_id=step.id, seq=4, kind="tool_call", payload={
            "tool": "bash", "input": {"command": "git log --oneline -5"}
        }))
        s.commit()
        text = _step_prior_activity(s, step)

    assert "Read" not in text
    assert "bash: git log" in text
    assert "None" not in text


# ---------------------------------------------------------------------------
# _subtask_progress_summary (motivo da falha no handoff)
# ---------------------------------------------------------------------------


def _task_fake(subtasks) -> SimpleNamespace:
    return SimpleNamespace(subtasks=subtasks)


def _sub(status, error=None, verdict=None, summary=None) -> SimpleNamespace:
    return SimpleNamespace(
        position=0, title="Sub 1", status=status, error=error,
        verdict=verdict, summary=summary,
    )


def test_progress_summary_inclui_motivo_da_falha():
    text = _subtask_progress_summary(_task_fake([_sub("failed", error="veredicto AUSENTE")]))
    assert "[FALHOU]" in text
    assert "motivo: veredicto AUSENTE" in text


def test_progress_summary_inclui_motivo_inconclusivo():
    text = _subtask_progress_summary(
        _task_fake([_sub("pending", error="inconclusive: veredicto AUSENTE")])
    )
    assert "motivo: inconclusive: veredicto AUSENTE" in text


def test_progress_summary_sem_erro_nao_inventa_motivo():
    text = _subtask_progress_summary(_task_fake([_sub("done", verdict="PASS")]))
    assert "[OK]" in text
    assert "motivo" not in text