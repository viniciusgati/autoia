"""Testes do sistema de subtarefas: parse, API e fluxo worker."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# parse_subtasks
# ---------------------------------------------------------------------------

class TestParseSubtasks:
    def test_parse_empty(self):
        from app.verdicts import parse_subtasks

        assert parse_subtasks("") == []
        assert parse_subtasks("sem plano nenhum") == []

    def test_parse_single_subtask(self):
        from app.verdicts import parse_subtasks

        text = """## Plano de implementação

### Subtarefa 1: Criar função
**Escopo:** Implementar a função no módulo.
**Critérios:**
- [ ] teste passa
- [ ] type hints

## Fora de escopo
nada
"""
        subs = parse_subtasks(text)
        assert len(subs) == 1
        assert subs[0]["title"] == "Criar função"
        assert "Implementar a função" in subs[0]["description"]
        assert "teste passa" in subs[0]["acceptance_criteria"]

    def test_parse_multiple_subtasks(self):
        from app.verdicts import parse_subtasks

        text = """## Plano de implementação

### Subtarefa 1: Model
**Escopo:** Criar a tabela.
**Critérios:**
- [ ] migration existe

### Subtarefa 2: API
**Escopo:** Criar os endpoints REST.
**Critérios:**
- [ ] GET retorna 200
- [ ] POST cria registro

### Subtarefa 3: Testes
**Escopo:** Escrever testes de integração.
**Critérios:**
- [ ] cobertura > 80%
"""
        subs = parse_subtasks(text)
        assert len(subs) == 3
        assert subs[0]["title"] == "Model"
        assert subs[1]["title"] == "API"
        assert subs[2]["title"] == "Testes"
        assert "Criar a tabela" in subs[0]["description"]
        assert "endpoints REST" in subs[1]["description"]
        assert "integração" in subs[2]["description"]

    def test_parse_preserves_subtask_order(self):
        from app.verdicts import parse_subtasks

        text = """## Plano de implementação

### Subtarefa 2: Segunda
**Escopo:** Depois.
**Critérios:**
- [ ] ok

