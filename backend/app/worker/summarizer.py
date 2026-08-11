"""Geração do resumo estruturado do desenvolvimento por LLM dedicada.

Segue a mesma filosofia dos robôs: NUNCA chama API de LLM diretamente — usa o executor
da task (`kimi`/`opencode`) com um contrato de saída `autoia_summary.json` estruturado.

O resumo é uma representação resumida da execução (fases, timeline, arquivos, testes,
status) e NUNCA a fonte de verdade. Falha na geração NÃO afeta o desenvolvimento nem
altera os dados originais: a task segue funcionando sem resumo (a UI apenas oculta).
"""

from __future__ import annotations

import json
import logging
import os

from .. import prompts, verdicts
from ..db import utcnow
from ..models import RunEvent, Task, TaskStep, TaskSummary
from . import gitops
from .runner import _effective, _run_executor, _system_event, _task_workspace

log = logging.getLogger("autoia.summarizer")


def build_summary_prompt(
    task: Task,
    timeline_text: str,
    subtasks_text: str,
    phases_text: str,
    project_info: str = "",
) -> str:
    """Monta o prompt do resumo com o contexto COMPLETO disponível da execução."""
    parts: list[str] = [
        "Você está resumindo um desenvolvimento já executado pelo pipeline autoia.",
        f"Tarefa #{task.id}: {task.title}",
        f"Status da task: {task.status} | Executor: {task.executor} | "
        f"Custo: {task.cost_spent:.2f} US$",
    ]
    if task.description:
        parts.append(f"## Solicitação original\n{task.description}")
    if task.details:
        parts.append(f"## Detalhes adicionados pelo usuário\n{task.details}")
    if task.acceptance_criteria:
        parts.append(f"## Critérios de aceite\n{task.acceptance_criteria}")
    if task.feedback:
        parts.append(f"## Feedback externo\n{task.feedback}")
    if task.resume_instruction:
        parts.append(f"## Intervenção do usuário (retomada)\n{task.resume_instruction}")
    if task.error:
        parts.append(f"## Erro/observação\n{task.error}")
    if phases_text:
        parts.append(f"## Relatórios das fases\n{phases_text}")
    if subtasks_text:
        parts.append(subtasks_text)
    if timeline_text:
        parts.append(f"## Timeline da execução\n{timeline_text}")
    if project_info:
        parts.append(project_info)
    parts.append(prompts.CONTRACT_SUMMARY)
    return "\n\n".join(parts)


def _phases_text(task: Task) -> str:
    lines = []
    for st in sorted(task.steps, key=lambda x: x.position):
        robot = st.robot.name if st.robot else "?"
        role = st.robot.role if st.robot else ""
        head = f"### Fase {st.position} — {robot} ({role}) — {st.status}"
        if st.verdict:
            head += f" — veredicto: {st.verdict}"
        body = st.summary or "(sem relatório)"
        if st.diff_stat:
            body += f"\n\n**Alterações:**\n{st.diff_stat}"
        lines.append(f"{head}\n{body}")
    return "\n\n".join(lines)


def _subtasks_text(task: Task) -> str:
    if not task.subtasks:
        return ""
    lines = ["## Tarefas (subtarefas)"]
    for s in sorted(task.subtasks, key=lambda x: x.position):
        line = f"- [{s.status}] Tarefa {s.position + 1}: {s.title}"
        if s.verdict:
            line += f" (veredicto: {s.verdict})"
        if s.error:
            line += f" — erro: {s.error}"
        lines.append(line)
    return "\n".join(lines)


def _persist_summary(session, task_id: int, data: dict, model: str | None) -> None:
    """Grava o TaskSummary mais recente e registra o evento na fase âncora."""
    task = session.get(Task, task_id)
    if task is None:
        return
    summary = TaskSummary(
        task_id=task_id,
        summary=data.get("summary") or "",
        request=data.get("request"),
        implementation=data.get("implementation"),
        changes=data.get("changes") or [],
        result=data.get("result"),
        issues=data.get("issues") or [],
        files=data.get("files") or [],
        tasks_summary=data.get("tasks_summary"),
        model=model,
    )
    session.add(summary)
    anchor = sorted(task.steps, key=lambda x: x.position)[-1] if task.steps else None
    _system_event(
        session, anchor, "summary_generated",
        {"result": data.get("result"), "len": len(data.get("summary") or "")},
    )
    session.commit()


def summarize_task(settings, session_factory, task_id: int) -> bool:
    """Gera (ou regenera) o resumo da task via executor. Retorna True em sucesso.

    Nunca levanta: falhas são logadas e a task segue funcionando sem resumo.
    """
    from .. import timeline

    try:
        with session_factory() as s:
            task = s.get(Task, task_id)
            if task is None:
                return False
            repo = task.repository
            eff = _effective(settings, repo)
            checkout = _task_workspace(eff, repo.id, task.id)

            # Garante o checkout para o executor rodar (clone se necessário).
            git_dir = os.path.join(checkout, ".git")
            if not os.path.isdir(git_dir):
                source = repo.url or repo.local_path or ""
                if not source:
                    log.warning("resumo: task %s sem repo para gerar resumo", task_id)
                    return False
                try:
                    gitops.clone(source, checkout)
                except gitops.GitError as exc:
                    log.warning("resumo: clone falhou para task %s: %s", task_id, exc)
                    return False

            tl = timeline.derive_task_timeline(s, task)
            timeline_text = timeline.timeline_summary_text(tl)
            phases = _phases_text(task)
            subtasks = _subtasks_text(task)
            project_info = ""
            try:
                from . import project

                project_info = project.detect_project(checkout)
            except Exception:
                pass
            prompt = build_summary_prompt(task, timeline_text, subtasks, phases, project_info)
            log_path = os.path.join(eff.log_dir, f"summary_task_{task_id}.log")
            model = None
            executor = task.executor

        outcome = _run_executor(
            eff,
            executor,
            prompt,
            cwd=checkout,
            log_path=log_path,
            model=model,
            on_event=None,
            kimi_cost_per_interaction=0.0,
        )

        data = verdicts.read_summary(checkout)
        verdicts.remove_summary(checkout)
        if data is None:
            log.warning(
                "resumo: executor não retornou autoia_summary.json válido "
                "(exit=%s, abort=%s) para task %s",
                outcome.exit_code, outcome.aborted, task_id,
            )
            return False

        with session_factory() as s:
            _persist_summary(s, task_id, data, model)
        return True
    except Exception:
        log.exception("resumo: falha ao gerar resumo da task %s", task_id)
        return False
