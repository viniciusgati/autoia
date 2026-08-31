"""Harness de benchmark de performance (baseline e pós-otimização).

Roda sem rede e sem executor real (kimi/opencode): monta uma task sintética com
`--events` RunEvent num SQLite de teste e mede 4 métricas reprodutíveis:

1. latência do endpoint ``GET /api/tasks/{id}/workspace`` (TestClient) — p50/p95
2. tempo de ``derive_task_timeline`` / ``derive_task_occurrences`` (500 eventos)
3. pico de memória (tracemalloc) da derivação da timeline/ocorrências
4. crescimento de logs/disco (bytes em ``log_dir`` + workspace + banco antes/depois)

Uso:

    python -m app.perf_bench [--events 500] [--samples 30]

Os números são relativos à máquina — o valor está em comparar a MESMA execução
antes e depois das otimizações (os critérios da história: p95 ≥ 30% mais rápido
no endpoint; tempo de derivação ≥ 50% menor; nunca truncar payloads).
"""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import make_engine, make_session_factory, utcnow
from app.main import create_app
from app.models import Repository, RunEvent, Task, TaskStep
from app.timeline import derive_task_occurrences, derive_task_timeline

DEFAULT_EVENTS = 500
DEFAULT_SAMPLES = 30
DEFAULT_STEPS = 5


def _dir_size(path: Path) -> int:
    """Soma recursiva dos tamanhos dos arquivos (0 para inexistente)."""
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _disk_bytes(settings: Settings, db_path: Path) -> int:
    """Bytes totais ocupados por logs + workspaces + banco (o que o autoia gera)."""
    return (
        _dir_size(Path(settings.log_dir))
        + _dir_size(Path(settings.workspace_dir))
        + _dir_size(db_path)
    )


