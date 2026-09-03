"""Worker do modo human-in-the-loop de tasks (`Task.mode == "manual"`).

Roda em processo separado (`autoia-chat-worker`) com lock/heartbeat próprios,
espelhando o `chamado_runner`. Reusa os executores (kimi/opencode), o sandbox e o
gitops do worker de tasks via `runner._effective`/`runner._run_executor`.

Fluxo por ação (`Task.pending_action`):
- `dispatch`      -> roda o robô dispatcher (lê o checkout read-only) que escreve
                     `autoia_dispatch.json`; o worker valida e encaminha a ação
                     (`run_agent:<id>` | `merge` | chat | ask).
- `run_agent:<id>`-> roda o agente escolhido (papel + instrução do humano) na branch
                     da task, commita por fase e devolve o resultado ao humano. Sem
                     merge e sem bounce-back (o humano decide o próximo passo).
- `merge`         -> roda o robô merger (se existir) e faz `merge_and_push`.

As interações viram `TaskMessage` (transcript da task, payload SEMPRE completo) e
cada rodada de agente vira `TaskRun` (histórico/handoff).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import exists, func, update
from sqlalchemy.orm import Session

from .. import budget, prompts, verdicts
from ..config import Settings
from ..db import utcnow
from ..models import (
    CHAT_DISPATCH,
    CHAT_MERGE,
    CHAT_STATUS_IDLE,
    CHAT_STATUS_QUEUED,
    CHAT_STATUS_RUNNING,
    DISPATCH_CHAT,
    DISPATCH_ASK,
    DISPATCH_MERGE,
    DISPATCH_RUN_AGENT,
    STEP_RUNNING,
    TASK_DONE,
    TASK_OPEN,
    TASKRUN_BLOCKED,
    TASKRUN_DONE,
    TASKRUN_FAILED,
    TASKRUN_RUNNING,
    Robot,
    Task,
    TaskMessage,
    TaskRun,
    TaskStep,
)
from . import gitops, handoff, project
from .runner import (
    VERDICT_EXPECTED,
    _effective,
    _effective_model,
    _materialize_skills,
    _repo_context,
    _run_executor,
    _task_workspace,
)

log = logging.getLogger("autoia.chat")

CHAT_HEARTBEAT_FILE = "chat-worker.heartbeat"


def _touch(path: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _heartbeat_loop(path: str, stop, interval: float = 5.0) -> None:
    while not stop.wait(interval):
        _touch(path)


def _append_message(s: Session, task_id: int, kind: str, payload: dict, cost: float = 0.0) -> None:
    max_seq = (
        s.query(func.max(TaskMessage.seq)).filter(TaskMessage.task_id == task_id).scalar() or 0
    )
    s.add(TaskMessage(task_id=task_id, seq=max_seq + 1, kind=kind, payload=payload, cost=cost))


def _msg_count(session_factory, task_id: int) -> int:
    with session_factory() as s:
        return (
            s.query(func.count(TaskMessage.id)).filter(TaskMessage.task_id == task_id).scalar()
            or 0
        )


def recover_stale_chats(session_factory) -> int:
    """Ações de chat `running` órfãs de restart/crash → voltam a idle (o humano
    refaz o pedido). TaskRun `executando` órfã → falhou."""
    with session_factory() as s:
        stale = s.query(Task).filter(Task.chat_status == CHAT_STATUS_RUNNING).all()
        for task in stale:
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(
                s, task.id, "system",
                {"event": "worker_recovered", "reason": "ação órfã cancelada; refaça o pedido"},
            )
        orphan_runs = s.query(TaskRun).filter(TaskRun.status == TASKRUN_RUNNING).all()
        for run in orphan_runs:
            run.status = TASKRUN_FAILED
            run.finished_at = utcnow()
        s.commit()
        return len(stale) + len(orphan_runs)


def claim_next_chat(session_factory) -> tuple[int, str] | None:
    """Reivindica a próxima ação de chat pendente (claim atômico).

    Nunca reclama uma task que tem fase `running` (o auto-worker está nela).
    Retorna `(task_id, action)` ou None.
    """
    running_step = exists().where(
        TaskStep.task_id == Task.id, TaskStep.status == STEP_RUNNING
    )
    with session_factory() as s:
        task = (
            s.query(Task)
            .filter(
                Task.chat_status == CHAT_STATUS_QUEUED,
                Task.pending_action.isnot(None),
                Task.status == TASK_OPEN,
                ~running_step,
            )
            .order_by(Task.id)
            .first()
        )
        if task is None:
            return None
        action = task.pending_action or ""
        result = s.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.chat_status == CHAT_STATUS_QUEUED,
                Task.status == TASK_OPEN,
                ~running_step,
            )
            .values(chat_status=CHAT_STATUS_RUNNING)
        )
        if result.rowcount != 1:
            return None
        s.commit()
        return task.id, action


def chat_worker_loop(settings: Settings, session_factory, workspace_dir: str) -> None:
    log.info("chat-worker iniciado (dir de trabalho: %s)", workspace_dir)
    hb_path = os.path.join(workspace_dir, CHAT_HEARTBEAT_FILE)
    while True:
        _touch(hb_path)
        try:
            claimed = claim_next_chat(session_factory)
        except Exception:
            log.exception("erro no claim de ação de chat")
            time.sleep(2)
            continue
        if claimed is None:
            time.sleep(2)
            continue
        task_id, action = claimed
        log.info("processando chat da task %s (action=%s)", task_id, action)
        stop = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop, args=(hb_path, stop), daemon=True, name="chat-hb"
        )
        hb_thread.start()
        try:
            execute_chat_action(settings, session_factory, task_id, action)
        except Exception:
            log.exception("erro executando ação de chat da task %s", task_id)
            _fail_chat(session_factory, task_id, "erro interno do worker")
        finally:
            stop.set()
            hb_thread.join(timeout=1)


def _fail_chat(session_factory, task_id: int, error: str) -> None:
    """Devolve a task ao humano (open) com um erro, sem ação pendente."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        task.chat_status = CHAT_STATUS_IDLE
        task.pending_action = None
        _append_message(s, task_id, "system", {"event": "action_error", "error": error[:2000]})
        s.commit()


