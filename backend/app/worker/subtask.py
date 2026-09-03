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
    STEP_GUARDRAIL_BLOCKED,
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
from . import codex_exec, exec_common, gitops, kimi_exec, opencode_exec
from .sandbox import (
    SANDBOX_OFF,
    SandboxConfig,
    docker_available,
    docker_image_available,
)

log = logging.getLogger("autoia.worker.subtask")


def _sub_sandbox(settings) -> SandboxConfig | None:
    """Sandbox efetivo do ciclo de subtarefas (só aplica quando é um SandboxConfig;
    um `Settings` legado tem `sandbox` como string — executa direto)."""
    sb = getattr(settings, "sandbox", None)
    return sb if isinstance(sb, SandboxConfig) else None


def _sub_extra_env(settings) -> dict[str, str]:
    sb = _sub_sandbox(settings)
    base = sb.host_services_base if sb else "http://127.0.0.1"
    mode = sb.mode if sb else "off"
    return {"AUTOIA_HOST_SERVICES_BASE": base, "AUTOIA_SANDBOX": mode}


def _task_effective_model(task, robot=None) -> str | None:
    """Modelo efetivo de uma execução de subtarefa: `task.model` (escolha na
    task) > `robot.model` > None (default do executor). `""` = ausente."""
    model = (getattr(task, "model", None) or "").strip()
    if model:
        return model
    if robot is not None:
        robot_model = (getattr(robot, "model", None) or "").strip()
        if robot_model:
            return robot_model
    return None


def _run_subtask_executor(
    settings,
    executor: str,
    prompt: str,
    *,
    cwd: str,
    log_path: str,
    checkout_path: str,
    repo_id: int | None,
    stop_file: str | None,
    task_stop_file: str | None,
    sandbox: SandboxConfig | None,
    model: str | None = None,
    on_event,
):
    """Dispatch do executor da task (kimi/opencode/codex) para UMA execução de
    subtarefa — espelha o `_run_executor` do runner, mas o ciclo de subtarefas
    gerencia o próprio lock_push/sandbox (por isso não reusa o runner)."""
    workspace_dir = getattr(settings, "workspace_dir", None)
    extra_env = _sub_extra_env(settings)
    # Fallback do sandbox (mesmo contrato do runner): sem docker/imagem e sem
    # fail_closed, executa sem isolamento com aviso no log — sem isso, o ciclo
    # de subtarefas falhava em cadeia em ambientes sem daemon docker (ex.: CI)
    # mesmo com `AUTOIA_SANDBOX=fs`, porque este dispatch não tinha o fallback.
    if sandbox is not None and sandbox.enabled:
        if not (docker_available() and docker_image_available(sandbox.image)):
            if sandbox.fail_closed:
                outcome = exec_common.ExecOutcome()
                outcome.aborted = True
                outcome.abort_reason = (
                    f"sandbox {sandbox.mode} obrigatório mas docker/imagem "
                    f"{sandbox.image} indisponíveis (fail-closed)"
                )
                outcome.sandbox_mode = sandbox.mode
                return outcome
            log.warning(
                "subtask: sandbox %s solicitado mas docker/imagem %s indisponível — "
                "executando sem isolamento (fallback transitório; "
                "AUTOIA_SANDBOX_FAIL_CLOSED=1 para falhar)",
                sandbox.mode, sandbox.image,
            )
            sandbox = SandboxConfig(mode=SANDBOX_OFF)
    if executor == "opencode":
        return opencode_exec.run_opencode(
            prompt,
            cwd=cwd,
            opencode_bin=settings.opencode_bin,
            log_path=log_path,
            timeout=settings.run_timeout,
            max_identical_calls=settings.max_identical_calls,
            risky_patterns=settings.risky_patterns,
            checkout_path=checkout_path,
            whitelisted_hosts=settings.whitelisted_hosts,
            model=model or settings.opencode_model,
            no_progress_timeout=settings.no_progress_timeout,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            on_event=on_event,
        )
    if executor == "codex":
        return codex_exec.run_codex(
            prompt,
            cwd=cwd,
            codex_bin=settings.codex_bin,
            log_path=log_path,
            timeout=settings.run_timeout,
            max_identical_calls=settings.max_identical_calls,
            risky_patterns=settings.risky_patterns,
            checkout_path=checkout_path,
            whitelisted_hosts=settings.whitelisted_hosts,
            cost_per_interaction=settings.cost_per_interaction,
            no_progress_timeout=settings.no_progress_timeout,
            model=model or settings.codex_model or None,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            extra_env=extra_env,
            on_event=on_event,
        )
    return kimi_exec.run_kimi(
        prompt,
        cwd=cwd,
        kimi_bin=settings.kimi_bin,
        log_path=log_path,
        timeout=settings.run_timeout,
        max_identical_calls=settings.max_identical_calls,
        risky_patterns=settings.risky_patterns,
        checkout_path=checkout_path,
        cost_per_interaction=settings.cost_per_interaction,
        repo_id=repo_id,
        stop_file=stop_file,
        task_stop_file=task_stop_file,
        sandbox=sandbox,
        workspace_dir=workspace_dir,
        extra_env=extra_env,
        on_event=on_event,
    )

