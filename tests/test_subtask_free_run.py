"""Regressões da task-86 (itagfm):

1. Re-execução da fase implement com TODAS as subtarefas `done` + instrução do
   usuário (ou falha de fase posterior) NÃO pode terminar a fase em silêncio
   (`phase_done` em ~75 ms sem rodar o robô) — roda UMA execução livre do
   developer com a instrução no prompt.
2. `autoia_subtasks_done.json` é bookkeeping do SISTEMA: o worker normaliza o
   conteúdo como lista ACUMULADA e commita — o robô não pode gravar só a posição
   atual (o dev da task-86 gravou `[4]` em vez de `[1, 2, 3, 4]`).
"""

from __future__ import annotations

import subprocess

import pytest


class TestFreeRunImplement:
    def _setup_done_subtasks(self, flow, n: int = 2):
        """Marca PO/QA done, cria N subtarefas done e ativa o developer (fase 2)."""
        from app.models import STEP_DONE, SUB_DONE, SubTask, TaskStep

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            for i in range(n):
                s.add(SubTask(
                    task_id=task_id, position=i, title=f"Sub {i + 1}",
                    description=f"fazer {i + 1}", status=SUB_DONE,
                ))
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[0].status = STEP_DONE
            steps[0].summary = "história"
            steps[1].status = STEP_DONE
            steps[1].summary = "revisão ok"
            steps[2].status = "pending"
            s.commit()

    def test_instrucao_do_usuario_roda_execucao_livre(self, flow, settings, monkeypatch):
        """Todas as subtarefas `done` + instrução de retomada: a fase implement
        NÃO termina em silêncio — roda o developer UMA vez com a instrução no
        prompt e o texto final vira o resumo da fase (caso da task-86)."""
        import app.worker.kimi_exec as ke
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        self._setup_done_subtasks(flow)

        with session_factory() as s:
            from app.models import Task
            t = s.get(Task, task_id)
            t.resume_instruction = "Arrume os commits: autoia_subtasks_done.json deve ser [1, 2]"
            s.commit()

        captured: list[str] = []

        def fake_run_kimi(prompt, **kw):
            captured.append(prompt)
            return ke.KimiOutcome(
                exit_code=0,
                final_text="commits ajustados: json corrigido para [1, 2]",
                interaction_count=1,
            )

        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            claimed = claim_next(session_factory)
            assert claimed is not None
            trigger = execute_step(settings, session_factory, claimed)
            assert trigger is None

        assert captured, "developer NÃO rodou — a instrução do usuário foi ignorada em silêncio"
        assert "Arrume os commits" in captured[0]
        assert "re-execução" in captured[0] or "Correção do trabalho já entregue" in captured[0]

        with session_factory() as s:
            from app.models import TaskStep
            step = s.get(TaskStep, claimed)
            assert step.status == "done"
            assert "commits ajustados" in (step.summary or "")
            kinds = {e.kind for e in step.events}
            assert "subtask_free_run" in kinds

    def test_falha_de_fase_posterior_roda_execucao_livre(self, flow, settings, monkeypatch):
        """Todas as subtarefas `done` + fase posterior FALHOU (bounce/PM retry):
        o developer roda com o relatório da falha no prompt."""
        import app.worker.kimi_exec as ke
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        self._setup_done_subtasks(flow)

        with session_factory() as s:
            from app.models import STEP_DONE, STEP_FAILED, TaskStep
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[3].status = STEP_DONE
            steps[4].status = STEP_FAILED
            steps[4].error = "veredicto FAIL (esperado PASS)"
            steps[4].summary = "FAIL\nSUMMARY: autoia_subtasks_done.json com [2] em vez de [1, 2]"
            s.commit()

        captured: list[str] = []

        def fake_run_kimi(prompt, **kw):
            captured.append(prompt)
            return ke.KimiOutcome(exit_code=0, final_text="corrigido", interaction_count=1)

        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            claimed = claim_next(session_factory)
            assert claimed is not None
            trigger = execute_step(settings, session_factory, claimed)
            assert trigger is None

        assert captured, "developer NÃO rodou — a falha da fase posterior foi ignorada"
        assert "falhou e o trabalho voltou" in captured[0]
        assert "autoia_subtasks_done.json com [2]" in captured[0]

    def test_sem_motivo_externo_finaliza_sem_rodar(self, flow, settings, monkeypatch):
        """Sem instrução do usuário e sem fase posterior falhada, a re-execução
        com tudo `done` finaliza a fase sem rodar o robô (não há o que fazer —
        e nada é ignorado em silêncio)."""
        import app.worker.kimi_exec as ke
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        self._setup_done_subtasks(flow)

        captured: list[str] = []

        def fake_run_kimi(prompt, **kw):
            captured.append(prompt)
            return ke.KimiOutcome(exit_code=0, final_text="não devia rodar", interaction_count=1)

        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            claimed = claim_next(session_factory)
            assert claimed is not None
            trigger = execute_step(settings, session_factory, claimed)
            assert trigger is None

        assert captured == []

    def test_instrucao_entra_no_prompt_de_subtarefa_pendente(self, flow, settings, monkeypatch):
        """Com subtarefa PENDENTE, a instrução de retomada entra direto no prompt
        da subtarefa (não depende só do handoff)."""
        import app.worker.kimi_exec as ke
        from app.worker.runner import claim_next, execute_step

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            from app.models import STEP_DONE, SUB_PENDING, SubTask, Task, TaskStep
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1",
                          description="fazer A", status=SUB_PENDING))
            t = s.get(Task, task_id)
            t.resume_instruction = "Use o padrão do repositório nos commits"
            steps = s.query(TaskStep).filter(TaskStep.task_id == task_id).order_by(TaskStep.position).all()
            steps[0].status = STEP_DONE
            steps[1].status = STEP_DONE
            steps[2].status = "pending"
            s.commit()

        captured: list[str] = []

        def fake_run_kimi(prompt, **kw):
            captured.append(prompt)
            return ke.KimiOutcome(exit_code=0, final_text="ok", interaction_count=1)

        with monkeypatch.context() as mp:
            mp.setattr(ke, "run_kimi", fake_run_kimi)
            claimed = claim_next(session_factory)
            assert claimed is not None
            trigger = execute_step(settings, session_factory, claimed)
            assert trigger is None

        assert captured, "developer não rodou"
        assert "Use o padrão do repositório nos commits" in captured[0]
        assert "Intervenção do usuário (retomada)" in captured[0]


