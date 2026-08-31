"""Testes do harness de benchmark de performance (`app.perf_bench`).

Validam que o harness executa sem rede/sem kimi e produz as 4 métricas com
valores numéricos — SEM asserção de tempo mínimo (números variam por máquina;
os critérios de melhoria relativa são medidos comparando baseline × pós, fora
da suíte). Também verificam a PARIDADE determinística da timeline (diff vazio
antiga × nova) e a contagem de queries do endpoint workspace (≤ 3 leituras por
ocorrência com 500 eventos).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
PARITY_SNAPSHOT = FIXTURES / "timeline_parity_500.json"


@pytest.fixture
def bench_settings(tmp_path) -> Settings:
    """Settings de teste para o benchmark (tudo em tmp_path, sem rede)."""
    return Settings(
        database_url=f"sqlite:///{tmp_path}/bench.db",
        workspace_dir=str(tmp_path / "workspaces"),
        log_dir=str(tmp_path / "logs"),
        skills_dir=str(tmp_path / "skills"),
        kimi_bin="kimi",
        run_timeout=30,
        max_attempts=1,
        max_pm_decisions=0,
        step_mission=False,
        step_summary=False,
        auth_enabled=False,
    )


def test_benchmark_produces_four_numeric_metrics(bench_settings):
    """O harness roda de ponta a ponta e devolve as 4 métricas numéricas."""
    from app.perf_bench import benchmark

    report = benchmark(bench_settings, events=200, samples=5)

    # 1. latência do endpoint workspace
    ws = report["workspace"]
    assert ws["samples"] == 5
    assert ws["p50_ms"] >= 0
    assert ws["p95_ms"] >= 0

    # 2. derivação da timeline/ocorrências
    tl = report["timeline"]
    assert tl["iterations"] > 0
    assert tl["timeline_ms"] >= 0
    assert tl["occurrences_ms"] >= 0

    # 3. memória (tracemalloc)
    mem = report["memory"]
    assert mem["peak_kib"] >= 0
    assert mem["current_kib"] >= 0

    # 4. crescimento de logs/disco
    disk = report["disk"]
    assert disk["growth_bytes"] > 0  # criou banco + logs da task sintética


def test_benchmark_main_prints_report(bench_settings, capsys, tmp_path):
    """`python -m app.perf_bench` imprime as 4 métricas com valores numéricos."""
    from app.perf_bench import main

    workdir = tmp_path / "bench-main"
    code = main(["--workdir", str(workdir), "--events", "100", "--samples", "3"])
    assert code == 0
    out = capsys.readouterr().out
    assert "benchmark de performance" in out
    # As 4 métricas aparecem com números.
    assert "p95:" in out and "ms" in out
    assert "derive_task_timeline:" in out
    assert "derive_task_occurrences:" in out
    assert "pico:" in out
    assert "crescimento:" in out


def test_timeline_parity_antiga_x_nova(bench_settings):
    """Paridade determinística: a derivação atual (500 eventos) é IDÊNTICA à
    snapshot da versão pré-otimização (diff vazio) — nenhuma otimização mudou
    a saída da timeline/ocorrências para o mesmo input."""
    from app.models import Task
    from app.perf_bench import build_synthetic_task
    from app.timeline import derive_task_occurrences, derive_task_timeline

    assert PARITY_SNAPSHOT.is_file(), "fixture de paridade ausente"
    snapshot = json.loads(PARITY_SNAPSHOT.read_text(encoding="utf-8"))

    ctx = build_synthetic_task(bench_settings, events=500, steps=5)
    with ctx["session_factory"]() as s:
        task = s.get(Task, ctx["task_id"])
        timeline = derive_task_timeline(s, task)
        occurrences = derive_task_occurrences(s, task)

    assert timeline == snapshot["timeline"], (
        "derive_task_timeline divergiu da versão anterior (mesmo input)"
    )
    assert occurrences == snapshot["occurrences"], (
        "derive_task_occurrences divergiu da versão anterior (mesmo input)"
    )


def test_workspace_queries_le_3_por_ocorrencia(bench_settings):
    """O endpoint workspace com 500 eventos executa ≤ 3 consultas de leitura por
    ocorrência (5 ocorrências → ≤ 15 SELECTs) — sem N+1 por evento/step."""
    from sqlalchemy import event as sa_event

    from app.perf_bench import build_synthetic_task

    ctx = build_synthetic_task(bench_settings, events=500, steps=5)
    engine = ctx["app"].state.Session.kw["bind"]
    client = ctx["client"]

    selects: list[str] = []

    @sa_event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        st = statement.strip()
        if st.upper().startswith("SELECT"):
            selects.append(st)

    # warm-up (plano de execução/SQLAlchemy compile)
    client.get(f"/api/tasks/{ctx['task_id']}/workspace")
    selects.clear()

    resp = client.get(f"/api/tasks/{ctx['task_id']}/workspace")
    assert resp.status_code == 200

    n_occurrences = len(resp.json()["occurrences"])
    assert n_occurrences == 5
    assert len(selects) <= 3 * n_occurrences, (
        f"{len(selects)} SELECTs para {n_occurrences} ocorrências "
        f"(limite: {3 * n_occurrences})"
    )


# ---------------------------------------------------------------------------
# Subtarefa 4: liberação de disco (workspaces) — AUTOIA_KEEP_WORKSPACES
# ---------------------------------------------------------------------------


def _make_sf(settings) -> "object":
    """Engine + schema + session_factory para testes de sessão direta."""
    from app.db import ensure_schema, make_engine, make_session_factory

    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    return make_session_factory(engine)


def _make_task_with_workspace(
    session_factory, workspace_dir: str, task_id: int, status: str
) -> str:
    """Cria repo + task no banco e o diretório do workspace; devolve o checkout."""
    from app.models import Repository, Task

    with session_factory() as s:
        repo = Repository(
            name=f"cleanup-repo-{task_id}",
            url="http://localhost/cleanup.git",
            default_branch="main",
        )
        s.add(repo)
        s.commit()
        task = Task(
            repository_id=repo.id,
            pipeline_id=1,
            title=f"t{task_id}",
            description="d",
            kind="feature",
            status=status,
        )
        s.add(task)
        s.commit()
        repo_id = repo.id
    checkout = os.path.join(workspace_dir, str(repo_id), f"task_{task_id}")
    Path(checkout).mkdir(parents=True)
    (Path(checkout) / "arquivo.txt").write_text("x", encoding="utf-8")
    return checkout


def test_keep_workspaces_0_remove_task_done(bench_settings):
    """Com AUTOIA_KEEP_WORKSPACES=0, task concluída tem o workspace removido."""
    from app.models import Repository, TASK_DONE
    from app.worker.runner import _effective, _maybe_release_workspace

    sf = _make_sf(bench_settings)
    bench_settings.keep_workspaces = False
    checkout = _make_task_with_workspace(sf, bench_settings.workspace_dir, 1, TASK_DONE)

    with sf() as s:
        repo = s.get(Repository, 1)
        eff = _effective(bench_settings, repo)
    _maybe_release_workspace(eff, sf, 1, checkout)
    assert not Path(checkout).exists(), "workspace deveria ter sido removido"


def test_keep_workspaces_1_preserva_task_done(bench_settings):
    """Com AUTOIA_KEEP_WORKSPACES=1 (default), task concluída preserva o workspace."""
    from app.models import Repository, TASK_DONE
    from app.worker.runner import _effective, _maybe_release_workspace

    sf = _make_sf(bench_settings)
    bench_settings.keep_workspaces = True
    checkout = _make_task_with_workspace(sf, bench_settings.workspace_dir, 1, TASK_DONE)

    with sf() as s:
        repo = s.get(Repository, 1)
        eff = _effective(bench_settings, repo)
    _maybe_release_workspace(eff, sf, 1, checkout)
    assert Path(checkout).is_dir(), "workspace deveria ser preservado"


def test_keep_workspaces_0_nao_toca_task_ativa(bench_settings):
    """Com AUTOIA_KEEP_WORKSPACES=0, task ATIVA (in_progress) preserva o workspace."""
    from app.models import Repository, TASK_IN_PROGRESS
    from app.worker.runner import _effective, _maybe_release_workspace

    sf = _make_sf(bench_settings)
    bench_settings.keep_workspaces = False
    checkout = _make_task_with_workspace(sf, bench_settings.workspace_dir, 1, TASK_IN_PROGRESS)

    with sf() as s:
        repo = s.get(Repository, 1)
        eff = _effective(bench_settings, repo)
    _maybe_release_workspace(eff, sf, 1, checkout)
    assert Path(checkout).is_dir(), "workspace de task ativa deve ser preservado"


def test_cleanup_remove_apenas_concluidas_antigas(bench_settings):
    """`autoia-cleanup` remove workspaces de tasks done/failed mais antigas que N
    dias e não toca tasks ativas nem concluídas recentes."""
    from datetime import timedelta

    from app.db import utcnow
    from app.main import _cleanup_workspaces
    from app.models import (
        TASK_DONE,
        TASK_FAILED,
        TASK_IN_PROGRESS,
        Repository,
        Task,
        TaskStep,
    )

    sf = _make_sf(bench_settings)
    workspace_dir = bench_settings.workspace_dir

    def _task(repo_id: int, status: str) -> int:
        with sf() as s:
            task = Task(
                repository_id=repo_id,
                pipeline_id=1,
                title="t",
                description="d",
                kind="feature",
                status=status,
            )
            s.add(task)
            s.commit()
            return task.id

    def _step(task_id: int, finished_at) -> None:
        with sf() as s:
            s.add(TaskStep(
                task_id=task_id,
                position=0,
                robot_id=1,
                status="done",
                finished_at=finished_at,
            ))
            s.commit()

    def _workspace(repo_id: int, task_id: int) -> Path:
        ws = Path(workspace_dir) / str(repo_id) / f"task_{task_id}"
        ws.mkdir(parents=True)
        (ws / "f").write_text("x", encoding="utf-8")
        return ws

    with sf() as s:
        r1 = Repository(name="cleanup-r1", url="http://x", default_branch="main")
        r2 = Repository(name="cleanup-r2", url="http://x", default_branch="main")
        r3 = Repository(name="cleanup-r3", url="http://x", default_branch="main")
        r4 = Repository(name="cleanup-r4", url="http://x", default_branch="main")
        s.add_all([r1, r2, r3, r4])
        s.commit()
        ids = [r1.id, r2.id, r3.id, r4.id]

    old = utcnow() - timedelta(days=30)
    recent = utcnow() - timedelta(hours=1)

    done_old = _task(ids[0], TASK_DONE)
    _step(done_old, old)
    ws_done_old = _workspace(ids[0], done_old)

    failed_old = _task(ids[1], TASK_FAILED)
    _step(failed_old, old)
    ws_failed_old = _workspace(ids[1], failed_old)

    done_recent = _task(ids[2], TASK_DONE)
    _step(done_recent, recent)
    ws_done_recent = _workspace(ids[2], done_recent)

    active = _task(ids[3], TASK_IN_PROGRESS)
    ws_active = _workspace(ids[3], active)

    result = _cleanup_workspaces(sf, workspace_dir, days=7)

    assert not ws_done_old.exists(), "workspace done antiga deveria ser removido"
    assert not ws_failed_old.exists(), "workspace failed antiga deveria ser removido"
    assert ws_done_recent.is_dir(), "workspace done recente deve ser preservado"
    assert ws_active.is_dir(), "workspace de task ativa deve ser preservado"
    assert result["removed"] == 2
    assert result["skipped_active"] >= 1
    assert result["skipped_recent"] >= 1

