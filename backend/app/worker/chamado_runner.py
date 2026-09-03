"""Worker de CHAMADOS: processa ferramentas de etapa (assistente LLM) e avaliações
de fechamento de estágio do fluxo de atendimento — entidade paralela à pipeline.

Roda em processo separado (`autoia-chamado-worker`) com lock/heartbeat próprios.
Reusa os executores (kimi/opencode), o sandbox e o gitops do worker de tasks via
`runner._effective`/`runner._run_executor`: cada ação executa contra o checkout do
chamado na branch default (somente leitura, push bloqueado) e as interações viram
`ChamadoMessage` (transcript por etapa, payload SEMPRE completo).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import exists, func, update
from sqlalchemy.orm import Session

from .. import budget, chamado_prompts, verdicts
from ..config import Settings
from ..db import utcnow
from ..models import (
    CHAMADO_EM_ANDAMENTO,
    CHAMADO_STAGE_AGUARDANDO,
    CHAMADO_STAGE_ATIVA,
    CHAMADO_STAGE_EXECUTANDO,
    CHAMADO_STAGE_FECHADA,
    STAGE_DECISION_CANCELAR,
    STAGE_DECISION_CONCLUIR,
    STAGE_DECISION_NEXT,
    STAGE_DECISION_RESPOSTA,
    Chamado,
    ChamadoMessage,
    ChamadoStage,
    ChamadoStageType,
    Epic,
    Project,
)
from . import gitops, kimi_exec, opencode_exec
from .runner import _effective, _run_executor

log = logging.getLogger("autoia.chamado")

CHAMADO_HEARTBEAT_FILE = "chamado-worker.heartbeat"

# Guarda de geração de conteúdo (projeto/épico) para não sobrepor gerações.
_IN_FLIGHT: set[tuple[str, int]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def chamado_workspace(settings, repo_id: int, chamado_id: int) -> str:
    """Diretório de trabalho isolado por chamado (clone dedicado)."""
    return os.path.join(settings.workspace_dir, str(repo_id), f"chamado_{chamado_id}")


def _touch(path: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _heartbeat_loop(path: str, stop, interval: float = 5.0) -> None:
    while not stop.wait(interval):
        _touch(path)


def _append_message(s: Session, stage: ChamadoStage, kind: str, payload: dict, cost: float = 0.0) -> None:
    max_seq = (
        s.query(func.max(ChamadoMessage.seq))
        .filter(ChamadoMessage.stage_id == stage.id)
        .scalar()
        or 0
    )
    s.add(
        ChamadoMessage(
            chamado_id=stage.chamado_id,
            stage_id=stage.id,
            seq=max_seq + 1,
            kind=kind,
            payload=payload,
            cost=cost,
        )
    )


def _msg_count(session_factory, stage_id: int) -> int:
    with session_factory() as s:
        return (
            s.query(func.count(ChamadoMessage.id))
            .filter(ChamadoMessage.stage_id == stage_id)
            .scalar()
            or 0
        )


def recover_stale_chamados(session_factory) -> int:
    """Etapas `executando` órfãs de restart/crash do chamado-worker → voltam a
    `ativa` sem ação pendente (o usuário refaz o pedido)."""
    with session_factory() as s:
        stale = (
            s.query(ChamadoStage)
            .filter(ChamadoStage.status == CHAMADO_STAGE_EXECUTANDO)
            .all()
        )
        for st in stale:
            st.status = CHAMADO_STAGE_ATIVA
            st.pending_action = None
            st.started_at = None
            st.error = "worker reiniciado — ação órfã cancelada; refaça o pedido"
            _append_message(
                s, st, "system",
                {"event": "worker_recovered", "reason": st.error},
            )
        s.commit()
        return len(stale)


def claim_next_stage(session_factory) -> tuple[int, str] | None:
    """Reivindica a próxima etapa de chamado com ação pendente (claim atômico).

    Uma ação por chamado por vez (nunca reclama outra etapa de um chamado que já
    tem etapa `executando`). Retorna `(stage_id, action)` ou None."""
    with session_factory() as s:
        executing_chamado_ids = (
            s.query(ChamadoStage.chamado_id)
            .filter(ChamadoStage.status == CHAMADO_STAGE_EXECUTANDO)
            .scalar_subquery()
        )
        stage = (
            s.query(ChamadoStage)
            .filter(
                ChamadoStage.status == CHAMADO_STAGE_AGUARDANDO,
                ChamadoStage.pending_action.isnot(None),
                ChamadoStage.chamado_id.not_in(executing_chamado_ids),
            )
            .order_by(ChamadoStage.id)
            .first()
        )
        if stage is None:
            return None
        action = stage.pending_action or ""
        result = s.execute(
            update(ChamadoStage)
            .where(
                ChamadoStage.id == stage.id,
                ChamadoStage.status == CHAMADO_STAGE_AGUARDANDO,
                ~exists().where(
                    ChamadoStage.chamado_id == stage.chamado_id,
                    ChamadoStage.status == CHAMADO_STAGE_EXECUTANDO,
                    ChamadoStage.id != stage.id,
                ),
            )
            .values(status=CHAMADO_STAGE_EXECUTANDO, started_at=utcnow())
        )
        if result.rowcount != 1:
            return None
        s.commit()
        return stage.id, action


def chamado_worker_loop(settings: Settings, session_factory, workspace_dir: str) -> None:
    log.info("chamado-worker iniciado (dir de trabalho: %s)", workspace_dir)
    hb_path = os.path.join(workspace_dir, CHAMADO_HEARTBEAT_FILE)
    while True:
        _touch(hb_path)
        try:
            claimed = claim_next_stage(session_factory)
        except Exception:
            log.exception("erro no claim de etapa de chamado")
            time.sleep(2)
            continue
        if claimed is None:
            time.sleep(2)
            continue
        stage_id, action = claimed
        log.info("processando chamado stage %s (action=%s)", stage_id, action)
        stop = threading.Event()
        hb_thread = threading.Thread(
            target=_heartbeat_loop, args=(hb_path, stop), daemon=True, name="chamado-hb"
        )
        hb_thread.start()
        try:
            execute_stage_action(settings, session_factory, stage_id, action)
        except Exception:
            log.exception("erro executando ação de chamado %s", stage_id)
            with session_factory() as s:
                st = s.get(ChamadoStage, stage_id)
                if st is not None:
                    st.status = CHAMADO_STAGE_ATIVA
                    st.pending_action = None
                    st.error = "erro interno do worker"
                    _append_message(
                        s, st, "system",
                        {"event": "action_error", "error": "erro interno do worker"},
                    )
                    s.commit()
        finally:
            stop.set()
            hb_thread.join(timeout=1)


def _fail_stage(s: Session, stage: ChamadoStage, error: str) -> None:
    """Marca a etapa de volta a `ativa` sem ação pendente + evento de erro."""
    stage.status = CHAMADO_STAGE_ATIVA
    stage.pending_action = None
    stage.error = error[:2000]
    stage.chamado.status = CHAMADO_EM_ANDAMENTO
    _append_message(s, stage, "system", {"event": "action_error", "error": error[:2000]})


def execute_stage_action(settings: Settings, session_factory, stage_id: int, action: str) -> None:
    with session_factory() as s:
        stage = s.get(ChamadoStage, stage_id)
        if stage is None:
            return
        chamado = stage.chamado
        repo = chamado.repository
        eff = _effective(settings, repo)
        checkout = chamado_workspace(eff, repo.id, chamado.id)
        source = repo.url or repo.local_path or ""
        if not source:
            _fail_stage(s, stage, "repositório sem URL ou caminho local")
            s.commit()
            return
        base = repo.default_branch
        git_dir = os.path.join(checkout, ".git")
        if not os.path.isdir(git_dir):
            try:
                gitops.clone(source, checkout)
            except gitops.GitError as exc:
                _fail_stage(s, stage, f"clone falhou: {exc}")
                s.commit()
                return
        try:
            # Somente leitura na branch default (espelho do remote).
            gitops.checkout_default(checkout, base)
        except gitops.GitError as exc:
            _fail_stage(s, stage, f"git: {exc}")
            s.commit()
            return
    # Fora da sessão: execução longa (LLM).
    if action == "evaluate":
        result = _run_evaluation(eff, session_factory, stage_id, checkout)
    elif action.startswith("tool:"):
        result = _run_tool(eff, session_factory, stage_id, checkout, action[len("tool:"):])
    else:
        result = {"ok": False, "error": f"ação desconhecida: {action}"}

    with session_factory() as s:
        stage = s.get(ChamadoStage, stage_id)
        if stage is None:
            return
        chamado = stage.chamado
        if not result["ok"]:
            _fail_stage(s, stage, result.get("error") or "falha na execução")
            s.commit()
            return
        if action == "evaluate":
            _apply_decision(s, stage, chamado, result["decision"])
        else:
            stage.status = CHAMADO_STAGE_ATIVA
            stage.pending_action = None
            stage.error = None
            chamado.status = CHAMADO_EM_ANDAMENTO
            _append_message(
                s, stage, "system",
                {"event": "tool_done", "tool": action[len("tool:"):]},
            )
        s.commit()


def _run_tool(eff, session_factory, stage_id: int, checkout: str, tool_key: str) -> dict:
    """Executa uma ferramenta da etapa (assistente/escopo/...) com a última
    mensagem `user` da etapa como pedido. Interações viram ChamadoMessage."""
    with session_factory() as s:
        stage = s.get(ChamadoStage, stage_id)
        chamado = stage.chamado
        user_msg = next(
            (m for m in reversed(stage.messages) if m.kind == "user"), None
        )
        if user_msg is None:
            return {"ok": False, "error": "nenhum pedido do usuário para a ferramenta"}
        text = (user_msg.payload.get("text") or "").strip()
        tool_key = user_msg.payload.get("tool") or tool_key
        if tool_key not in chamado_prompts.TOOL_PRESETS:
            return {"ok": False, "error": f"ferramenta desconhecida: {tool_key}"}
        if not text:
            return {"ok": False, "error": "pedido do usuário vazio"}
        prompt = chamado_prompts.build_tool_prompt(chamado, stage, tool_key, text)
        executor = chamado.executor
        model = (chamado.model or "").strip() or None
        repo_id = chamado.repository_id
        chamado_id = chamado.id
    log_path = os.path.join(eff.log_dir, f"chamado_{chamado_id}_{stage_id}.log")
    state = {"seq": _msg_count(session_factory, stage_id), "cost": 0.0}

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        state["seq"] += 1
        with session_factory() as es:
            st = es.get(ChamadoStage, stage_id)
            if st is None:
                return None
            c = st.chamado
            c.cost_spent = (c.cost_spent or 0.0) + cost
            es.add(
                ChamadoMessage(
                    chamado_id=c.id,
                    stage_id=stage_id,
                    seq=state["seq"],
                    kind=kind,
                    payload=payload,
                    cost=cost,
                )
            )
            es.commit()
            if budget.budget_exceeded(c.cost_spent, c.budget_limit):
                return (
                    f"orçamento estourado: gasto {c.cost_spent:.2f} "
                    f">= limite {c.budget_limit:.2f}"
                )
        return None

    outcome = _run_executor(
        eff,
        executor,
        prompt,
        cwd=checkout,
        log_path=log_path,
        on_event=on_event,
        repo_id=repo_id,
        model=model,
    )
    if outcome.aborted or outcome.exit_code != 0:
        return {
            "ok": False,
            "error": outcome.abort_reason or f"execução falhou (exit {outcome.exit_code})",
        }
    return {"ok": True, "final_text": outcome.final_text}


def _run_evaluation(eff, session_factory, stage_id: int, checkout: str) -> dict:
    """Robô de avaliação decide o fechamento da etapa (chamado_decision.json)."""
    with session_factory() as s:
        stage = s.get(ChamadoStage, stage_id)
        chamado = stage.chamado
        allowed = list(stage.stage_type.close_options or [])
        prompt = chamado_prompts.build_evaluation_prompt(chamado, stage, allowed)
        executor = chamado.executor
        model = (chamado.model or "").strip() or None
        repo_id = chamado.repository_id
        chamado_id = chamado.id
    log_path = os.path.join(eff.log_dir, f"chamado_{chamado_id}_{stage_id}.log")
    state = {"seq": _msg_count(session_factory, stage_id), "cost": 0.0}

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        state["seq"] += 1
        with session_factory() as es:
            st = es.get(ChamadoStage, stage_id)
            if st is None:
                return None
            c = st.chamado
            c.cost_spent = (c.cost_spent or 0.0) + cost
            es.add(
                ChamadoMessage(
                    chamado_id=c.id,
                    stage_id=stage_id,
                    seq=state["seq"],
                    kind=kind,
                    payload=payload,
                    cost=cost,
                )
            )
            es.commit()
            if budget.budget_exceeded(c.cost_spent, c.budget_limit):
                return (
                    f"orçamento estourado: gasto {c.cost_spent:.2f} "
                    f">= limite {c.budget_limit:.2f}"
                )
        return None

    outcome = _run_executor(
        eff,
        executor,
        prompt,
        cwd=checkout,
        log_path=log_path,
        on_event=on_event,
        repo_id=repo_id,
        model=model,
    )
    decision = verdicts.read_chamado_decision(checkout)
    verdicts.remove_chamado_decision(checkout)
    if outcome.aborted or outcome.exit_code != 0:
        return {
            "ok": False,
            "error": outcome.abort_reason or f"execução falhou (exit {outcome.exit_code})",
        }
    if decision is None:
        return {
            "ok": False,
            "error": "robô não emitiu decisão válida (chamado_decision.json ausente/inválido)",
        }
    return {"ok": True, "decision": decision}


def _resolve_stage_type(s: Session, repo_id: int, name: str) -> ChamadoStageType | None:
    return (
        s.query(ChamadoStageType)
        .filter(ChamadoStageType.repository_id == repo_id, ChamadoStageType.name == name)
        .first()
    ) or (
        s.query(ChamadoStageType)
        .filter(ChamadoStageType.repository_id.is_(None), ChamadoStageType.name == name)
        .first()
    )


def _apply_decision(s: Session, stage: ChamadoStage, chamado: Chamado, decision: dict) -> None:
    """Valida a decisão contra o `close_options` da etapa e aplica a transição."""
    close_options = list(stage.stage_type.close_options or [])
    kind = decision["decision"]
    target_name: str | None = None
    valid = False
    if kind == STAGE_DECISION_NEXT:
        target_name = (decision.get("next_stage") or "").strip() or None
        if target_name is not None:
            valid = f"next:{target_name}" in close_options
    elif kind == STAGE_DECISION_RESPOSTA:
        valid = "resposta" in close_options
    elif kind == STAGE_DECISION_CANCELAR:
        valid = "cancelar" in close_options
    elif kind == STAGE_DECISION_CONCLUIR:
        valid = "concluir" in close_options

    if not valid:
        stage.status = CHAMADO_STAGE_ATIVA
        stage.pending_action = None
        stage.error = f"decisão inválida para esta etapa: {kind}"
        if target_name:
            stage.error += f" -> {target_name}"
        _append_message(
            s, stage, "system",
            {"event": "decision_invalid", "decision": kind, "close_options": close_options},
        )
        return

    stage.status = CHAMADO_STAGE_FECHADA
    stage.pending_action = None
    stage.decision = (
        kind if kind != STAGE_DECISION_NEXT else f"{STAGE_DECISION_NEXT}:{target_name}"
    )
    stage.result = decision.get("resposta_texto") or decision.get("justificativa") or ""
    stage.error = None
    stage.finished_at = utcnow()
    _append_message(
        s, stage, "system",
        {
            "event": "stage_closed",
            "decision": stage.decision,
            "justificativa": decision.get("justificativa") or "",
        },
    )

    if kind == STAGE_DECISION_NEXT:
        new_type = _resolve_stage_type(s, chamado.repository_id, target_name or "")
        if new_type is None:
            stage.status = CHAMADO_STAGE_ATIVA
            stage.pending_action = None
            stage.error = f"tipo de etapa não encontrado: {target_name}"
            return
        max_pos = max((st.position for st in chamado.stages), default=0)
        s.add(
            ChamadoStage(
                chamado_id=chamado.id,
                stage_type_id=new_type.id,
                position=max_pos + 1,
                status=CHAMADO_STAGE_ATIVA,
            )
        )
        chamado.workflow_status = new_type.name
        chamado.status = CHAMADO_EM_ANDAMENTO
    elif kind == STAGE_DECISION_RESPOSTA:
        chamado.workflow_status = f"{stage.stage_type.name} (respondido)"
        chamado.status = "respondido"
        chamado.error = None
    elif kind == STAGE_DECISION_CANCELAR:
        chamado.workflow_status = f"{stage.stage_type.name} (cancelado)"
        chamado.status = "cancelado"
        chamado.error = None
    else:  # concluir
        chamado.workflow_status = f"{stage.stage_type.name} (concluído)"
        chamado.status = "concluido"
        chamado.error = None


# ── Conteúdo de Projeto/Épico (recursos LLM, one-shot sem checkout) ────────

def generate_text(settings: Settings, executor: str, prompt: str, log_suffix: str) -> str | None:
    """Execução LLM one-shot (resumo de projeto/épico) — sem checkout, sem git.

    Reusa o executor configurado; eventos são descartados (só o texto final importa).
    """
    cwd = settings.workspace_dir
    os.makedirs(cwd, exist_ok=True)
    log_path = os.path.join(settings.log_dir, f"{log_suffix}.log")
    captured: list[str] = []

    def on_event(kind: str, payload: dict, cost: float) -> str | None:
        if kind == "assistant_text":
            captured.append(payload.get("content") or "")
        return None

    if executor == "opencode":
        outcome = opencode_exec.run_opencode(
            prompt,
            cwd=cwd,
            opencode_bin=settings.opencode_bin,
            log_path=log_path,
            timeout=settings.run_timeout,
            max_identical_calls=settings.max_identical_calls,
            risky_patterns=[],
            checkout_path=cwd,
            whitelisted_hosts=[],
            no_progress_timeout=settings.no_progress_timeout,
            model=settings.opencode_model,
            on_event=on_event,
        )
    else:
        outcome = kimi_exec.run_kimi(
            prompt,
            cwd=cwd,
            kimi_bin=settings.kimi_bin,
            log_path=log_path,
            timeout=settings.run_timeout,
            max_identical_calls=settings.max_identical_calls,
            risky_patterns=[],
            checkout_path=cwd,
            whitelisted_hosts=[],
            cost_per_interaction=settings.cost_per_interaction,
            no_progress_timeout=settings.no_progress_timeout,
            on_event=on_event,
        )
    if outcome.aborted or outcome.exit_code != 0:
        return None
    return outcome.final_text or "".join(captured)


def _generate_content(settings: Settings, session_factory, kind: str, obj_id: int) -> None:
    with session_factory() as s:
        prompt: str | None = None
        if kind == "project":
            obj = s.get(Project, obj_id)
            if obj is None:
                return
            prompt = chamado_prompts.build_project_summary_prompt(obj, list(obj.epics), list(obj.chamados))
        elif kind == "epic_scope":
            obj = s.get(Epic, obj_id)
            if obj is None:
                return
            prompt = chamado_prompts.build_epic_scope_prompt(obj, list(obj.chamados))
        elif kind == "epic_summary":
            obj = s.get(Epic, obj_id)
            if obj is None:
                return
            prompt = chamado_prompts.build_epic_summary_prompt(obj, list(obj.chamados))
        else:
            return
    text = generate_text(settings, "kimi", prompt, f"chamado_content_{kind}_{obj_id}")
    if not text:
        return
    with session_factory() as s:
        if kind == "project":
            obj = s.get(Project, obj_id)
            if obj is not None:
                obj.summary = text
        else:
            obj = s.get(Epic, obj_id)
            if obj is not None:
                if kind == "epic_scope":
                    obj.scope = text
                else:
                    obj.summary = text
        s.commit()


def start_content_generation(settings: Settings, session_factory, kind: str, obj_id: int) -> bool:
    """Dispara a geração de conteúdo em background (uma por objeto por vez)."""
    key = (kind, obj_id)
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(key)

    def run() -> None:
        try:
            _generate_content(settings, session_factory, kind, obj_id)
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(key)

    threading.Thread(
        target=run, daemon=True, name=f"chamado-content-{kind}-{obj_id}"
    ).start()
    return True


def is_content_generating(kind: str, obj_id: int) -> bool:
    """True se a geração de conteúdo (resumo/escopo) do objeto está em voo."""
    with _IN_FLIGHT_LOCK:
        return (kind, obj_id) in _IN_FLIGHT