def _prepare_checkout(eff, repo, checkout: str, branch: str, base: str) -> str | None:
    source = repo.url or repo.local_path or ""
    if not source:
        return "repositório sem URL ou caminho local"
    git_dir = os.path.join(checkout, ".git")
    if not os.path.isdir(git_dir):
        try:
            gitops.clone(source, checkout)
        except gitops.GitError as exc:
            return f"clone falhou: {exc}"
    try:
        gitops.ensure_task_branch(checkout, branch, base)
    except gitops.GitError as exc:
        return f"git: {exc}"
    return None


def _build_run_context(task: Task, current_run: TaskRun) -> str:
    lines = [f"## Instrução do humano (esta rodada)\n{current_run.instruction.strip()}"]
    prior = [r for r in task.runs if r.id != current_run.id]
    if prior:
        lines.append("## Rodadas anteriores nesta task")
        for r in prior[-6:]:
            head = f"- {r.robot_name or 'agente'} ({r.robot_role}) [{r.status}]"
            if r.verdict:
                head += f" veredicto {r.verdict}"
            lines.append(head)
    return "\n".join(lines)


def _write_chat_handoff(s: Session, task: Task, current_run: TaskRun, checkout: str, base: str, branch: str) -> None:
    sections: list[str] = []
    for r in task.runs:
        if r.id == current_run.id:
            continue
        head = f"### Agente {r.robot_name or '?'} ({r.robot_role}) — {r.status}"
        if r.verdict:
            head += f" — veredicto: {r.verdict}"
        sections.append(f"{head}\n{r.final_text or '(sem texto final)'}")
    diff = ""
    try:
        diff = gitops.diff_stat(checkout, base, branch)
    except gitops.GitError:
        pass
    current = (
        f"**Agente {current_run.robot_name or '?'} ({current_run.robot_role})**\n"
        f"Instrução do humano: {current_run.instruction}"
    )
    handoff.write_handoff(
        checkout,
        handoff.build_handoff(
            task_id=task.id,
            task_title=task.title or "",
            task_status=task.status,
            branch=branch,
            phase_sections=sections,
            diff=diff,
            current=current,
            feedback=task.feedback or "",
        ),
    )


