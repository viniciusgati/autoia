"""Worker único: consome TaskSteps pendentes e executa a fase com o kimi.

Fluxo de decisão por step:
- abort "orçamento"        -> task `needs_review` (e PM pode decidir continuar/retry/escalar)
- abort "guardrail"        -> step `guardrail_blocked` + **bounce-back** para a fase anterior
- abort (timeout) / exit!=0 / veredicto FAIL/NEEDS_WORK/AUSENTE -> step `failed` + **bounce-back**
- bounce-back: fase anterior volta a `pending` (attempt+1) com o relatório no contexto;
  sem fase anterior ou max_attempts estourado -> task `failed` (+ PM)
- sucesso em fase de verificação (review/verify) exige veredicto esperado
- sucesso em `refine` (po) grava a história (descrição + critérios de aceite) na task
- sucesso no último passo -> merge+push (feito pelo worker, nunca pelo robô)
"""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .. import budget, prompts, verdicts
from ..config import Settings
from ..db import Base, make_engine, make_session_factory, migrate_schema, utcnow
from ..models import (
    STEP_DONE,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_PENDING,
    STEP_RUNNING,
    TASK_BLOCKED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_QUEUED,
    Robot,
    RunEvent,
    Task,
    TaskStep,
)
from . import gitops, kimi_exec, project

log = logging.getLogger("autoia.worker")

# Papéis que exigem veredicto e o veredicto esperado para avançar.
VERDICT_EXPECTED = {
    "review": verdicts.V_READY,
    "verify": verdicts.V_PASS,
}


def worker_loop(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # não depende da API ter subido antes
    migrate_schema(engine)
    session_factory = make_session_factory(engine)
    log.info("worker iniciado (dir de trabalho: %s)", settings.workspace_dir)
    while True:
        try:
            step_id = claim_next(session_factory)
        except Exception:
            log.exception("erro no claim de step")
            time.sleep(2)
            continue
        if step_id is None:
            time.sleep(2)
            continue
        log.info("executando step %s", step_id)
        try:
            trigger = execute_step(settings, session_factory, step_id)
            if trigger:
                _maybe_pm(session_factory, settings, trigger["task_id"], trigger["reason"])
        except Exception:
            log.exception("erro executando step %s", step_id)
            _fail_step_hard(session_factory, step_id)


def claim_next(session_factory) -> int | None:
    """Reivindica o próximo step pendente de uma task ativa (claim atômico)."""
    with session_factory() as s:
        step = (
            s.query(TaskStep)
            .join(Task)
            .filter(
                TaskStep.status == STEP_PENDING,
                Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
            )
            .order_by(TaskStep.id)
            .first()
        )
        if step is None:
            return None
        result = s.execute(
            update(TaskStep)
            .where(TaskStep.id == step.id, TaskStep.status == STEP_PENDING)
            .values(status=STEP_RUNNING, started_at=utcnow())
        )
        if result.rowcount != 1:
            return None
        step.task.status = TASK_IN_PROGRESS
        s.commit()
        return step.id


def _system_event(s: Session, step: TaskStep | None, kind: str, payload: dict) -> None:
    if step is None:
        return
    max_seq = (
        s.query(func.max(RunEvent.seq)).filter(RunEvent.step_id == step.id).scalar() or 0
    )
    s.add(RunEvent(step_id=step.id, seq=max_seq + 1, kind=kind, payload=payload))


def _finish(step: TaskStep) -> None:
    step.finished_at = utcnow()


def _build_step_context(
    s: Session, task: Task, current_step: TaskStep, checkout: str, base: str, branch: str
) -> str:
    parts = []
    for st in sorted(task.steps, key=lambda x: x.position):
        if st.position == current_step.position:
            continue
        robot_name = st.robot.name if st.robot else "?"
        if st.position < current_step.position and st.summary:
            parts.append(f"Fase {st.position} ({robot_name}): {st.summary[:500]}")
        elif (
            st.position > current_step.position
            and st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)
            and (st.summary or st.error)
        ):
            # relatório COMPLETO da fase que falhou (alimenta o bounce-back)
            detail = st.summary[:2000] if st.summary else ""
            if st.error:
                detail = f"{st.error}\n{detail}".strip()
            parts.append(
                f"FASE POSTERIOR {st.position} ({robot_name}) FALHOU:\n{detail[:2500]}"
            )
    context = "\n".join(parts)
    try:
        diff = gitops.diff_stat(checkout, base, branch)
        if diff:
            context += f"\nDiff atual:\n{diff}"
    except gitops.GitError:
        pass
    return context


