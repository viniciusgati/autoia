"""Resumo de UMA fase concluída ("O que foi entregue") por LLM dedicada a resumo.

Mesma filosofia do `summarizer`: usa o executor da task (kimi/opencode) com o
contrato `autoia_step_summary.json`, custo contábil zerado, e NUNCA é fonte de
verdade — a LLM apenas interpreta eventos/arquivos/diff já registrados.

O resumo é chaveado por (step, attempt): re-execuções têm resumos independentes,
preservando o histórico imutável da timeline do workspace. Falha na geração NUNCA
afeta o pipeline (a UI simplesmente mostra o texto final do robô como fallback).
"""

from __future__ import annotations

import logging
import os

from .. import prompts, verdicts
from ..models import RunEvent, StepSummary, Task, TaskStep
from . import gitops
from .runner import _effective, _run_executor, _task_workspace

log = logging.getLogger("autoia.step_summarizer")


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut.rsplit("\n", 1)[0] + f"\n… (contexto truncado em {limit} chars para o resumo)"


def build_step_summary_prompt(
    task: Task,
    step: TaskStep,
    activity: str,
    delivered_text: str,
    diff_text: str,
    verdict_label: str | None,
    failure_reason: str | None = None,
    failure_detail: str = "",
) -> str:
    parts = [
        "Você está resumindo UMA fase de um desenvolvimento já executado pelo pipeline autoia.",
        f"Tarefa #{task.id}: {task.title}",
        f"Fase {step.position} — {step.robot.name if step.robot else '?'} "
        f"({step.robot.role if step.robot else '?'}) — tentativa {step.attempt} — status {step.status}",
    ]
    if task.description:
        parts.append(f"## Solicitação original\n{_cap(task.description, 2000)}")
    if task.details:
        parts.append(f"## Detalhes adicionados pelo usuário\n{_cap(task.details, 1000)}")
    if task.resume_instruction:
        parts.append(f"## Intervenção do usuário (retomada)\n{_cap(task.resume_instruction, 1000)}")
    if step.goal:
        parts.append(f"## O que esta fase deveria fazer\n{step.goal}")
    if verdict_label:
        parts.append(f"## Veredicto da fase\n{verdict_label}")
    if failure_reason:
        parts.append(f"## Motivo da parada/falha\n{failure_reason}")
    if failure_detail:
        parts.append(f"## Detalhe da reprovação/falha\n{_cap(failure_detail, 3000)}")
    if activity:
        parts.append(f"## Atividade da fase (ferramentas/comandos)\n{_cap(activity, 6000)}")
    if delivered_text:
        parts.append(f"## Texto final do robô da fase\n{_cap(delivered_text, 8000)}")
    if diff_text:
        parts.append(f"## Alterações\n{_cap(diff_text, 6000)}")
    parts.append(prompts.CONTRACT_STEP_SUMMARY)
    return "\n\n".join(parts)


def summarize_step(settings, session_factory, step_id: int) -> bool:
    """Gera o resumo de uma fase concluída via executor. Retorna True em sucesso.

    Nunca levanta: falhas são logadas e a fase segue sem resumo (a UI usa o texto
    final do robô como fallback).
    """
    from .. import timeline

    try:
        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None:
                return False
            task = step.task
            repo = task.repository
            eff = _effective(settings, repo)
            checkout = _task_workspace(eff, repo.id, task.id)

            git_dir = os.path.join(checkout, ".git")
            if not os.path.isdir(git_dir):
                source = repo.url or repo.local_path or ""
                if not source:
                    log.warning("resumo de fase: step %s sem repo", step_id)
                    return False
                try:
                    gitops.clone(source, checkout)
                except gitops.GitError as exc:
                    log.warning("resumo de fase: clone falhou p/ step %s: %s", step_id, exc)
                    return False

            # Usa a ÚLTIMA ocorrência da fase (o `attempt` pode se repetir após
            # bounce-back sem incrementar o contador da própria fase).
            occurrences = [
                occ for occ in timeline.derive_task_occurrences(s, task)
                if occ["step_id"] == step.id
            ]
            if not occurrences:
                return False
            occurrence = occurrences[-1]
            stop = occurrence.get("stop") or {}
            activity_lines = []
            for ev in occurrence["events"]:
                if ev["type"] in ("tool_call", "system", "task", "warning", "error"):
                    activity_lines.append(ev["summary"])
            activity = "\n".join(activity_lines)
            delivered_text = occurrence.get("delivered_text") or ""
            if not delivered_text:
                delivered_text = step.summary or ""

            diff_text = ""
            if step.diff_stat:
                diff_text = step.diff_stat
            prompt = build_step_summary_prompt(
                task, step, activity, delivered_text, diff_text, step.verdict,
                failure_reason=stop.get("reason"),
                failure_detail=stop.get("detail") or "",
            )
            log_path = os.path.join(eff.log_dir, f"step_summary_{step_id}.log")

        outcome = _run_executor(
            eff,
            task.executor,
            prompt,
            cwd=checkout,
            log_path=log_path,
            model=None,
            on_event=None,
            kimi_cost_per_interaction=0.0,
        )

        data = verdicts.read_step_summary(checkout)
        verdicts.remove_step_summary(checkout)
        if data is None:
            log.warning(
                "resumo de fase: executor não retornou autoia_step_summary.json "
                "(exit=%s, abort=%s) p/ step %s",
                outcome.exit_code, outcome.aborted, step_id,
            )
            return False

        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None:
                return False
            task = step.task
            existing = (
                s.query(StepSummary)
                .filter(
                    StepSummary.step_id == step.id,
                    StepSummary.attempt == step.attempt,
                )
                .first()
            )
            if existing is not None:
                return True
            s.add(StepSummary(
                task_id=task.id,
                step_id=step.id,
                position=step.position,
                attempt=step.attempt,
                summary=data.get("summary") or "",
                changes=data.get("changes") or [],
                result=data.get("result"),
                issues=data.get("issues") or [],
                files=data.get("files") or [],
            ))
            s.commit()
        return True
    except Exception:
        log.exception("resumo de fase: falha ao gerar resumo do step %s", step_id)
        return False