def execute_chat_action(settings: Settings, session_factory, task_id: int, action: str) -> None:
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        repo = task.repository
        eff = _effective(settings, repo)
        checkout = _task_workspace(eff, repo.id, task.id)
        base = repo.default_branch
        branch = task.branch or f"{eff.branch_prefix}/task-{task.id}"
        err = _prepare_checkout(eff, repo, checkout, branch, base)
        if err:
            _fail_chat(session_factory, task_id, err)
            return

    if action == CHAT_DISPATCH:
        _run_dispatch(settings, eff, session_factory, task_id, checkout, base, branch)
    elif action == CHAT_MERGE:
        _run_merge(settings, eff, session_factory, task_id, checkout, base, branch)
    elif action.startswith("run_agent:"):
        try:
            run_id = int(action[len("run_agent:"):])
        except ValueError:
            _fail_chat(session_factory, task_id, f"ação inválida: {action}")
            return
        _run_agent(settings, eff, session_factory, task_id, run_id, checkout, base, branch)
    else:
        _fail_chat(session_factory, task_id, f"ação desconhecida: {action}")


# ── Dispatcher ──────────────────────────────────────────────────────────────

def _resolve_agent(s: Session, repo_id: int, name_or_role: str) -> Robot | None:
    needle = (name_or_role or "").strip().lower()
    if not needle:
        return None
    robots = (
        s.query(Robot)
        .filter(
            (Robot.repository_id.is_(None)) | (Robot.repository_id == repo_id),
            Robot.active.is_(True),
            Robot.archived.is_(False),
        )
        .all()
    )
    for r in robots:
        if (r.name or "").lower() == needle or (r.role or "").lower() == needle:
            return r
    for r in robots:
        if needle in (r.name or "").lower():
            return r
    return None


def _available_agents(s: Session, repo_id: int) -> list[Robot]:
    return (
        s.query(Robot)
        .filter(
            (Robot.repository_id.is_(None)) | (Robot.repository_id == repo_id),
            Robot.active.is_(True),
            Robot.archived.is_(False),
        )
        .order_by(Robot.name)
        .all()
    )


def _recent_transcript(s: Session, task_id: int, limit: int = 12) -> str:
    msgs = (
        s.query(TaskMessage)
        .filter(TaskMessage.task_id == task_id, TaskMessage.kind.in_(["user", "assistant_text"]))
        .order_by(TaskMessage.seq.desc())
        .limit(limit)
        .all()
    )
    msgs = list(reversed(msgs))
    if not msgs:
        return "Nenhuma interação ainda."
    lines = []
    for m in msgs:
        label = "Usuário" if m.kind == "user" else "Assistente"
        text = m.payload.get("text") or m.payload.get("content") or ""
        lines.append(f"### {label}\n{text}")
    return "\n\n".join(lines)


def _build_dispatch_prompt(s: Session, task: Task, repo_id: int) -> str:
    agents = _available_agents(s, repo_id)
    agent_lines = []
    for a in agents:
        first = next((ln.strip() for ln in (a.mission or "").splitlines() if ln.strip()), "")
        agent_lines.append(f"- {a.name} (role: {a.role}) — {first[:120]}")
    return "\n\n".join(
        [
            prompts.CONTRACT_DISPATCH,
            "## Contexto da task",
            f"Task #{task.id}: {task.title or '(sem título)'}\n{task.description or '—'}\n"
            f"Branch: {task.branch or '?'}\nStatus: {task.status}",
            "## Agentes disponíveis (escolha um destes; nunca invente outro)",
            "\n".join(agent_lines) or "(nenhum agente)",
            "## Transcrição recente da conversa",
            _recent_transcript(s, task.id),
        ]
    )


