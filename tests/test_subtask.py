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

    def test_retry_subtask_pending_task_failed(self, flow):
        """Subtarefa `pending` com a task morta (failed) não é "em andamento":
        o worker nunca vai reclamá-la — o retry deve funcionar."""
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
        from app.db import make_engine, make_session_factory
        session_factory = make_session_factory(make_engine(flow["settings"].database_url))
        with session_factory() as s:
            from app.models import SubTask, Task
            t = s.get(Task, task["id"])
            t.status = "failed"
            t.error = "erro interno do worker"
            st = s.query(SubTask).filter(SubTask.task_id == task["id"]).first()
            st.status = "pending"  # abortou (guardrail) e ficou pending
            st.attempt = 2
            st.error = "guardrail: path-outside-workspace"
            s.commit()

        resp = client.post(f"/api/tasks/{task['id']}/subtasks/0/retry")
        assert resp.status_code == 200, resp.text
        sub = resp.json()
        assert sub["status"] == "pending"
        assert sub["attempt"] == 3
        assert sub["error"] is None
        # task reencaminhada para a fila
        with session_factory() as s:
            from app.models import Task
            t = s.get(Task, task["id"])
            assert t.status == "queued"
            assert t.error is None

    def test_retry_subtask_pending_task_running_blocked(self, flow):
        """Subtarefa `pending` com a task em andamento continua bloqueada
        (o worker vai processá-la; retry duplicaria a execução)."""
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
        from app.db import make_engine, make_session_factory
        session_factory = make_session_factory(make_engine(flow["settings"].database_url))
        with session_factory() as s:
            from app.models import SubTask, Task
            t = s.get(Task, task["id"])
            t.status = "in_progress"
            st = s.query(SubTask).filter(SubTask.task_id == task["id"]).first()
            st.status = "pending"
            st.attempt = 2
            s.commit()

        resp = client.post(f"/api/tasks/{task['id']}/subtasks/0/retry")
        assert resp.status_code == 400, resp.text
        assert "em andamento" in resp.json()["detail"]


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

    def test_implement_marks_done_via_tool(self, flow, fake_kimi, settings, monkeypatch):
        """O agente pode marcar uma subtarefa como implementada via `autoia_subtasks_done.json`
        (evita re-implementar código já commitado, ex.: após restart do worker). A chamada
        vira tool_call/tool_result no timeline."""
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        with session_factory() as s:
            from app.models import STEP_DONE, SUB_PENDING, SubTask, TaskStep
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", description="fazer A", status=SUB_PENDING))
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[0].status = STEP_DONE
            steps[0].summary = "história"
            steps[1].status = STEP_DONE
            steps[1].summary = "revisão ok"
            steps[2].status = "pending"
            s.commit()

        step_id = claim_next(session_factory)
        assert step_id is not None
        monkeypatch.setattr(settings, "kimi_bin", fake_kimi(
            [{"role": "assistant", "content": "código já estava na branch"}],
            write_file="autoia_subtasks_done.json",
            write_content="[1]",
        ))
        trigger = execute_step(settings, session_factory, step_id)
        assert trigger is None

        with session_factory() as s:
            from app.models import RunEvent, SubTask
            sub = s.query(SubTask).filter(SubTask.task_id == task_id).first()
            assert sub.status == "implemented"
            assert sub.error is None

            kinds = [e.kind for e in s.query(RunEvent).filter(RunEvent.step_id == step_id).all()]
            assert "tool_call" in kinds
            assert "tool_result" in kinds
            assert "subtask_marked_done" in kinds

            tc = next(e for e in s.query(RunEvent).filter(RunEvent.step_id == step_id).all()
                      if e.kind == "tool_call")
            fn = (tc.payload or {}).get("tool_call", {}).get("function", {})
            assert fn.get("name") == "autoia_mark_subtask_done"
            assert '"subtask_id": 1' in fn.get("arguments", "")

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


# ---------------------------------------------------------------------------
# Cenário real: pós-deploy — erro em produção → nova subtarefa → resolve
# ---------------------------------------------------------------------------

