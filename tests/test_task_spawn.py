"""Testes da ferramenta de propostas de tasks filhas durante a execução
(autoia_tasks.json → _spawn_tasks → propostas pendentes de aprovação humana).

O worker NUNCA cria a task automaticamente: grava a proposta `pending` (dedup por
task_id + title) e o humano decide aceitar/rejeitar (ver test_proposals.py).
"""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Task, TaskStep
from app.worker import runner
from conftest import rich_proposal

HARMLESS = [{"role": "assistant", "content": "tarefa concluída"}]


@pytest.fixture
def spawn_flow(settings, bare_repo):
    """App + session_factory + repo (allow_auto_tasks é obsoleto/ignorado)."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={
            "name": "r",
            "url": bare_repo,
            "default_branch": "main",
            "allow_auto_tasks": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": resp.json()["id"],
    }


def _kimi_spawn(tmp_path, tasks: list[dict]) -> str:
    """Fake kimi que emite JSONL e, quando o prompt NÃO pede veredicto (fases de
    implementação/refino), escreve autoia_tasks.json no cwd. Em fases verify
    escreve apenas o veredicto PASS — reproduz o caso real (arquivo escrito uma
    vez e consumido pelo worker no fim da fase)."""
    script = tmp_path / f"kimi_spawn_{len(list(tmp_path.glob('kimi_spawn_*')))}"
    payload = json.dumps(tasks)
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'role': 'assistant', 'content': 'ok'}))\n"
        "import os\n"
        "prompt = sys.argv[sys.argv.index('-p') + 1] if '-p' in sys.argv else ''\n"
        "if 'VEREDICTO' not in prompt.upper():\n"
        f"    with open('autoia_tasks.json', 'w') as f:\n"
        f"        f.write({payload!r})\n"
        "if 'VEREDICTO' in prompt.upper() and 'PASS' in prompt:\n"
        "    with open('autoia_verdict.txt', 'w') as f:\n"
        "        f.write('PASS\\nSUMMARY: ok')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _simple_pipeline(flow) -> int:
    """Pipeline com 2 fases do propositor (role propose) e retorna o id.

    Propostas (autoia_tasks.json) são papel EXCLUSIVO do propositor — nenhum outro
    robô recebe a ferramenta de spawn no prompt nem tem o arquivo consumido.
    """
    client = flow["client"]
    robots = client.get(f"/api/robots?repository_id={flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "spawn-pipeline",
            "repository_id": flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["propositor"]},
                {"position": 1, "robot_id": by_name["propositor"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _claim_and_execute(flow) -> None:
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)


def _all_tasks(flow) -> list[dict]:
    return flow["client"].get(f"/api/tasks?repository_id={flow['repo_id']}").json()


def _proposals(flow, task_id: int) -> list[dict]:
    return flow["client"].get(f"/api/tasks/{task_id}/proposals").json()


def test_robo_cria_proposta_de_task_filha(spawn_flow, tmp_path):
    """propositor escreve autoia_tasks.json → o worker grava uma proposta `pending`, sem
    criar a task filha automaticamente."""
    settings = spawn_flow["settings"]
    entry = rich_proposal(title="filha", kind="bug")
    settings.kimi_bin = _kimi_spawn(tmp_path, [entry])
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    pipeline_id = _simple_pipeline(spawn_flow)
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)

    # Nenhuma task filha foi criada — só a proposta ficou pendente
    assert len(_all_tasks(spawn_flow)) == 1

    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["status"] == "pending"
    assert prop["title"] == "filha"
    assert prop["kind"] == "bug"
    assert prop["description"] == entry["description"]
    assert prop["target_repository_id"] is None
    assert prop["accepted_task_id"] is None

    # evento de auditoria no step do pai
    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        step = next(st for st in parent.steps if st.position == 0)
        spawned = [e for e in step.events if e.kind == "task_spawned"]
        assert len(spawned) == 1
        assert spawned[0].payload["count"] == 1
        assert "filha" in spawned[0].payload["titles"]


def test_reexecucao_nao_duplica_proposta(spawn_flow, tmp_path):
    """Dedup por task_id + title: se a fase re-executa e o robô propõe a mesma
    tarefa, não cria uma segunda proposta pendente."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, [rich_proposal(title="filha")])
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    pipeline_id = _simple_pipeline(spawn_flow)
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)  # po (escreve autoia_tasks.json)
    with spawn_flow["session_factory"]() as s:
        s.query(TaskStep).update({"status": "pending"})  # re-executa a fase
    _claim_and_execute(spawn_flow)  # po de novo

    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending"