# Arquivos de controle do autoia (não versionados) que NÃO contam como "mudança
# de código" no guard de re-declaração de subtarefa já implementada.
_AUTOIA_CONTROL_FILES = (
    "autoia_subtasks_done.json",
    "autoia_tasks.json",
    "autoia_verdict.txt",
    "autoia_handoff.md",
    "autoia_blocked.json",
    "autoia_summary.json",
    "autoia_step_mission.json",
    "autoia_screenshots/",
    "AGENTS.md",
)

# Instrução de simplificação quando a verificação ANTERIOR da mesma subtarefa
# estourou o tempo (timeout): o tester repete só o caminho rápido (headless) e
# avança com o aviso no SUMMARY — sem gastar o limite de novo com emulação lenta.
PRIOR_TIMEOUT_SIMPLIFY = """## Verificação anterior desta subtarefa estourou o tempo (timeout)

Uma tentativa anterior de verificar esta MESMA subtarefa falhou por exceder o
limite de tempo — em geral por testes lentos baseados em emulação (emulador,
navegador). Nesta re-execução, SIMPLIFIQUE a verificação:

- Rode APENAS a suíte headless do projeto (testes unitários/integração na
  JVM/Robolectric, sem subir emulador, navegador, servidor externo ou dispositivo)
  e os comandos rápidos exigidos pelos critérios.
- NÃO suba emulador/navegador nem ambiente externo nesta execução.
- Valide os critérios restantes por leitura de código/diff, sem executar o app.
- Emita o veredicto normalmente: PASS se o caminho rápido estiver verde. No
  SUMMARY, liste EXATAMENTE o que foi pulado (ex.: "E2E com emulador NÃO rodado —
  tentativa anterior estourou o tempo") e, se a verificação pulada for relevante,
  sugira UMA TAREFA SEPARADA para ela (título + critérios + passos), para o humano
  criar depois. Não crie arquivos de tarefas."""


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

    # Intervenção do usuário (retomada): entra direto no prompt — a subtarefa é
    # parte da re-execução e a instrução não pode depender só do handoff.
    if task.resume_instruction:
        parts.append(
            f"## Intervenção do usuário (retomada)\n{task.resume_instruction}\n\n"
            "_A execução foi bloqueada e o usuário forneceu esta instrução para continuar "
            "exatamente de onde parou. Atenda a instrução nesta execução._"
        )

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
    run_timeout: int | None = None,
    prior_timeout: bool = False,
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

    if run_timeout and run_timeout > 0:
        parts.append(
            f"## Limite de tempo desta execução\n"
            f"Você tem NO MÁXIMO {run_timeout} segundos para verificar esta subtarefa "
            f"(matando o processo ao estourar, sem aproveitar o veredicto parcial). "
            f"Planeje a verificação para caber nesse limite."
        )

    if prior_timeout:
        parts.append(PRIOR_TIMEOUT_SIMPLIFY)

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
) -> tuple[str | None, str | None]:
    """Executa a fase implement para cada subtarefa pendente.

    Retorna `(abort_reason, free_summary)`:
    - `abort_reason`: erro fatal (orçamento, guardrail, commit) ou None se concluiu;
    - `free_summary`: texto final da execução livre (re-execução sem subtarefa
      pendente, motivada por instrução do usuário ou falha de fase posterior) —
      vira o resumo da fase. None quando não houve execução livre.
    """
    on_event = _make_on_event(session_factory, step.id, log_path)

    with session_factory() as s:
        # Re-execução da fase implement (retry manual, instrução do usuário ou
        # decisão do PM): subtarefas com tentativas esgotadas (`failed`) ou presas
        # em `implementing` (worker morto no meio de uma execução anterior) voltam
        # a `pending` para serem REALMENTE re-trabalhadas pelo developer. Sem
        # isso, reabrir o developer terminava a fase imediatamente (`phase_done`
        # sem executar nada) — a instrução do usuário era ignorada em silêncio.
        for st in (
            s.query(SubTask)
            .filter(
                SubTask.task_id == task_id,
                SubTask.status.in_([SUB_FAILED, SUB_IMPLEMENTING]),
            )
            .all()
        ):
            st.status = SUB_PENDING
            st.error = None
        if s.dirty:
            s.commit()
        task = s.get(Task, task_id)
        pending = (
            s.query(SubTask)
            .filter(SubTask.task_id == task_id, SubTask.status.in_([SUB_PENDING]))
            .order_by(SubTask.position)
            .all()
        )
        free_reason = None if (pending or task is None) else _free_run_reason(s, step, task)

    if pending:
        for subtask in pending:
            abort_reason = _run_one_implement(
                settings, session_factory, step, task_id, subtask,
                checkout, base, branch, project_info, log_path, on_event,
            )
            if abort_reason:
                return abort_reason, None
        free_summary = None
    elif free_reason is not None:
        # Re-execução sem subtarefa pendente motivada externamente (instrução do
        # usuário / fase posterior falhou): roda UMA execução livre do developer
        # para corrigir o trabalho já entregue — nunca termina a fase em silêncio.
        abort_reason, free_summary = _run_free_implement(
            settings, session_factory, step, task_id,
            checkout, base, branch, project_info, log_path, on_event, free_reason,
        )
        if abort_reason:
            return abort_reason, None
    else:
        # Nada pendente e nenhum motivo externo: fase concluída sem trabalho novo.
        free_summary = None

    # Bookkeeping do SISTEMA: normaliza `autoia_subtasks_done.json` (lista
    # acumulada) e commita se mudou — o conteúdo nunca fica a cargo do robô
    # (a task-86 morreu porque o developer gravou `[4]` em vez de `[1, 2, 3, 4]`).
    try:
        _normalize_and_commit_subtasks_done(session_factory, task_id, checkout)
    except gitops.GitError as exc:
        return f"bookkeeping: {exc}", None

    return None, free_summary