def _dispatcher_robot(s: Session) -> Robot | None:
    return s.query(Robot).filter(Robot.name == "dispatcher", Robot.repository_id.is_(None)).first()


def _run_dispatch(settings, eff, session_factory, task_id: int, checkout: str, base: str, branch: str) -> None:
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        repo = task.repository
        prompt = _build_dispatch_prompt(s, task, repo.id)
        executor = task.executor
        repo_id = repo.id
        robot = _dispatcher_robot(s)
        model = _effective_model(task, robot)
    log_path = os.path.join(eff.log_dir, f"chat_dispatch_{task_id}.log")
    state = {"seq": _msg_count(session_factory, task_id), "cost": 0.0}

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        state["seq"] += 1
        state["cost"] += cost
        with session_factory() as es:
            t = es.get(Task, task_id)
            if t is None:
                return None
            t.cost_spent = (t.cost_spent or 0.0) + cost
            es.add(TaskMessage(task_id=task_id, seq=state["seq"], kind=kind, payload=payload, cost=cost))
            es.commit()
            if budget.budget_exceeded(t.cost_spent, t.budget_limit):
                return f"orçamento estourado: gasto {t.cost_spent:.2f} >= limite {t.budget_limit:.2f}"
        return None

    outcome = _run_executor(
        eff, executor, prompt,
        cwd=checkout, log_path=log_path, model=model,
        on_event=on_event, repo_id=repo_id, task_id=task_id,
    )
    decision = verdicts.read_dispatch(checkout)
    verdicts.remove_dispatch(checkout)

    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        _append_message(
            s, task_id, "dispatch",
            {"decision": decision, "aborted": outcome.aborted, "exit_code": outcome.exit_code},
        )
        if outcome.aborted or outcome.exit_code != 0:
            _append_message(
                s, task_id, "system",
                {"event": "dispatch_error",
                 "error": outcome.abort_reason or f"dispatcher saiu com {outcome.exit_code}"},
            )
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            s.commit()
            return
        if decision is None:
            _append_message(
                s, task_id, "system",
                {"event": "dispatch_invalid", "error": "dispatcher não emitiu decisão válida"},
            )
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            s.commit()
            return

        action = decision["action"]
        if action == DISPATCH_CHAT:
            _append_message(s, task_id, "assistant_text", {"content": decision["reply"] or ""})
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
        elif action == DISPATCH_ASK:
            _append_message(s, task_id, "assistant_text", {"content": decision["question"] or ""})
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
        elif action == DISPATCH_RUN_AGENT:
            robot = _resolve_agent(s, task.repository_id, decision["agent"] or "")
            if robot is None or not (decision.get("instruction") or "").strip():
                _append_message(
                    s, task_id, "system",
                    {"event": "dispatch_invalid",
                     "error": f"agente desconhecido ou sem instrução: {decision.get('agent')}"},
                )
                task.chat_status = CHAT_STATUS_IDLE
                task.pending_action = None
            else:
                run = TaskRun(
                    task_id=task.id,
                    robot_id=robot.id,
                    robot_name=robot.name,
                    robot_role=robot.role,
                    instruction=decision["instruction"],
                    status=TASKRUN_RUNNING,
                    started_at=utcnow(),
                )
                s.add(run)
                s.flush()
                if decision.get("reply"):
                    _append_message(s, task_id, "assistant_text", {"content": decision["reply"]})
                task.pending_action = f"run_agent:{run.id}"
                task.chat_status = CHAT_STATUS_QUEUED
        elif action == DISPATCH_MERGE:
            if decision.get("reply"):
                _append_message(s, task_id, "assistant_text", {"content": decision["reply"]})
            task.pending_action = CHAT_MERGE
            task.chat_status = CHAT_STATUS_QUEUED
        s.commit()


