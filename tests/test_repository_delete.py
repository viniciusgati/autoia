"""Testes da exclusão cascata completa de projetos (`DELETE /api/repositories/{id}`).

Cobrem: remoção de todos os registros dependentes (sem órfãos), cancelamento de
tasks não terminais, remoção do checkout do disco, liberação do nome, gates de
permissão (403/404), `delete-info` e a parada cooperativa de execuções ativas
(arquivo `.stop-<repo_id>` + kill seletivo por projeto).
"""

from __future__ import annotations

import os
import stat
import threading
import time

from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import (
    Pipeline,
    PipelineStep,
    Repository,
    RepositoryUser,
    Robot,
    RunEvent,
    StepArtifact,
    StepSummary,
    SubTask,
    Task,
    TaskProposal,
    TaskStep,
    TaskSummary,
    User,
)
from app.worker import exec_common, runner


def _make_loop_script(tmp_path, name="fake_loop") -> str:
    """Binário kimi fake que fica em loop SEM emitir saída no stdout (hang real)."""
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _new_app(settings, bare_repo):
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    return app, session_factory, client


def _wait_repo_procs(repo_ids: set[int], count: int, timeout: float = 10.0) -> None:
    """Espera até que `count` subprocessos dos repos informados estejam registrados
    em `_ACTIVE_PROCS` (filtra por repo_id — não depende do estado global)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with exec_common._ACTIVE_LOCK:
            registered = [
                rid for rid in exec_common._ACTIVE_PROCS.values() if rid in repo_ids
            ]
        if len(registered) >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"subprocessos dos repos {repo_ids} não registrados em {timeout}s")


# ---------- Exclusão básica ----------


def test_delete_returns_204_and_frees_name(settings, bare_repo):
    """DELETE retorna 204, o projeto some da listagem e o nome é liberado."""
    _, _, client = _new_app(settings, bare_repo)
    repo = client.post(
        "/api/repositories",
        json={"name": "repo-teste", "url": bare_repo, "default_branch": "main"},
    ).json()
    resp = client.delete(f"/api/repositories/{repo['id']}")
    assert resp.status_code == 204
    assert client.get("/api/repositories").json() == []

    recreated = client.post(
        "/api/repositories",
        json={"name": repo["name"], "url": repo["url"], "default_branch": "main"},
    )
    assert recreated.status_code == 201, recreated.text


def test_delete_removes_checkout_from_disk(flow):
    """O diretório `workspace_dir/<repo_id>` é removido do disco."""
    settings = flow["settings"]
    checkout = os.path.join(settings.workspace_dir, "1")
    assert os.path.isdir(checkout)
    resp = flow["client"].delete("/api/repositories/1")
    assert resp.status_code == 204
    assert not os.path.isdir(checkout)
    assert not os.path.exists(checkout)


def test_delete_unknown_repo_404_and_task_create_404(flow):
    """Projeto inexistente → 404; criar task referenciando projeto excluído → 404."""
    resp = flow["client"].delete("/api/repositories/999")
    assert resp.status_code == 404

    flow["client"].delete("/api/repositories/1")
    resp = flow["client"].post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "x", "kind": "feature"},
    )
    assert resp.status_code == 404


def test_delete_removes_all_related_records(flow):
    """Nenhum registro órfão após a exclusão: tasks, steps, eventos, resumos,
    subtasks, propostas, artifacts, membros e robôs/pipelines escopados."""
    client = flow["client"]
    sf = flow["session_factory"]
    pipe_scoped_id = None

    with sf() as s:
        # membro do projeto (o usuário em si não é apagado)
        u = User(name="Membro", email="membro@ex.com", password_hash="x", role="member")
        s.add(u)
        s.flush()
        s.add(RepositoryUser(repository_id=1, user_id=u.id, role="member"))

        # robô e pipeline escopados ao projeto
        robot = Robot(repository_id=1, name="dev-local", mission="m", role="implement")
        s.add(robot)
        s.flush()
        pipe = Pipeline(repository_id=1, name="pipe-local")
        s.add(pipe)
        s.flush()
        pipe_scoped_id = pipe.id
        s.add(PipelineStep(pipeline_id=pipe.id, position=0, robot_id=robot.id))

        # tasks em estados variados (ativas e terminais), cada uma com dados
        for i, status in enumerate(
            ["queued", "in_progress", "blocked", "needs_review",
             "waiting_approval", "paused", "done", "failed", "cancelled", "created"]
        ):
            extra = Task(
                repository_id=1, pipeline_id=1, title=f"t-{status}",
                kind="issue", status=status,
            )
            s.add(extra)
            s.flush()
            step = TaskStep(task_id=extra.id, position=0, robot_id=1, status="done")
            s.add(step)
            s.flush()
            s.add(RunEvent(step_id=step.id, seq=1, kind="assistant_text", payload={"content": "x"}))
            s.add(TaskSummary(task_id=extra.id, summary="s"))
            s.add(StepSummary(task_id=extra.id, step_id=step.id, position=0, attempt=1, summary="ss"))
            s.add(SubTask(task_id=extra.id, position=0, title="sub"))
            s.add(TaskProposal(task_id=extra.id, step_id=step.id, position=0, title="prop"))
            s.add(StepArtifact(step_id=step.id, filename="a.png", filepath="a.png"))
        s.commit()

    resp = client.delete("/api/repositories/1")
    assert resp.status_code == 204

    with sf() as s:
        assert s.query(Repository).filter(Repository.id == 1).count() == 0
        assert s.query(Task).filter(Task.repository_id == 1).count() == 0
        assert s.query(TaskStep).count() == 0
        assert s.query(RunEvent).count() == 0
        assert s.query(TaskSummary).count() == 0
        assert s.query(StepSummary).count() == 0
        assert s.query(SubTask).count() == 0
        assert s.query(TaskProposal).count() == 0
        assert s.query(StepArtifact).count() == 0
        assert s.query(RepositoryUser).filter(RepositoryUser.repository_id == 1).count() == 0
        assert s.query(Robot).filter(Robot.repository_id == 1).count() == 0
        assert s.query(Pipeline).filter(Pipeline.repository_id == 1).count() == 0
        assert s.query(PipelineStep).filter(PipelineStep.pipeline_id == pipe_scoped_id).count() == 0
        # o usuário membro continua existindo (não é um registro do projeto)
        assert s.query(User).filter(User.email == "membro@ex.com").count() == 1


def test_delete_info_counts_active_tasks(flow):
    """delete-info conta apenas tasks NÃO terminais (mesma enumeração do cancelamento)."""
    client = flow["client"]
    sf = flow["session_factory"]
    with sf() as s:
        for i, status in enumerate(
            ["queued", "in_progress", "blocked", "needs_review",
             "waiting_approval", "paused", "done", "failed", "cancelled"]
        ):
            s.add(Task(repository_id=1, pipeline_id=1, title=f"t{i}", kind="issue", status=status))
        s.commit()

    info = client.get("/api/repositories/1/delete-info").json()
    # 1 do flow (queued) + 1 queued + in_progress + blocked + needs_review +
    # waiting_approval + paused = 7 ativas; done/failed/cancelled não contam
    assert info["active_tasks"] == 7
    assert info["checkout_path"] is not None

    # projeto excluído → delete-info responde 404
    client.delete("/api/repositories/1")
    assert client.get("/api/repositories/1/delete-info").status_code == 404


# ---------- Permissões (auth ON) ----------


def test_delete_requires_admin(settings, bare_repo):
    """Não-admin e não-membro-admin → 403 (delete-info e DELETE); admin do
    projeto → 204; usuário comum sem membership não pode."""
    settings.auth_enabled = True
    app, sf, client = _new_app(settings, bare_repo)
    client.post(
        "/api/auth/register",
        json={"name": "Ana", "email": "ana@ex.com", "password": "senha123"},
    )
    repo = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    ).json()
    bob = client.post(
        "/api/users",
        json={"name": "Bob", "email": "bob@ex.com", "password": "senha456", "role": "member"},
    ).json()

    member_client = TestClient(app)
    assert member_client.post(
        "/api/auth/login", json={"email": "bob@ex.com", "password": "senha456"}
    ).status_code == 200

    # usuário comum (não-admin e não-membro) → 403 nos dois endpoints
    assert member_client.get(f"/api/repositories/{repo['id']}/delete-info").status_code == 403
    assert member_client.delete(f"/api/repositories/{repo['id']}").status_code == 403

    # torna Bob admin do projeto → DELETE 204
    resp = client.post(
        f"/api/repositories/{repo['id']}/members",
        json={"user_id": bob["id"], "role": "admin"},
    )
    assert resp.status_code == 201, resp.text
    assert member_client.delete(f"/api/repositories/{repo['id']}").status_code == 204
    assert member_client.get("/api/repositories").json() == []


# ---------- Parada cooperativa de execuções ativas ----------


def test_delete_stops_running_execution(settings, bare_repo, tmp_path):
    """DELETE durante um step running (executor em loop): o subprocesso ativo é
    morto rapidamente e a execução não prossegue."""
    settings.kimi_bin = _make_loop_script(tmp_path)
    settings.no_progress_timeout = 0  # isola o mecanismo do stop file
    settings.run_timeout = 60
    app, sf, client = _new_app(settings, bare_repo)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    task = client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    ).json()
    client.post(f"/api/tasks/{task['id']}/start")
    step_id = runner.claim_next(sf)
    assert step_id is not None

    result: dict = {}

    def _run():
        result["trigger"] = runner.execute_step(settings, sf, step_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _wait_repo_procs({1}, 1)
    with exec_common._ACTIVE_LOCK:
        assert 1 in exec_common._ACTIVE_PROCS.values(), "subprocesso do executor não registrado"

    resp = client.delete("/api/repositories/1")
    assert resp.status_code == 204

    thread.join(timeout=15)
    assert not thread.is_alive(), "execução não parou após a exclusão do projeto"
    assert result["trigger"] is None

    with sf() as s:
        assert s.query(Repository).count() == 0
        assert s.query(Task).count() == 0
        assert s.query(TaskStep).count() == 0
    assert not os.path.isdir(os.path.join(settings.workspace_dir, "1"))
    # subprocesso do projeto foi morto e desregistrado (kill seletivo por repo)
    with exec_common._ACTIVE_LOCK:
        assert 1 not in exec_common._ACTIVE_PROCS.values()


def test_stop_file_kills_proc_and_step_does_not_advance(settings, bare_repo, tmp_path):
    """Sinal de parada mata o subprocesso (rápido) e, com a task cancelada, o
    step não avança para a próxima fase (mesmo guard do runner)."""
    settings.kimi_bin = _make_loop_script(tmp_path)
    settings.no_progress_timeout = 0
    settings.run_timeout = 60
    app, sf, client = _new_app(settings, bare_repo)
    client.post(
        "/api/repositories", json={"name": "r", "url": bare_repo, "default_branch": "main"}
    )
    client.post(
        "/api/tasks",
        json={"repository_id": 1, "pipeline_id": 1, "title": "t", "description": "d", "kind": "feature"},
    )
    client.post("/api/tasks/1/start")
    step_id = runner.claim_next(sf)

    # task cancelada (como a API faz) + sinal de parada gravado no workspace
    assert client.post("/api/tasks/1/cancel").status_code == 200
    stop = exec_common.repo_stop_path(settings.workspace_dir, 1)
    with open(stop, "w", encoding="utf-8") as f:
        f.write(str(time.time()))

    start = time.monotonic()
    runner.execute_step(settings, sf, step_id)  # bloqueia até o watcher matar
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"kill demorou demais ({elapsed:.1f}s)"

    with sf() as s:
        t = s.get(Task, 1)
        assert t.status == "cancelled"
        step = s.get(TaskStep, step_id)
        assert step.status == "pending"
        assert "cancelada" in (step.error or "")
    # task cancelada não é mais reclamada (não avança para a próxima fase)
    assert runner.claim_next(sf) is None
    # subprocesso do projeto foi morto e desregistrado (kill seletivo por repo)
    with exec_common._ACTIVE_LOCK:
        assert 1 not in exec_common._ACTIVE_PROCS.values()


def test_stop_file_kill_is_selective_per_repo(settings, bare_repo, tmp_path):
    """Parar um projeto NÃO afeta execuções ativas de outros projetos; o arquivo
    de sinalização é removido após o processamento."""
    settings.kimi_bin = _make_loop_script(tmp_path)
    settings.no_progress_timeout = 0
    settings.run_timeout = 60
    app, sf, client = _new_app(settings, bare_repo)
    for name in ("r1", "r2"):
        client.post(
            "/api/repositories",
            json={"name": name, "url": bare_repo, "default_branch": "main"},
        )
    for i in (1, 2):
        client.post(
            "/api/tasks",
            json={"repository_id": i, "pipeline_id": 1, "title": f"t{i}", "kind": "feature"},
        )
        client.post(f"/api/tasks/{i}/start")
    step1 = runner.claim_next(sf)
    step2 = runner.claim_next(sf)
    assert step1 is not None and step2 is not None

    def _run(step_id, key, results):
        results[key] = runner.execute_step(settings, sf, step_id)

    results: dict = {}
    th1 = threading.Thread(target=_run, args=(step1, "s1", results), daemon=True)
    th2 = threading.Thread(target=_run, args=(step2, "s2", results), daemon=True)
    th1.start()
    th2.start()
    _wait_repo_procs({1, 2}, 2)

    try:
        stop1 = exec_common.repo_stop_path(settings.workspace_dir, 1)
        with open(stop1, "w", encoding="utf-8") as f:
            f.write("1")

        processed = runner.process_stop_files(settings.workspace_dir)
        assert processed == 1
        assert not os.path.exists(stop1)  # sinal removido (não causa paradas repetidas)

        th1.join(timeout=15)
        assert not th1.is_alive(), "execução do projeto 1 não parou"
        assert th2.is_alive(), "execução do projeto 2 foi afetada indevidamente"

        with exec_common._ACTIVE_LOCK:
            remaining = [rid for rid in exec_common._ACTIVE_PROCS.values()]
        assert 2 in remaining  # subprocesso do projeto 2 segue registrado
        assert 1 not in remaining
    finally:
        exec_common.kill_all_procs()
        th1.join(timeout=10)
        th2.join(timeout=10)


def test_recover_stale_steps_ignores_deleted_repo(flow):
    """`recover_stale_steps` não re-executa steps de projetos excluídos (o projeto
    some do banco, então não há task ativa para recuperar)."""
    sf = flow["session_factory"]
    with sf() as s:
        step = s.get(TaskStep, flow["task"]["steps"][0]["id"])
        step.status = "running"  # simula órfão de restart anterior
        s.commit()
    flow["client"].delete("/api/repositories/1")
    assert runner.recover_stale_steps(sf) == 0