def _free_run_reason(s: Session, step: TaskStep, task: Task) -> str | None:
    """Motivo para rodar UMA execução livre do developer quando não há subtarefa
    pendente (re-execução motivada externamente). None = nada a fazer — a fase
    conclui legitimamente (ex.: primeira execução sem subtarefas a fazer)."""
    parts: list[str] = []
    if task.resume_instruction:
        parts.append(
            "o usuário forneceu uma instrução de retomada:\n"
            f"{task.resume_instruction}"
        )
    for st in sorted(
        (x for x in task.steps if not x.archived), key=lambda x: x.position
    ):
        if st.position > step.position and st.status in (
            STEP_FAILED,
            STEP_GUARDRAIL_BLOCKED,
        ):
            detail = (
                "\n".join(p for p in (st.error, st.summary) if p).strip()
                or "(sem detalhes)"
            )
            parts.append(
                f"a fase {st.position} ({st.robot.name if st.robot else '?'}) "
                f"falhou e o trabalho voltou para esta fase corrigir:\n{detail}"
            )
    return "\n\n".join(parts) if parts else None


def _build_free_implement_prompt(
    task: Task,
    reason: str,
    project_info: str,
    base: str,
    branch: str,
    checkout: str,
) -> str:
    """Prompt da execução livre do developer: re-execução da fase sem subtarefa
    pendente, para corrigir/ajustar o trabalho já entregue."""
    parts: list[str] = []
    robot = next(
        (st.robot for st in task.steps if st.robot and st.robot.role == "implement"),
        None,
    )
    if robot:
        mission = (robot.mission or "").strip()
        parts.append(
            mission.replace("{task_title}", task.title or "")
            .replace("{task_description}", task.description or "")
            .replace(
                "{step_context}",
                "Re-execução da fase de implementação (correção do trabalho entregue)",
            )
            .replace("{default_branch}", base or "main")
        )

    parts.append(prompts.GIT_WORKFLOW)
    if project_info:
        parts.append(project_info)

    parts.append(
        "## Correção do trabalho já entregue (re-execução da fase)\n"
        "Todas as subtarefas desta fase já estão implementadas na branch. Esta "
        f"execução existe porque {reason}.\n\n"
        "Corrija exatamente o que foi apontado (faça os commits necessários) e "
        "documente no texto final o que foi feito e a evidência. Se nada precisar "
        "de mudança no código, documente isso claramente no texto final."
    )
    parts.append(prompts.HANDOFF_READ)
    parts.append(prompts.HANDOFF_DOCUMENT)
    parts.append(prompts.EVIDENCE)
    parts.append(prompts.GUARDRAIL_INSTRUCTIONS)

    return "\n\n".join(p for p in parts if p)