class TestPostDeployFix:
    def test_post_deploy_error_new_subtask_fix(
        self, flow, settings, monkeypatch, tmp_path,
    ):
        """Task conclui → erro de deploy (variável de ambiente faltante)
        → usuário cria subtarefa de correção → pipeline re-executa → sucesso.

        Fluxo:
        1. Task com 2 subtarefas vai até done (mockado)
        2. Usuário reporta erro via feedback + cria subtarefa 3 de correção
        3. Retry da fase implement → só a subtarefa nova é processada
        4. Verify, assess, merge → task done de novo, subtarefa 3 done
        """
        import app.worker.kimi_exec as ke
        import app.verdicts as vmod
        from app.models import Task
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        client = flow["client"]
        task_id = flow["task"]["id"]

        # --- Fase 1: task original até done (mock rápido) ---
        # Configura subtarefas e pula direto para o developer
        with session_factory() as s:
            from app.models import (
                STEP_DONE, SUB_PENDING, SubTask, Task, TaskStep,
            )
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", description="parte A", status=SUB_PENDING))
            s.add(SubTask(task_id=task_id, position=1, title="Sub 2", description="parte B", status=SUB_PENDING))
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[0].status = STEP_DONE
            steps[0].summary = "história"
            steps[1].status = STEP_DONE
            steps[1].summary = "revisão ok"
            steps[2].status = "pending"  # developer
            s.commit()

        # Mocks
        read_count = 0

        def fake_run_kimi(prompt, **kw):
            return ke.KimiOutcome(exit_code=0, final_text="ok", interaction_count=1)

        def fake_read_verdict(checkout):
            nonlocal read_count
            read_count += 1
            return "PASS\nSUMMARY: ok"

        def fake_remove_verdict(checkout):
            pass

        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            mp.setattr(vmod, "read_verdict", fake_read_verdict)
            mp.setattr(vmod, "remove_verdict", fake_remove_verdict)

            # Avança do developer até o fim (5 fases: dev, tester, assess, merger, deploy-tester)
            for _ in range(7):
                claimed = claim_next(session_factory)
                if claimed is None:
                    break
                execute_step(settings, session_factory, claimed)

        # Task deve estar done
        with session_factory() as s:
            t = s.get(Task, task_id)
            assert t.status == "done", f"task status={t.status}"
            subs = (
                s.query(SubTask).filter(SubTask.task_id == task_id)
                .order_by(SubTask.position).all()
            )
            assert len(subs) == 2
            for sub in subs:
                assert sub.status == "done", f"sub {sub.position} = {sub.status}"

        # --- Fase 2: erro de deploy → correção ---

        # Reporta erro de deploy
        resp = client.post(
            f"/api/tasks/{task_id}/feedback",
            json={"text": "Erro no deploy: variável DATABASE_URL não está definida no ambiente de produção."},
        )
        assert resp.status_code == 200

        # Cria subtarefa de correção via API
        resp = client.post(
            f"/api/tasks/{task_id}/subtasks",
            json={
                "title": "Corrigir deploy: adicionar DATABASE_URL",
                "description": "Adicionar fallback para DATABASE_URL no entrypoint e documentar no README.",
                "acceptance_criteria": "- [ ] app sobe sem DATABASE_URL definida\n- [ ] README documenta a variável",
            },
        )
        assert resp.status_code == 201, resp.text
        new_sub = resp.json()
        assert new_sub["position"] == 2  # terceira subtarefa
        assert new_sub["status"] == "pending"

        # Retry da fase implement (posição 2) — sem note para não sobrescrever o feedback
        resp = client.post(f"/api/tasks/{task_id}/steps/2/retry")
        assert resp.status_code == 200, resp.text

        # --- Fase 3: re-execução ---
        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            mp.setattr(vmod, "read_verdict", fake_read_verdict)
            mp.setattr(vmod, "remove_verdict", fake_remove_verdict)

            for _ in range(7):
                claimed = claim_next(session_factory)
                if claimed is None:
                    break
                execute_step(settings, session_factory, claimed)

        # --- Verificações finais ---
        with session_factory() as s:
            t = s.get(Task, task_id)
            assert t.status == "done", f"task status={t.status}, error={t.error}"
            assert t.feedback and "DATABASE_URL" in t.feedback

            subs = (
                s.query(SubTask).filter(SubTask.task_id == task_id)
                .order_by(SubTask.position).all()
            )
            assert len(subs) == 3
            # Subtarefas originais continuam done
            assert subs[0].status == "done"
            assert subs[1].status == "done"
            # Nova subtarefa concluída
            assert subs[2].status == "done", f"sub 2 status={subs[2].status}"
            assert subs[2].title == "Corrigir deploy: adicionar DATABASE_URL"
            assert subs[2].verdict == "PASS"
            assert subs[2].summary == "ok"

            # Todos os steps devem estar done (exceto talvez o deploy-tester que
            # pode ter ficado pending na segunda rodada — mas o merge re-ocorreu)
            for st in t.steps:
                assert st.status == "done", f"step {st.position} = {st.status}"

        # Feedback ainda está salvo e visível
        task_json = client.get(f"/api/tasks/{task_id}").json()
        assert task_json["feedback"] and "DATABASE_URL" in task_json["feedback"]