class TestSubtasksDoneBookkeeping:
    def test_normaliza_conteudo_acumulado_e_commita(self, flow, bare_repo, tmp_path):
        """O worker reescreve `autoia_subtasks_done.json` como lista ACUMULADA das
        subtarefas implementadas/done e commita — o conteúdo `[3]` gravado pelo
        robô vira `[1, 2, 3]` (regressão da task-86)."""
        from app.worker.subtask import _normalize_and_commit_subtasks_done

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            from app.models import SUB_DONE, SubTask
            for i in range(3):
                s.add(SubTask(task_id=task_id, position=i, title=f"Sub {i + 1}",
                              status=SUB_DONE))
            s.commit()

        checkout = tmp_path / "checkout"
        subprocess.run(["git", "clone", "-q", bare_repo, str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
        (checkout / "autoia_subtasks_done.json").write_text("[3]\n")
        subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "dev registrou [3]"], check=True)

        _normalize_and_commit_subtasks_done(session_factory, task_id, str(checkout))

        assert (checkout / "autoia_subtasks_done.json").read_text() == "[1, 2, 3]\n"
        out = subprocess.run(
            ["git", "-C", str(checkout), "show", "HEAD:autoia_subtasks_done.json"],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout == "[1, 2, 3]\n"
        log_out = subprocess.run(
            ["git", "-C", str(checkout), "log", "-1", "--format=%s"],
            capture_output=True, text=True, check=True,
        )
        assert "bookkeeping" in log_out.stdout

    def test_conteudo_ja_correto_nao_gera_commit(self, flow, bare_repo, tmp_path):
        """Conteúdo já cumulativo: normalização não toca no git (sem commit vazio)."""
        from app.worker.subtask import _normalize_and_commit_subtasks_done

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            from app.models import SUB_DONE, SubTask
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", status=SUB_DONE))
            s.add(SubTask(task_id=task_id, position=1, title="Sub 2", status=SUB_DONE))
            s.commit()

        checkout = tmp_path / "checkout"
        subprocess.run(["git", "clone", "-q", bare_repo, str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
        (checkout / "autoia_subtasks_done.json").write_text("[1, 2]\n")
        subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "base"], check=True)
        head_before = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout

        _normalize_and_commit_subtasks_done(session_factory, task_id, str(checkout))

        head_after = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert head_before == head_after

    def test_prompt_do_marcar_subtarefa_explica_acumulo(self):
        """O contrato da ferramenta deixa explícito que o arquivo é o estado FINAL
        da task (cumulativo) — o dev não pode sobrescrever só com a posição atual."""
        from app.prompts import SUB_TASK_DONE_TOOL

        assert "TODAS as subtarefas" in SUB_TASK_DONE_TOOL
        assert "NUNCA sobrescreva" in SUB_TASK_DONE_TOOL
        assert "PRESERVE os índices" in SUB_TASK_DONE_TOOL

    def test_bookkeeping_inclui_apenas_subtarefas_verificadas(
        self, flow, bare_repo, tmp_path
    ):
        """Uma subtarefa apenas `implemented` (ainda não verificada) NÃO pode
        entrar em `autoia_subtasks_done.json`: o dev usaria o arquivo como prova
        de "já está pronta" e re-declararia sem código (task-279)."""
        from app.worker.subtask import _normalize_and_commit_subtasks_done

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            from app.models import SUB_DONE, SUB_IMPLEMENTED, SubTask
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", status=SUB_DONE))
            s.add(SubTask(task_id=task_id, position=1, title="Sub 2", status=SUB_DONE))
            s.add(SubTask(task_id=task_id, position=2, title="Sub 3", status=SUB_IMPLEMENTED))
            s.commit()

        checkout = tmp_path / "checkout"
        subprocess.run(["git", "clone", "-q", bare_repo, str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
        (checkout / "autoia_subtasks_done.json").write_text("[1, 2, 3]\n")
        subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "stale"], check=True)

        _normalize_and_commit_subtasks_done(session_factory, task_id, str(checkout))

        assert (checkout / "autoia_subtasks_done.json").read_text() == "[1, 2]\n"

    def test_implement_repara_bookkeeping_antes_de_montar_o_prompt(
        self, flow, bare_repo, tmp_path
    ):
        """A fase implement normaliza o arquivo ANTES do dev ler o estado: um
        `[1,2,3]` com a subtarefa 3 apenas `implemented` vira `[1,2]` antes da
        primeira execução, sem esperar o fim da fase."""
        from app.models import SUB_DONE, SUB_IMPLEMENTED, SubTask, Task
        from app.worker.subtask import run_implement_subtasks

        session_factory = flow["session_factory"]
        task_id = flow["task"]["id"]
        with session_factory() as s:
            s.add(SubTask(task_id=task_id, position=0, title="Sub 1", status=SUB_DONE))
            s.add(SubTask(task_id=task_id, position=1, title="Sub 2", status=SUB_DONE))
            s.add(SubTask(task_id=task_id, position=2, title="Sub 3", status=SUB_IMPLEMENTED))
            task = s.get(Task, task_id)
            step = next(st for st in task.steps if st.position == 2)
            s.commit()

        checkout = tmp_path / "checkout"
        subprocess.run(["git", "clone", "-q", bare_repo, str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@test"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
        (checkout / "autoia_subtasks_done.json").write_text("[1, 2, 3]\n")
        subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "stale"], check=True)

        abort, free = run_implement_subtasks(
            flow["settings"], session_factory, step, task_id, str(checkout),
            "main", "autoia/task-1", "node/npm", str(tmp_path / "step.log"),
        )

        assert abort is None
        assert free is None
        assert (checkout / "autoia_subtasks_done.json").read_text() == "[1, 2]\n"


def test_done_declaration_ignora_commit_so_de_bookkeeping(bare_repo, tmp_path):
    """Um commit que só toca `autoia_subtasks_done.json` não pode ser confundido
    com correção de código pela guarda `subtask_done_rejected`."""
    from app.worker.subtask import _done_declaration_has_changes

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", bare_repo, str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@test"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)

    (checkout / "code.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "code"], check=True)
    head_before = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    (checkout / "autoia_subtasks_done.json").write_text("[1]\n")
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "bookkeeping"], check=True)

    assert _done_declaration_has_changes(str(checkout), head_before) is False

    (checkout / "code.py").write_text("x = 2\n")
    assert _done_declaration_has_changes(str(checkout), head_before) is True
