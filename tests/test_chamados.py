"""Testes do fluxo de CHAMADOS (subsistema paralelo à pipeline).

Cobre: catálogo de tipos de etapa, hierarquia Projeto > Épico > Chamado, execução
de ferramenta (assistente LLM com checkout read-only), avaliação de fechamento
(next_stage/resposta/cancelar/concluir, decisão inválida), recuperação de etapas
órfãs e geração de conteúdo de Projeto/Épico por LLM.
"""

from __future__ import annotations

import json

import pytest

from app.models import ChamadoStage


@pytest.fixture
def env(settings, bare_repo):
    """App + session_factory + repositório registrado, prontos para o fluxo de chamados."""
    from fastapi.testclient import TestClient

    from app.db import make_engine, make_session_factory
    from app.main import create_app

    app = create_app(settings)
    session_factory = make_session_factory(make_engine(settings.database_url))
    client = TestClient(app)
    response = client.post(
        "/api/repositories",
        json={"name": "r", "url": bare_repo, "default_branch": "main"},
    )
    assert response.status_code == 201, response.text
    return {
        "settings": settings,
        "session_factory": session_factory,
        "client": client,
        "repo_id": response.json()["id"],
    }


def _run_action(env, stage_id: int, action: str) -> None:
    """Executa uma ação de etapa diretamente (o chamado-worker é um processo separado;
    nos testes rodamos o runner síncrono)."""
    from app.worker import chamado_runner

    chamado_runner.execute_stage_action(env["settings"], env["session_factory"], stage_id, action)


def _claim(env):
    from app.worker import chamado_runner

    claimed = chamado_runner.claim_next_stage(env["session_factory"])
    assert claimed is not None, "nenhuma etapa com ação pendente"
    return claimed


def _new_chamado(env, *, title="Bug grave", description="usuário não consegue logar", **extra):
    data = {"repository_id": env["repo_id"], "title": title, "description": description}
    data.update(extra)
    r = env["client"].post("/api/chamados", json=data)
    assert r.status_code == 201, r.text
    return r.json()


# ── Catálogo ─────────────────────────────────────────────────────────────────

def test_catalog_seeded(env):
    types = env["client"].get("/api/chamado-stage-types").json()
    by_name = {t["name"]: t for t in types}
    assert "entrada" in by_name and "analise" in by_name
    assert by_name["entrada"]["is_initial"] is True
    assert "assistente" in by_name["entrada"]["allowed_tools"]
    assert "next:analise" in by_name["entrada"]["close_options"]


def test_stage_type_custom_per_repo(env):
    r = env["client"].post(
        "/api/chamado-stage-types",
        json={
            "repository_id": env["repo_id"],
            "name": "triagem",
            "allowed_tools": ["assistente"],
            "close_options": ["resposta", "cancelar"],
        },
    )
    assert r.status_code == 201, r.text
    types = env["client"].get(f"/api/chamado-stage-types?repository_id={env['repo_id']}").json()
    assert any(t["name"] == "triagem" for t in types)


# ── Projeto / Épico ──────────────────────────────────────────────────────────

def test_project_epic_crud(env):
    client = env["client"]
    p = client.post(
        "/api/projects",
        json={"repository_id": env["repo_id"], "name": "App", "description": "app web"},
    )
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    e = client.post("/api/epics", json={"project_id": pid, "name": "Auth", "description": "login"})
    assert e.status_code == 201, e.text
    eid = e.json()["id"]

    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["chamado_count"] == 0
    assert detail["epics"][0]["name"] == "Auth"

    c = _new_chamado(env, project_id=pid, epic_id=eid)
    assert c["project_id"] == pid and c["epic_id"] == eid

    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["chamado_count"] == 1
    epic_detail = client.get(f"/api/epics/{eid}").json()
    assert epic_detail["chamado_count"] == 1

    # excluir épico desvincula o chamado (não apaga)
    assert client.delete(f"/api/epics/{eid}").status_code == 200
    updated = client.get(f"/api/chamados/{c['id']}").json()
    assert updated["epic_id"] is None

    # excluir projeto desvincula os chamados e apaga épicos
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    updated = client.get(f"/api/chamados/{c['id']}").json()
    assert updated["project_id"] is None


