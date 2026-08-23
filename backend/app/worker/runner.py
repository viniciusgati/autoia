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
import re
import shutil
import threading
import time
import types
from dataclasses import dataclass, field

from sqlalchemy import exists, func, update
from sqlalchemy.orm import Session

from .. import budget, prompts, verdicts
from ..config import Settings
from ..db import Base, make_engine, make_session_factory, migrate_schema, utcnow
from ..models import (
    CHAT_STATUS_IDLE,
    STEP_BLOCKED,
    STEP_DONE,
    STEP_FAILED,
    STEP_GUARDRAIL_BLOCKED,
    STEP_MODE_MANUAL,
    STEP_PENDING,
    STEP_RUNNING,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_IN_PROGRESS,
    TASK_MODE_MANUAL,
    TASK_NEEDS_REVIEW,
    TASK_OPEN,
    TASK_QUEUED,
    TASK_WAITING_APPROVAL,
    Pipeline,
    PipelineStep,
    Repository,
    RepositorySkill,
    Robot,
    RunEvent,
    StepArtifact,
    StepMission,
    StepSummary,
    SubTask,
    Task,
    TaskProposal,
    TaskStep,
)
from . import (
    arch_metric,
    exec_common,
    gitops,
    handoff,
    kimi_exec,
    opencode_exec,
    project,
    sandbox as sandbox_mod,
    subtask,
)
from .sandbox import SandboxConfig

log = logging.getLogger("autoia.worker")

# Prefixo dos arquivos de sinalização de parada de projeto (API → worker):
# `workspace_dir/.stop-<repo_id>` — ver `process_stop_files`.
STOP_FILE_PREFIX = ".stop-"
# Prefixo dos arquivos de parada de UMA task: `workspace_dir/.stop-task-<task_id>`.
TASK_STOP_FILE_PREFIX = ".stop-task-"

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
    whitelisted_hosts: list[str]
    db_rule: str
    kimi_bin: str
    opencode_bin: str
    opencode_model: str
    log_dir: str
    workspace_dir: str
    branch_prefix: str
    max_identical_calls: int
    no_progress_timeout: int
    sandbox: sandbox_mod.SandboxConfig


def _sandbox_config(settings: Settings, repo: Repository) -> sandbox_mod.SandboxConfig:
    """Configuração efetiva do sandbox para o projeto (repo > global).

    `host_services_base` vira `http://host.docker.internal` no modo `full` (o
    contêiner alcança o host pelo host-gateway) e `http://127.0.0.1` caso contrário
    (rede host/loopback direto). O mesmo valor é injetado no ambiente da execução
    como `AUTOIA_HOST_SERVICES_BASE` para os robôs usarem.
    """
    mode = sandbox_mod.normalize_mode(repo.sandbox or settings.sandbox)
    base = "http://host.docker.internal" if mode == sandbox_mod.SANDBOX_FULL else "http://127.0.0.1"
    return sandbox_mod.SandboxConfig(
        mode=mode,
        image=settings.sandbox_image,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        tmpfs_size=settings.sandbox_tmpfs_size,
        read_only=settings.sandbox_read_only,
        init=settings.sandbox_init,
        proxy_port=settings.sandbox_proxy_port,
        home=settings.sandbox_home,
        fail_closed=settings.sandbox_fail_closed,
        host_services_base=base,
    )


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
        whitelisted_hosts=list(settings.whitelisted_hosts),
        db_rule=repo.db_rule or settings.db_rule,
        kimi_bin=settings.kimi_bin,
        opencode_bin=settings.opencode_bin,
        opencode_model=settings.opencode_model,
        log_dir=settings.log_dir,
        branch_prefix=settings.branch_prefix,
        workspace_dir=settings.workspace_dir,
        max_identical_calls=settings.max_identical_calls,
        no_progress_timeout=settings.no_progress_timeout,
        sandbox=_sandbox_config(settings, repo),
    )


def _effective_step_mode(step: TaskStep, task: Task) -> str:
    """Modo efetivo de execução de uma fase: o modo manual da task prevalece;
    senão, o da própria fase (None = herda = auto)."""
    if task.mode == TASK_MODE_MANUAL:
        return STEP_MODE_MANUAL
    return step.execution_mode or "auto"


def _step_goal(step: TaskStep, task: Task) -> str:
    """Objetivo legível da fase ("O que será feito") — derivado de forma
    determinística da mission do robô + título da task, sem LLM."""
    mission = (step.robot.mission if step.robot else "") or ""
    first = next((s.strip() for s in re.split(r"[.\n]", mission) if s.strip()), "")
    if len(first) > 180:
        first = first[:177].rstrip() + "..."
    title = (task.title or "").strip()
    if title and first:
        return f"{title} — {first}"
    return title or first or f"Fase {step.position}"


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
                TaskStep.archived.is_(False),
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


def process_stop_files(workspace_dir: str) -> int:
    """Processa os arquivos de parada gravados pela API:
    - `.stop-<repo_id>` (projeto excluído): mata os subprocessos do projeto e remove;
    - `.stop-task-<task_id>` (task pausada/instruída pelo usuário): o executor da
      fase observa o arquivo e se mata; aqui apenas o arquivo é removido.

    Canal de parada cooperativa entre API e worker (processos separados). Retorna
    quantos arquivos foram processados.
    """
    try:
        names = os.listdir(workspace_dir)
    except OSError:
        return 0
    count = 0
    for name in names:
        path = os.path.join(workspace_dir, name)
        if name.startswith(STOP_FILE_PREFIX):
            try:
                repo_id = int(name[len(STOP_FILE_PREFIX) :])
            except ValueError:
                continue
            exec_common.kill_repo_procs(repo_id)
            try:
                os.remove(path)
            except OSError:
                pass
            count += 1
        elif name.startswith(TASK_STOP_FILE_PREFIX):
            try:
                os.remove(path)
            except OSError:
                pass
            count += 1
    return count


def _repo_context(repo: Repository) -> str:
    """Seção de contexto de integração do projeto (repos-alvo + informações úteis
    como DNS/URLs) injetada no prompt e no AGENTS.md dos robôs."""
    return project.build_repo_context(list(repo.task_targets or []), repo.external_context)


def _active_steps(task: Task) -> list:
    """Steps ATIVOS da task (ordena por posição), ignorando os arquivados por uma
    mudança de pipeline (`change-pipeline`). Os arquivados preservam o histórico
    (RunEvent) mas não participam mais da execução nem da UI."""
    return sorted(
        (st for st in task.steps if not st.archived),
        key=lambda x: x.position,
    )


def _task_workspace(settings, repo_id: int, task_id: int) -> str:
    """Diretório de trabalho isolado por tarefa (clone dedicado)."""
    return os.path.join(settings.workspace_dir, str(repo_id), f"task_{task_id}")


def acquire_worker_lock(lock_path: str, shared: bool = False) -> object | None:
    """Trava de worker (flock). Retorna o handle se adquirido, ou None se negado.

    - `shared=False` (default): lock EXCLUSIVO — instância única. Um segundo
      `autoia-worker` se recusa a iniciar.
    - `shared=True`: lock COMPARTILHADO — usada pelos N processos de um worker
      multi-processo (`--workers N`): vários workers coexistem, mas um worker
      avulso (`--workers 1`, exclusivo) é recusado enquanto houver workers
      compartilhados (e vice-versa).

    O lock é liberado automaticamente quando o processo morre; o PID é gravado no
    arquivo para diagnóstico. Usa modo append: não trunca o arquivo antes do flock
    (senão o segundo worker apagaria o PID do primeiro ao tentar adquirir).
    """
    import fcntl

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        fcntl.flock(handle, flag | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}{' (shared)' if shared else ''}")
    handle.flush()
    return handle