# ── Agente ──────────────────────────────────────────────────────────────────

def _consume_run_verdict(checkout: str, role: str) -> str | None:
    if role not in VERDICT_EXPECTED:
        return None
    raw = verdicts.read_verdict(checkout)
    verdicts.remove_verdict(checkout)
    if role == "review":
        return verdicts.parse_ready_work(raw)
    return verdicts.parse_pass_fail(raw)


def _run_agent(settings, eff, session_factory, task_id: int, run_id: int, checkout: str, base: str, branch: str) -> None:
    with session_factory() as s:
        run = s.get(TaskRun, run_id)
        if run is None or run.task_id != task_id:
            _fail_chat(session_factory, task_id, "rodada de agente não encontrada")
            return
        task = run.task
        repo = task.repository
        robot = s.get(Robot, run.robot_id) if run.robot_id else None
        if robot is None:
            run.status = TASKRUN_FAILED
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "run_error", "error": "agente não encontrado"})
            s.commit()
            return

        project_info = project.detect_project(checkout)
        repo_context = _repo_context(repo)
        try:
            project.ensure_agents_md(checkout, project_info, eff.db_rule, repo_context)
        except (OSError, gitops.GitError):
            log.warning("não foi possível escrever AGENTS.md em %s", checkout, exc_info=True)
        skills_dir, skills_info = _materialize_skills(s, repo, checkout, settings.skills_dir)
        step_context = _build_run_context(task, run)
        _write_chat_handoff(s, task, run, checkout, base, branch)
        try:
            project.exclude_local(checkout, "autoia_dispatch.json")
            project.exclude_local(checkout, "autoia_screenshots/")
            project.exclude_local(checkout, "autoia_tasks.json")
        except OSError:
            pass
        prompt = prompts.build_prompt(
            robot, task, step_context, base,
            project_info=project_info, skills_info=skills_info, repo_context=repo_context,
        )
        executor = task.executor
        repo_id = repo.id
        model = _effective_model(task, robot)
    log_path = os.path.join(eff.log_dir, f"chat_agent_{task_id}_{run_id}.log")
    state = {"seq": _msg_count(session_factory, task_id), "cost": 0.0}

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        state["seq"] += 1
        state["cost"] += cost
        with session_factory() as es:
            t = es.get(Task, task_id)
            if t is None:
                return None
            t.cost_spent = (t.cost_spent or 0.0) + cost
            es.add(TaskMessage(task_id=task_id, seq=state["seq"], kind=kind, payload=payload, cost=cost))
            es.commit()
            if budget.budget_exceeded(t.cost_spent, t.budget_limit):
                return f"orçamento estourado: gasto {t.cost_spent:.2f} >= limite {t.budget_limit:.2f}"
        return None

    outcome = _run_executor(
        eff, executor, prompt,
        cwd=checkout, log_path=log_path, model=model,
        on_event=on_event, repo_id=repo_id, task_id=task_id, skills_dir=skills_dir,
    )

    with session_factory() as s:
        run = s.get(TaskRun, run_id)
        if run is None:
            return
        task = run.task
        verdict_label = _consume_run_verdict(checkout, run.robot_role)

        block = verdicts.read_block(checkout)
        verdicts.remove_block(checkout)
        decision = verdicts.read_decision(checkout)
        verdicts.remove_decision(checkout)

        if block:
            run.status = TASKRUN_BLOCKED
            run.final_text = outcome.final_text or ""
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {
                "event": "blocked",
                "reason_type": block["reason_type"],
                "reason": block["reason"],
                "question": block["question"],
            })
            s.commit()
            return

        if decision:
            run.status = TASKRUN_BLOCKED
            run.final_text = outcome.final_text or ""
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {
                "event": "decision_request",
                "question": decision["question"],
                "options": decision["options"],
                "context": decision["context"],
            })
            s.commit()
            return

        if outcome.aborted or outcome.exit_code != 0:
            reason = outcome.abort_reason or f"executor saiu com {outcome.exit_code}"
            run.status = TASKRUN_FAILED
            run.final_text = outcome.final_text or ""
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "run_error", "error": reason})
            s.commit()
            return

        # Veredicto de verificação (review/verify/assess): obrigatório. No modo
        # manual, reprovação NÃO faz bounce-back — vira resultado no chat.
        if run.robot_role in VERDICT_EXPECTED and verdict_label != VERDICT_EXPECTED[run.robot_role]:
            run.verdict = verdict_label or "AUSENTE"
            run.status = TASKRUN_FAILED
            run.final_text = outcome.final_text or ""
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {
                "event": "verdict",
                "verdict": run.verdict,
                "expected": VERDICT_EXPECTED[run.robot_role],
                "reason": "veredicto não aprovou esta rodada (sem bounce-back no modo manual)",
            })
            s.commit()
            return

        # Sucesso: commit por fase (sem merge) + texto final integral.
        try:
            message = f"autoia: {task.title} (agente {run.robot_name})"
            committed = gitops.commit_all(checkout, message)
            if committed:
                try:
                    run.diff_stat = gitops.diff_last_commit(checkout) or ""
                except gitops.GitError:
                    pass
        except gitops.GitError as exc:
            run.status = TASKRUN_FAILED
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "git_error", "error": f"commit: {exc}"})
            s.commit()
            return

        if run.robot_role in VERDICT_EXPECTED:
            run.verdict = verdict_label
        run.status = TASKRUN_DONE
        run.final_text = outcome.final_text or ""
        run.cost = state["cost"]
        run.finished_at = utcnow()
        task.chat_status = CHAT_STATUS_IDLE
        task.pending_action = None
        _append_message(s, task_id, "system", {
            "event": "run_done", "robot": run.robot_name, "verdict": run.verdict,
        })
        s.commit()


