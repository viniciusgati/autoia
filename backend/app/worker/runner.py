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

import json
import logging
import os
import time
import types
from dataclasses import dataclass, field

from sqlalchemy import exists, func, update
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
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_NEEDS_REVIEW,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Pipeline,
    PipelineStep,
    Repository,
    Robot,
    RunEvent,
    StepArtifact,
    SubTask,
    Task,
    TaskStep,
)
from . import arch_metric, gitops, handoff, kimi_exec, opencode_exec, project, subtask

log = logging.getLogger("autoia.worker")

# Papéis que exigem veredicto e o veredicto esperado para avançar.
VERDICT_EXPECTED = {
    "review": verdicts.V_READY,
    "verify": verdicts.V_PASS,
    "assess": verdicts.V_PASS,
}


@dataclass
class EffectiveSettings:
    """Configurações efetivas para uma task (repo > global)."""
    max_attempts: int
    max_pm_decisions: int
    run_timeout: int
    task_budget: float
    cost_per_interaction: float
    pm_budget_topup: float
    risky_patterns: list[str]
    db_rule: str
    kimi_bin: str
    opencode_bin: str
    log_dir: str
    workspace_dir: str
    branch_prefix: str
    max_identical_calls: int


def _effective(settings: Settings, repo: Repository) -> EffectiveSettings:
    """Merge: configurações do repositório sobrescrevem as globais."""
    patterns = list(settings.risky_patterns)
    if repo.risky_patterns_extra:
        try:
            extra = json.loads(repo.risky_patterns_extra)
            if isinstance(extra, list):
                patterns += extra
        except (json.JSONDecodeError, TypeError):
            pass
    return EffectiveSettings(
        max_attempts=repo.max_attempts if repo.max_attempts is not None else settings.max_attempts,
        max_pm_decisions=repo.max_pm_decisions if repo.max_pm_decisions is not None else settings.max_pm_decisions,
        run_timeout=repo.run_timeout if repo.run_timeout is not None else settings.run_timeout,
        task_budget=repo.task_budget if repo.task_budget is not None else settings.task_budget,
        cost_per_interaction=repo.cost_per_interaction if repo.cost_per_interaction is not None else settings.cost_per_interaction,
        pm_budget_topup=settings.pm_budget_topup,
        risky_patterns=patterns,
        db_rule=repo.db_rule or settings.db_rule,
        kimi_bin=settings.kimi_bin,
        opencode_bin=settings.opencode_bin,
        log_dir=settings.log_dir,
        branch_prefix=settings.branch_prefix,
        workspace_dir=settings.workspace_dir,
        max_identical_calls=settings.max_identical_calls,
    )


def recover_stale_steps(session_factory) -> int:
    """No startup do worker: steps `running` e subtarefas `implementing`/`verifying`
    de tasks ativas são órfãos de um restart/crash anterior (worker síncrono e único)
    — volta para `pending` para re-executar, em vez de travar a task para sempre."""
    with session_factory() as s:
        stale = (
            s.query(TaskStep)
            .join(Task)
            .filter(
                TaskStep.status == STEP_RUNNING,
                Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
            )
            .all()
        )
        for st in stale:
            st.status = STEP_PENDING
            st.started_at = None
            _system_event(
                s, st, "worker_recovered",
                {"reason": "step running órfão de restart anterior; re-executando"},
            )

        # Subtarefas órfãs: se o worker caiu durante `_run_one_implement` ou
        # `_run_one_verify`, a subtarefa fica em `implementing`/`verifying` e
        # nunca mais é coletada (run_*_subtasks só pega `pending`/`implemented`).
        stale_subs = (
            s.query(SubTask)
            .join(Task)
            .filter(
                SubTask.status.in_(["implementing", "verifying"]),
                Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
            )
            .all()
        )
        for sub in stale_subs:
            sub.status = "pending"
            sub.started_at = None
            sub.error = "worker reiniciado — subtarefa órfã re-enfileirada"

        s.commit()
        return len(stale) + len(stale_subs)


def _touch_heartbeat(path: str) -> None:
    """Grava timestamp no arquivo de heartbeat (cria se não existir)."""
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass  # silencioso — heartbeat é best-effort


