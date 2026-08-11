"""Execução de subtarefas (implement → verify com bounce-back interno).

Quando uma task tem subtarefas, as fases `implement` e `verify` do pipeline são
"iteradores": o worker executa o ciclo implement→verify para cada subtarefa em ordem,
na mesma branch. Cada subtarefa tem seu próprio bounce-back: se o tester reprovar,
apenas ESSA subtarefa volta para o developer (as concluídas permanecem).

O módulo é chamado de `execute_step` (runner.py) quando `task.subtasks` não vazio
e o papel do step é `implement` ou `verify`.
"""

from __future__ import annotations

import json
import logging
import os

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .. import budget, prompts, verdicts
from ..config import Settings
from ..models import (
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    SUB_DONE,
    SUB_FAILED,
    SUB_IMPLEMENTED,
    SUB_IMPLEMENTING,
    SUB_PENDING,
    SUB_VERIFYING,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    RunEvent,
    SubTask,
    Task,
    TaskStep,
)
from . import gitops, kimi_exec

log = logging.getLogger("autoia.worker.subtask")


def _system_event(s: Session, step: TaskStep, kind: str, payload: dict) -> None:
    max_seq = (
        s.query(func.max(RunEvent.seq)).filter(RunEvent.step_id == step.id).scalar() or 0
    )
    s.add(RunEvent(step_id=step.id, seq=max_seq + 1, kind=kind, payload=payload))


def _event_count(session_factory, step_id: int) -> int:
    with session_factory() as s:
        return s.query(func.count(RunEvent.id)).filter(RunEvent.step_id == step_id).scalar() or 0


def _make_on_event(session_factory, step_id: int, log_path: str):
    """Callback de evento do kimi_exec: persiste eventos e verifica orçamento."""
    state = {"seq": _event_count(session_factory, step_id), "cost": 0.0}

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        state["seq"] += 1
        with session_factory() as es:
            st = es.get(TaskStep, step_id)
            if st is None:
                return None
            t = st.task
            t.cost_spent = (t.cost_spent or 0.0) + cost
            es.add(
                RunEvent(
                    step_id=step_id,
                    seq=state["seq"],
                    kind=kind,
                    payload=payload,
                    cost=cost,
                )
            )
            es.commit()
            if budget.budget_exceeded(t.cost_spent, t.budget_limit):
                return (
                    f"orçamento estourado: gasto {t.cost_spent:.2f} "
                    f">= limite {t.budget_limit:.2f}"
                )
        return None

    return on_event


def _build_subtask_implement_prompt(
    task: Task,
    subtask,
    project_info: str,
    base: str,
    branch: str,
    checkout: str,
) -> str:
    """Prompt para o developer implementar UMA subtarefa."""
    parts: list[str] = []

    # Missão do developer (com placeholders da task global)
    robot = next((st.robot for st in task.steps if st.robot and st.robot.role == "implement"), None)
    if robot:
        mission = (robot.mission or "").strip()
        parts.append(
            mission.replace("{task_title}", task.title or "")
            .replace("{task_description}", task.description or "")
            .replace("{step_context}", f"Subtarefa {subtask.position + 1}: {subtask.title}")
            .replace("{default_branch}", base or "main")
        )

    parts.append(prompts.GIT_WORKFLOW)
    if project_info:
        parts.append(project_info)

    # Contexto da subtarefa
    parts.append(f"## Subtarefa {subtask.position + 1}: {subtask.title}")
    if subtask.description:
        parts.append(subtask.description)
    if subtask.acceptance_criteria:
        parts.append(f"### Seus critérios\n{subtask.acceptance_criteria}")

    # Critérios gerais da história como referência
    if task.acceptance_criteria:
        parts.append(f"### Critérios gerais da história (referência)\n{task.acceptance_criteria}")

    # Progresso das subtarefas anteriores
    done = [s for s in sorted(task.subtasks, key=lambda x: x.position) if s.status == SUB_DONE]
    if done:
        lines = ["### Subtarefas já concluídas (código na branch)"]
        for s in done:
            lines.append(f"- Subtarefa {s.position + 1}: {s.title} — {s.summary or '(sem resumo)'[:200]}")
        parts.append("\n".join(lines))

    # Diff acumulado
    try:
        diff = gitops.diff_stat(checkout, base, branch)
        if diff:
            parts.append(f"### Diff acumulado da branch\n```\n{diff}\n```")
    except gitops.GitError:
        pass

    parts.append(prompts.SUB_TASK_DONE_TOOL)
    parts.append(prompts.HANDOFF_READ)
    parts.append(prompts.HANDOFF_DOCUMENT)
    parts.append(prompts.EVIDENCE)
    parts.append(prompts.GUARDRAIL_INSTRUCTIONS)

    return "\n\n".join(p for p in parts if p)


