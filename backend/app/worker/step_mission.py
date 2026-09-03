"""Missão humana de UMA execução de fase ("por que esta execução existe") por LLM.

Mesma filosofia do `step_summarizer`: usa o executor da task (kimi/opencode) com o
contrato `autoia_step_mission.json`, custo contábil zerado, e NUNCA é fonte de
verdade — a LLM apenas interpreta o contexto que originou a execução (devolutivas,
instruções do usuário, reprovações) já registrado nos RunEvent.

A missão é chaveada por (step, run) — a `run` é a numeração real das execuções da
fase (única mesmo quando `attempt` se repete após bounce-back). A UI usa a missão
LLM e, enquanto não está pronta (ou se falhou), um fallback determinístico
(`timeline.fallback_mission`). Falha na geração NUNCA afeta o pipeline.
"""

from __future__ import annotations

import logging
import os

from .. import prompts, verdicts
from ..models import StepMission, TaskStep
from . import gitops, project
from .runner import _effective, _run_executor, _task_workspace

log = logging.getLogger("autoia.step_mission")


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "…"


def _mission_context(session, task: object, step: TaskStep, run: int) -> str:
    """Contexto determinístico que originou esta execução (fases anteriores + o que
    aconteceu nas execuções anteriores da MESMA fase). A LLM só interpreta isso."""
    from .. import timeline

    lines: list[str] = []
    for occ in timeline.derive_task_occurrences(session, task):
        if occ["step_id"] == step.id and occ["run"] >= run:
            continue
        robot = occ.get("robot")
        head = f"Fase {occ['position']} ({robot['name'] if robot else '?'}) — execução {occ['run']} — {occ['status']}"
        stop = occ.get("stop") or {}
        detail = stop.get("detail") or stop.get("reason") or ""
        if detail:
            head += f" — motivo: {_cap(detail, 300)}"
        delivered = (occ.get("delivered_text") or "").strip()
        if delivered:
            head += f" — resultado: {_cap(delivered, 300)}"
        lines.append(head)
    return "\n".join(lines[-30:]) if lines else "(primeira execução desta fase — sem histórico de execução)"


def build_mission_prompt(task, step: TaskStep, context: str) -> str:
    parts = [
        "Você está definindo a missão de UMA execução de uma fase do pipeline autoia.",
        f"Tarefa #{task.id}: {task.title}",
        f"Fase {step.position} — {step.robot.name if step.robot else '?'} "
        f"({step.robot.role if step.robot else '?'}) — tentativa {step.attempt}",
    ]
    if task.description:
        parts.append(f"## Solicitação original\n{_cap(task.description, 1500)}")
    if task.details:
        parts.append(f"## Detalhes adicionados pelo usuário\n{_cap(task.details, 800)}")
    if task.resume_instruction:
        parts.append(f"## Intervenção do usuário (retomada)\n{_cap(task.resume_instruction, 800)}")
    parts.append(f"## Contexto desta execução\n{context}")
    parts.append(prompts.CONTRACT_STEP_MISSION)
    return "\n\n".join(parts)


def generate_mission(settings, session_factory, step_id: int, run: int) -> bool:
    """Gera a missão humana de uma execução de fase via executor. Retorna True se ok.

    Nunca levanta: falhas são logadas e a UI usa o fallback determinístico.
    """
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
                    log.warning("missão: step %s sem repo", step_id)
                    return False
                try:
                    gitops.clone(source, checkout)
                except gitops.GitError as exc:
                    log.warning("missão: clone falhou p/ step %s: %s", step_id, exc)
                    return False

            context = _mission_context(s, task, step, run)
            prompt = build_mission_prompt(task, step, context)
            log_path = os.path.join(eff.log_dir, f"step_mission_{step_id}_{run}.log")

        # A missão roda em background, CONCORRENTE à fase (que pode commitar com
        # `git add -A`) — o arquivo de contrato fica excluído do histórico.
        project.exclude_local(checkout, "autoia_step_mission.json")

        outcome = _run_executor(
            eff,
            task.executor,
            prompt,
            cwd=checkout,
            log_path=log_path,
            model=(task.model or "").strip() or None,
            on_event=None,
            kimi_cost_per_interaction=0.0,
        )

        data = verdicts.read_step_mission(checkout)
        verdicts.remove_step_mission(checkout)
        if data is None:
            log.warning(
                "missão: executor não retornou autoia_step_mission.json "
                "(exit=%s, abort=%s) p/ step %s run %s",
                outcome.exit_code, outcome.aborted, step_id, run,
            )
            return False

        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None:
                return False
            existing = (
                s.query(StepMission)
                .filter(StepMission.step_id == step.id, StepMission.run == run)
                .first()
            )
            if existing is not None:
                return True
            s.add(StepMission(
                task_id=step.task_id,
                step_id=step.id,
                run=run,
                mission=data["mission"],
                source="llm",
            ))
            s.commit()
        return True
    except Exception:
        log.exception("missão: falha ao gerar missão do step %s run %s", step_id, run)
        return False