def _run_free_implement(
    settings: Settings,
    session_factory,
    step: TaskStep,
    task_id: int,
    checkout: str,
    base: str,
    branch: str,
    project_info: str,
    log_path: str,
    on_event,
    reason: str,
) -> tuple[str | None, str]:
    """Uma execução livre do developer (sem subtarefa pendente): o robô corrige o
    trabalho já entregue conforme instrução do usuário / falha de fase posterior.
    Retorna `(abort_reason, final_text)`. Não há veredicto nem bounce interno —
    o fluxo normal da fase decide o próximo passo."""
    with session_factory() as s:
        task = s.get(Task, task_id)
        if task is None:
            return "task não encontrada", ""
        step_obj = s.get(TaskStep, step.id)
        executor = task.executor
        advpl = gitops.is_advpl_robot(step_obj.robot.name if step_obj.robot else None)
        repo_id = task.repository_id
        stop_file = (
            exec_common.repo_stop_path(settings.workspace_dir, repo_id)
            if repo_id is not None
            else None
        )
        task_stop_file = (
            exec_common.task_stop_path(settings.workspace_dir, task_id)
            if getattr(settings, "workspace_dir", None)
            else None
        )
        prompt = _build_free_implement_prompt(
            task, reason, project_info, base, branch, checkout
        )
        _system_event(
            s, step, "subtask_prompt",
            {"position": None, "title": "re-execução livre da fase", "prompt": prompt},
        )
        _system_event(
            s, step, "subtask_free_run",
            {"position": None, "title": "re-execução livre da fase", "reason": reason},
        )
        s.commit()

    model = _task_effective_model(task, step_obj.robot)
    sandbox = _sub_sandbox(settings)
    try:
        gitops.lock_push(checkout)
    except gitops.GitError:
        log.warning(
            "subtask: não foi possível bloquear push em %s", checkout, exc_info=True
        )
    try:
        outcome = _run_subtask_executor(
            settings, executor, prompt,
            cwd=checkout,
            log_path=log_path,
            checkout_path=checkout,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            model=model,
            on_event=on_event,
        )
    finally:
        try:
            gitops.unlock_push(checkout)
        except gitops.GitError:
            log.warning(
                "subtask: não foi possível liberar push em %s", checkout, exc_info=True
            )

    if outcome.aborted:
        return outcome.abort_reason or "abortado", outcome.final_text or ""
    if outcome.exit_code != 0:
        return f"{executor} saiu com código {outcome.exit_code}", outcome.final_text or ""

    message = (
        gitops.advpl_commit_message(executor, "ajustes da re-execução da fase")
        if advpl
        else "autoia: ajustes da re-execução da fase implement"
    )
    try:
        committed = gitops.commit_all(checkout, message)
        if advpl and committed:
            gitops.push_branch(checkout, branch)
    except gitops.GitError as exc:
        return f"commit: {exc}", outcome.final_text or ""

    return None, outcome.final_text or ""


def _normalize_and_commit_subtasks_done(
    session_factory, task_id: int, checkout: str
) -> None:
    """Bookkeeping do SISTEMA: reescreve `autoia_subtasks_done.json` com a lista
    ACUMULADA (posições 1-based) das subtarefas implementadas/verificadas da task
    e commita o arquivo se o conteúdo mudou.

    O conteúdo nunca fica a cargo do robô: o developer pode gravar apenas a
    posição atual (regressão) — aqui o estado final é sempre cumulativo e
    commitado, como a convenção do repo exige (caso da task-86)."""
    with session_factory() as s:
        positions = sorted(
            int(p) + 1
            for (p,) in s.query(SubTask.position).filter(
                SubTask.task_id == task_id,
                SubTask.status.in_([SUB_IMPLEMENTED, SUB_DONE]),
            )
        )
    content = json.dumps(positions) + "\n"
    path = os.path.join(checkout, "autoia_subtasks_done.json")
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current == content:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    gitops.run_git(checkout, "add", "--", "autoia_subtasks_done.json")
    if gitops.run_git(checkout, "diff", "--cached", "--quiet", check=False).returncode != 0:
        gitops.run_git(
            checkout, "commit", "-m",
            "autoia: registra subtarefas implementadas (bookkeeping)",
        )


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