def _build_subtask_verify_prompt(
    task: Task,
    subtask,
    project_info: str,
    base: str,
    branch: str,
    checkout: str,
) -> str:
    """Prompt para o tester verificar UMA subtarefa."""
    parts: list[str] = []

    robot = next((st.robot for st in task.steps if st.robot and st.robot.role == "verify"), None)
    if robot:
        mission = (robot.mission or "").strip()
        parts.append(
            mission.replace("{task_title}", task.title or "")
            .replace("{task_description}", task.description or "")
            .replace("{step_context}", f"Subtarefa {subtask.position + 1}: {subtask.title}")
            .replace("{default_branch}", base or "main")
        )

    parts.append(prompts.GIT_WORKFLOW)
    if project_info:
        parts.append(project_info)

    parts.append(f"## Subtarefa a verificar: {subtask.title}")
    if subtask.acceptance_criteria:
        parts.append(f"### Critérios a validar\n{subtask.acceptance_criteria}")

    # Histórico da subtarefa (resumo do developer)
    if subtask.summary:
        parts.append(f"### Resumo da implementação\n{subtask.summary[:1000]}")

    # Progresso
    implemented = [s for s in sorted(task.subtasks, key=lambda x: x.position) if s.status == SUB_IMPLEMENTED]
    if len(implemented) > 1:
        lines = ["### Subtarefas implementadas (na branch)"]
        for s in implemented:
            if s.id != subtask.id:
                lines.append(f"- Subtarefa {s.position + 1}: {s.title}")
        parts.append("\n".join(lines))

    try:
        diff = gitops.diff_stat(checkout, base, branch)
        if diff:
            parts.append(f"### Diff acumulado da branch\n```\n{diff}\n```")
    except gitops.GitError:
        pass

    parts.append(prompts.HANDOFF_READ)
    parts.append(prompts.CONTRACT_VERIFY)
    parts.append(prompts.HANDOFF_DOCUMENT)
    parts.append(prompts.EVIDENCE)
    parts.append(prompts.GUARDRAIL_INSTRUCTIONS)

    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Execução do implement (developer) para subtarefas
# ---------------------------------------------------------------------------


def run_implement_subtasks(
    settings: Settings,
    session_factory,
    step: TaskStep,
    task_id: int,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
    log_path: str,
) -> str | None:
    """Executa a fase implement para cada subtarefa pendente.

    Retorna string de erro (orçamento, guardrail fatal) ou None se concluiu.
    """
    on_event = _make_on_event(session_factory, step.id, log_path)

    with session_factory() as s:
        pending = (
            s.query(SubTask)
            .filter(SubTask.task_id == task_id, SubTask.status.in_([SUB_PENDING]))
            .order_by(SubTask.position)
            .all()
        )
        if not pending:
            return None

    for subtask in pending:
        abort_reason = _run_one_implement(
            settings, session_factory, step, task_id, subtask,
            checkout, base, branch, project_info, log_path, on_event,
        )
        if abort_reason:
            return abort_reason

    return None