# ── Chamado + etapa inicial ──────────────────────────────────────────────────

def test_chamado_create_initial_stage(env):
    c = _new_chamado(env)
    assert c["workflow_status"] == "entrada"
    assert c["status"] == "em_andamento"
    assert len(c["stages"]) == 1
    assert c["stages"][0]["stage_type_name"] == "entrada"
    assert c["stages"][0]["status"] == "ativa"

    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert [t["key"] for t in ws["tools"]] == ["assistente"]
    assert "next:analise" in ws["close_options"]
    assert ws["current_stage"]["id"] == c["stages"][0]["id"]


def test_chamado_epic_mismatch(env):
    # épico de um repositório diferente do informado → 400
    r2 = env["client"].post(
        "/api/repositories", json={"name": "r2", "url": env["client"].get("/api/repositories").json()[0]["url"]}
    )
    assert r2.status_code == 201, r2.text
    repo2_id = r2.json()["id"]
    p1 = env["client"].post("/api/projects", json={"repository_id": env["repo_id"], "name": "A"}).json()
    e1 = env["client"].post("/api/epics", json={"project_id": p1["id"], "name": "E"}).json()
    r = env["client"].post(
        "/api/chamados",
        json={"repository_id": repo2_id, "epic_id": e1["id"], "title": "t"},
    )
    assert r.status_code == 400


def test_run_tool(env, fake_kimi):
    c = _new_chamado(env)
    fake = fake_kimi(lines=[{"role": "assistant", "content": "Analisei src/app.py:12 e o bug é claro."}])
    env["settings"].kimi_bin = fake

    r = env["client"].post(
        f"/api/chamados/{c['id']}/tools/assistente",
        json={"text": "me ajuda a entender o problema"},
    )
    assert r.status_code == 200, r.text
    stage_id, action = _claim(env)
    assert action == "tool:assistente"
    _run_action(env, stage_id, action)

    msgs = env["client"].get(f"/api/chamados/{c['id']}/messages").json()
    kinds = [m["kind"] for m in msgs]
    assert kinds == ["user", "assistant_text", "system"]
    assert msgs[0]["payload"]["tool"] == "assistente"
    assert "bug é claro" in msgs[1]["payload"]["content"]

    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"


def test_run_tool_unknown_tool(env):
    c = _new_chamado(env)
    r = env["client"].post(
        f"/api/chamados/{c['id']}/tools/inexistente",
        json={"text": "oi"},
    )
    assert r.status_code == 400


def test_tool_budget_aborts(env, fake_kimi):
    c = _new_chamado(env, budget_limit=0.005)  # abaixo de 1 interação (custo 0.01)
    fake = fake_kimi(lines=[{"role": "assistant", "content": "texto"}])
    env["settings"].kimi_bin = fake
    env["client"].post(f"/api/chamados/{c['id']}/tools/assistente", json={"text": "x"})
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"
    assert "orçamento" in (ws["current_stage"]["error"] or "")


# ── Avaliação de fechamento ─────────────────────────────────────────────────

def test_close_next_stage(env, fake_kimi):
    c = _new_chamado(env)
    decision = json.dumps(
        {"decision": "next_stage", "next_stage": "analise", "justificativa": "precisa de escopo"}
    )
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "vou avaliar"}],
        write_file="chamado_decision.json",
        write_content=decision,
    )
    r = env["client"].post(f"/api/chamados/{c['id']}/close")
    assert r.status_code == 200, r.text
    stage_id, action = _claim(env)
    assert action == "evaluate"
    _run_action(env, stage_id, action)

    updated = env["client"].get(f"/api/chamados/{c['id']}").json()
    assert updated["workflow_status"] == "analise"
    assert updated["status"] == "em_andamento"
    assert [s["status"] for s in updated["stages"]] == ["fechada", "ativa"]
    assert updated["stages"][0]["decision"] == "next_stage:analise"