def _percentile(xs: list[float], p: float) -> float:
    """Percentil empírico (ordinal): p=0.95 → p95."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


def build_synthetic_task(
    settings: Settings,
    events: int,
    steps: int = DEFAULT_STEPS,
    start_ts: datetime | None = None,
) -> dict:
    """Cria app + task sintética com `events` RunEvent distribuídos pelas fases.

    Não usa rede nem executor: o repositório é cadastrado direto no banco (URL
    fictícia) e os eventos são inseridos via SQLAlchemy. `start_ts` (default
    determinístico) ancora os timestamps dos eventos — o mesmo input sempre gera
    a MESMA timeline (paridade testável). Devolve o mapa com `app`,
    `session_factory`, `task_id`, `client` e `checkout` (inexistente — o endpoint
    workspace tolera ausência de `.git` no checkout).
    """
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    app = create_app(settings)
    client = TestClient(app)

    with session_factory() as s:
        repo = Repository(
            name="perf-bench",
            url="http://localhost/perf-bench.git",
            default_branch="main",
        )
        s.add(repo)
        s.commit()
        # Pipeline do seed (id 1) existe globalmente; robôs id 1..n também.
        task = Task(
            repository_id=repo.id,
            pipeline_id=1,
            title="benchmark sintético",
            description="task com %d RunEvent para medição" % events,
            kind="feature",
            status="in_progress",
            executor="kimi",
            budget_limit=100.0,
        )
        s.add(task)
        s.commit()

        robots = [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
        ][:steps]
        task_steps: list[TaskStep] = []
        for pos, robot_id in enumerate(robots):
            st = TaskStep(
                task_id=task.id,
                position=pos,
                robot_id=robot_id,
                status="done",
                attempt=1,
                goal=f"fase {pos}",
            )
            s.add(st)
            task_steps.append(st)
        s.commit()

        seq_by_step: dict[int, int] = {}
        now = start_ts or datetime(2026, 1, 1, 12, 0, 0)
        # Distribui os eventos entre os steps (balanceado), começando com um
        # `attempt_started` por fase e terminando com `phase_done`.
        per_step = max(events // len(task_steps), 1)
        for st in task_steps:
            seq_by_step[st.id] = 0
            ts = now
            s.add(RunEvent(
                step_id=st.id,
                seq=seq_by_step[st.id],
                ts=ts,
                kind="attempt_started",
                payload={"attempt": 1, "run": 1, "robot": "robo"},
                cost=0.0,
            ))
            seq_by_step[st.id] += 1
            ts += timedelta(seconds=1)
            # Ciclos realistas: texto + tool_call + tool_result intercalados.
            for i in range(per_step - 2):
                if i % 3 == 0:
                    s.add(RunEvent(
                        step_id=st.id,
                        seq=seq_by_step[st.id],
                        ts=ts,
                        kind="tool_call",
                        payload={
                            "tool_call": {
                                "function": {
                                    "name": "Bash" if i % 2 else "Read",
                                    "arguments": '{"command": "pytest -q"}'
                                    if i % 2
                                    else '{"path": "src/arquivo_%d.py"}' % (i % 50),
                                }
                            }
                        },
                        cost=0.01,
                    ))
                elif i % 3 == 1:
                    s.add(RunEvent(
                        step_id=st.id,
                        seq=seq_by_step[st.id],
                        ts=ts,
                        kind="tool_result",
                        payload={
                            "content": "saída %d do comando executado no checkout"
                            " — conteúdo completo, sem truncar.\n" % i * 4
                        },
                        cost=0.0,
                    ))
                else:
                    s.add(RunEvent(
                        step_id=st.id,
                        seq=seq_by_step[st.id],
                        ts=ts,
                        kind="assistant_text",
                        payload={
                            "content": "analisando o resultado %d da fase %d"
                            % (i, st.position)
                        },
                        cost=0.0,
                    ))
                seq_by_step[st.id] += 1
                ts += timedelta(seconds=1)
            s.add(RunEvent(
                step_id=st.id,
                seq=seq_by_step[st.id],
                ts=ts,
                kind="phase_done",
                payload={"next": None},
                cost=0.0,
            ))
        s.commit()
        task_id = task.id

    return {
        "app": app,
        "session_factory": session_factory,
        "task_id": task_id,
        "client": client,
    }


def measure_workspace_latency(client: TestClient, task_id: int, samples: int) -> dict:
    """Latência do endpoint workspace (TestClient) — p50/p95, ms."""
    # Primeira chamada "quente" (cache do SQLAlchemy/ORM, compile do plano).
    client.get(f"/api/tasks/{task_id}/workspace")
    latencies: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        resp = client.get(f"/api/tasks/{task_id}/workspace")
        latencies.append((time.perf_counter() - start) * 1000.0)
        if resp.status_code != 200:
            raise RuntimeError(
                f"endpoint workspace retornou {resp.status_code}: {resp.text[:300]}"
            )
    return {
        "p50_ms": round(_percentile(latencies, 0.50), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "samples": len(latencies),
    }


def measure_timeline(session_factory, task_id: int, iterations: int = 5) -> dict:
    """Tempo médio de derive_task_timeline/derive_task_occurrences, ms."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        tl_times: list[float] = []
        occ_times: list[float] = []
        for _ in range(iterations):
            start = time.perf_counter()
            derive_task_timeline(s, task)
            tl_times.append((time.perf_counter() - start) * 1000.0)
            start = time.perf_counter()
            derive_task_occurrences(s, task)
            occ_times.append((time.perf_counter() - start) * 1000.0)
        s.rollback()
    return {
        "timeline_ms": round(statistics.mean(tl_times), 2),
        "occurrences_ms": round(statistics.mean(occ_times), 2),
        "iterations": iterations,
    }