def execute_step(settings: Settings, session_factory, step_id: int) -> dict | None:
    """Executa o step. Retorna um gatilho de PM ({task_id, reason}) quando aplicável."""
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task
        repo = task.repository
        checkout = repo.local_path or ""
        base = repo.default_branch
        branch = task.branch or f"{settings.branch_prefix}/task-{task.id}"

        if not checkout or not os.path.isdir(checkout):
            step.status = STEP_FAILED
            step.error = f"checkout ausente: {checkout}"
            task.status = TASK_FAILED
            task.error = step.error
            _finish(step)
            s.commit()
            return None

        try:
            if step.post_merge:
                # fase pós-merge: roda na branch default integrada (espelho do remote)
                gitops.checkout_default(checkout, base)
            else:
                gitops.ensure_task_branch(checkout, branch, base)
        except gitops.GitError as exc:
            step.status = STEP_FAILED
            step.error = f"git: {exc}"
            task.status = TASK_FAILED
            task.error = step.error
            _finish(step)
            s.commit()
            return None

        step_context = _build_step_context(s, task, step, checkout, base, branch)
        project_info = project.detect_project(checkout)
        prompt = prompts.build_prompt(
            step.robot, task, step_context, base, project_info=project_info
        )
        log_path = os.path.join(settings.log_dir, f"step_{step.id}.log")
        step.log_path = str(log_path)
        s.commit()

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
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        verdict_label = _consume_verdict(s, step, checkout)
        s.commit()
    return _decide(settings, session_factory, step_id, checkout, outcome, verdict_label)


def _consume_verdict(s: Session, step: TaskStep, checkout: str) -> str | None:
    """Lê e remove o veredicto, se o papel do robô exigir. Retorna o rótulo."""
    role = step.robot.role if step.robot else ""
    if role not in VERDICT_EXPECTED:
        return None
    raw = verdicts.read_verdict(checkout)
    verdicts.remove_verdict(checkout)
    if role == "review":
        label = verdicts.parse_ready_work(raw)
    else:
        label = verdicts.parse_pass_fail(raw)
    step.verdict = label or (raw or "")[:30] or "AUSENTE"
    return label


def _event_count(session_factory, step_id: int) -> int:
    with session_factory() as s:
        return s.query(func.count(RunEvent.id)).filter(RunEvent.step_id == step_id).scalar() or 0