def test_close_resposta(env, fake_kimi):
    c = _new_chamado(env)
    decision = json.dumps(
        {"decision": "resposta", "resposta_texto": "Olá! Corrigimos o problema.", "justificativa": "resolvido"}
    )
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "vou responder"}],
        write_file="chamado_decision.json",
        write_content=decision,
    )
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)

    updated = env["client"].get(f"/api/chamados/{c['id']}").json()
    assert updated["status"] == "respondido"
    assert "respondido" in updated["workflow_status"]
    assert "Corrigimos" in updated["stages"][-1]["result"]


def test_close_cancelar_e_concluir(env, fake_kimi):
    # cancelar é permitido na etapa entrada
    c = _new_chamado(env)
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "ok"}],
        write_file="chamado_decision.json",
        write_content=json.dumps({"decision": "cancelar", "justificativa": "não é nosso"}),
    )
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    assert env["client"].get(f"/api/chamados/{c['id']}").json()["status"] == "cancelado"

    # concluir só é permitido em etapas que o habilitam (ex.: desenvolvimento) —
    # primeiro avançamos da entrada para desenvolvimento.
    c = _new_chamado(env)
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "ok"}],
        write_file="chamado_decision.json",
        write_content=json.dumps({"decision": "next_stage", "next_stage": "desenvolvimento"}),
    )
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    assert env["client"].get(f"/api/chamados/{c['id']}").json()["workflow_status"] == "desenvolvimento"

    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "ok"}],
        write_file="chamado_decision.json",
        write_content=json.dumps({"decision": "concluir", "justificativa": "tudo certo"}),
    )
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    updated = env["client"].get(f"/api/chamados/{c['id']}").json()
    assert updated["status"] == "concluido"
    assert [s["status"] for s in updated["stages"]] == ["fechada", "fechada"]


def test_close_invalid_decision_keeps_stage_active(env, fake_kimi):
    c = _new_chamado(env)
    # next para etapa inexistente + fora do close_options
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "ok"}],
        write_file="chamado_decision.json",
        write_content=json.dumps({"decision": "next_stage", "next_stage": "inexistente"}),
    )
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"
    assert "decisão inválida" in (ws["current_stage"]["error"] or "")
    assert len(ws["stages"]) == 1  # nenhuma etapa nova criada


def test_close_without_decision_file(env, fake_kimi):
    c = _new_chamado(env)
    env["settings"].kimi_bin = fake_kimi(lines=[{"role": "assistant", "content": "sem contrato"}])
    env["client"].post(f"/api/chamados/{c['id']}/close")
    stage_id, action = _claim(env)
    _run_action(env, stage_id, action)
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"
    assert "não emitiu decisão válida" in (ws["current_stage"]["error"] or "")


# ── Worker ───────────────────────────────────────────────────────────────────

def test_claim_one_action_per_chamado(env, fake_kimi):
    c = _new_chamado(env)
    env["settings"].kimi_bin = fake_kimi(lines=[{"role": "assistant", "content": "x"}])
    env["client"].post(f"/api/chamados/{c['id']}/tools/assistente", json={"text": "a"})
    stage_id, action = _claim(env)
    assert action == "tool:assistente"
    # enquanto está executando, novas ações na mesma etapa são recusadas
    r = env["client"].post(f"/api/chamados/{c['id']}/tools/assistente", json={"text": "b"})
    assert r.status_code == 400
    assert "andamento" in r.json()["detail"]
    _run_action(env, stage_id, action)
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"