def _subtask_previously_failed_verify(session_factory, task_id: int, position: int) -> bool:
    """True se a subtarefa já reprovou na verificação por VEREDICTO REAL (FAIL/AUSENTE)
    em tentativa anterior — ou seja, o código foi apontado como defeituoso e devolvido
    ao developer por correção.

    Falhas de INFRAESTRUTURA na verificação (guardrail, timeout, kimi saiu com erro)
    NÃO contam como defeito: o código não foi avaliado, então o developer pode
    legitimamente re-declarar a subtarefa como já implementada sem alterar nada."""
    with session_factory() as s:
        step_ids = [
            sid for (sid,) in s.query(TaskStep.id).filter(TaskStep.task_id == task_id).all()
        ]
        if not step_ids:
            return False
        events = (
            s.query(RunEvent)
            .filter(
                RunEvent.step_id.in_(step_ids),
                RunEvent.kind == "subtask_failed",
            )
            .all()
        )
    for ev in events:
        payload = ev.payload or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != "verify" or payload.get("position") != position:
            continue
        reason = str(payload.get("reason") or "")
        if "veredicto" in reason.lower():
            return True
    return False


def _subtask_verify_timed_out_before(session_factory, task_id: int, position: int) -> bool:
    """True se uma verificação ANTERIOR desta subtarefa falhou por TIMEOUT.

    Usado para simplificar a re-verificação: estourar o tempo indica verificação
    lenta demais (emulador/browser), então o tester é orientado a repetir apenas
    o caminho headless e avançar avisando no SUMMARY."""
    with session_factory() as s:
        step_ids = [
            sid for (sid,) in s.query(TaskStep.id).filter(TaskStep.task_id == task_id).all()
        ]
        if not step_ids:
            return False
        events = (
            s.query(RunEvent)
            .filter(
                RunEvent.step_id.in_(step_ids),
                RunEvent.kind == "subtask_failed",
            )
            .all()
        )
    for ev in events:
        payload = ev.payload or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != "verify" or payload.get("position") != position:
            continue
        reason = str(payload.get("reason") or "")
        if "timeout" in reason.lower():
            return True
    return False


