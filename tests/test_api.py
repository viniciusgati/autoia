"""Testes de API + fluxo do worker (com kimi fake) ponta a ponta.

Cobrem: registro de repo, seed, criação/start de tarefa, avanço de fases,
merge final, orçamento -> needs_review -> revisão, e guardrail -> bloqueio.
O pipeline default agora é po-qa-dev-tester-merge (5 fases).
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import RunEvent, Task, TaskStep
from app.worker import runner

HARMLESS = [
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "tarefa concluída com sucesso"},
]

RISKY = [
    {"role": "assistant", "tool_calls": [{"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": '{"command":"rm -rf /"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
]

ONLY_TEXT = [
    {"role": "assistant", "content": "resposta única"},
]

PIPELINE_STEPS = 7


@pytest.fixture
def app_client(settings, bare_repo):
    app = create_app(settings)
    return TestClient(app)


@pytest.fixture
def registered_repo(app_client, bare_repo):
    response = app_client.post(
        "/api/repositories",
        json={"name": "repo-teste", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_and_start_task(app_client, title="implementar hello"):
    response = app_client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": title,
            "description": "criar um hello.py",
            "kind": "feature",
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "created"
    assert len(task["steps"]) == PIPELINE_STEPS

    response = app_client.post(f"/api/tasks/{task['id']}/start")
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "queued"
    assert task["branch"] == "autoia/task-1"
    assert task["steps"][0]["status"] == "pending"
    return task


# ---------- API básica ----------

def test_register_repository(app_client, bare_repo, settings):
    repo = app_client.post(
        "/api/repositories",
        json={"name": "r1", "url": bare_repo, "default_branch": "main"},
    ).json()
    assert repo["default_branch"] == "main"
    assert repo["local_path"].startswith(settings.workspace_dir)
    assert app_client.get("/api/repositories").status_code == 200


def test_clone_failure_allows_retry(app_client, bare_repo, settings):
    """Falha de clone não deixa checkout órfão: o retry com URL válida funciona."""
    url_invalida = "/tmp/nao-existe-xyz.git"

    primeiro = app_client.post(
        "/api/repositories",
        json={"name": "r-falha", "url": url_invalida, "default_branch": "main"},
    )
    assert primeiro.status_code == 400
    assert "falha ao clonar" in primeiro.text
    # registro foi removido e nenhum checkout órfão ficou no workspace
    assert app_client.get("/api/repositories").json() == []
    assert not os.path.isdir(os.path.join(settings.workspace_dir, "1"))

    retry = app_client.post(
        "/api/repositories",
        json={"name": "r-falha", "url": bare_repo, "default_branch": "main"},
    )
    assert retry.status_code == 201, retry.text
    assert os.path.isdir(os.path.join(settings.workspace_dir, "1"))


def test_register_empty_repository_creates_readme_and_commit(app_client, tmp_path):
    """Repo recém-criado no remote (sem branch): a autoia cria README + commit
    inicial na default e registra o repositório normalmente."""
    import subprocess

    src = tmp_path / "vazio"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(src)], check=True, capture_output=True)
    empty_bare = tmp_path / "vazio.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(empty_bare)], check=True, capture_output=True
    )

    response = app_client.post(
        "/api/repositories",
        json={"name": "r-vazio", "url": str(empty_bare), "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    repo = response.json()
    assert repo["default_branch"] == "main"

    # o bare agora tem a branch com README (visto via novo clone)
    dest = tmp_path / "clone"
    subprocess.run(["git", "clone", str(empty_bare), str(dest)], check=True, capture_output=True)
    tree = subprocess.run(
        ["git", "-C", str(dest), "ls-tree", "-r", "--name-only", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "README.md" in tree
    head = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline", "-1", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "commit inicial" in head


def test_register_repository_relative_workspace(bare_repo, tmp_path, monkeypatch):
    """Com workspace_dir relativo, o checkout é criado no lugar certo (não aninhado)."""
    monkeypatch.chdir(tmp_path)
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/rel.db",
            workspace_dir="data/workspaces",
            log_dir="data/logs",
        )
    )
    client = TestClient(app)

    repo = client.post(
        "/api/repositories",
        json={"name": "r-rel", "url": bare_repo, "default_branch": "main"},
    )
    assert repo.status_code == 201, repo.text
    dest = repo.json()["local_path"]
    assert os.path.isabs(dest)
    assert dest == os.path.join(os.getcwd(), "data", "workspaces", "1")
    assert os.path.isfile(os.path.join(dest, "README.md"))
    # nada aninhado (o bug antigo criava workspaces/workspaces/...)
    assert not os.path.isdir(os.path.join(dest, "data"))


def test_seed_robots_and_pipeline(app_client):
    robots = app_client.get("/api/robots").json()
    names = {r["name"] for r in robots}
    assert {"po", "qa", "developer", "tester", "avaliador", "merger", "pm"} <= names
    roles = {r["name"]: r["role"] for r in robots}
    assert roles["po"] == "refine"
    assert roles["qa"] == "review"
    assert roles["tester"] == "verify"
    assert roles["avaliador"] == "assess"
    assert roles["pm"] == "pm"

    pipelines = app_client.get("/api/pipelines").json()
    # pipeline único: avaliador pré-merge + deploy-tester pós-merge
    default = next(
        p for p in pipelines if p["name"] == "po-qa-dev-tester-avaliador-deploytest"
    )
    assert len(default["steps"]) == 7
    order = [st["robot"]["name"] for st in default["steps"]]
    assert order == ["po", "qa", "developer", "tester", "avaliador", "merger", "deploy-tester"]
    post = [st["post_merge"] for st in default["steps"]]
    assert post == [False, False, False, False, False, False, True]


def test_create_and_start_task(app_client, registered_repo):
    task = _create_and_start_task(app_client)
    assert task["repository_id"] == 1


def test_retry_step(app_client, registered_repo, settings):
    task = _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        step = s.get(TaskStep, task["steps"][0]["id"])
        step.status = "failed"
        step.error = "algum erro"
        s.commit()

    response = app_client.post(f"/api/tasks/{task['id']}/steps/0/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"][0]["attempt"] == 2
    assert body["steps"][0]["status"] == "pending"
    assert body["status"] == "queued"


def test_pause_resume_cancel(app_client, registered_repo):
    """Pausa/retoma/cancela uma task em andamento, com as transições válidas."""
    task = _create_and_start_task(app_client)

    # pausa
    body = app_client.post(f"/api/tasks/{task['id']}/pause").json()
    assert body["status"] == "paused"

    # pausar de novo é erro
    r = app_client.post(f"/api/tasks/{task['id']}/pause")
    assert r.status_code == 400

    # retoma → volta para a fila
    body = app_client.post(f"/api/tasks/{task['id']}/resume").json()
    assert body["status"] == "queued"

    # cancela
    body = app_client.post(f"/api/tasks/{task['id']}/cancel").json()
    assert body["status"] == "cancelled"
    assert "cancelada" in body["error"]

    # terminal: cancelar de novo dá erro
    r = app_client.post(f"/api/tasks/{task['id']}/cancel")
    assert r.status_code == 400


def test_cancel_during_run_stops_pipeline(settings, bare_repo, tmp_path, fake_kimi):
    """Task cancelada enquanto a fase roda: o worker não avança nem faz merge."""
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    # executa a primeira fase (po) normalmente
    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    # cancela durante a execução da fase 2 (como se o usuário cancelasse no meio)
    with session_factory() as s:
        t = s.get(Task, task["id"])
        t.status = "cancelled"
        s.commit()

    claimed = runner.claim_next(session_factory)
    assert claimed is None  # worker não reclama mais nada

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "cancelled"


def test_review_approve_and_cancel(app_client, registered_repo):
    task = _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        t = s.get(Task, task["id"])
        t.status = "needs_review"
        t.error = "orçamento estourado"
        s.commit()

    response = app_client.post(
        f"/api/tasks/{task['id']}/review",
        json={"action": "approve", "extra_budget": 5.0},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "in_progress"
    assert body["budget_limit"] == pytest.approx(6.0)

    with app_client.app.state.Session() as s:
        t = s.get(Task, task["id"])
        t.status = "needs_review"
        s.commit()

    response = app_client.post(
        f"/api/tasks/{task['id']}/review",
        json={"action": "cancel", "note": "não aprovado"},
    )
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "não aprovado"


def test_dashboard(app_client, registered_repo):
    _create_and_start_task(app_client)
    data = app_client.get("/api/dashboard").json()
    assert data["total_tasks"] >= 1
    assert "queued" in data["tasks_by_status"]


def test_dashboard_notices(app_client, registered_repo):
    """Avisos do dashboard: guardrail, needs_review, blocked, custo alto e arquitetura."""
    _create_and_start_task(app_client)
    with app_client.app.state.Session() as s:
        t1 = s.get(Task, 1)
        t1.status = "in_progress"
        t1.cost_spent = 0.9 * (t1.budget_limit or 1.0)  # custo alto (>= 80%)
        step = t1.steps[0]
        step.status = "guardrail_blocked"
        step.error = "guardrail: rm -rf"
        s.add(
            RunEvent(
                step_id=step.id,
                seq=99,
                kind="arch_metric",
                payload={"score": 80, "level": "alto", "reasons": ["A Dockerfile"]},
            )
        )
        s.add_all(
            [
                Task(
                    repository_id=1, pipeline_id=1, title="t2", kind="issue",
                    status="needs_review", error="orçamento estourado",
                ),
                Task(
                    repository_id=1, pipeline_id=1, title="t3", kind="issue",
                    status="blocked", error="conflito de merge",
                ),
            ]
        )
        s.commit()

    data = app_client.get("/api/dashboard").json()
    kinds = {n["kind"] for n in data["notices"]}
    assert kinds >= {"guardrail", "arch", "needs_review", "blocked", "budget_high"}
    by_kind = {n["kind"]: n for n in data["notices"]}
    assert by_kind["guardrail"]["level"] == "critical"
    assert by_kind["arch"]["level"] == "critical"
    assert by_kind["needs_review"]["level"] == "warning"
    # críticos ordenados antes dos warnings
    levels = [n["level"] for n in data["notices"]]
    assert levels == sorted(levels, key=lambda l: 0 if l == "critical" else 1)


# ---------- Fluxo do worker (kimi fake) ----------

OPENCODE_LINES = [
    {"type": "step_start", "part": {"type": "step-start"}},
    {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"command": "ls"}, "output": "ok"},
        },
    },
    {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "README.md"}, "output": "# repo"},
        },
    },
    {"type": "text", "part": {"type": "text", "text": "tarefa concluída com sucesso"}},
    {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop", "cost": 0.001}},
]


def test_worker_opencode_executor_runs_pipeline(settings, bare_repo, fake_kimi):
    """Task com executor=opencode roda todo o pipeline via opencode CLI fake,
    com tool calls, texto final e custo real vindo do step_finish."""
    settings.opencode_bin = fake_kimi(OPENCODE_LINES, verdict="ready_pass")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": "t-opencode",
            "description": "d",
            "kind": "feature",
            "executor": "opencode",
        },
    ).json()
    assert task["executor"] == "opencode"
    client.post(f"/api/tasks/{task['id']}/start")

    # task sem executor explícito → default kimi
    default_task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t-kimi", "description": "d", "kind": "feature"},
    ).json()
    assert default_task["executor"] == "kimi"

    for _ in range(PIPELINE_STEPS + 2):
        claimed = runner.claim_next(session_factory)
        if claimed is None:
            break
        runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done"
        assert all(st.status == "done" for st in t.steps)
        # eventos de tool call do opencode registrados, e custo real acumulado
        kinds = {e.kind for st in t.steps for e in st.events}
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        assert t.cost_spent > 0


def test_worker_advances_phases_and_merges(settings, bare_repo, tmp_path, fake_kimi):
    """po -> qa -> developer -> tester -> avaliador -> merger (merge+push) -> deploy-tester."""
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    for _ in range(PIPELINE_STEPS + 2):  # margem
        claimed = runner.claim_next(session_factory)
        if claimed is None:
            break
        runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done"
        assert all(st.status == "done" for st in t.steps)
        total_events = sum(len(st.events) for st in t.steps)
        assert total_events > 0

    # merge chegou no bare
    dest = tmp_path / "verify"
    import subprocess

    subprocess.run(["git", "clone", bare_repo, str(dest)], check=True, capture_output=True)
    assert (dest / "README.md").exists()


def test_worker_budget_hits_needs_review(settings, bare_repo, tmp_path, fake_kimi):
    settings.kimi_bin = fake_kimi(ONLY_TEXT)
    settings.task_budget = 0.01  # primeira interação já estoura
    settings.cost_per_interaction = 0.01
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "needs_review"
        assert "orçamento" in (t.error or "")
        assert t.steps[0].status == "pending"
        kinds = [e.kind for e in t.steps[0].events]
        assert "budget_hit" in kinds


def test_worker_guardrail_blocks(settings, bare_repo, tmp_path, fake_kimi):
    settings.kimi_bin = fake_kimi(RISKY)
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "failed"
        step = t.steps[0]
        assert step.status == "guardrail_blocked"
        assert "rm -rf" in (step.error or "")
        kinds = [e.kind for e in step.events]
        assert "guardrail_blocked" in kinds


def test_worker_arch_metric_event(settings, bare_repo, tmp_path, fake_kimi):
    """Task que adiciona Dockerfile gera evento arch_metric 'alto' e aviso no dashboard."""
    settings.kimi_bin = fake_kimi(HARMLESS, verdict="ready_pass", write_file="Dockerfile")
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    for _ in range(PIPELINE_STEPS + 2):  # margem
        claimed = runner.claim_next(session_factory)
        if claimed is None:
            break
        runner.execute_step(settings, session_factory, claimed)

    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done"
        # a métrica é gravada na última fase PRÉ-merge (o deploy-tester é pós-merge)
        merger = max(
            (st for st in t.steps if not st.post_merge), key=lambda st: st.position
        )
        events = {e.kind: e for e in merger.events}
        assert "arch_metric" in events
        payload = events["arch_metric"].payload
        assert payload["level"] == "alto"
        assert any("Dockerfile" in r for r in payload["reasons"])

    data = client.get("/api/dashboard").json()
    kinds = {n["kind"] for n in data["notices"]}
    # task concluída não gera aviso de arquitetura (não pede mais ação humana)
    assert "arch" not in kinds


def test_events_endpoint_order_desc(settings, bare_repo, tmp_path, fake_kimi):
    """O endpoint de eventos aceita order=desc (mais recente primeiro) — usado pelo Resumo."""
    settings.kimi_bin = fake_kimi(ONLY_TEXT)
    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = _create_and_start_task(client)

    claimed = runner.claim_next(session_factory)
    runner.execute_step(settings, session_factory, claimed)

    step_id = task["steps"][0]["id"]
    asc = client.get(f"/api/steps/{step_id}/events").json()
    desc = client.get(f"/api/steps/{step_id}/events?order=desc").json()
    assert len(asc) > 0
    assert [e["id"] for e in desc] == [e["id"] for e in reversed(asc)]
    # marcadores que alimentam a visão de chat: início de tentativa e prompt da fase
    kinds = {e["kind"] for e in asc}
    assert "attempt_started" in kinds
    assert "prompt" in kinds


# ---------- Fluxo ponta-a-ponta com subtarefas ----------


def test_worker_subtasks_full_pipeline(settings, bare_repo, tmp_path, monkeypatch):
    """Task com subtarefas definidas manualmente → pipeline completo → merge.

    Fluxo: PO → QA → developer (itera 3 subtarefas) → tester (verifica cada uma)
    → avaliador → merger (merge+push) → deploy-tester (pós-merge).

    Usa mock de kimi_exec.run_kimi para evitar subprocess; os veredictos são
    injetados via monkeypatch de verdicts.read_verdict.
    """
    import app.worker.kimi_exec as ke
    import app.verdicts as vmod

    settings.task_budget = 100.0
    from app.db import make_engine, make_session_factory
    from app.models import SubTask

    app = create_app(settings)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    client = TestClient(app)

    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )

    # Cria task com 3 subtarefas manuais
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": 1,
            "pipeline_id": 1,
            "title": "task com subtarefas",
            "description": "ideia crua",
            "kind": "feature",
            "subtasks": [
                {"title": "Sub 1", "description": "parte A", "acceptance_criteria": "- [ ] crit A"},
                {"title": "Sub 2", "description": "parte B", "acceptance_criteria": "- [ ] crit B"},
                {"title": "Sub 3", "description": "parte C", "acceptance_criteria": "- [ ] crit C"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert len(task["subtasks"]) == 3
    client.post(f"/api/tasks/{task['id']}/start")

    # Mock: kimi sempre retorna sucesso
    def fake_run_kimi(prompt, **kwargs):
        return ke.KimiOutcome(exit_code=0, final_text="ok", interaction_count=1)

    _read_count = 0

    def fake_read_verdict(checkout):
        nonlocal _read_count
        _read_count += 1
        # QA (review) é a primeira leitura — espera READY
        if _read_count == 1:
            return "READY\nSUMMARY: historia ok"
        return "PASS\nSUMMARY: ok"

    def fake_remove_verdict(checkout):
        pass

    with monkeypatch.context() as mp:
        mp.setattr(ke, "run_kimi", fake_run_kimi)
        mp.setattr(vmod, "read_verdict", fake_read_verdict)
        mp.setattr(vmod, "remove_verdict", fake_remove_verdict)

        # Avança todas as fases (7 passos + margem)
        for _ in range(PIPELINE_STEPS + 2):
            claimed = runner.claim_next(session_factory)
            if claimed is None:
                break
            runner.execute_step(settings, session_factory, claimed)

    # Verifica estado final
    with session_factory() as s:
        t = s.get(Task, task["id"])
        assert t.status == "done", f"task status={t.status}, error={t.error}"
        assert all(st.status == "done" for st in t.steps), [
            (st.position, st.status, st.error) for st in t.steps
        ]

        subs = (
            s.query(SubTask)
            .filter(SubTask.task_id == task["id"])
            .order_by(SubTask.position)
            .all()
        )
        assert len(subs) == 3
        for sub in subs:
            assert sub.status == "done", f"sub {sub.position} status={sub.status}"
            assert sub.verdict == "PASS", f"sub {sub.position} verdict={sub.verdict}"
            assert sub.summary, f"sub {sub.position} sem resumo"

        # Eventos de subtarefa: implement no developer, verify no tester
        dev_step = t.steps[2]
        dev_kinds = {e.kind for e in dev_step.events}
        for expected in ("subtask_start", "subtask_implemented"):
            assert expected in dev_kinds, f"evento {expected} ausente no developer"

        tester_step = t.steps[3]
        tester_kinds = {e.kind for e in tester_step.events}
        assert "subtask_verified" in tester_kinds, "subtask_verified ausente no tester"

        total_events = sum(len(st.events) for st in t.steps)
        assert total_events > 0

        # Fase implement deve ter diff_stat (pode ser vazio em testes mock)
        assert dev_step.diff_stat is not None, "developer com diff_stat=None"

    # merge chegou no bare
    dest = tmp_path / "verify_subtasks"
    import subprocess
    subprocess.run(["git", "clone", bare_repo, str(dest)], check=True, capture_output=True)
    assert (dest / "README.md").exists()