def test_recover_stale_chamados(env):
    from app.worker import chamado_runner

    c = _new_chamado(env)
    stage_id = c["stages"][0]["id"]
    with env["session_factory"]() as s:
        st = s.get(ChamadoStage, stage_id)
        st.status = "executando"
        st.pending_action = "tool:assistente"
        s.commit()
    assert chamado_runner.recover_stale_chamados(env["session_factory"]) == 1
    with env["session_factory"]() as s:
        st = s.get(ChamadoStage, stage_id)
        assert st.status == "ativa" and st.pending_action is None


# ── Conteúdo LLM (Projeto/Épico) ────────────────────────────────────────────

def test_project_summary_regenerate(env, fake_kimi):
    import time

    p = env["client"].post(
        "/api/projects", json={"repository_id": env["repo_id"], "name": "App"}
    ).json()
    # flag `generating` presente (falsa em repouso)
    assert "generating" in p and p["generating"] is False
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "Resumo executivo: plataforma de chamados."}]
    )
    r = env["client"].post(f"/api/projects/{p['id']}/summary/regenerate")
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    time.sleep(1.0)  # geração roda em thread daemon
    detail = env["client"].get(f"/api/projects/{p['id']}").json()
    assert "plataforma de chamados" in (detail["summary"] or "")


def test_epic_scope_regenerate(env, fake_kimi):
    import time

    p = env["client"].post(
        "/api/projects", json={"repository_id": env["repo_id"], "name": "App"}
    ).json()
    e = env["client"].post("/api/epics", json={"project_id": p["id"], "name": "Auth"}).json()
    env["settings"].kimi_bin = fake_kimi(
        lines=[{"role": "assistant", "content": "Escopo: implementar login e sessão."}]
    )
    assert env["client"].post(f"/api/epics/{e['id']}/scope/regenerate").status_code == 200
    time.sleep(1.0)
    detail = env["client"].get(f"/api/epics/{e['id']}").json()
    assert "login" in (detail["scope"] or "")


# ── Recuperação na UI: cancelar ação pendente / cancelamento manual ──────────

def test_cancel_pending_action(env):
    c = _new_chamado(env)
    env["client"].post(f"/api/chamados/{c['id']}/tools/assistente", json={"text": "oi"})
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "aguardando"
    r = env["client"].post(f"/api/chamados/{c['id']}/cancel-action")
    assert r.status_code == 200, r.text
    ws = env["client"].get(f"/api/chamados/{c['id']}/workspace").json()
    assert ws["current_stage"]["status"] == "ativa"
    assert ws["current_stage"]["pending_action"] is None
    # cancelar sem ação pendente → 400
    assert env["client"].post(f"/api/chamados/{c['id']}/cancel-action").status_code == 400


def test_cancel_chamado_manual(env):
    c = _new_chamado(env)
    r = env["client"].post(f"/api/chamados/{c['id']}/cancel")
    assert r.status_code == 200, r.text
    updated = env["client"].get(f"/api/chamados/{c['id']}").json()
    assert updated["status"] == "cancelado"
    assert "cancelado" in updated["workflow_status"]
    assert updated["stages"][-1]["status"] == "fechada"
    assert updated["stages"][-1]["decision"] == "cancelado_manualmente"
    # chamado já encerrado → 400
    assert env["client"].post(f"/api/chamados/{c['id']}/cancel").status_code == 400


def test_cancel_chamado_com_acao_pendente(env):
    """Cancelamento manual também desfaz uma ação pendente (aguardando)."""
    c = _new_chamado(env)
    env["client"].post(f"/api/chamados/{c['id']}/tools/assistente", json={"text": "oi"})
    r = env["client"].post(f"/api/chamados/{c['id']}/cancel")
    assert r.status_code == 200, r.text
    updated = env["client"].get(f"/api/chamados/{c['id']}").json()
    assert updated["status"] == "cancelado"
    assert updated["stages"][-1]["pending_action"] is None