def _done_declaration_has_changes(checkout: str, head_before: str | None) -> bool:
    """True se o código mudou desde o início desta execução (commit novo e/ou
    árvore de trabalho suja) — evidência de que o agente realmente corrigiu algo
    antes de declarar a subtarefa como já implementada.

    Arquivos de controle do autoia (não versionados) não contam como mudança."""
    try:
        head_now = gitops.run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    except gitops.GitError:
        head_now = None
    if head_before and head_now and head_now != head_before:
        return True
    try:
        result = gitops.run_git(checkout, "status", "--porcelain")
    except gitops.GitError:
        return False
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if not path:
            continue
        if any(path.startswith(name) for name in _AUTOIA_CONTROL_FILES):
            continue
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
        # Fluxo ADVPL: commit com prefixo do executor + push da branch por subtarefa.
        step_obj = s.get(TaskStep, step.id)
        executor = task.executor
        advpl = gitops.is_advpl_robot(step_obj.robot.name if step_obj.robot else None)
        # Parada cooperativa: se o projeto for excluído durante a execução da
        # subtarefa, o watchdog do executor mata o processo.
        repo_id = task.repository_id
        stop_file = (
            exec_common.repo_stop_path(settings.workspace_dir, repo_id)
            if repo_id is not None
            else None
        )
        task_stop_file = (
            exec_common.task_stop_path(settings.workspace_dir, task_id)
            if getattr(settings, "workspace_dir", None)
            else None
        )
        try:
            head_before = gitops.run_git(checkout, "rev-parse", "HEAD").stdout.strip()
        except gitops.GitError:
            head_before = None
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

    model = _task_effective_model(task, step_obj.robot)
    sandbox = _sub_sandbox(settings)
    try:
        gitops.lock_push(checkout)
    except gitops.GitError:
        log.warning("subtask: não foi possível bloquear push em %s", checkout, exc_info=True)
    try:
        outcome = _run_subtask_executor(
            settings, executor, prompt,
            cwd=checkout,
            log_path=log_path,
            checkout_path=checkout,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            model=model,
            on_event=on_event,
        )
    finally:
        try:
            gitops.unlock_push(checkout)
        except gitops.GitError:
            log.warning("subtask: não foi possível liberar push em %s", checkout, exc_info=True)

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
            reason = f"{executor} saiu com código {outcome.exit_code}"
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
            # Guarda anti-laço: numa subtarefa que JÁ reprovou na verificação, a
            # declaração só é aceita se o código mudou nesta execução (fix real).
            # Re-declarar "já implementada" sem alterar nada encobre a falha e
            # queima ciclos de tester — nesse caso, falha a subtarefa na hora.
            if (
                _subtask_previously_failed_verify(session_factory, task_id, st.position)
                and not _done_declaration_has_changes(checkout, head_before)
            ):
                reason = (
                    f"subtask_done_rejected: subtarefa {st.position + 1} re-declarada "
                    "como já implementada sem alterar o código (falha anterior não corrigida)"
                )
                st.status = SUB_FAILED
                st.error = reason
                st.finished_at = func.now()
                _system_event(
                    s, step, "subtask_failed",
                    {"position": st.position, "title": st.title, "reason": reason, "phase": "implement"},
                )
                _system_event(
                    s, step, "subtask_done_rejected",
                    {"position": st.position, "title": st.title,
                     "reason": "re-declaração sem alteração de código após falha na verificação"},
                )
                s.commit()
                return reason

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
            if advpl:
                message = gitops.advpl_commit_message(
                    executor, st.title or f"subtarefa {st.position + 1}"
                )
            else:
                message = f"autoia: subtask {st.position + 1} - {st.title}"
            gitops.commit_all(checkout, message)
            if advpl:
                gitops.push_branch(checkout, branch)
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
        # Modelo efetivo (task.model > robô do step): robot carregado DENTRO da
        # sessão p/ não disparar lazy-load com a sessão fechada ou num step
        # detached passado pelo chamador.
        step_obj = s.get(TaskStep, step.id) if step is not None else None
        robot = step_obj.robot if step_obj is not None else None
        # Parada cooperativa: se o projeto for excluído durante a execução da
        # subtarefa, o watchdog do executor mata o processo.
        repo_id = task.repository_id
        executor = task.executor
        stop_file = (
            exec_common.repo_stop_path(settings.workspace_dir, repo_id)
            if repo_id is not None
            else None
        )
        task_stop_file = (
            exec_common.task_stop_path(settings.workspace_dir, task_id)
            if getattr(settings, "workspace_dir", None)
            else None
        )
        st.status = SUB_VERIFYING
        st.started_at = func.now()
        _system_event(
            s, step, "subtask_start",
            {"position": st.position, "title": st.title, "attempt": st.attempt, "phase": "verify"},
        )
        prior_timeout = _subtask_verify_timed_out_before(session_factory, task_id, st.position)
        prompt = _build_subtask_verify_prompt(
            task, st, project_info, base, branch, checkout,
            run_timeout=getattr(settings, "run_timeout", None),
            prior_timeout=prior_timeout,
        )
        _system_event(
            s, step, "subtask_prompt",
            {"position": st.position, "title": st.title, "prompt": prompt},
        )
        s.commit()

    model = _task_effective_model(task, robot)
    sandbox = _sub_sandbox(settings)
    try:
        gitops.lock_push(checkout)
    except gitops.GitError:
        log.warning("subtask: não foi possível bloquear push em %s", checkout, exc_info=True)
    try:
        outcome = _run_subtask_executor(
            settings, executor, prompt,
            cwd=checkout,
            log_path=log_path,
            checkout_path=checkout,
            repo_id=repo_id,
            stop_file=stop_file,
            task_stop_file=task_stop_file,
            sandbox=sandbox,
            model=model,
            on_event=on_event,
        )
    finally:
        try:
            gitops.unlock_push(checkout)
        except gitops.GitError:
            log.warning("subtask: não foi possível liberar push em %s", checkout, exc_info=True)

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
            reason = f"{executor} saiu com código {outcome.exit_code}"
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