def _task_workspace(settings, repo_id: int, task_id: int) -> str:
    """Diretório de trabalho isolado por tarefa (clone dedicado)."""
    return os.path.join(settings.workspace_dir, str(repo_id), f"task_{task_id}")


def acquire_worker_lock(lock_path: str) -> object | None:
    """Trava de instância única do worker (flock não-bloqueante).

    Retorna o handle do lock se adquirido, ou None se outro worker já está
    rodando. O lock é liberado automaticamente quando o processo morre
    (sem lock órfão); o PID é gravado no arquivo para diagnóstico.
    Usa modo append: não trunca o arquivo antes do flock (senão o segundo
    worker apagaria o PID do primeiro ao tentar adquirir).
    """
    import fcntl

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def worker_loop(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # não depende da API ter subido antes
    migrate_schema(engine)
    session_factory = make_session_factory(engine)
    recovered = recover_stale_steps(session_factory)
    if recovered:
        log.info("worker recuperou %s step(s) running órfão(s) para re-execução", recovered)
    log.info("worker iniciado (dir de trabalho: %s)", settings.workspace_dir)
    hb_path = os.path.join(settings.workspace_dir, "worker.heartbeat")
    while True:
        _touch_heartbeat(hb_path)
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
    """Reivindica o próximo step pendente de uma task ativa (claim atômico).

    Steps com `pause_before` (gate de aprovação humana configurado no pipeline)
    NUNCA são reclamados: a task é marcada como `waiting_approval` e o step fica
    `pending` aguardando o humano aprovar (POST /api/tasks/{id}/approve-step).
    Como a task sai do filtro `queued/in_progress`, a transição acontece uma só vez.
    """
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
        if step.pause_before:
            step.task.status = TASK_WAITING_APPROVAL
            _system_event(
                s, step, "human_gate",
                {
                    "position": step.position,
                    "robot": step.robot.name if step.robot else None,
                    "reason": "aguardando aprovação humana (pause_before no pipeline)",
                },
            )
            s.commit()
            return None
        result = s.execute(
            update(TaskStep)
            .where(
                TaskStep.id == step.id,
                TaskStep.status == STEP_PENDING,
                # Trava de concorrência: nunca reclama outra fase da MESMA task
                # enquanto uma já está running (ex.: segundo worker por engano).
                ~exists().where(
                    TaskStep.task_id == step.task_id,
                    TaskStep.status == STEP_RUNNING,
                    TaskStep.id != step.id,
                ),
            )
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


def _build_handoff(
    s: Session, task: Task, current_step: TaskStep, checkout: str, base: str, branch: str
) -> str:
    """Monta o autoia_handoff.md: histórico COMPLETO das fases + diff + instrução atual.

    Fases anteriores entram com o resumo INTEGRAL (sem truncar) + veredicto; fases
    posteriores que falharam (bounce-back) entram com o relatório completo da falha.
    """
    sections: list[str] = []
    for st in sorted(task.steps, key=lambda x: x.position):
        if st.position == current_step.position:
            continue
        robot_name = st.robot.name if st.robot else "?"
        role = st.robot.role if st.robot else ""
        if st.position < current_step.position:
            head = f"### Fase {st.position} — {robot_name} ({role}) — {st.status}"
            if st.verdict:
                head += f" — veredicto: {st.verdict}"
            body = st.summary or "(sem resumo)"
            if st.diff_stat:
                body += f"\n\n**Alterações:**\n```\n{st.diff_stat}\n```"
            sections.append(f"{head}\n{body}")
        elif st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED) and (st.summary or st.error):
            detail = st.summary or ""
            if st.error:
                detail = f"{st.error}\n{detail}".strip()
            sections.append(
                f"### Fase {st.position} — {robot_name} ({role}) — FALHOU\n"
                f"_Esta fase falhou e o trabalho voltou para você._\n{detail}"
            )
    diff = ""
    try:
        diff = gitops.diff_stat(checkout, base, branch)
    except gitops.GitError:
        pass

    # Inclui progresso de subtarefas no handoff, se existirem
    if task.subtasks:
        sections.append(_subtask_progress_summary(task, post_merge=current_step.post_merge))

    current = (
        f"**Fase {current_step.position} — {current_step.robot.name if current_step.robot else '?'} "
        f"({current_step.robot.role if current_step.robot else '?'})**\n"
        "Ao terminar, documente no seu texto final: o que fez, arquivos alterados, "
        "evidência (comandos/saídas), pendências e instruções para a próxima fase."
    )
    return handoff.build_handoff(
        task_id=task.id,
        task_title=task.title or "",
        task_status=task.status,
        branch=branch,
        phase_sections=sections,
        diff=diff,
        current=current,
        feedback=task.feedback or "",
    )


def _run_executor(
    eff: EffectiveSettings,
    executor: str,
    prompt: str,
    *,
    cwd: str,
    log_path: str,
    model: str | None = None,
    on_event=None,
    kimi_cost_per_interaction: float | None = None,
):
    """Executa a fase com o executor da task: `kimi` (kimi-code) ou `opencode`."""
    if executor == "opencode":
        return opencode_exec.run_opencode(
            prompt,
            cwd=cwd,
            opencode_bin=eff.opencode_bin,
            log_path=log_path,
            timeout=eff.run_timeout,
            max_identical_calls=eff.max_identical_calls,
            risky_patterns=eff.risky_patterns,
            checkout_path=cwd,
            model=model,
            on_event=on_event,
        )
    return kimi_exec.run_kimi(
        prompt,
        cwd=cwd,
        kimi_bin=eff.kimi_bin,
        log_path=log_path,
        timeout=eff.run_timeout,
        max_identical_calls=eff.max_identical_calls,
        risky_patterns=eff.risky_patterns,
        checkout_path=cwd,
        cost_per_interaction=(
            kimi_cost_per_interaction
            if kimi_cost_per_interaction is not None
            else eff.cost_per_interaction
        ),
        on_event=on_event,
    )


def execute_step(settings: Settings, session_factory, step_id: int) -> dict | None:
    """Executa o step. Retorna um gatilho de PM ({task_id, reason}) quando aplicável."""
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task
        repo = task.repository
        base = repo.default_branch
        branch = task.branch or f"{eff.branch_prefix}/task-{task.id}"
        eff = _effective(settings, repo)

        # Workspace isolado por task — cada task tem seu próprio clone, sem conflito
        checkout = _task_workspace(eff, repo.id, task.id)

        # Fonte do clone: URL do repo ou caminho local cadastrado
        source = repo.url or repo.local_path or ""
        if not source:
            step.status = STEP_FAILED
            step.error = "repositório sem URL ou caminho local"
            task.status = TASK_FAILED
            task.error = step.error
            _finish(step)
            s.commit()
            return None

        # Garante que o workspace existe e é um clone git
        git_dir = os.path.join(checkout, ".git")
        if not os.path.isdir(git_dir):
            try:
                gitops.clone(source, checkout)
            except gitops.GitError as exc:
                step.status = STEP_FAILED
                step.error = f"clone falhou: {exc}"
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
        try:
            project.ensure_agents_md(checkout, project_info, eff.db_rule)
        except (OSError, gitops.GitError):
            log.warning(
                "não foi possível escrever AGENTS.md no checkout %s", checkout, exc_info=True
            )
        try:
            handoff.write_handoff(
                checkout, _build_handoff(s, task, step, checkout, base, branch)
            )
            project.exclude_local(checkout, "autoia_screenshots/")
            project.exclude_local(checkout, "autoia_tasks.json")
        except (OSError, gitops.GitError):
            log.warning(
                "não foi possível escrever autoia_handoff.md no checkout %s",
                checkout,
                exc_info=True,
            )
        prompt = prompts.build_prompt(
            step.robot, task, step_context, base, project_info=project_info
        )
        log_path = os.path.join(eff.log_dir, f"step_{step.id}.log")
        step.log_path = str(log_path)
        # Marca o início da tentativa nos eventos: uma fase pode ser re-executada
        # (bounce-back) e os eventos se acumulam no mesmo step — sem esse marcador
        # a UI não consegue separar as tentativas no histórico.
        _system_event(
            s, step, "attempt_started",
            {"attempt": step.attempt, "robot": step.robot.name if step.robot else None},
        )
        # Persiste o prompt da fase (o que o robô pediu) para a visão de chat.
        _system_event(
            s, step, "prompt",
            {"prompt": prompt, "robot": step.robot.name if step.robot else None},
        )
        role = step.robot.role if step.robot else ""
        has_subtasks = bool(task.subtasks)  # força eager load dentro da sessão
        s.commit()

    # ── Ramo de subtarefas: implement e verify iteram sobre subtarefas ──
    if has_subtasks and role == "implement":
        return _decide_subtask_implement(
            eff, session_factory, step_id, checkout, base, branch, project_info
        )
    if has_subtasks and role == "verify":
        return _decide_subtask_verify(
            eff, session_factory, step_id, checkout, base, branch, project_info
        )

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

    outcome = _run_executor(
        eff,
        task.executor,
        prompt,
        cwd=checkout,
        log_path=log_path,
        model=step.robot.model if step.robot else None,
        on_event=on_event,
    )

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        verdict_label = _consume_verdict(s, step, checkout)
        s.commit()
    return _decide(eff, session_factory, step_id, checkout, outcome, verdict_label)


def _consume_verdict(s: Session, step: TaskStep, checkout: str) -> str | None:
    """Lê e remove o veredicto, se o papel do robô exigir. Retorna o rótulo."""
    role = step.robot.role if step.robot else ""
    expected = VERDICT_EXPECTED.get(role)
    if expected is None:
        return None
    raw = verdicts.read_verdict(checkout)
    verdicts.remove_verdict(checkout)
    if role == "review":
        label = verdicts.parse_ready_work(raw)
    else:
        label = verdicts.parse_pass_fail(raw)
    step.verdict = label or (raw or "")[:30] or "AUSENTE"
    if label != expected and raw:
        # Veredicto de correção (NEEDS_WORK/FAIL): preserva o conteúdo COMPLETO
        # no summary — é o que a fase anterior precisa para corrigir (bounce-back)
        # e o que o handoff mostra na seção "FALHOU".
        step.summary = raw
    return label


def _event_count(session_factory, step_id: int) -> int:
    with session_factory() as s:
        return s.query(func.count(RunEvent.id)).filter(RunEvent.step_id == step_id).scalar() or 0


def _scan_artifacts(s: Session, step: TaskStep, checkout: str) -> int:
    """Scaneia `autoia_screenshots/step_<id>/` no checkout e registra arquivos de imagem
    como StepArtifact (idempotente: não duplica pelo filename). Retorna quantos foram
    registrados."""
    screens_dir = os.path.join(checkout, "autoia_screenshots", f"step_{step.id}")
    if not os.path.isdir(screens_dir):
        return 0
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    count = 0
    for fname in sorted(os.listdir(screens_dir)):
        fpath = os.path.join(screens_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.splitext(fname)[1].lower() not in IMAGE_EXTS:
            continue
        existing = (
            s.query(StepArtifact)
            .filter(StepArtifact.step_id == step.id, StepArtifact.filename == fname)
            .first()
        )
        if existing is not None:
            continue
        relpath = os.path.relpath(fpath, checkout)
        s.add(StepArtifact(step_id=step.id, filename=fname, filepath=relpath))
        count += 1
    return count


def _handle_cancelled(s: Session, step: TaskStep, task: Task) -> bool:
    """True se a task foi cancelada durante a execução: marca o step e não avança.

    Sem esse guard, o _decide pós-execução continuaria o pipeline (e até faria
    merge/push) de uma task que o usuário cancelou enquanto a fase rodava.
    """
    if task.status != TASK_CANCELLED:
        return False
    step.status = STEP_PENDING
    step.error = "tarefa cancelada durante a execução"
    _finish(step)
    return True


def _decide(eff: EffectiveSettings, session_factory, step_id: int, checkout: str, outcome, verdict_label: str | None) -> dict | None:
    trigger: dict | None = None
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task
        repo = task.repository
        role = step.robot.role if step.robot else ""
        reason = outcome.abort_reason or ""

        if _handle_cancelled(s, step, task):
            s.commit()
            return None

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
                trigger = _handle_failure(eff, s, step, task, reason, "guardrail_blocked", STEP_GUARDRAIL_BLOCKED)
            else:
                trigger = _handle_failure(eff, s, step, task, reason, "timeout", STEP_FAILED)
            s.commit()
            return trigger

        if outcome.exit_code != 0:
            trigger = _handle_failure(
                eff, s, step, task,
                f"executor ({task.executor}) saiu com código {outcome.exit_code}",
                "exec_exit", STEP_FAILED,
            )
            s.commit()
            return trigger

        # Veredicto de verificação (review/verify) é obrigatório e deve ser o esperado.
        if role in VERDICT_EXPECTED:
            expected = VERDICT_EXPECTED[role]
            if verdict_label != expected:
                trigger = _handle_failure(
                    eff, s, step, task,
                    f"veredicto {verdict_label or 'AUSENTE'} (esperado {expected})",
                    "verdict", STEP_FAILED,
                )
                s.commit()
                return trigger

        # Sucesso: commit local apenas em fases pré-merge.
        if not step.post_merge:
            try:
                committed = gitops.commit_all(checkout, f"autoia: {task.title} (fase {step.position})")
                if committed:
                    try:
                        step.diff_stat = gitops.diff_last_commit(checkout) or ""
                    except gitops.GitError:
                        pass
            except gitops.GitError as exc:
                trigger = _handle_failure(eff, s, step, task, f"commit: {exc}", "git_error", STEP_FAILED)
                s.commit()
                return trigger

        # Registra screenshots e outros arquivos gerados pelo robô no checkout.
        try:
            _scan_artifacts(s, step, checkout)
        except OSError:
            log.warning("não foi possível escanear artifacts no checkout %s", checkout, exc_info=True)

        # Texto final COMPLETO: é a documentação da fase e vira o histórico no handoff
        # da próxima fase (requisito: nunca truncar conteúdo).
        step.summary = outcome.final_text or ""

        # Alerta de diagnóstico: output muito curto em fase de verificação pode indicar
        # que o kimi não processou corretamente a fase (ex.: leu o handoff e reproduziu
        # marcadores em vez de avaliar o deploy real).
        if role in ("verify", "assess") and len(outcome.final_text or "") < 100:
            _system_event(
                s, step, "short_output_warning",
                {
                    "final_text_length": len(outcome.final_text or ""),
                    "interaction_count": outcome.interaction_count,
                },
            )

        # Papel refine (po): grava a história na task.
        if role == "refine":
            description, criteria = verdicts.parse_story(outcome.final_text or "")
            if description:
                task.description = description
            if criteria:
                task.acceptance_criteria = criteria
            # Gera subtarefas a partir do plano de implementação (se o PO gerou)
            if not task.subtasks:
                subtask_data = verdicts.parse_subtasks(outcome.final_text or "")
                for i, sd in enumerate(subtask_data):
                    task.subtasks.append(
                        SubTask(
                            position=i,
                            title=sd["title"],
                            description=sd["description"],
                            acceptance_criteria=sd["acceptance_criteria"],
                        )
                    )
                if subtask_data:
                    _system_event(
                        s, step, "subtasks_generated",
                        {"count": len(subtask_data), "titles": [sd["title"] for sd in subtask_data]},
                    )

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
            _spawn_tasks(session_factory, step_id, checkout)
            return None

        # Pré-merge: a última fase pré-merge (próxima é pós-merge, ou é a última) integra.
        merge_now = nxt is None or nxt.post_merge
        if merge_now:
            try:
                changes = gitops.diff_changes(checkout, repo.default_branch, task.branch)
                metric = arch_metric.compute_arch_metric(changes)
                _system_event(
                    s, step, "arch_metric",
                    {
                        "score": metric.score,
                        "level": metric.level,
                        "reasons": metric.reasons,
                        "arquivos": len(changes),
                    },
                )
            except gitops.GitError:
                log.warning(
                    "não foi possível computar a métrica de arquitetura no checkout %s",
                    checkout,
                    exc_info=True,
                )
            try:
                result = gitops.merge_and_push(checkout, task.branch, repo.default_branch)
            except gitops.GitError as exc:
                trigger = _handle_failure(eff, s, step, task, f"merge/push: {exc}", "merge_error", STEP_FAILED)
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
            _spawn_tasks(session_factory, step_id, checkout)
            return None

        step.status = STEP_DONE
        task.current_step = step.position
        nxt.status = STEP_PENDING
        _system_event(s, step, "phase_done", {"next": nxt.position})
        _finish(step)
        s.commit()
        _spawn_tasks(session_factory, step_id, checkout)
        return None


def _handle_failure(eff: EffectiveSettings, s: Session, step: TaskStep, task: Task, reason: str, kind: str, step_status: str) -> dict | None:
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
    if previous is not None and previous.attempt < eff.max_attempts:
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
# Decisão pós-execução de subtarefas (implement e verify)
# ---------------------------------------------------------------------------


def _decide_subtask_implement(
    eff: EffectiveSettings,
    session_factory,
    step_id: int,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
) -> dict | None:
    """Executa o ciclo implement das subtarefas e decide o próximo passo."""
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task_id = step.task_id
        log_path = step.log_path or os.path.join(eff.log_dir, f"step_{step_id}.log")

    abort_reason = subtask.run_implement_subtasks(
        eff, session_factory, step,
        task_id, checkout, base, branch, project_info, log_path,
    )

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task

        if _handle_cancelled(s, step, task):
            s.commit()
            return None

        if abort_reason:
            # Erro durante a implementação de uma subtarefa
            if "orçamento" in abort_reason:
                _system_event(s, step, "budget_hit", {"reason": abort_reason})
                task.status = TASK_NEEDS_REVIEW
                task.error = abort_reason
                step.status = STEP_PENDING
                step.error = abort_reason
                step.started_at = None
                s.commit()
                return {"task_id": task.id, "reason": abort_reason}

            # Guardrail / timeout / erro de commit
            trigger = _handle_failure(
                eff, s, step, task, abort_reason,
                "guardrail_blocked" if "guardrail" in abort_reason else "error",
                STEP_FAILED,
            )
            s.commit()
            return trigger

        # Todas as subtarefas implementadas → avança para verify
        step.summary = _subtask_progress_summary(task)
        try:
            step.diff_stat = gitops.diff_stat(checkout, base, branch) or ""
        except gitops.GitError:
            pass
        step.status = STEP_DONE
        task.current_step = step.position
        nxt = next(
            (st for st in sorted(task.steps, key=lambda x: x.position)
             if st.position > step.position),
            None,
        )
        if nxt:
            nxt.status = STEP_PENDING
        else:
            task.status = TASK_DONE
        _system_event(s, step, "phase_done", {"next": nxt.position if nxt else None, "subtasks": True})
        _finish(step)
        s.commit()
    _spawn_tasks(session_factory, step_id, checkout)
    return None


def _decide_subtask_verify(
    eff: EffectiveSettings,
    session_factory,
    step_id: int,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
) -> dict | None:
    """Executa o ciclo verify das subtarefas e decide: bounce-back ou avançar."""
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task_id = step.task_id
        log_path = step.log_path or os.path.join(eff.log_dir, f"step_{step_id}.log")

    result = subtask.run_verify_subtasks(
        eff, session_factory, step,
        task_id, checkout, base, branch, project_info, log_path,
    )

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task

        if _handle_cancelled(s, step, task):
            s.commit()
            return None

        if result is None:
            # Todas as subtarefas PASS → avança para o próximo step
            step.summary = _subtask_progress_summary(task)
            step.status = STEP_DONE
            step.verdict = "PASS"
            task.current_step = step.position
            nxt = next(
                (st for st in sorted(task.steps, key=lambda x: x.position)
                 if st.position > step.position),
                None,
            )
            if nxt:
                nxt.status = STEP_PENDING
            else:
                task.status = TASK_DONE
            _system_event(
                s, step, "phase_done",
                {"next": nxt.position if nxt else None, "subtasks": True},
            )
            _finish(step)
            s.commit()
            _spawn_tasks(session_factory, step_id, checkout)
            return None

        if result.startswith("sub:"):
            # Algumas subtarefas falharam → bounce-back para implement
            subs = [
                s for s in sorted(task.subtasks, key=lambda x: x.position)
                if s.status not in ("done",)
            ]
            for st in subs:
                st.status = "pending"
                st.verdict = None
                st.finished_at = None
                if st.attempt >= eff.max_attempts:
                    st.status = "failed"
                    st.error = f"tentativas excedidas ({eff.max_attempts})"

            all_failed = all(s.status == "failed" for s in task.subtasks if s.position in [
                int(p) for p in result.split(":")[1].split(",")
            ])

            if all_failed:
                task.status = TASK_NEEDS_REVIEW
                task.error = f"subtarefas falharam: {result}"
                step.status = STEP_FAILED
                step.error = task.error
                _finish(step)
                s.commit()
                return {"task_id": task.id, "reason": task.error}

            # Bounce-back: volta para o implement step
            previous = next(
                (st for st in sorted(task.steps, key=lambda x: -x.position)
                 if st.position < step.position),
                None,
            )
            if previous is not None:
                previous.status = STEP_PENDING
                previous.attempt += 1
                previous.error = None
                previous.summary = None
                previous.finished_at = None
                task.status = TASK_IN_PROGRESS
                task.current_step = previous.position
                task.error = None
                step.status = STEP_FAILED
                step.error = f"subtarefas reprovadas: {result}"
                _system_event(
                    s, step, "subtask_bounce_back",
                    {"positions": result, "reason": "veredicto FAIL em subtarefas"},
                )
                _finish(step)
                s.commit()
                return None

            task.status = TASK_FAILED
            task.error = f"subtarefas falharam sem fase anterior: {result}"
            step.status = STEP_FAILED
            step.error = task.error
            _finish(step)
            s.commit()
            return {"task_id": task.id, "reason": task.error}

        # Erro genérico (abort, etc.)
        step.status = STEP_FAILED
        step.error = result
        _finish(step)
        task.status = TASK_NEEDS_REVIEW
        task.error = result
        s.commit()
        return {"task_id": task.id, "reason": result}


def _subtask_progress_summary(task: Task, post_merge: bool = False) -> str:
    """Resumo legível do progresso das subtarefas para o handoff.

    Em fases pós-merge, mostra apenas subtarefas com status final (done, failed),
    ocultando intermediárias para não poluir o contexto do deploy-tester.
    """
    lines = ["## Progresso das subtarefas"]
    final_statuses = {"done", "failed"}
    for s in sorted(task.subtasks, key=lambda x: x.position):
        if post_merge and s.status not in final_statuses:
            continue
        status_icon = {
            "done": "[OK]",
            "implemented": "[IMPLEMENTADA]",
            "implementing": "[EM ANDAMENTO]",
            "verifying": "[VERIFICANDO]",
            "pending": "[PENDENTE]",
            "failed": "[FALHOU]",
        }.get(s.status, f"[{s.status.upper()}]")
        line = f"- {status_icon} Subtarefa {s.position + 1}: {s.title}"
        if s.summary:
            line += f" — {s.summary[:200]}"
        if s.verdict:
            line += f" (veredicto: {s.verdict})"
        lines.append(line)
    return "\n".join(lines)


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
            f"{' erro: ' + st.error if st.error else ''}"
        )
    return "\n".join(lines)


def _pm_decide(session_factory, eff: EffectiveSettings, task_id: int, trigger: str) -> None:
    """Roda o PM e aplica a decisão (retry / continuar / escalar). Sempre bound por pm_decisions."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        checkout = _task_workspace(eff, task.repository.id, task.id)
        pm_robot = (
            s.query(Robot)
            .filter(Robot.name == "pm", Robot.repository_id.is_(None))
            .first()
        )
        if pm_robot is None:
            return
        context = _pm_context(s, task)

        # Garante workspace (pode ainda não existir se a task nunca rodou)
        if not os.path.isdir(os.path.join(checkout, ".git")):
            source = task.repository.url or task.repository.local_path or ""
            if source:
                try:
                    gitops.clone(source, checkout)
                except gitops.GitError:
                    log.warning("PM: clone falhou para %s", checkout, exc_info=True)

        project_info = project.detect_project(checkout) if os.path.isdir(checkout) else ""
        if os.path.isdir(checkout):
            try:
                project.ensure_agents_md(checkout, project_info, eff.db_rule)
                last_pos = max((st.position for st in task.steps), default=-1)
                ghost = types.SimpleNamespace(position=last_pos + 1, robot=None)
                handoff.write_handoff(
                    checkout,
                    _build_handoff(
                        s, task, ghost, checkout, task.repository.default_branch, task.branch
                    ),
                )
            except (OSError, gitops.GitError):
                log.warning(
                    "não foi possível escrever AGENTS.md/handoff no checkout %s",
                    checkout,
                    exc_info=True,
                )
        prompt = prompts.build_prompt(
            pm_robot, task, context, task.repository.default_branch, project_info=project_info
        )
        log_path = os.path.join(eff.log_dir, f"pm_task_{task_id}.log")
        task.pm_decisions += 1
        s.commit()

    effective_cwd = checkout if os.path.isdir(checkout) else eff.workspace_dir
    outcome = _run_executor(
        eff,
        task.executor,
        prompt,
        cwd=effective_cwd,
        log_path=log_path,
        model=pm_robot.model if pm_robot else None,
        on_event=None,
        kimi_cost_per_interaction=0.0,
    )

    raw = verdicts.read_verdict(effective_cwd)
    verdicts.remove_verdict(effective_cwd)
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
            if target is not None and target.attempt < eff.max_attempts:
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
            task.budget_limit = (task.budget_limit or 0.0) + eff.pm_budget_topup
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
        eff = _effective(settings, task.repository)
        if task.pm_decisions >= eff.max_pm_decisions:
            anchor = sorted(task.steps, key=lambda x: x.position)[-1] if task.steps else None
            _system_event(
                s, anchor, "pm_skip",
                {"reason": f"limite de decisões ({eff.max_pm_decisions}) atingido"},
            )
            s.commit()
            return
    log.info("PM decidindo para a task %s (%s)", task_id, reason)
    _pm_decide(session_factory, eff, task_id, reason)


def _spawn_tasks(session_factory, step_id: int, checkout: str) -> None:
    """Lê autoia_tasks.json do checkout e cria tasks filhas."""
    tasks_file = os.path.join(checkout, "autoia_tasks.json")
    if not os.path.isfile(tasks_file):
        return
    try:
        with open(tasks_file, encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("autoia_tasks.json inválido em %s", checkout, exc_info=True)
        return

    if not isinstance(entries, list):
        return

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return
        task = step.task
        repo = task.repository

        if not repo.allow_auto_tasks:
            return  # repo não permite spawn automático

        spawned = 0
        for entry in entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            description = entry.get("description", "")
            kind = entry.get("kind", "feature")
            target_repo_name = (entry.get("repository") or "").strip()

            # Determina repositório alvo
            if target_repo_name:
                target_repo = s.query(Repository).filter(Repository.name == target_repo_name).first()
                if target_repo is None:
                    log.warning("repo alvo '%s' não encontrado para spawn", target_repo_name)
                    continue
                if not target_repo.allow_external_tasks:
                    log.warning("repo '%s' não aceita tasks externas", target_repo_name)
                    continue
            else:
                target_repo = repo

            # Pipeline: usa default do repo alvo, ou o mesmo da task pai
            pipeline_id = target_repo.default_pipeline_id or task.pipeline_id

            child = Task(
                repository_id=target_repo.id,
                pipeline_id=pipeline_id,
                title=title,
                description=description,
                kind=kind,
                status="created",
                executor=task.executor,
                budget_limit=target_repo.task_budget if target_repo.task_budget is not None else task.budget_limit,
                parent_task_id=task.id,
            )
            s.add(child)
            s.flush()  # para obter child.id

            # Copia steps do pipeline
            pipeline_steps = (
                s.query(PipelineStep)
                .filter(PipelineStep.pipeline_id == pipeline_id)
                .order_by(PipelineStep.position)
                .all()
            )
            for ps in pipeline_steps:
                child.steps.append(
                    TaskStep(
                        position=ps.position,
                        robot_id=ps.robot_id,
                        post_merge=ps.post_merge,
                        pause_before=ps.pause_before,
                        status="created",
                    )
                )

            # Task fica como "created" — aguardando aprovação humana

            spawned += 1
            log.info("task #%d spawnada de #%d: %s (repo: %s)", child.id, task.id, title, target_repo.name)

        if spawned > 0:
            _system_event(s, step, "task_spawned", {"count": spawned, "titles": [e.get("title") for e in entries[:10]]})
            s.commit()

    # Remove o arquivo após processar
    try:
        os.remove(tasks_file)
    except OSError:
        pass


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