### Subtarefa 1: Primeira
**Escopo:** Antes.
**Critérios:**
- [ ] ok
"""
        subs = parse_subtasks(text)
        # A ordem no texto é preservada (ordem de aparição, não numérica)
        assert subs[0]["title"] == "Segunda"
        assert subs[1]["title"] == "Primeira"


# ---------------------------------------------------------------------------
# API de subtarefas
# ---------------------------------------------------------------------------

class TestSubtaskAPI:
    def test_create_task_with_subtasks(self, flow):
        """Criar task já com subtarefas definidas."""
        client = flow["client"]
        resp = client.post(
            "/api/tasks",
            json={
                "repository_id": 1,
                "pipeline_id": 1,
                "title": "task com subtarefas",
                "description": "ideia crua",
                "kind": "feature",
                "subtasks": [
                    {"title": "Sub 1", "description": "escopo 1", "acceptance_criteria": "- [ ] c1"},
                    {"title": "Sub 2", "description": "escopo 2"},
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        task = resp.json()
        assert len(task["subtasks"]) == 2
        assert task["subtasks"][0]["title"] == "Sub 1"
        assert task["subtasks"][0]["position"] == 0
        assert task["subtasks"][1]["position"] == 1
        assert task["subtasks"][0]["status"] == "pending"

    def test_list_subtasks(self, flow):
        client = flow["client"]
        # cria task com subtarefas
        resp = client.post(
            "/api/tasks",
            json={
                "repository_id": 1,
                "pipeline_id": 1,
                "title": "t",
                "description": "d",
                "kind": "feature",
                "subtasks": [{"title": "Sub 1", "description": "d1"}],
            },
        )
        task = resp.json()
        resp = client.get(f"/api/tasks/{task['id']}/subtasks")
        assert resp.status_code == 200
        subs = resp.json()
        assert len(subs) == 1

    def test_create_subtask_after_creation(self, flow):
        client = flow["client"]
        # task sem subtarefas
        resp = client.post(
            "/api/tasks",
            json={
                "repository_id": 1,
                "pipeline_id": 1,
                "title": "t",
                "description": "d",
                "kind": "feature",
            },
        )
        task = resp.json()
        assert len(task["subtasks"]) == 0

        # adiciona subtarefa
        resp = client.post(
            f"/api/tasks/{task['id']}/subtasks",
            json={"title": "Nova", "description": "adicionada depois"},
        )
        assert resp.status_code == 201, resp.text
        sub = resp.json()
        assert sub["title"] == "Nova"
        assert sub["position"] == 0

    def test_update_subtask(self, flow):
        client = flow["client"]
        resp = client.post(
            "/api/tasks",
            json={
                "repository_id": 1,
                "pipeline_id": 1,
                "title": "t",
                "description": "d",
                "kind": "feature",
                "subtasks": [{"title": "Sub 1", "description": "original"}],
            },
        )
        task = resp.json()
        resp = client.patch(
            f"/api/tasks/{task['id']}/subtasks/0",
            json={"description": "alterada", "title": "Sub 1 v2"},
        )
        assert resp.status_code == 200
        sub = resp.json()
        assert sub["description"] == "alterada"
        assert sub["title"] == "Sub 1 v2"

    def test_retry_subtask(self, flow):
        client = flow["client"]
        resp = client.post(
            "/api/tasks",
            json={
                "repository_id": 1,
                "pipeline_id": 1,
                "title": "t",
                "description": "d",
                "kind": "feature",
                "subtasks": [{"title": "Sub 1", "description": "d1"}],
            },
        )
        task = resp.json()
        # força subtask como failed (simulando)
        from app.db import make_session_factory, make_engine
        session_factory = make_session_factory(
            make_engine(flow["settings"].database_url)
        )
        with session_factory() as s:
            from app.models import SubTask
            st = s.query(SubTask).filter(SubTask.task_id == task["id"]).first()
            st.status = "failed"
            st.attempt = 2
            s.commit()

        resp = client.post(f"/api/tasks/{task['id']}/subtasks/0/retry")
        assert resp.status_code == 200, resp.text
        sub = resp.json()
        assert sub["status"] == "pending"
        assert sub["attempt"] == 3
        assert sub["error"] is None


# ---------------------------------------------------------------------------
# Worker: fluxo de subtarefas com fake_kimi
# ---------------------------------------------------------------------------

class TestSubtaskWorker:
    def test_implement_subtasks_flow(self, flow, fake_kimi, settings, monkeypatch):
        """Worker executa implement para cada subtarefa pendente."""
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        # Configura subtarefas e pula direto para o developer (fase 2)
        with session_factory() as s:
            from app.models import STEP_DONE, SUB_PENDING, SubTask, TaskStep
            # Cria subtarefas na task
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", description="fazer A", status=SUB_PENDING))
            s.add(SubTask(task_id=task_id, position=1, title="Sub 2", description="fazer B", status=SUB_PENDING))
            # Marca fases PO e QA como concluídas
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[0].status = STEP_DONE
            steps[0].summary = "história"
            steps[1].status = STEP_DONE
            steps[1].summary = "revisão ok"
            # Ativa fase developer
            steps[2].status = "pending"
            s.commit()

        # Executa developer
        step_id = claim_next(session_factory)
        assert step_id is not None
        monkeypatch.setattr(settings, "kimi_bin", fake_kimi(
            [{"role": "assistant", "content": "implementado!"}],
        ))
        trigger = execute_step(settings, session_factory, step_id)
        assert trigger is None

        with session_factory() as s:
            from app.models import SubTask
            subs = (
                s.query(SubTask)
                .filter(SubTask.task_id == task_id)
                .order_by(SubTask.position)
                .all()
            )
            assert len(subs) == 2
            for sub in subs:
                assert sub.status == "implemented", f"sub {sub.position} status={sub.status}"
                assert sub.summary == "implementado!"

    def test_verify_subtasks_pass(self, flow, fake_kimi, settings, monkeypatch):
        """Tester verifica cada subtarefa e todas passam."""
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        # Adiciona subtarefas implementadas via DB
        with session_factory() as s:
            from app.models import SUB_IMPLEMENTED, SubTask
            s.add(SubTask(
                task_id=task_id, position=0, title="Sub 1",
                description="fazer A", status=SUB_IMPLEMENTED, summary="feito",
            ))
            s.add(SubTask(
                task_id=task_id, position=1, title="Sub 2",
                description="fazer B", status=SUB_IMPLEMENTED, summary="feito tb",
            ))
            s.commit()

        # Avança até a fase verify (tester)
        # PO → QA → Developer (sem subtarefas, task já tem subtarefas implementadas)
        # Precisamos "pular" as fases anteriores manualmente
        with session_factory() as s:
            from app.models import STEP_DONE, TaskStep
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            # PO
            steps[0].status = STEP_DONE
            steps[0].summary = "história"
            # QA
            steps[1].status = STEP_DONE
            steps[1].summary = "revisão ok"
            # Developer (implement) — também marcamos como done pois subtarefas já estão implementadas
            steps[2].status = STEP_DONE
            steps[2].summary = "subs implementadas"
            # Ativa verify
            steps[3].status = "pending"
            s.commit()

        # Executa verify
        step_id = claim_next(session_factory)
        assert step_id is not None
        # fake que emite PASS para cada subtarefa
        monkeypatch.setattr(settings, "kimi_bin", fake_kimi(
            [{"role": "assistant", "content": "tudo certo!"}],
            verdict="ready_pass",  # escreve PASS no autoia_verdict.txt
        ))
        trigger = execute_step(settings, session_factory, step_id)
        assert trigger is None

        # Verifica subtarefas
        with session_factory() as s:
            from app.models import SubTask
            subs = (
                s.query(SubTask)
                .filter(SubTask.task_id == task_id)
                .order_by(SubTask.position)
                .all()
            )
            for sub in subs:
                assert sub.status == "done", f"sub {sub.position} status={sub.status}"
                assert sub.verdict == "PASS"

    def test_verify_subtask_fail_triggers_bounceback(self, flow, fake_kimi, settings, monkeypatch):
        """Se o tester reprova uma subtarefa, bounce-back para implement."""
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        # Adiciona subtarefas implementadas
        with session_factory() as s:
            from app.models import SUB_IMPLEMENTED, SubTask
            s.add(SubTask(
                task_id=task_id, position=0, title="Sub 1",
                description="fazer A", status=SUB_IMPLEMENTED, summary="feito",
            ))
            s.commit()

        # Pula fases até verify
        with session_factory() as s:
            from app.models import STEP_DONE, TaskStep
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            for st in steps[:3]:
                st.status = STEP_DONE
            steps[3].status = "pending"
            s.commit()

        # Executa verify com FAIL
        step_id = claim_next(session_factory)
        assert step_id is not None
        monkeypatch.setattr(settings, "kimi_bin", fake_kimi(
            [{"role": "assistant", "content": "teste falhou!"}],
            verdict="fail",  # escreve FAIL
        ))
        trigger = execute_step(settings, session_factory, step_id)
        # Deve ter bounce-back (sem PM trigger, pois bounce-back não gera trigger)
        assert trigger is None

        # Subtarefa deve estar pending novamente
        with session_factory() as s:
            from app.models import SubTask
            sub = s.query(SubTask).filter(SubTask.task_id == task_id).first()
            assert sub.status == "pending", f"sub status={sub.status}"

        # Fase implement deve estar pending (bounce-back)
        with session_factory() as s:
            from app.models import TaskStep
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            assert steps[2].status == "pending"  # implement step

    def test_subtask_progress_summary(self):
        """Formata resumo de progresso das subtarefas."""
        from app.worker.runner import _subtask_progress_summary

        # Cria task fake com subtarefas em diferentes estados
        class FakeSub:
            def __init__(self, position, title, status, summary="", verdict=None):
                self.position = position
                self.title = title
                self.status = status
                self.summary = summary
                self.verdict = verdict

        class FakeTask:
            subtasks = [
                FakeSub(0, "Primeira", "done", "tudo ok", "PASS"),
                FakeSub(1, "Segunda", "implemented", "código pronto"),
                FakeSub(2, "Terceira", "pending"),
                FakeSub(3, "Quarta", "failed", "", None),
            ]

        summary = _subtask_progress_summary(FakeTask())
        assert "Primeira" in summary
        assert "Segunda" in summary
        assert "Terceira" in summary
        assert "Quarta" in summary
        assert "[OK]" in summary
        assert "[IMPLEMENTADA]" in summary
        assert "[PENDENTE]" in summary
        assert "[FALHOU]" in summary


# ---------------------------------------------------------------------------
# PO gera subtarefas automaticamente
# ---------------------------------------------------------------------------

class TestPOGeneratesSubtasks:
    def test_po_creates_subtasks_from_plan(self, flow, fake_kimi, settings, monkeypatch):
        """Quando o PO gera ## Plano de implementação, o worker cria SubTask records."""
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        # PO (refine) — fase 0
        step_id = claim_next(session_factory)
        assert step_id is not None

        monkeypatch.setattr(settings, "kimi_bin", fake_kimi(
            [{"role": "assistant", "content": (
                "## Descrição\nhistória de teste\n\n"
                "## Critérios de aceite\n- [ ] critério 1\n\n"
                "## Plano de implementação\n\n"
                "### Subtarefa 1: Model\n"
                "**Escopo:** Criar modelo.\n"
                "**Critérios:**\n- [ ] migration ok\n\n"
                "### Subtarefa 2: API\n"
                "**Escopo:** Criar endpoints.\n"
                "**Critérios:**\n- [ ] GET /api funciona\n\n"
                "### Subtarefa 3: Testes\n"
                "**Escopo:** Testar.\n"
                "**Critérios:**\n- [ ] cobertura ok\n"
            )}],
        ))
        trigger = execute_step(settings, session_factory, step_id)
        assert trigger is None

        # Verifica subtarefas criadas
        with session_factory() as s:
            from app.models import SubTask
            subs = (
                s.query(SubTask)
                .filter(SubTask.task_id == task_id)
                .order_by(SubTask.position)
                .all()
            )
            assert len(subs) == 3
            assert subs[0].title == "Model"
            assert subs[1].title == "API"
            assert subs[2].title == "Testes"
            assert subs[0].status == "pending"
            assert "Criar modelo" in subs[0].description
            assert "migration ok" in subs[0].acceptance_criteria