def measure_memory(session_factory, task_id: int) -> dict:
    """Pico de memória (tracemalloc) da derivação timeline + ocorrências, KiB."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        tracemalloc.start()
        try:
            derive_task_timeline(s, task)
            derive_task_occurrences(s, task)
        finally:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        s.rollback()
    return {
        "peak_kib": round(peak / 1024.0, 2),
        "current_kib": round(current / 1024.0, 2),
    }


def benchmark(
    settings: Settings,
    events: int = DEFAULT_EVENTS,
    samples: int = DEFAULT_SAMPLES,
) -> dict:
    """Roda o benchmark completo e devolve as 4 métricas numéricas."""
    db_path = Path(settings.database_url[len("sqlite:///"):])
    disk_before = _disk_bytes(settings, db_path)

    ctx = build_synthetic_task(settings, events=events)

    # Log de exemplo por step (o que o worker grava por fase) — mede crescimento
    # de disco com conteúdo realista (nunca truncado).
    with ctx["session_factory"]() as s:
        steps = s.query(TaskStep).filter(TaskStep.task_id == ctx["task_id"]).all()
        for st in steps:
            log_path = Path(settings.log_dir) / f"step_{st.id}.log"
            log_path.write_text(
                f"[{utcnow().isoformat()}] fase {st.position} — execução sintética\n"
                + "linha de log com o histórico completo da fase (sem truncar).\n" * 40,
                encoding="utf-8",
            )

    workspace_lat = measure_workspace_latency(
        ctx["client"], ctx["task_id"], samples=samples
    )
    timeline = measure_timeline(ctx["session_factory"], ctx["task_id"])
    memory = measure_memory(ctx["session_factory"], ctx["task_id"])
    disk_after = _disk_bytes(settings, db_path)

    return {
        "events": events,
        "workspace": workspace_lat,
        "timeline": timeline,
        "memory": memory,
        "disk": {
            "before_bytes": disk_before,
            "after_bytes": disk_after,
            "growth_bytes": disk_after - disk_before,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.perf_bench",
        description="Benchmark de performance da autoia (task sintética, sem rede/kimi).",
    )
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS, help="nº de RunEvent sintéticos")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="amostras do endpoint workspace")
    parser.add_argument("--workdir", type=str, default=None, help="dir de trabalho (default: tmp)")
    args = parser.parse_args(argv)

    base = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="autoia-perf-"))
    settings = Settings(
        database_url=f"sqlite:///{base / 'autoia.db'}",
        workspace_dir=str(base / "workspaces"),
        log_dir=str(base / "logs"),
        skills_dir=str(base / "skills"),
        kimi_bin="kimi",
        run_timeout=30,
        max_attempts=1,
        max_pm_decisions=0,
        step_mission=False,
        step_summary=False,
        auth_enabled=False,
    )
    settings.ensure_dirs()

    report = benchmark(settings, events=args.events, samples=args.samples)

    print(f"autoia — benchmark de performance ({report['events']} RunEvent sintéticos)")
    print("=" * 72)
    w = report["workspace"]
    print("1. Endpoint GET /api/tasks/{id}/workspace (TestClient)")
    print(f"   p50: {w['p50_ms']:>10.2f} ms   p95: {w['p95_ms']:>10.2f} ms   (n={w['samples']})")
    t = report["timeline"]
    print("2. Derivação da timeline/ocorrências (média de %d iterações)" % t["iterations"])
    print(f"   derive_task_timeline:    {t['timeline_ms']:>10.2f} ms")
    print(f"   derive_task_occurrences: {t['occurrences_ms']:>10.2f} ms")
    m = report["memory"]
    print("3. Memória (tracemalloc, pico durante a derivação)")
    print(f"   pico:    {m['peak_kib']:>10.2f} KiB")
    print(f"   atual:   {m['current_kib']:>10.2f} KiB")
    d = report["disk"]
    print("4. Logs/disco (workspaces + logs + banco)")
    print(
        f"   antes: {d['before_bytes']:>10d} B   depois: {d['after_bytes']:>10d} B"
        f"   crescimento: +{d['growth_bytes']} B"
    )
    print("=" * 72)
    print(f"workdir: {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