def _heartbeat_loop(path: str, stop, interval: float = 5.0, workspace_dir: str | None = None) -> None:
    """Toca o heartbeat periodicamente enquanto o worker executa uma fase.

    O worker é síncrono: durante a execução do kimi/opencode (que pode levar
    dezenas de minutos) o loop principal fica bloqueado no subprocess e o
    heartbeat estaria parado — a UI reportaria "worker offline" (alive = age < 15).
    Uma thread daemon mantém o arquivo fresco até a fase terminar.

    Com `workspace_dir`, a thread também processa os sinais de parada de projetos
    excluídos (`.stop-<repo_id>`) a cada ciclo — o loop principal está bloqueado
    no subprocess durante a fase, e é esta thread que faz o kill cooperativo.
    """
    while not stop.wait(interval):
        _touch_heartbeat(path)
        if workspace_dir:
            process_stop_files(workspace_dir)


def worker_loop(settings: Settings, session_factory, workspace_dir: str) -> None:
    log.info("worker iniciado (dir de trabalho: %s)", workspace_dir)
    hb_path = os.path.join(workspace_dir, "worker.heartbeat")
    while True:
        _touch_heartbeat(hb_path)
        try:
            process_stop_files(workspace_dir)
        except Exception:
            log.exception("erro ao processar sinais de parada de projetos")
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
        # Heartbeat contínuo durante a fase (e o PM): o loop bloqueia no subprocess.
        import threading

        stop = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(hb_path, stop),
            kwargs={"workspace_dir": workspace_dir},
            daemon=True,
            name="heartbeat",
        )
        hb_thread.start()
        try:
            trigger = execute_step(settings, session_factory, step_id)
            if trigger:
                _maybe_pm(session_factory, settings, trigger["task_id"], trigger["reason"])
            # Resumo automático (se o repo tiver auto_summary) a cada fase decidida.
            _maybe_auto_summary(settings, session_factory, step_id)
            # Resumo "O que foi entregue" por fase (LLM de resumo dedicada).
            _maybe_step_summary(settings, session_factory, step_id)
        except Exception:
            log.exception("erro executando step %s", step_id)
            _fail_step_hard(session_factory, step_id)
        finally:
            stop.set()
            hb_thread.join(timeout=1)