def _decide(settings: Settings, session_factory, step_id: int, checkout: str, outcome, verdict_label: str | None) -> dict | None:
    trigger: dict | None = None
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task
        repo = task.repository
        role = step.robot.role if step.robot else ""
        reason = outcome.abort_reason or ""

        if outcome.aborted:
            if "orçamento" in reason:
                _system_event(s, step, "budget_hit", {"reason": reason})
                task.status = TASK_NEEDS_REVIEW
                task.error = reason
                step.status = STEP_PENDING
                step.error = reason
                step.started_at = None
                s.commit()
                return {"task_id": task.id, "reason": reason}

            if reason.startswith("guardrail"):
                trigger = _handle_failure(settings, s, step, task, reason, "guardrail_blocked", STEP_GUARDRAIL_BLOCKED)
            else:
                trigger = _handle_failure(settings, s, step, task, reason, "timeout", STEP_FAILED)
            s.commit()
            return trigger

        if outcome.exit_code != 0:
            trigger = _handle_failure(
                settings, s, step, task,
                f"kimi saiu com código {outcome.exit_code}", "kimi_exit", STEP_FAILED,
            )
            s.commit()
            return trigger

        # Veredicto de verificação (review/verify) é obrigatório e deve ser o esperado.
        if role in VERDICT_EXPECTED:
            expected = VERDICT_EXPECTED[role]
            if verdict_label != expected:
                trigger = _handle_failure(
                    settings, s, step, task,
                    f"veredicto {verdict_label or 'AUSENTE'} (esperado {expected})",
                    "verdict", STEP_FAILED,
                )
                s.commit()
                return trigger

        # Sucesso: commit local apenas em fases pré-merge.
        if not step.post_merge:
            try:
                gitops.commit_all(checkout, f"autoia: {task.title} (fase {step.position})")
            except gitops.GitError as exc:
                trigger = _handle_failure(settings, s, step, task, f"commit: {exc}", "git_error", STEP_FAILED)
                s.commit()
                return trigger

        step.summary = (outcome.final_text or "")[:2000]

        # Papel refine (po): grava a história na task.
        if role == "refine":
            description, criteria = verdicts.parse_story(outcome.final_text or "")
            if description:
                task.description = description
            if criteria:
                task.acceptance_criteria = criteria

        steps = sorted(task.steps, key=lambda x: x.position)
        nxt = next((st for st in steps if st.position > step.position), None)

        if step.post_merge:
            # fase pós-merge: nunca faz merge nem commit; só avança (ou conclui)
            step.status = STEP_DONE
            task.current_step = step.position
            if nxt is None:
                task.status = TASK_DONE
            else:
                nxt.status = STEP_PENDING
            _system_event(s, step, "phase_done", {"next": nxt.position if nxt else None})
            _finish(step)
            s.commit()
            return None

        # Pré-merge: a última fase pré-merge (próxima é pós-merge, ou é a última) integra.
        merge_now = nxt is None or nxt.post_merge
        if merge_now:
            try:
                result = gitops.merge_and_push(checkout, task.branch, repo.default_branch)
            except gitops.GitError as exc:
                trigger = _handle_failure(settings, s, step, task, f"merge/push: {exc}", "merge_error", STEP_FAILED)
                s.commit()
                return trigger
            if result.ok:
                _system_event(s, step, "merged", {"detail": result.detail})
                step.status = STEP_DONE
                if nxt is not None:
                    nxt.status = STEP_PENDING
                    task.current_step = step.position
                else:
                    task.status = TASK_DONE
            else:
                _system_event(s, step, "merge_failed", {"detail": result.detail})
                step.status = STEP_FAILED
                step.error = result.detail
                if result.conflict:
                    task.status = TASK_BLOCKED
                    task.error = "conflito de merge"
                else:
                    task.status = TASK_FAILED
                    task.error = result.detail
            _finish(step)
            s.commit()
            return None

        step.status = STEP_DONE
        task.current_step = step.position
        nxt.status = STEP_PENDING
        _system_event(s, step, "phase_done", {"next": nxt.position})
        _finish(step)
        s.commit()
        return None


def _handle_failure(settings: Settings, s: Session, step: TaskStep, task: Task, reason: str, kind: str, step_status: str) -> dict | None:
    """Registra a falha e decide: bounce-back (pré-merge) ou revisão + PM (pós-merge).

    Retorna um gatilho de PM quando a task precisa de decisão automática.
    """
    _system_event(s, step, kind, {"reason": reason})
    step.status = step_status
    step.error = reason
    _finish(step)

    if step.post_merge:
        # Código já integrado: nada é revertido sozinho; vai para revisão + PM decide.
        task.status = TASK_NEEDS_REVIEW
        task.error = f"falha pós-merge (código já integrado): {reason}"
        _system_event(s, step, "post_merge_failed", {"reason": reason})
        return {"task_id": task.id, "reason": task.error}

    previous = next(
        (
            st
            for st in sorted(task.steps, key=lambda x: -x.position)
            if st.position < step.position
        ),
        None,
    )
    if previous is not None and previous.attempt < settings.max_attempts:
        previous.status = STEP_PENDING
        previous.attempt += 1
        previous.error = None
        previous.summary = None
        previous.finished_at = None
        task.status = TASK_IN_PROGRESS
        task.current_step = previous.position
        task.error = None
        _system_event(
            s, previous, "bounce_back",
            {"from_position": step.position, "reason": reason},
        )
        return None

    task.status = TASK_FAILED
    task.error = reason
    return {"task_id": task.id, "reason": reason}


# ---------------------------------------------------------------------------
# PM (controle do projeto)
# ---------------------------------------------------------------------------