def test_propostas_independentes_de_allow_auto_tasks(settings, bare_repo, tmp_path):
    """allow_auto_tasks é obsoleto: propostas são gravadas sempre, independente do
    flag do repositório."""
    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    resp = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main", "allow_auto_tasks": False},
    )
    assert resp.status_code == 201, resp.text
    repo_id = resp.json()["id"]

    settings.kimi_bin = _kimi_spawn(tmp_path, [rich_proposal(title="filha")])
    settings.task_budget = 100.0

    robots = client.get(f"/api/robots?repository_id={repo_id}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "p",
            "repository_id": repo_id,
            "steps": [{"position": 0, "robot_id": by_name["propositor"]}],
        },
    )
    resp = client.post(
        "/api/tasks",
        json={"repository_id": repo_id, "pipeline_id": resp.json()["id"], "title": "pai", "description": "d"},
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    flow = {"settings": settings, "session_factory": session_factory, "client": client, "repo_id": repo_id}
    _claim_and_execute(flow)

    proposals = _proposals(flow, parent_id)
    assert len(proposals) == 1
    assert proposals[0]["title"] == "filha"


def test_spawn_bloqueado_em_task_com_subtarefas(spawn_flow, tmp_path):
    """Exclusividade: task com subtarefas NÃO gera propostas (autoia_tasks.json é
    ignorado) — as subtarefas já dividem o trabalho na mesma branch e propostas
    criariam branches paralelas no mesmo repo (risco de conflito de merge)."""
    settings = spawn_flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, [rich_proposal(title="netinha", kind="chore")])
    settings.task_budget = 100.0
    client = spawn_flow["client"]

    robots = client.get(f"/api/robots?repository_id={spawn_flow['repo_id']}").json()
    by_name = {r["name"]: r["id"] for r in robots}
    resp = client.post(
        "/api/pipelines",
        json={
            "name": "subtask-pipeline",
            "repository_id": spawn_flow["repo_id"],
            "steps": [
                {"position": 0, "robot_id": by_name["propositor"]},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": spawn_flow["repo_id"],
            "pipeline_id": resp.json()["id"],
            "title": "pai-com-subtarefas",
            "description": "d",
            "kind": "feature",
            "subtasks": [
                {"title": "sub 1", "description": "d1", "acceptance_criteria": "c1"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")

    _claim_and_execute(spawn_flow)  # propositor (escreve autoia_tasks.json)

    assert len(_all_tasks(spawn_flow)) == 1  # só o pai; nada criado automaticamente
    proposals = _proposals(spawn_flow, parent_id)
    assert len(proposals) == 0

    # evento de auditoria registra o bloqueio no step
    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        blocked = [
            e for st in parent.steps for e in st.events if e.kind == "task_spawn_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0].payload["reason"] == "task já tem subtarefas"


def _run_proposer(flow, tmp_path, entries: list[dict]) -> tuple[int, list[dict]]:
    """Cria a pipeline de propositor, roda a fase com `entries` como
    autoia_tasks.json e retorna (parent_id, propostas gravadas)."""
    settings = flow["settings"]
    settings.kimi_bin = _kimi_spawn(tmp_path, entries)
    settings.task_budget = 100.0
    client = flow["client"]
    pipeline_id = _simple_pipeline(flow)
    resp = client.post(
        "/api/tasks",
        json={
            "repository_id": flow["repo_id"],
            "pipeline_id": pipeline_id,
            "title": "pai",
            "description": "d",
            "kind": "feature",
        },
    )
    parent_id = resp.json()["id"]
    client.post(f"/api/tasks/{parent_id}/start")
    _claim_and_execute(flow)
    return parent_id, _proposals(flow, parent_id)


def test_surface_define_pipeline_da_proposta(spawn_flow, tmp_path):
    """`surface` no autoia_tasks.json → a proposta já nasce com a pipeline
    deep-v2-<surface> (sem depender do default do repo na hora do aceite)."""
    _, proposals = _run_proposer(
        spawn_flow, tmp_path, [rich_proposal(title="filha", surface="fullstack")]
    )
    pipelines = spawn_flow["client"].get(
        f"/api/pipelines?repository_id={spawn_flow['repo_id']}"
    ).json()
    fullstack = next(p for p in pipelines if p["name"] == "deep-v2-fullstack")
    assert proposals[0]["pipeline_id"] == fullstack["id"]


def test_pipeline_explicito_tem_prioridade_sobre_surface(spawn_flow, tmp_path):
    """`pipeline` (nome exato) vence `surface`."""
    _, proposals = _run_proposer(
        spawn_flow,
        tmp_path,
        [rich_proposal(title="filha", surface="backend", pipeline="deep-v2-frontend-lean")],
    )
    pipelines = spawn_flow["client"].get(
        f"/api/pipelines?repository_id={spawn_flow['repo_id']}"
    ).json()
    chosen = next(p for p in pipelines if p["name"] == "deep-v2-frontend-lean")
    assert proposals[0]["pipeline_id"] == chosen["id"]


def test_depends_on_vira_encadeamento_da_proposta(spawn_flow, tmp_path):
    """`depends_on` (1-based no arquivo) é traduzido para a posição real da
    proposta predecessora."""
    entries = [
        rich_proposal(title="primeira"),
        rich_proposal(title="segunda", depends_on=[1]),
    ]
    _, proposals = _run_proposer(spawn_flow, tmp_path, entries)
    first = next(p for p in proposals if p["title"] == "primeira")
    second = next(p for p in proposals if p["title"] == "segunda")
    assert first["depends_on"] == []
    assert second["depends_on"] == [first["position"]]


def test_descricao_curta_descarta_entrada(spawn_flow, tmp_path):
    """Entrada com descrição vaga é DESCARTADA com evento `task_spawn_rejected`
    (motivo visível na timeline) — a entrada válida seguinte é gravada."""
    entries = [
        {"title": "vaga", "description": "curta", "kind": "feature"},
        rich_proposal(title="boa"),
    ]
    parent_id, proposals = _run_proposer(spawn_flow, tmp_path, entries)
    assert [p["title"] for p in proposals] == ["boa"]

    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        step = next(st for st in parent.steps if st.position == 0)
        rejected = [e for e in step.events if e.kind == "task_spawn_rejected"]
        assert len(rejected) == 1
        assert rejected[0].payload["rejected"][0]["title"] == "vaga"
        assert "curta" in rejected[0].payload["rejected"][0]["reason"]


def test_surface_invalida_gera_warning_e_usa_fallback(spawn_flow, tmp_path):
    """`surface` desconhecida não descarta a proposta: grava com o fallback e
    deixa evento `task_spawn_warning` com o motivo."""
    parent_id, proposals = _run_proposer(
        spawn_flow, tmp_path, [rich_proposal(title="filha", surface="mobile")]
    )
    assert len(proposals) == 1
    assert proposals[0]["pipeline_id"] is None
    with spawn_flow["session_factory"]() as s:
        parent = s.get(Task, parent_id)
        step = next(st for st in parent.steps if st.position == 0)
        warnings = [e for e in step.events if e.kind == "task_spawn_warning"]
        assert len(warnings) == 1
        assert "surface" in warnings[0].payload["warnings"][0]["reason"]