def claim_next(session_factory) -> int | None:
    """Reivindica o próximo step pendente de uma task ativa (claim atômico).

    Steps com `pause_before` (gate de aprovação humana configurado no pipeline)
    NUNCA são reclamados: a task é marcada como `waiting_approval` e o step fica
    `pending` aguardando o humano aprovar (POST /api/tasks/{id}/approve-step).
    Como a task sai do filtro `queued/in_progress`, a transição acontece uma só vez.
    """
    with session_factory() as s:
        # Exclui steps de tasks que JÁ têm uma fase running (uma task só executa
        # uma fase por vez). Sem isso, com o primeiro candidato sendo o próximo
        # step da task em execução, o claim travaria e nunca chegaria às outras
        # tasks pendentes (bug de paralelismo com `--workers N`).
        running_task_ids = (
            s.query(TaskStep.task_id)
            .filter(TaskStep.status == STEP_RUNNING)
            .scalar_subquery()
        )
        step = (
            s.query(TaskStep)
            .join(Task)
            .filter(
                TaskStep.status == STEP_PENDING,
                TaskStep.archived.is_(False),
                Task.status.in_([TASK_QUEUED, TASK_IN_PROGRESS]),
                TaskStep.task_id.not_in(running_task_ids),
                # Modo human-in-the-loop: o auto-worker não reclama fases de tasks
                # em modo manual, nem fases marcadas como manuais (o chat-worker
                # dirige via dispatcher).
                Task.mode != TASK_MODE_MANUAL,
                (TaskStep.execution_mode.is_(None)) | (TaskStep.execution_mode != STEP_MODE_MANUAL),
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
            .values(
                status=STEP_RUNNING,
                started_at=utcnow(),
                # Snapshot do responsável da task no momento do claim.
                responsible_id=step.task.responsible_id,
            )
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
    # Quem "concluiu" a fase: o responsável snapshotado no claim (o worker executa
    # em nome do dono da task). Nunca sobrescreve uma conclusão já registrada.
    if step.finished_by_id is None:
        step.finished_by_id = step.responsible_id


def _tool_call_target(arguments) -> str:
    """Extrai o alvo (arquivo/comando/query) de argumentos de tool call (kimi/opencode)."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "path", "pattern", "query", "url", "file", "skill", "agent", "description"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _step_prior_activity(s: Session, step: TaskStep, limit: int = 40) -> str:
    """Atividade das tentativas ANTERIORES da MESMA fase, para retomada pós-abort.

    Quando uma execução morre (timeout/stall), a conversa do LLM se perde — o novo
    kimi começa do zero. Esta seção reconstrói (determinístico, sem LLM), a partir
    dos RunEvent anteriores ao `attempt_started` atual, o que a fase já fez nesta
    tentativa: tool calls + textos + guardrails. Na primeira execução, retorna vazio.
    """
    if not isinstance(step, TaskStep):
        return ""
    events = (
        s.query(RunEvent)
        .filter(RunEvent.step_id == step.id)
        .order_by(RunEvent.seq)
        .all()
    )
    start = 0
    for i, ev in enumerate(events):
        if ev.kind == "attempt_started":
            start = i
            break
    if not events[start:]:
        return ""
    lines: list[str] = []
    for ev in events[start:][-limit:]:
        p = ev.payload or {}
        if ev.kind == "tool_call":
            tc = p.get("tool_call") or {}
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name") if isinstance(fn, dict) else (p.get("tool") or "?")
            args = fn.get("arguments") if isinstance(fn, dict) else p.get("input")
            target = _tool_call_target(args)
            lines.append(f"- {name}: {target}" if target else f"- {name}")
        elif ev.kind == "assistant_text":
            text = str(p.get("content") or "")
            first = next((l for l in text.splitlines() if l.strip()), "")
            if first:
                lines.append(f"> {first[:180]}")
        elif ev.kind == "guardrail_blocked":
            lines.append(f"! guardrail bloqueou: {p.get('detail') or p.get('pattern') or ''}")
    return "\n".join(lines)


def _should_resume(s: Session, step: TaskStep) -> str | None:
    """Session_id do kimi a retomar se a fase foi INTERROMPIDA (timeout/stall) sem
    concluir — para continuar a MESMA conversa (contexto preservado).

    Se a última execução concluiu (`phase_done`/`merged`) ou encerrou com um
    VEREDICTO (`verdict`, inclusive FAIL), retorna None: re-execução recomeça do
    zero — retomar uma conversa que já concluiu um veredicto só reproduz a
    conclusão antiga (stale verdict), nunca uma reavaliação.
    """
    if not step.session_id:
        return None
    completed = False
    in_run = False
    for ev in (
        s.query(RunEvent)
        .filter(RunEvent.step_id == step.id)
        .order_by(RunEvent.seq)
        .all()
    ):
        if ev.kind == "attempt_started":
            in_run = True
            completed = False
        elif in_run and ev.kind in ("phase_done", "merged", "verdict"):
            completed = True
    return None if completed else step.session_id


def _resume_prompt(step: TaskStep, task: Task) -> str:
    return (
        "CONTINUAÇÃO — a execução anterior desta fase foi interrompida antes de concluir "
        "e você está retomando a MESMA sessão (o contexto das suas ações anteriores está "
        "preservado aqui).\n\n"
        "1. Leia o autoia_handoff.md na raiz (pode ter sido atualizado com a atividade da "
        "execução anterior e o estado do checkout).\n"
        "2. Retome EXATAMENTE de onde parou: confira o que já foi feito "
        f"(git status/diff) e continue o trabalho desta fase ('{task.title}') até concluir.\n"
        "3. Ao terminar, produza o texto final/documentação da fase como instruído no "
        "prompt original (O que foi feito / Arquivos alterados / Evidência / Pendências / "
        "Para a próxima fase).\n\n"
        "IMPORTANTE: o código/checkout PODE ter mudado desde a interrupção (outras fases "
        "ou tentativas podem ter agido no meio). Reconcilie com o estado ATUAL (git log, "
        "git status, diff) antes de continuar. Se esta fase já havia escrito um veredicto "
        "(autoia_verdict.txt), ele foi CONSUMIDO pelo sistema e perdeu a validade: "
        "reavalie/reverifique o estado atual ANTES de escrever um novo — nunca republicar "
        "o veredicto anterior sem re-verificar o código atual."
    )


def _compact_phase_line(
    position: int,
    robot_name: str,
    status: str,
    verdict: str | None,
    summary: str,
    max_chars: int = 200,
) -> str:
    """Uma linha determinística de uma fase ANTIGA no contexto do prompt (sem LLM).

    Formato: `Fase {pos} ({robô}) [{status}] — veredicto: {v} — {trecho}`. O trecho
    é a 1ª linha não vazia do resumo com whitespace colapsado, truncado
    DINAMICAMENTE para a linha total respeitar `max_chars` (trecho ≤ 160 chars no
    máximo — prefixos de robôs do seed chegam a ~57 chars, então 160 fixos
    estourariam o total). Resumo vazio → `(sem resumo)`; veredicto ausente → `—`.
    Puro: não toca no banco (`TaskStep.summary` permanece integral).
    """
    prefix = f"Fase {position} ({robot_name}) [{status}] — veredicto: {verdict or '—'} — "
    excerpt = ""
    for line in summary.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            excerpt = collapsed
            break
    if not excerpt:
        excerpt = "(sem resumo)"
    excerpt_limit = max(0, min(160, max_chars - len(prefix)))
    if len(excerpt) > excerpt_limit:
        excerpt = excerpt[:excerpt_limit]
    return prefix + excerpt


def _build_step_context(
    s: Session,
    task: Task,
    current_step: TaskStep,
    checkout: str,
    base: str,
    branch: str,
    recent_phases: int = 1,
) -> str:
    parts = []
    prev_positions = sorted(
        (st.position for st in _active_steps(task) if st.position < current_step.position),
        reverse=True,
    )
    # Janela recente: as N posições anteriores mais próximas (em ordem de pipeline).
    recent_positions = set(prev_positions[:recent_phases]) if recent_phases > 0 else set()
    for st in _active_steps(task):
        if st.position == current_step.position:
            continue
        robot_name = st.robot.name if st.robot else "?"
        if st.position < current_step.position:
            if st.position in recent_positions:
                if st.summary:
                    # janela recente: resumo INTEGRAL (o robô precisa do contexto imediato)
                    parts.append(f"Fase {st.position} ({robot_name}): {st.summary}")
            else:
                # fases antigas: 1 linha determinística (compactação de leitura;
                # o banco continua com o resumo completo)
                parts.append(
                    _compact_phase_line(
                        st.position, robot_name, st.status, st.verdict, st.summary or ""
                    )
                )
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
    prior = _step_prior_activity(s, current_step)
    if prior:
        context += (
            f"\n\n## Atividade da execução anterior desta fase (tentativas anteriores)\n"
            f"Uma execução anterior desta MESMA fase foi interrompida. O que já foi feito "
            f"nela (continue a partir daqui, sem refazer):\n{prior}"
        )
    return context


def _build_handoff(
    s: Session, task: Task, current_step: TaskStep, checkout: str, base: str, branch: str
) -> str:
    """Monta o autoia_handoff.md: histórico COMPLETO das fases + diff + instrução atual.

    Fases anteriores entram com o resumo INTEGRAL (sem truncar) + veredicto; fases
    posteriores que falharam (bounce-back) entram com o relatório completo da falha.
    """
    sections: list[str] = []
    for st in _active_steps(task):
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

    # Atividade das tentativas anteriores da MESMA fase (retomada pós-abort)
    prior = _step_prior_activity(s, current_step)
    if prior:
        sections.append(
            f"## Atividade da execução anterior desta fase (tentativas anteriores)\n"
            "_Uma execução anterior desta MESMA fase foi interrompida (ex.: timeout). "
            "O que já foi feito nela — continue a partir daqui, sem refazer:_\n\n"
            f"{prior}"
        )

    current = (
        f"**Fase {current_step.position} — {current_step.robot.name if current_step.robot else '?'} "
        f"({current_step.robot.role if current_step.robot else '?'})**\n"
        "Ao terminar, documente no seu texto final: o que fez, arquivos alterados, "
        "evidência (comandos/saídas), pendências e instruções para a próxima fase."
    )
    # Detalhes adicionados pelo usuário + instrução de retomada entram no handoff
    # como seções próprias (diferenciadas da solicitação original).
    if task.details:
        sections.append(
            f"## Detalhes adicionados pelo usuário (contexto da implementação)\n{task.details}"
        )
    if task.resume_instruction:
        sections.append(
            f"## Intervenção do usuário (retomada)\n{task.resume_instruction}\n\n"
            "_A execução foi bloqueada e o usuário forneceu esta instrução para continuar "
            "exatamente de onde parou._"
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


def _materialize_skills(
    s: Session, repo: Repository, checkout: str, skills_base: str
) -> tuple[str | None, str]:
    """Materializa as skills do projeto no checkout dos robôs.

    Copia `data/skills/<repo_id>/<skill_id>/` (upload do usuário) para
    `.autoia/skills/<nome>/` e `.opencode/skills/<nome>/` no checkout — sempre
    que houver skills, independente do executor (custo zero e determinístico).
    `.autoia/` e `.opencode/` entram no `.git/info/exclude` via
    `project.exclude_local` para nunca serem versionados pelos robôs.

    Retorna `(skills_dir, skills_info)`:
    - `skills_dir`: `<checkout>/.autoia/skills` (o `--skills-dir` do kimi) ou
      None quando o repo não tem skills;
    - `skills_info`: seção `## Skills do projeto disponíveis` (`nome — descrição`)
      injetada no prompt (fallback determinístico da UI/auditoria).

    Best-effort: falha de cópia não derruba a fase (a execução segue sem skills).
    """
    rows = (
        s.query(RepositorySkill)
        .filter(RepositorySkill.repository_id == repo.id)
        .order_by(RepositorySkill.id)
        .all()
    )
    if not rows:
        return None, ""
    for skill in rows:
        source = os.path.join(skills_base, str(repo.id), str(skill.id))
        if not os.path.isdir(source):
            log.warning(
                "skills: diretório %s não existe no disco; ignorando skill %s",
                source, skill.name,
            )
            continue
        for prefix in (".autoia", ".opencode"):
            dest = os.path.join(checkout, prefix, "skills", skill.name)
            try:
                shutil.copytree(source, dest, dirs_exist_ok=True)
            except OSError:
                log.warning(
                    "skills: falha ao copiar %s para %s", source, dest, exc_info=True
                )
    try:
        project.exclude_local(checkout, ".autoia/")
        project.exclude_local(checkout, ".opencode/")
    except OSError:
        pass
    info_lines = ["## Skills do projeto disponíveis"]
    for skill in rows:
        info_lines.append(
            f"- {skill.name} — {skill.description}"
            if skill.description
            else f"- {skill.name}"
        )
    # `--skills-dir` só é anunciado ao kimi quando a pasta existe de fato
    # (falha de cópia → None; a seção do prompt segue como fallback).
    skills_dir = os.path.join(checkout, ".autoia", "skills")
    return (skills_dir if os.path.isdir(skills_dir) else None), "\n".join(info_lines)


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
    resume_session_id: str | None = None,
    repo_id: int | None = None,
    task_id: int | None = None,
    skills_dir: str | None = None,
):
    """Executa a fase com o executor da task: `kimi` (kimi-code) ou `opencode`.

    `repo_id` identifica o projeto no registro de subprocessos ativos e alimenta o
    watchdog de parada cooperativa (`stop_file`): se a API excluir o projeto
    enquanto o robô roda, o processo é morto.

    O sandbox vem de `eff.sandbox`: com modo ligado, o comando roda dentro de um
    contêiner (mesma árvore do checkout). No modo `full`, garante o proxy de egress
    allowlist no host e libera o host do remote git do projeto (fetch para resolver
    merge sem quebrar). Falha do sandbox (docker indisponível) → falha a execução
    se `fail_closed`, senão cai para execução direta com aviso no log.

    O push do checkout é bloqueado durante toda a execução (`lock_push`/`unlock_push`)
    — defesa em profundidade: nem a CLI nem o robô conseguem fazer push, mesmo com
    a rede liberada. Restaurado no finally (funciona para qualquer executor, PM,
    missões e resumos em background).
    """
    stop_file = (
        exec_common.repo_stop_path(eff.workspace_dir, repo_id)
        if repo_id is not None
        else None
    )
    task_stop_file = (
        exec_common.task_stop_path(eff.workspace_dir, task_id)
        if task_id is not None
        else None
    )
    sandbox = eff.sandbox
    extra_env = {"AUTOIA_HOST_SERVICES_BASE": sandbox.host_services_base, "AUTOIA_SANDBOX": sandbox.mode}
    # Resultado da varredura de segredos dos mounts ([] = limpo).
    sandbox_scan: list[str] = []
    if sandbox.enabled:
        # Modo full: proxy de egress + liberar o host do remote git (fetch p/ merge).
        if sandbox.mode == sandbox_mod.SANDBOX_FULL:
            sandbox_mod.ensure_egress_proxy(sandbox.proxy_port, eff.whitelisted_hosts)
            try:
                remote_url = gitops.run_git(cwd, "remote", "get-url", "origin", check=False).stdout.strip()
                if "://" in remote_url:
                    host = remote_url.split("://")[1].split("/")[0]
                elif remote_url.startswith(("git@", "ssh://")):
                    host = remote_url.split("@")[-1].split(":")[0]
                else:
                    host = ""
                if host:
                    sandbox_mod.add_proxy_hosts([host])
            except gitops.GitError:
                pass
        if not (sandbox_mod.docker_available() and sandbox_mod.docker_image_available(sandbox.image)):
            if sandbox.fail_closed:
                # Fail-closed: sem sandbox não há execução (meta: falha do sandbox
                # → falha da execução, nunca dano).
                outcome = exec_common.ExecOutcome()
                outcome.aborted = True
                outcome.abort_reason = (
                    f"sandbox {sandbox.mode} obrigatório mas docker/imagem "
                    f"{sandbox.image} indisponíveis (fail-closed)"
                )
                outcome.sandbox_mode = sandbox.mode
                return outcome
            log.warning(
                "sandbox %s solicitado mas docker/imagem %s indisponível — executando sem "
                "isolamento (fallback transitório; AUTOIA_SANDBOX_FAIL_CLOSED=1 para falhar)",
                sandbox.mode, sandbox.image,
            )
            sandbox = SandboxConfig(mode=sandbox_mod.SANDBOX_OFF)
        else:
            # Varredura de segredos dos mounts EFETIVOS (chaves SSH, credenciais…):
            # avisa sempre; com fail_closed, aborta a execução se algo sensível
            # entrou como mount (regressão do builder é pega na hora).
            scan_cli = eff.opencode_bin if executor == "opencode" else eff.kimi_bin
            sandbox_scan = sandbox_mod.scan_secret_mounts(sandbox, cwd, eff.workspace_dir, scan_cli)
            if sandbox_scan:
                log.warning(
                    "sandbox: varredura de segredos encontrou mounts expostos: %s",
                    sandbox_scan,
                )
                if sandbox.fail_closed:
                    outcome = exec_common.ExecOutcome()
                    outcome.aborted = True
                    outcome.abort_reason = (
                        f"secrets_scan: mounts expõem segredos do host: {sandbox_scan}"
                    )
                    outcome.sandbox_mode = sandbox.mode
                    outcome.sandbox_scan = sandbox_scan
                    return outcome
    try:
        gitops.lock_push(cwd)
    except gitops.GitError:
        log.warning("não foi possível bloquear push no checkout %s", cwd, exc_info=True)
    try:
        if executor == "opencode":
            outcome = opencode_exec.run_opencode(
                prompt,
                cwd=cwd,
                opencode_bin=eff.opencode_bin,
                log_path=log_path,
                timeout=eff.run_timeout,
                max_identical_calls=eff.max_identical_calls,
                risky_patterns=eff.risky_patterns,
                checkout_path=cwd,
                whitelisted_hosts=eff.whitelisted_hosts,
                model=model or eff.opencode_model,
                no_progress_timeout=eff.no_progress_timeout,
                repo_id=repo_id,
                stop_file=stop_file,
                task_stop_file=task_stop_file,
                sandbox=sandbox,
                workspace_dir=eff.workspace_dir,
                extra_env=extra_env,
                on_event=on_event,
            )
        else:
            outcome = kimi_exec.run_kimi(
                prompt,
                cwd=cwd,
                kimi_bin=eff.kimi_bin,
                log_path=log_path,
                timeout=eff.run_timeout,
                max_identical_calls=eff.max_identical_calls,
                risky_patterns=eff.risky_patterns,
                checkout_path=cwd,
                whitelisted_hosts=eff.whitelisted_hosts,
                cost_per_interaction=(
                    kimi_cost_per_interaction
                    if kimi_cost_per_interaction is not None
                    else eff.cost_per_interaction
                ),
                no_progress_timeout=eff.no_progress_timeout,
                resume_session_id=resume_session_id,
                repo_id=repo_id,
                stop_file=stop_file,
                task_stop_file=task_stop_file,
                skills_dir=skills_dir,
                sandbox=sandbox,
                workspace_dir=eff.workspace_dir,
                extra_env=extra_env,
                on_event=on_event,
            )
        if sandbox_scan and not outcome.sandbox_scan:
            outcome.sandbox_scan = sandbox_scan
        return outcome
    finally:
        try:
            gitops.unlock_push(cwd)
        except gitops.GitError:
            log.warning("não foi possível liberar push no checkout %s", cwd, exc_info=True)


def execute_step(settings: Settings, session_factory, step_id: int) -> dict | None:
    """Executa o step. Retorna um gatilho de PM ({task_id, reason}) quando aplicável."""
    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        task = step.task
        repo = task.repository
        base = repo.default_branch
        eff = _effective(settings, repo)
        branch = task.branch or f"{eff.branch_prefix}/task-{task.id}"

        # Workspace isolado por task — cada task tem seu próprio clone, sem conflito
        checkout = _task_workspace(eff, repo.id, task.id)

        # Remove um possível stop file de task obsoleto (escrito pela API ao
        # pausar/instruir): uma execução NOVA desta task não pode se auto-matar.
        try:
            stale_stop = exec_common.task_stop_path(eff.workspace_dir, task.id)
            if os.path.isfile(stale_stop):
                os.remove(stale_stop)
        except OSError:
            pass

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

        # Workflow ADVPL: antes de cada execução do DESENVOLVEDOR, atualiza a branch do
        # remoto (pull) — o robô vê o estado mais recente e entende o que mudou (ex.:
        # push de uma execução anterior da mesma fase após re-clone/reset do checkout).
        if (
            not step.post_merge
            and (step.robot.role if step.robot else "") == "implement"
            and gitops.is_advpl_robot(step.robot.name if step.robot else None)
        ):
            try:
                gitops.pull_branch(checkout, branch)
            except gitops.GitError as exc:
                log.warning("não foi possível fazer pull da branch %s: %s", branch, exc)

        step_context = _build_step_context(
            s, task, step, checkout, base, branch,
            recent_phases=settings.step_context_recent_phases,
        )
        project_info = project.detect_project(checkout)
        repo_context = _repo_context(repo)
        try:
            project.ensure_agents_md(checkout, project_info, eff.db_rule, repo_context)
        except (OSError, gitops.GitError):
            log.warning(
                "não foi possível escrever AGENTS.md no checkout %s", checkout, exc_info=True
            )
        # Skills do projeto materializadas no checkout (`.autoia/skills/` +
        # `.opencode/skills/`) e seção do prompt; sem skills → nada muda.
        skills_dir, skills_info = _materialize_skills(
            s, repo, checkout, settings.skills_dir
        )
        try:
            handoff.write_handoff(
                checkout, _build_handoff(s, task, step, checkout, base, branch)
            )
            project.exclude_local(checkout, "autoia_screenshots/")
            project.exclude_local(checkout, "autoia_tasks.json")
            project.exclude_local(checkout, "autoia_step_mission.json")
        except (OSError, gitops.GitError):
            log.warning(
                "não foi possível escrever autoia_handoff.md no checkout %s",
                checkout,
                exc_info=True,
            )
        resume_session_id = _should_resume(s, step)
        if resume_session_id:
            # Retoma a MESMA conversa do kimi (contexto preservado) — sem o prompt
            # original inteiro de novo (já está na sessão).
            prompt = _resume_prompt(step, task)
        else:
            prompt = prompts.build_prompt(
                step.robot, task, step_context, base,
                project_info=project_info, skills_info=skills_info, repo_context=repo_context,
            )
        log_path = os.path.join(eff.log_dir, f"step_{step.id}.log")
        step.log_path = str(log_path)
        # Número real da execução desta fase (run): o contador de `attempt_started`
        # para este step + 1. Único por execução (bounce-back não o repete), é a
        # chave da missão humana por ocorrência — igual ao `run` da timeline.
        run = (
            s.query(func.count(RunEvent.id))
            .filter(RunEvent.step_id == step.id, RunEvent.kind == "attempt_started")
            .scalar()
            or 0
        ) + 1
        # Marca o início da tentativa nos eventos: uma fase pode ser re-executada
        # (bounce-back) e os eventos se acumulam no mesmo step — sem esse marcador
        # a UI não consegue separar as tentativas no histórico.
        _system_event(
            s, step, "attempt_started",
            {"attempt": step.attempt, "run": run, "robot": step.robot.name if step.robot else None},
        )
        # Persiste o prompt da fase (o que o robô pediu) para a visão de chat.
        _system_event(
            s, step, "prompt",
            {"prompt": prompt, "robot": step.robot.name if step.robot else None},
        )
        role = step.robot.role if step.robot else ""
        has_subtasks = bool(task.subtasks)  # força eager load dentro da sessão
        if not step.goal:
            step.goal = _step_goal(step, task)
        s.commit()

    # Missão humana desta execução (LLM dedicada, custo zero) em background — a UI
    # mostra o fallback determinístico enquanto ela não fica pronta.
    _maybe_step_mission(settings, session_factory, step_id, run)

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

    run_started = time.monotonic()
    outcome = _run_executor(
        eff,
        task.executor,
        prompt,
        cwd=checkout,
        log_path=log_path,
        model=step.robot.model if step.robot else None,
        on_event=on_event,
        resume_session_id=resume_session_id,
        repo_id=repo.id,
        task_id=task.id,
        skills_dir=skills_dir,
    )

    with session_factory() as s:
        step = s.get(TaskStep, step_id)
        if step is None:
            return None
        # Captura a sessão do kimi p/ retomar a mesma conversa se a fase for
        # re-executada após interrupção (timeout/stall).
        if outcome.session_id:
            step.session_id = outcome.session_id
        # Observabilidade do sandbox desta execução (modo, contêiner, overhead de
        # startup medido como tempo até a primeira linha de saída quando possível).
        _system_event(
            s, step, "sandbox",
            {
                "mode": outcome.sandbox_mode or eff.sandbox.mode,
                "container_id": outcome.container_id,
                "wall_ms": round((time.monotonic() - run_started) * 1000),
                "secrets": outcome.sandbox_scan or [],
            },
        )
        if outcome.sandbox_scan:
            # Varredura de segredos: mounts expõem paths sensíveis (aviso/auditoria;
            # com fail_closed a fase teria abortado antes de rodar).
            _system_event(
                s, step, "secrets_scan",
                {"mounts": outcome.sandbox_scan, "mode": outcome.sandbox_mode},
            )
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
    cited = verdicts.parse_head_hash(raw)
    if cited:
        try:
            current = gitops.head_short(checkout)
        except gitops.GitError:
            current = None
        if current and not current.startswith(cited) and not cited.startswith(current):
            # Veredicto escrito contra uma árvore ANTIGA (o código mudou depois que o
            # robô avaliou) — diagnóstico na timeline; a decisão continua sendo do
            # fluxo normal (não falha a fase sozinho).
            _system_event(
                s, step, "stale_verdict_warning",
                {
                    "verdict_head": cited,
                    "checkout_head": current,
                    "verdict": label,
                },
            )
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


def _consume_block_declaration(s: Session, step: TaskStep, checkout: str) -> dict | None:
    """Lê (e remove) a declaração de bloqueio do agente (autoia_blocked.json)."""
    data = verdicts.read_block(checkout)
    if data is None:
        return None
    verdicts.remove_block(checkout)
    return data


def _consume_decision(s: Session, step: TaskStep, checkout: str) -> dict | None:
    """Lê (e remove) o pedido de decisão do agente (autoia_decision.json).

    Difere do bloqueio: o agente PAROU para perguntar algo ao usuário — a resposta
    destrava a continuidade. Tratado como um `blocked` com reason_type específico
    para a UI renderizar a pergunta e as opções.
    """
    data = verdicts.read_decision(checkout)
    if data is None:
        return None
    verdicts.remove_decision(checkout)
    return data


def _mark_decision(s: Session, step: TaskStep, task: Task, data: dict) -> None:
    """Marca a fase como `blocked` aguardando DECISÃO do usuário (autoia_decision.json).

    A task fica parada com a pergunta + opções visíveis na UI; o usuário responde
    pelo campo de instrução do workspace e a execução retoma da MESMA fase.
    """
    task.status = TASK_BLOCKED
    task.error = data.get("question") or "agente aguardando decisão do usuário"
    task.block_reason_type = "decision_request"
    task.block_reason = data.get("question") or "agente aguardando decisão"
    task.block_question = data.get("context") or ""
    task.block_options = data.get("options") or []
    step.status = STEP_BLOCKED
    step.error = task.error
    _finish(step)
    payload = {
        "reason_type": "decision_request",
        "reason": task.block_reason,
        "question": task.block_reason,
        "options": task.block_options,
    }
    _system_event(s, step, "task_blocked", payload)


def _mark_blocked(s: Session, step: TaskStep, task: Task, data: dict) -> None:
    """Marca a fase e a task como `blocked` aguardando instrução do usuário.

    O agente declarou que não consegue continuar sozinho (ambiguidade, decisão,
    dependência, permissão...). Isso NÃO é falha: a task fica parada até o usuário
    fornecer uma instrução e retomar (POST /api/tasks/{id}/blocked/continue).
    """
    task.status = TASK_BLOCKED
    task.error = data.get("reason")
    task.block_reason_type = data.get("reason_type")
    task.block_reason = data.get("reason")
    task.block_question = data.get("question")
    step.status = STEP_BLOCKED
    step.error = data.get("reason")
    _finish(step)
    _system_event(s, step, "task_blocked", data)


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

        # Entrega ao usuário: se a fase deixou de estar `running` durante a
        # execução (pause/rewind/instrução pela API — o robô foi morto pelo stop
        # file da task), o worker NÃO decide por ela: a ação do usuário manda.
        if step.status != STEP_RUNNING:
            s.commit()
            return None

        # O agente declarou bloqueio aguardando instrução do usuário (autoia_blocked.json).
        block = _consume_block_declaration(s, step, checkout)
        if block:
            _mark_blocked(s, step, task, block)
            s.commit()
            return None

        # O agente pediu uma decisão ao usuário (autoia_decision.json).
        decision = _consume_decision(s, step, checkout)
        if decision:
            _mark_decision(s, step, task, decision)
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
                advpl = gitops.is_advpl_robot(step.robot.name if step.robot else None)
                if advpl:
                    message = gitops.advpl_commit_message(
                        task.executor,
                        step.goal or task.title or f"fase {step.position}",
                    )
                else:
                    message = f"autoia: {task.title} (fase {step.position})"
                committed = gitops.commit_all(checkout, message)
                if committed:
                    try:
                        step.diff_stat = gitops.diff_last_commit(checkout) or ""
                    except gitops.GitError:
                        pass
                # Workflow ADVPL: publica a branch no remoto a cada fase de
                # desenvolvimento (o robô pode retomar/continuar o trabalho).
                if advpl:
                    gitops.push_branch(checkout, task.branch)
            except gitops.GitError as exc:
                trigger = _handle_failure(eff, s, step, task, f"commit/push: {exc}", "git_error", STEP_FAILED)
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

        steps = _active_steps(task)
        nxt = next((st for st in steps if st.position > step.position), None)

        # Modo human-in-the-loop: se a PRÓXIMA fase é manual (ou a task está em modo
        # manual no fim da pipeline), o auto-worker entrega o controle ao humano em
        # vez de avançar/integrar sozinho. O merge também deixa de ser automático
        # (fica sob demanda via dispatcher).
        nxt_manual = nxt is not None and _effective_step_mode(nxt, task) == STEP_MODE_MANUAL
        end_manual = nxt is None and task.mode == TASK_MODE_MANUAL
        if nxt_manual or end_manual:
            step.status = STEP_DONE
            task.current_step = step.position
            task.status = TASK_OPEN
            task.chat_status = CHAT_STATUS_IDLE
            task.pending_action = None
            _system_event(s, step, "pipeline_opened", {"next": nxt.position if nxt else None})
            _finish(step)
            s.commit()
            _spawn_tasks(session_factory, step_id, checkout)
            return None

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
    payload: dict = {"reason": reason}
    if kind == "verdict" and step.summary:
        # Em reprovação de revisão/verificação, `step.summary` guarda o conteúdo
        # COMPLETO do autoia_verdict.txt (a reprovação com os pontos) — persiste no
        # evento para a timeline mostrar o MOTIVO real da falha por ocorrência.
        payload["detail"] = step.summary
    _system_event(s, step, kind, payload)
    step.status = step_status
    step.error = reason
    if kind == "verdict":
        # A fase CONCLUIU (escreveu um veredicto): a próxima re-execução deve
        # começar do zero — retomar a sessão antiga só repete o veredicto antigo
        # sem reavaliar (stale verdict). Complementa a checagem em _should_resume.
        step.session_id = None
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
            for st in reversed(_active_steps(task))
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

    abort_reason, free_summary = subtask.run_implement_subtasks(
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

        # Entrega ao usuário: fase resetada (pause/rewind/instrução) durante a
        # execução das subtarefas — o worker não decide por ela.
        if step.status != STEP_RUNNING:
            s.commit()
            return None

        block = _consume_block_declaration(s, step, checkout)
        if block:
            _mark_blocked(s, step, task, block)
            s.commit()
            return None

        if abort_reason:
            # Re-declaração de "já implementada" sem alterar código após falha na
            # verificação: falha a task na hora (não queima mais ciclos de tester).
            if abort_reason.startswith("subtask_done_rejected:"):
                _system_event(s, step, "subtask_done_rejected", {"reason": abort_reason})
                task.status = TASK_NEEDS_REVIEW
                task.error = abort_reason
                step.status = STEP_FAILED
                step.error = abort_reason
                _finish(step)
                s.commit()
                return {"task_id": task.id, "reason": task.error}

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
        # Execução livre (correção sem subtarefa pendente): o texto final do robô
        # vira o resumo da fase — o avaliador precisa ver o que foi corrigido.
        step.summary = free_summary or _subtask_progress_summary(task)
        try:
            step.diff_stat = gitops.diff_stat(checkout, base, branch) or ""
        except gitops.GitError:
            pass
        step.status = STEP_DONE
        task.current_step = step.position
        nxt = next(
            (st for st in _active_steps(task)
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

        # Entrega ao usuário: fase resetada (pause/rewind/instrução) durante a
        # execução das subtarefas — o worker não decide por ela.
        if step.status != STEP_RUNNING:
            s.commit()
            return None

        block = _consume_block_declaration(s, step, checkout)
        if block:
            _mark_blocked(s, step, task, block)
            s.commit()
            return None

        if result is None:
            # Todas as subtarefas PASS → avança para o próximo step
            step.summary = _subtask_progress_summary(task)
            step.status = STEP_DONE
            step.verdict = "PASS"
            task.current_step = step.position
            nxt = next(
                (st for st in _active_steps(task)
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
                (st for st in reversed(_active_steps(task))
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
    for st in _active_steps(task):
        robot_name = st.robot.name if st.robot else "?"
        lines.append(
            f"Fase {st.position} ({robot_name}) [{st.status}] tentativa {st.attempt}"
            f"{' veredicto ' + st.verdict if st.verdict else ''}"
            f"{' erro: ' + st.error if st.error else ''}"
        )
    return "\n".join(lines)


def _pm_decide(session_factory, settings: Settings, task_id: int, trigger: str) -> None:
    """Roda o PM e aplica a decisão (retry / continuar / escalar). Sempre bound por pm_decisions."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        eff = _effective(settings, task.repository)
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
        repo_context = _repo_context(task.repository)
        skills_dir = None
        skills_info = ""
        if os.path.isdir(checkout):
            try:
                project.ensure_agents_md(checkout, project_info, eff.db_rule, repo_context)
                # Skills do projeto materializadas no checkout do PM também: a
                # decisão recebe a seção `## Skills do projeto disponíveis`.
                skills_dir, skills_info = _materialize_skills(
                    s, task.repository, checkout, settings.skills_dir
                )
                last_pos = max((st.position for st in _active_steps(task)), default=-1)
                # `post_merge` é lido por _build_handoff (progresso das subtarefas)
                # mesmo para o "step fantasma" do PM.
                ghost = types.SimpleNamespace(position=last_pos + 1, robot=None, post_merge=False)
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
            pm_robot, task, context, task.repository.default_branch,
            project_info=project_info, skills_info=skills_info, repo_context=repo_context,
        )
        log_path = os.path.join(eff.log_dir, f"pm_task_{task_id}.log")
        task.pm_decisions += 1
        s.commit()

    effective_cwd = checkout if os.path.isdir(checkout) else eff.workspace_dir
    # Heartbeat contínuo durante a decisão do PM (pode demorar como qualquer fase).
    import threading

    hb_path = os.path.join(eff.workspace_dir, "worker.heartbeat")
    _touch_heartbeat(hb_path)
    stop = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(hb_path, stop),
        kwargs={"workspace_dir": eff.workspace_dir},
        daemon=True,
        name="pm-heartbeat",
    )
    hb_thread.start()
    try:
        outcome = _run_executor(
            eff,
            task.executor,
            prompt,
            cwd=effective_cwd,
            log_path=log_path,
            model=pm_robot.model if pm_robot else None,
            on_event=None,
            kimi_cost_per_interaction=0.0,
            repo_id=task.repository_id,
            task_id=task_id,
            skills_dir=skills_dir,
        )
    finally:
        stop.set()
        hb_thread.join(timeout=1)

    raw = verdicts.read_verdict(effective_cwd)
    verdicts.remove_verdict(effective_cwd)
    decision = verdicts.parse_pm_decision(raw)
    decision["reason"] = decision["reason"] or outcome.final_text[:300]

    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        anchor = _active_steps(task)[-1]
        _system_event(s, anchor, "pm_decision", {"trigger": trigger, **decision})

        if decision["action"] == verdicts.PM_RETRY:
            target = None
            if decision.get("position") is not None:
                target = next((st for st in _active_steps(task) if st.position == decision["position"]), None)
            if target is None:
                target = next(
                    (st for st in _active_steps(task) if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
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
            pending = next((st for st in _active_steps(task) if st.status == STEP_PENDING), None)
            if pending is None:
                failed = next(
                    (st for st in _active_steps(task) if st.status in (STEP_FAILED, STEP_GUARDRAIL_BLOCKED)),
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

        # Resumo automático após a decisão do PM (estado final/decisão mudou).
        if task.repository.auto_summary:
            _spawn_auto_summary(settings, session_factory, task_id, eff)


# Tasks cujo resumo automático já está sendo gerado (evita sobrepor gerações).
_SUMMARY_IN_FLIGHT: set[int] = set()
_SUMMARY_LOCK = threading.Lock()

# Fases cujo resumo ("O que foi entregue") já está sendo gerado.
_STEP_SUMMARY_LOCK = threading.Lock()

# Execuções de fase cuja missão (LLM) já está sendo gerada.
_STEP_MISSION_LOCK = threading.Lock()
_STEP_MISSION_IN_FLIGHT: set[tuple[int, int]] = set()


def _maybe_auto_summary(settings: Settings, session_factory, step_id: int) -> None:
    """Gera o resumo automaticamente após a decisão de uma fase, se o repo tiver
    `auto_summary` ligado — a cada avanço de fase e nos estados finais/decisão.

    Roda em thread daemon (com heartbeat próprio): não bloqueia o worker nem faz a
    UI reportar "worker offline" durante a geração.
    """
    try:
        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None:
                return
            task = step.task
            if not task.repository.auto_summary:
                return
            task_id = task.id
            eff = _effective(settings, task.repository)
    except Exception:
        log.exception("auto-resumo: falha ao avaliar o step %s", step_id)
        return

    _spawn_auto_summary(settings, session_factory, task_id, eff)


def _spawn_auto_summary(settings, session_factory, task_id: int, eff: EffectiveSettings) -> None:
    """Dispara a geração do resumo em background (deduplicada por task em andamento)."""
    with _SUMMARY_LOCK:
        if task_id in _SUMMARY_IN_FLIGHT:
            return
        _SUMMARY_IN_FLIGHT.add(task_id)

    def _run() -> None:
        try:
            from .summarizer import summarize_task

            hb_path = os.path.join(eff.workspace_dir, "worker.heartbeat")
            stop = threading.Event()
            hb = threading.Thread(
                target=_heartbeat_loop, args=(hb_path, stop),
                daemon=True, name="summary-heartbeat",
            )
            hb.start()
            try:
                summarize_task(settings, session_factory, task_id)
            finally:
                stop.set()
                hb.join(timeout=1)
        except Exception:
            log.exception("auto-resumo falhou para a task %s", task_id)
        finally:
            with _SUMMARY_LOCK:
                _SUMMARY_IN_FLIGHT.discard(task_id)

    threading.Thread(target=_run, daemon=True, name=f"auto-summary-{task_id}").start()


# Fases cujo resumo ("O que foi entregue") já está em geração (evita sobrepor).
_STEP_SUMMARY_IN_FLIGHT: set[tuple[int, int]] = set()


def _maybe_step_summary(settings: Settings, session_factory, step_id: int) -> None:
    """Gera o resumo da fase ("O que foi entregue") via LLM de resumo.

    Roda em thread daemon com heartbeat próprio e NUNCA falha o pipeline. A geração
    é por (step, attempt): re-execuções têm resumos independentes, preservando o
    histórico imutável da timeline do workspace. Fases concluídas geram o resumo do
    que foi feito; fases com falha geram a explicação humana da falha.
    """
    if not settings.step_summary:
        return
    try:
        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None or step.status not in (
                STEP_DONE, STEP_FAILED, STEP_GUARDRAIL_BLOCKED,
            ):
                return
            key = (step.id, step.attempt)
            existing = (
                s.query(StepSummary)
                .filter(
                    StepSummary.step_id == step.id,
                    StepSummary.attempt == step.attempt,
                )
                .first()
            )
            if existing is not None:
                return
            eff = _effective(settings, step.task.repository)
    except Exception:
        log.exception("resumo de fase: falha ao avaliar o step %s", step_id)
        return

    with _STEP_SUMMARY_LOCK:
        if key in _STEP_SUMMARY_IN_FLIGHT:
            return
        _STEP_SUMMARY_IN_FLIGHT.add(key)

    def _run() -> None:
        try:
            from .step_summarizer import summarize_step

            hb_path = os.path.join(eff.workspace_dir, "worker.heartbeat")
            stop = threading.Event()
            hb = threading.Thread(
                target=_heartbeat_loop, args=(hb_path, stop),
                daemon=True, name="step-summary-heartbeat",
            )
            hb.start()
            try:
                summarize_step(settings, session_factory, step_id)
            finally:
                stop.set()
                hb.join(timeout=1)
        except Exception:
            log.exception("resumo de fase falhou para o step %s", step_id)
        finally:
            with _STEP_SUMMARY_LOCK:
                _STEP_SUMMARY_IN_FLIGHT.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"step-summary-{step_id}").start()


def _maybe_step_mission(settings: Settings, session_factory, step_id: int, run: int) -> None:
    """Gera a missão humana desta execução de fase ("por que esta execução existe").

    Roda em thread daemon com heartbeat próprio e NUNCA bloqueia nem falha a fase.
    Chaveada por (step, run): cada execução real tem a sua missão, mesmo quando o
    `attempt` se repete. Enquanto a missão LLM não fica pronta (ou se falhar), a UI
    mostra o fallback determinístico derivado dos eventos.
    """
    if not settings.step_mission:
        return
    try:
        with session_factory() as s:
            step = s.get(TaskStep, step_id)
            if step is None:
                return
            existing = (
                s.query(StepMission)
                .filter(StepMission.step_id == step.id, StepMission.run == run)
                .first()
            )
            if existing is not None:
                return
            eff = _effective(settings, step.task.repository)
    except Exception:
        log.exception("missão: falha ao avaliar o step %s", step_id)
        return

    key = (step_id, run)
    with _STEP_MISSION_LOCK:
        if key in _STEP_MISSION_IN_FLIGHT:
            return
        _STEP_MISSION_IN_FLIGHT.add(key)

    def _run() -> None:
        try:
            from .step_mission import generate_mission

            hb_path = os.path.join(eff.workspace_dir, "worker.heartbeat")
            stop = threading.Event()
            hb = threading.Thread(
                target=_heartbeat_loop, args=(hb_path, stop),
                daemon=True, name="step-mission-heartbeat",
            )
            hb.start()
            try:
                generate_mission(settings, session_factory, step_id, run)
            finally:
                stop.set()
                hb.join(timeout=1)
        except Exception:
            log.exception("missão falhou para o step %s run %s", step_id, run)
        finally:
            with _STEP_MISSION_LOCK:
                _STEP_MISSION_IN_FLIGHT.discard(key)

    threading.Thread(target=_run, daemon=True, name=f"step-mission-{step_id}-{run}").start()


def _maybe_pm(session_factory, settings: Settings, task_id: int, reason: str) -> None:
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return
        eff = _effective(settings, task.repository)
        if task.pm_decisions >= eff.max_pm_decisions:
            anchor = _active_steps(task)[-1] if _active_steps(task) else None
            _system_event(
                s, anchor, "pm_skip",
                {"reason": f"limite de decisões ({eff.max_pm_decisions}) atingido"},
            )
            s.commit()
            return
    log.info("PM decidindo para a task %s (%s)", task_id, reason)
    _pm_decide(session_factory, settings, task_id, reason)


def _spawn_tasks(session_factory, step_id: int, checkout: str) -> None:
    """Lê autoia_tasks.json do checkout e grava propostas de tasks filhas.

    As propostas ficam `pending` aguardando APROVAÇÃO HUMANA — o worker NUNCA cria
    a task automaticamente (allow_auto_tasks é obsoleto e ignorado). Dedup por
    `task_id + title`: re-execuções da fase não duplicam a proposta."""
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
        # Exclusividade: task com subtarefas não pode gerar propostas (autoia_tasks.json).
        # As subtarefas já dividem o trabalho na MESMA branch; propostas criariam tasks
        # com branches paralelas no mesmo repo → sobreposição de arquivos e conflito de merge.
        if task.subtasks:
            _system_event(
                s, step, "task_spawn_blocked",
                {"reason": "task já tem subtarefas",
                 "titles": [e.get("title") for e in entries if (e.get("title") or "").strip()][:10]},
            )
            s.commit()
            try:
                os.remove(tasks_file)
            except OSError:
                pass
            return
        existing_titles = {
            p.title for p in s.query(TaskProposal).filter(TaskProposal.task_id == task.id).all()
        }

        added = 0
        for entry in entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            if title in existing_titles:
                continue  # re-execução da fase: não duplica a proposta

            description = entry.get("description", "")
            kind = entry.get("kind", "feature")
            target_repo_name = (entry.get("repository") or "").strip()
            target_repo_id: int | None = None
            if target_repo_name:
                # Allowlist de saída do projeto: `task_targets` vazio = restritivo
                # (propostas só para o próprio projeto). Proposta cross-repo fora
                # da lista é ignorada com evento de auditoria.
                allowed_targets = [t for t in (task.repository.task_targets or []) if t.strip()]
                if target_repo_name not in allowed_targets:
                    _system_event(
                        s, step, "task_spawn_blocked",
                        {
                            "reason": "repo alvo fora da allowlist (task_targets)",
                            "target": target_repo_name,
                            "allowed": allowed_targets,
                            "title": title,
                        },
                    )
                    continue
                target_repo = s.query(Repository).filter(Repository.name == target_repo_name).first()
                if target_repo is None:
                    log.warning("repo alvo '%s' não encontrado para spawn", target_repo_name)
                    continue
                # A validação de `allow_external_tasks` acontece no ACCEPT (decisão
                # humana); aqui a proposta é sempre gravada para o humano decidir.
                target_repo_id = target_repo.id

            s.add(
                TaskProposal(
                    task_id=task.id,
                    step_id=step.id,
                    position=len(task.proposals) + added,
                    title=title,
                    description=description,
                    kind=kind,
                    target_repository_id=target_repo_id,
                    status="pending",
                )
            )
            existing_titles.add(title)
            added += 1

        if added > 0:
            titles = [e.get("title") for e in entries if (e.get("title") or "").strip()]
            _system_event(s, step, "task_spawned", {"count": added, "titles": titles[:10]})
        s.commit()

    # Remove o arquivo após processar (as propostas ficam no banco)
    try:
        os.remove(tasks_file)
    except OSError:
        pass


def create_child_task(
    s: Session,
    parent: Task,
    *,
    title: str,
    description: str,
    kind: str,
    target_repository_id: int | None = None,
    pipeline_id: int | None = None,
) -> Task:
    """Cria a task filha a partir de uma proposta aprovada.

    Reutilizado pelo _spawn_tasks antigo e pela API de aceitação de propostas:
    copia os steps do pipeline (escolhido na proposta, senão o default do repo
    alvo, senão o da task pai), herda o `executor` da task pai e usa o budget do
    repo alvo (ou o da task pai).
    """
    target_repo = (
        s.get(Repository, target_repository_id) if target_repository_id else parent.repository
    )
    pipeline_id = pipeline_id or target_repo.default_pipeline_id or parent.pipeline_id
    child = Task(
        repository_id=target_repo.id,
        pipeline_id=pipeline_id,
        title=title,
        description=description,
        kind=kind,
        status="created",
        executor=parent.executor,
        budget_limit=target_repo.task_budget if target_repo.task_budget is not None else parent.budget_limit,
        parent_task_id=parent.id,
        # Tarefas geradas por spawn herdam o responsável da task pai (se definido).
        responsible_id=parent.responsible_id,
    )
    s.add(child)
    s.flush()  # para obter child.id

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
    return child


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