def _pm_context(s: Session, task: Task) -> str:
    lines = [
        f"Tarefa #{task.id}: {task.title}",
        f"Status: {task.status} | Orçamento: {task.budget_limit:.2f} US$ | Gasto: {task.cost_spent:.2f} US$",
        f"Branch: {task.branch}",
        f"Decisões de PM já tomadas: {task.pm_decisions}",
    ]
    for st in sorted(task.steps, key=lambda x: x.position):
        robot_name = st.robot.name if st.robot else "?"
        lines.append(
            f"Fase {st.position} ({robot_name}) [{st.status}] tentativa {st.attempt}"
            f"{' veredicto ' + st.verdict if st.verdict else ''}"
            f"{' erro: ' + st.error[:300] if st.error else ''}"
            f"{' resumo: ' + (st.summary or '')[:300] if st.summary else ''}"
        )
    return "\n".join(lines)


def _pm_decide(session_factory, settings: Settings, task_id: int, trigger: str) -> None:
    """Roda o PM e aplica a decisão (retry / continuar / escalar). Sempre bound por pm_decisions."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        checkout = task.repository.local_path or ""
        pm_robot = s.query(Robot).filter(Robot.name == "pm").first()
        if pm_robot is None:
            return
        context = _pm_context(s, task)
        project_info = project.detect_project(checkout) if os.path.isdir(checkout) else ""
        prompt = prompts.build_prompt(
            pm_robot, task, context, task.repository.default_branch, project_info=project_info
        )
        log_path = os.path.join(settings.log_dir, f"pm_task_{task_id}.log")
        task.pm_decisions += 1
        s.commit()

    outcome = kimi_exec.run_kimi(
        prompt,
        cwd=checkout if os.path.isdir(checkout) else settings.workspace_dir,
        kimi_bin=settings.kimi_bin,
        log_path=log_path,
        timeout=settings.run_timeout,
        max_identical_calls=settings.max_identical_calls,
        risky_patterns=settings.risky_patterns,
        checkout_path=checkout if os.path.isdir(checkout) else settings.workspace_dir,
        cost_per_interaction=0.0,
        on_event=None,
    )

    raw = verdicts.read_verdict(checkout) if os.path.isdir(checkout) else None
    verdicts.remove_verdict(checkout) if os.path.isdir(checkout) else None
    decision = verdicts.parse_pm_decision(raw)
    decision["reason"] = decision["reason"] or outcome.final_text[:300]

    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        anchor = sorted(task.steps, key=lambda x: x.position)[-1]
        _system_event(s, anchor, "pm_decision", {"trigger": trigger, **decision})

        if decision["action"] == verdicts.PM_RETRY:
            target = None
            if decision.get("position") is not None:
                target = next((st for st in task.steps if st.position == decision["position"]), None)
            if target is None:
                target = next(
                    (st for st in task.steps if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
                    None,
                )
            if target is not None and target.attempt < settings.max_attempts:
                target.status = STEP_PENDING
                target.attempt += 1
                target.error = None
                target.summary = None
                target.finished_at = None
                task.status = TASK_IN_PROGRESS
                task.error = None
                task.current_step = target.position
            else:
                task.status = TASK_NEEDS_REVIEW
                task.error = f"PM: retry inválido/limitado ({decision['reason']})"

        elif decision["action"] == verdicts.PM_CONTINUE:
            task.budget_limit = (task.budget_limit or 0.0) + settings.pm_budget_topup
            task.status = TASK_IN_PROGRESS
            task.error = None
            pending = next((st for st in task.steps if st.status == STEP_PENDING), None)
            if pending is None:
                failed = next(
                    (st for st in task.steps if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
                    None,
                )
                if failed is not None:
                    failed.status = STEP_PENDING
                    failed.error = None
                    task.current_step = failed.position

        else:  # escalar (default seguro)
            task.status = TASK_NEEDS_REVIEW
            task.error = f"PM escalou: {decision['reason']}"

        s.commit()


def _maybe_pm(session_factory, settings: Settings, task_id: int, reason: str) -> None:
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        if task.pm_decisions >= settings.max_pm_decisions:
            anchor = sorted(task.steps, key=lambda x: x.position)[-1] if task.steps else None
            _system_event(
                s, anchor, "pm_skip",
                {"reason": f"limite de decisões ({settings.max_pm_decisions}) atingido"},
            )
            s.commit()
            return
    log.info("PM decidindo para a task %s (%s)", task_id, reason)
    _pm_decide(session_factory, settings, task_id, reason)


def _fail_step_hard(session_factory, step_id: int) -> None:
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return
        step.status = STEP_FAILED
        step.error = "erro interno do worker"
        step.task.status = TASK_FAILED
        step.task.error = step.error
        step.finished_at = utcnow()
        s.commit()
