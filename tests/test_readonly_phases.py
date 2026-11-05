"""Fases somente-leitura não podem alterar código (PO/QA/avaliador/validador).

Regressão da task-326: o PO (`refine`) implementou e commitou 35 arquivos de
código/testes na fase 0 porque o worker commitava qualquer mudança de fase
pré-merge. O guard detecta árvore suja/commit e descarta antes de decidir.
"""

from __future__ import annotations

import json
import os
import stat

from app.models import TaskStep
from app.worker import gitops, runner

STORY = [
    {
        "role": "assistant",
        "content": (
            "## Descrição\n"
            "Como pai, quero entrar no app para acompanhar minhas filhas.\n\n"
            "## Critérios de aceite\n"
            "- [ ] login funciona\n"
            "- [ ] erro claro\n\n"
            "## Plano de implementação\n"
            "### Subtarefa 1: Login\n"
            "**Escopo:** implementar login.\n"
            "**Critérios:**\n"
            "- [ ] login ok\n"
        ),
    }
]


def _checkout(flow) -> str:
    with flow["session_factory"]() as s:
        from app.models import Task

        task = s.get(Task, flow["task"]["id"])
        repo_id = task.repository_id
    return runner._task_workspace(flow["settings"], repo_id, flow["task"]["id"])


def _execute_next(flow) -> int:
    step_id = runner.claim_next(flow["session_factory"])
    assert step_id is not None
    runner.execute_step(flow["settings"], flow["session_factory"], step_id)
    return step_id


def _events(flow, step_id: int) -> list[dict]:
    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        return [{"kind": e.kind, "payload": e.payload} for e in step.events]


def test_refine_nao_commit_codigo(flow, fake_kimi):
    """PO que escreve arquivo de código tem a alteração revertida e não commita."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(
        STORY,
        write_file="hacked.py",
        write_content="print('não deveria existir')\n",
    )

    step_id = _execute_next(flow)  # posição 0 = po/refine
    checkout = _checkout(flow)

    assert not os.path.exists(os.path.join(checkout, "hacked.py")), (
        "arquivo criado pelo PO deveria ter sido removido"
    )
    status = gitops.run_git(checkout, "status", "--porcelain").stdout.strip()
    assert status == "", f"árvore deveria estar limpa após o revert: {status}"

    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == "done"
        assert step.commit_sha is None, "fase read-only não pode ter commit"

    events = _events(flow, step_id)
    reverted = [e for e in events if e["kind"] == "readonly_phase_changes_reverted"]
    assert len(reverted) == 1
    assert "hacked.py" in reverted[0]["payload"]["files"]
    assert reverted[0]["payload"]["commit_reverted"] is False


def test_refine_sem_historia_preserva_descricao_original(flow, fake_kimi):
    """Relatório de implementação no lugar da história NÃO vira task.description.

    Regressão da task-326: o texto final do PO (relatório de developer) substituiu
    a ideia crua porque `parse_story` aceita texto sem marcadores.
    """
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": "Trabalho concluído. Tudo implementado."}],
        write_file="hacked_report.py",
        write_content="print(1)\n",
    )

    step_id = _execute_next(flow)
    with flow["session_factory"]() as s:
        from app.models import Task

        task = s.get(Task, flow["task"]["id"])
        assert task.description == "d", "descrição original foi sobrescrita"
        step = s.get(TaskStep, step_id)
        kinds = [e.kind for e in step.events]
        assert "refine_story_missing" in kinds

    checkout = _checkout(flow)
    assert not os.path.exists(os.path.join(checkout, "hacked_report.py"))


def test_refine_commit_do_robo_e_revertido(flow, tmp_path):
    """Se o PO commitar durante a fase, o commit é desfeito (reset --hard)."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    script = tmp_path / "kimi_po_commit"
    story = json.dumps(STORY[0]["content"])
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys\n"
        f"print(json.dumps({{'role': 'assistant', 'content': {story}}}))\n"
        "with open('hacked_commit.py', 'w') as f:\n"
        "    f.write('print(1)\\n')\n"
        "subprocess.run(['git', 'add', 'hacked_commit.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'po-hack'], check=True)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    settings.kimi_bin = str(script)

    step_id = _execute_next(flow)
    checkout = _checkout(flow)

    assert not os.path.exists(os.path.join(checkout, "hacked_commit.py"))
    last_subject = gitops.run_git(
        checkout, "log", "-1", "--format=%s"
    ).stdout.strip()
    assert last_subject != "po-hack"

    events = _events(flow, step_id)
    reverted = [e for e in events if e["kind"] == "readonly_phase_changes_reverted"]
    assert len(reverted) == 1
    assert reverted[0]["payload"]["commit_reverted"] is True


def test_review_nao_commit_codigo(flow, fake_kimi):
    """QA (review) que altera código também é revertido antes de avançar."""
    settings = flow["settings"]
    settings.task_budget = 100.0
    settings.kimi_bin = fake_kimi(STORY)
    _execute_next(flow)  # PO cria a história

    settings.kimi_bin = fake_kimi(
        [{"role": "assistant", "content": "revisão ok"}],
        verdict="ready_pass",
        write_file="hacked_review.py",
        write_content="print('não deveria existir')\n",
    )
    step_id = _execute_next(flow)  # posição 1 = qa/review
    checkout = _checkout(flow)

    assert not os.path.exists(os.path.join(checkout, "hacked_review.py"))
    status = gitops.run_git(checkout, "status", "--porcelain").stdout.strip()
    assert status == ""

    with flow["session_factory"]() as s:
        step = s.get(TaskStep, step_id)
        assert step.status == "done"
        assert step.commit_sha is None

    events = _events(flow, step_id)
    reverted = [e for e in events if e["kind"] == "readonly_phase_changes_reverted"]
    assert len(reverted) == 1
    assert "hacked_review.py" in reverted[0]["payload"]["files"]