def _subtask_marked_done(checkout: str, position_1based: int) -> bool:
    """True se o agente declarou a subtarefa como já implementada via
    `autoia_subtasks_done.json` (array de posições 1-based) no checkout."""
    path = os.path.join(checkout, "autoia_subtasks_done.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, list):
        return False
    for item in data:
        if isinstance(item, int) and item == position_1based:
            return True
        if isinstance(item, str) and item.strip().isdigit() and int(item.strip()) == position_1based:
            return True
    return False


def _run_one_implement(
    settings: Settings,
    session_factory,
    step: TaskStep,
    task_id: int,
    subtask,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
    log_path: str,
    on_event,
) -> str | None:
    """Executa o kimi (developer) para UMA subtarefa. Retorna erro se abortar."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return "task não encontrada"
        st = s.get(SubTask, subtask.id)
        if st is None:
            return "subtarefa não encontrada"
        st.status = SUB_IMPLEMENTING
        st.started_at = func.now()
        st.attempt += 1
        _system_event(
            s, step, "subtask_start",
            {"position": st.position, "title": st.title, "attempt": st.attempt, "phase": "implement"},
        )
        prompt = _build_subtask_implement_prompt(task, st, project_info, base, branch, checkout)
        _system_event(
            s, step, "subtask_prompt",
            {"position": st.position, "title": st.title, "prompt": prompt},
        )
        s.commit()

    outcome = kimi_exec.run_kimi(
        prompt,
        cwd=checkout,
        kimi_bin=settings.kimi_bin,
        log_path=log_path,
        timeout=settings.run_timeout,
        max_identical_calls=settings.max_identical_calls,
        risky_patterns=settings.risky_patterns,
        checkout_path=checkout,
        cost_per_interaction=settings.cost_per_interaction,
        on_event=on_event,
    )

    with session_factory() as s:
        st = s.get(SubTask, subtask.id)

        if outcome.aborted:
            reason = outcome.abort_reason or "abortado"
            st.status = SUB_PENDING
            st.error = reason
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_failed",
                {"position": st.position, "title": st.title, "reason": reason, "phase": "implement"},
            )
            s.commit()
            return reason

        if outcome.exit_code != 0:
            reason = f"kimi saiu com código {outcome.exit_code}"
            st.status = SUB_PENDING
            st.error = reason
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_failed",
                {"position": st.position, "title": st.title, "reason": reason, "phase": "implement"},
            )
            s.commit()
            return reason

        # Sucesso: o agente pode ter declarado a subtarefa como JÁ implementada na
        # branch (arquivo autoia_subtasks_done.json) — evita re-implementar trabalho
        # já commitado (ex.: status perdido por restart do worker).
        if _subtask_marked_done(checkout, st.position + 1):
            st.status = SUB_IMPLEMENTED
            st.error = None
            st.finished_at = func.now()
            args = json.dumps(
                {"subtask_id": st.position + 1, "title": st.title},
                ensure_ascii=False,
            )
            _system_event(
                s, step, "tool_call",
                {"tool_call": {"function": {"name": "autoia_mark_subtask_done", "arguments": args}},
                 "violation": None},
            )
            _system_event(
                s, step, "tool_result",
                {"content": f"subtarefa {st.position + 1} ({st.title}) marcada como "
                            "implementada — código já presente na branch"},
            )
            _system_event(
                s, step, "subtask_marked_done",
                {"position": st.position, "title": st.title,
                 "reason": "agente declarou a subtarefa como já implementada na branch"},
            )
            s.commit()
            return None

        # Sucesso: commit local e avança status
        try:
            gitops.commit_all(checkout, f"autoia: subtask {st.position + 1} - {st.title}")
        except gitops.GitError as exc:
            st.status = SUB_PENDING
            st.error = f"commit: {exc}"
            st.finished_at = func.now()
            s.commit()
            return f"commit: {exc}"

        st.status = SUB_IMPLEMENTED
        st.summary = outcome.final_text or ""
        st.error = None
        st.finished_at = func.now()
        _system_event(
            s, step, "subtask_implemented",
            {"position": st.position, "title": st.title, "summary": outcome.final_text[:500]},
        )
        s.commit()

    return None


# ---------------------------------------------------------------------------
# Execução do verify (tester) para subtarefas
# ---------------------------------------------------------------------------


def run_verify_subtasks(
    settings: Settings,
    session_factory,
    step: TaskStep,
    task_id: int,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
    log_path: str,
) -> str | None:
    """Executa a fase verify para cada subtarefa implementada.

    Retorna None se todas passaram, ou string com posição das que falharam
    (ex.: "sub:1,3") para o bounce-back decidir.
    """
    on_event = _make_on_event(session_factory, step.id, log_path)
    failed_positions: list[int] = []

    with session_factory() as s:
        to_verify = (
            s.query(SubTask)
            .filter(SubTask.task_id == task_id, SubTask.status.in_([SUB_IMPLEMENTED]))
            .order_by(SubTask.position)
            .all()
        )
        already_done = (
            s.query(SubTask)
            .filter(SubTask.task_id == task_id, SubTask.status == SUB_DONE)
            .count()
        )
        if not to_verify:
            if already_done > 0:
                return None  # todas já verificadas em fase anterior
            return "nenhuma subtarefa para verificar"

    for subtask in to_verify:
        abort_reason = _run_one_verify(
            settings, session_factory, step, task_id, subtask,
            checkout, base, branch, project_info, log_path, on_event,
        )
        if abort_reason:
            return abort_reason

        with session_factory() as s:
            st = s.get(SubTask, subtask.id)
            if st and st.status not in (SUB_DONE,):
                failed_positions.append(st.position)

    if failed_positions:
        return f"sub:{','.join(str(p) for p in failed_positions)}"
    return None


def _run_one_verify(
    settings: Settings,
    session_factory,
    step: TaskStep,
    task_id: int,
    subtask,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
    log_path: str,
    on_event,
) -> str | None:
    """Executa o kimi (tester) para UMA subtarefa. Retorna erro se abortar."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return "task não encontrada"
        st = s.get(SubTask, subtask.id)
        if st is None:
            return "subtarefa não encontrada"
        st.status = SUB_VERIFYING
        st.started_at = func.now()
        _system_event(
            s, step, "subtask_start",
            {"position": st.position, "title": st.title, "attempt": st.attempt, "phase": "verify"},
        )
        prompt = _build_subtask_verify_prompt(task, st, project_info, base, branch, checkout)
        _system_event(
            s, step, "subtask_prompt",
            {"position": st.position, "title": st.title, "prompt": prompt},
        )
        s.commit()

    outcome = kimi_exec.run_kimi(
        prompt,
        cwd=checkout,
        kimi_bin=settings.kimi_bin,
        log_path=log_path,
        timeout=settings.run_timeout,
        max_identical_calls=settings.max_identical_calls,
        risky_patterns=settings.risky_patterns,
        checkout_path=checkout,
        cost_per_interaction=settings.cost_per_interaction,
        on_event=on_event,
    )

    with session_factory() as s:
        st = s.get(SubTask, subtask.id)

        if outcome.aborted:
            reason = outcome.abort_reason or "abortado"
            st.status = SUB_PENDING
            st.error = reason
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_failed",
                {"position": st.position, "title": st.title, "reason": reason, "phase": "verify"},
            )
            s.commit()
            return reason

        if outcome.exit_code != 0:
            reason = f"kimi saiu com código {outcome.exit_code}"
            st.status = SUB_PENDING
            st.error = reason
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_failed",
                {"position": st.position, "title": st.title, "reason": reason, "phase": "verify"},
            )
            s.commit()
            return reason

        # Lê veredicto (tester escreve autoia_verdict.txt)
        raw = verdicts.read_verdict(checkout)
        verdicts.remove_verdict(checkout)
        label = verdicts.parse_pass_fail(raw)
        st.verdict = label or (raw or "")[:30] or "AUSENTE"

        if label == verdicts.V_PASS:
            st.status = SUB_DONE
            st.error = None
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_verified",
                {"position": st.position, "title": st.title, "verdict": "PASS"},
            )
        else:
            # FAIL ou AUSENTE → volta para o developer refazer
            st.status = SUB_PENDING
            st.summary = raw  # relatório do tester para o developer
            st.error = f"veredicto {label or 'AUSENTE'}"
            st.finished_at = func.now()
            _system_event(
                s, step, "subtask_failed",
                {"position": st.position, "title": st.title,
                 "reason": f"veredicto {label or 'AUSENTE'}", "phase": "verify"},
            )

        s.commit()
    return None


def bounce_back_subtasks(session_factory, step: TaskStep, task: Task) -> None:
    """Prepara bounce-back de verify → implement: marca subtarefas FAIL como pending.

    Chamado após o verify step falhar (alguma subtarefa com veredicto != PASS).
    """
    with session_factory() as s:
        t = s.merge(task)
        for st in t.subtasks:
            if st.status not in (SUB_DONE,):
                st.status = SUB_PENDING
                st.verdict = None
                st.finished_at = None
        s.commit()