def _run_merge(settings, eff, session_factory, task_id: int, checkout: str, base: str, branch: str) -> None:
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        repo = task.repository
        merger = _resolve_agent(s, repo.id, "merge")
        run = TaskRun(
            task_id=task.id,
            robot_id=merger.id if merger else None,
            robot_name=merger.name if merger else "merger",
            robot_role="merge",
            instruction="integrar a branch na default (merge + push)",
            status=TASKRUN_RUNNING,
            started_at=utcnow(),
        )
        s.add(run)
        s.flush()
        run_id = run.id
        s.commit()

    # Se há robô merger, roda-o primeiro (prepara a branch e commita). Depois o
    # worker faz o merge_and_push de fato (mesmo fluxo da última fase pré-merge).
    if merger is not None:
        _run_agent(settings, eff, session_factory, task_id, run_id, checkout, base, branch)
        with session_factory() as s:
            run = s.get(TaskRun, run_id)
            if run is None or run.status != TASKRUN_DONE:
                return  # _run_agent já devolveu a task ao humano com o erro

    with session_factory() as s:
        run = s.get(TaskRun, run_id)
        if run is None:
            return
        task = run.task
        repo = task.repository
        try:
            result = gitops.merge_and_push(checkout, branch, base)
        except gitops.GitError as exc:
            run.status = TASKRUN_FAILED
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "merge_error", "error": str(exc)})
            s.commit()
            return
        if result.ok:
            run.status = TASKRUN_DONE
            run.final_text = result.detail
            run.finished_at = utcnow()
            task.status = TASK_DONE
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "merged", "detail": result.detail})
        else:
            run.status = TASKRUN_FAILED
            run.final_text = result.detail
            run.finished_at = utcnow()
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _append_message(s, task_id, "system", {"event": "merge_failed", "detail": result.detail})
        s.commit()
