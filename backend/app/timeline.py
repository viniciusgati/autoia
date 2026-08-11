"""Derivação da timeline de execução de uma task a partir dos RunEvent.

A timeline é uma visão cronológica do desenvolvimento como sequência de eventos
("o que aconteceu", "como foi feito", "o que exatamente aconteceu tecnicamente").
Cada evento tem um resumo DETERMINÍSTICO (sem LLM) gerado por este módulo a partir
dos dados reais da execução — os RunEvent são a fonte de verdade.

Cada `tool_call` vira um evento próprio; o `tool_result` seguinte é pareado para
preencher `output`, `status` e `duration_ms`. Tool calls de plataformas diferentes
(kimi / opencode) são normalizadas em `name` + `input`/`output` + `summary`.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import STEP_GUARDRAIL_BLOCKED, RunEvent, Task, TaskStep

# Tipos de evento expostos na timeline.
EV_DEV_START = "development_started"
EV_DEV_DONE = "development_finished"
EV_PHASE = "phase"
EV_PHASE_DONE = "phase_done"
EV_TASK_EVENT = "task"  # tarefa/subtarefa (criada, iniciada, concluída...)
EV_TOOL_CALL = "tool_call"
EV_TEXT = "text"
EV_ERROR = "error"
EV_WARNING = "warning"
EV_USER = "user_intervention"
EV_BLOCK = "blocked"
EV_SYSTEM = "system"

# Eventos "marco" para a visão compacta (Nível 1 da timeline).
MILESTONE_KINDS = {
    EV_DEV_START,
    EV_DEV_DONE,
    EV_PHASE_DONE,
    EV_BLOCK,
    EV_USER,
    EV_ERROR,
    EV_TASK_EVENT,
}

# Nomes amigáveis dos tool calls para o resumo determinístico.
_TOOL_LABELS = {
    "Bash": "Executar comando",
    "Read": "Ler arquivo",
    "Write": "Escrever arquivo",
    "Edit": "Editar arquivo",
    "MultiEdit": "Editar arquivo (multiedit)",
    "Glob": "Procurar arquivos",
    "Grep": "Buscar código",
    "WebFetch": "Buscar na web",
    "Agent": "Delegar a subagente",
    "Task": "Criar tarefa",
    "opencode_task": "Criar tarefa",
    "autoia_mark_subtask_done": "Marcar subtarefa como implementada",
}

# Eventos de sistema do worker que são apenas ruído na timeline de execução.
_SKIP_KINDS = {"prompt", "subtask_prompt"}

# Nome do robô de cada fase (para eventos de fase).
_STEP_ROLE_LABEL = {
    "refine": "PO",
    "review": "QA",
    "implement": "developer",
    "verify": "tester",
    "assess": "avaliador",
    "merge": "merger",
    "pm": "PM",
}


def _parse_args(arguments: str) -> dict | None:
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _tool_target(args: dict | None) -> str:
    """Extrai o alvo (arquivo/comando/query) de argumentos de tool call."""
    if not args:
        return ""
    for key in ("command", "path", "pattern", "query", "url", "file", "skill", "agent", "description"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _fmt_ts(ts: datetime) -> str:
    return ts.isoformat()


def _parse_result_text(payload: dict) -> str:
    content = str(payload.get("content") or "")
    output = payload.get("output")
    if output is not None and content == "":
        content = str(output)
    return content


def _tool_call_name(event: RunEvent, payload: dict) -> str:
    tc = payload.get("tool_call") or {}
    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
    name = fn.get("name") if isinstance(fn, dict) else None
    return str(name or payload.get("tool") or "?")


def _event_base(event: RunEvent, payload: dict, ev_type: str, name: str, summary: str, *, step: TaskStep | None = None, status: str | None = None) -> dict:
    out = {
        "seq": event.seq,
        "ts": _fmt_ts(event.ts),
        "type": ev_type,
        "name": name,
        "summary": summary,
        "status": status,
        "duration_ms": None,
        "input": None,
        "output": None,
        "raw": {"kind": event.kind, "payload": payload},
        "step_id": event.step_id,
        "step_position": step.position if step else None,
        "step_robot": step.robot.name if step and step.robot else None,
        "step_role": (step.robot.role if step and step.robot else None),
    }
    return out


def derive_task_timeline(session: Session, task: Task) -> list[dict]:
    """Constrói a timeline cronológica da task a partir dos RunEvent de todas as fases."""
    steps = {st.id: st for st in task.steps}

    events = (
        session.query(RunEvent)
        .join(TaskStep, RunEvent.step_id == TaskStep.id)
        .filter(TaskStep.task_id == task.id)
        .order_by(RunEvent.ts, RunEvent.seq)
        .all()
    )

    timeline: list[dict] = []
    # tool_call pendente por step (para parear com o tool_result seguinte).
    pending_calls: dict[int, dict] = {}
    first_step_ts: datetime | None = None
    last_step_ts: datetime | None = None

    # Sincroniza o relógio de fases: usa o primeiro tool_call/assistant_text como
    # início "real" do desenvolvimento.
    for ev in events:
        if first_step_ts is None or ev.ts < first_step_ts:
            first_step_ts = ev.ts
        if last_step_ts is None or ev.ts > last_step_ts:
            last_step_ts = ev.ts

    for ev in events:
        payload = dict(ev.payload or {})
        step = steps.get(ev.step_id)
        kind = ev.kind
        if kind in _SKIP_KINDS:
            continue

        if kind == "attempt_started":
            attempt = payload.get("attempt")
            robot = payload.get("robot") or (step.robot.name if step and step.robot else "?")
            pos = step.position if step else "?"
            is_rerun = isinstance(attempt, int) and attempt > 1
            timeline.append(_event_base(
                ev, payload,
                EV_PHASE,
                "etapa iniciada" if not is_rerun else f"re-execução da fase {pos}",
                (
                    f"↺ re-execução da fase {pos} ({robot}) — tentativa {attempt}"
                    if is_rerun
                    else f"fase {pos} ({robot}) iniciada"
                ),
                step=step, status="running",
            ))
            continue

        if kind == "assistant_text":
            content = str(payload.get("content") or "")
            first_line = next((l for l in content.splitlines() if l.strip()), "")[:160]
            timeline.append(_event_base(
                ev, payload, EV_TEXT, "resposta do agente",
                first_line or "(resposta sem texto)",
                step=step,
            ))
            continue

        if kind == "tool_call":
            name = _tool_call_name(ev, payload)
            args_raw = (payload.get("tool_call") or {}).get("function", {}).get("arguments") if isinstance(payload.get("tool_call"), dict) else None
            args = _parse_args(args_raw) if isinstance(args_raw, str) else None
            if args is None and isinstance(payload.get("input"), dict):
                args = payload["input"]
            target = _tool_target(args)
            label = _TOOL_LABELS.get(name, name)
            summary = f"{label}: {target}" if target else label
            entry = _event_base(
                ev, payload, EV_TOOL_CALL, name, summary,
                step=step, status="running",
            )
            entry["input"] = args
            pending_calls[ev.step_id] = entry
            timeline.append(entry)
            continue

        if kind == "tool_result":
            pending = pending_calls.pop(ev.step_id, None)
            if pending is not None:
                pending["status"] = "error" if str(payload.get("error") or "").strip() else "completed"
                content = _parse_result_text(payload)
                if payload.get("error"):
                    pending["output"] = {"error": str(payload["error"]), "content": content}
                else:
                    pending["output"] = {"content": content}
                if isinstance(pending["ts"], str):
                    start = datetime.fromisoformat(pending["ts"])
                    delta = int((ev.ts - start).total_seconds() * 1000)
                    pending["duration_ms"] = max(delta, 0)
                    pending["ts"] = _fmt_ts(start)
                continue
            # tool_result sem tool_call pareado: evento próprio
            timeline.append(_event_base(
                ev, payload, EV_TOOL_CALL, "resultado",
                "resultado de tool call (sem chamada pareada)",
                step=step, status="completed",
            ))
            continue

        if kind == "guardrail_blocked":
            timeline.append(_event_base(
                ev, payload, EV_BLOCK, "guardrail bloqueou",
                f"⛔ guardrail: {payload.get('detail') or payload.get('pattern') or 'comando bloqueado'}",
                step=step, status="blocked",
            ))
            continue

        if kind == "task_blocked":
            timeline.append(_event_base(
                ev, payload, EV_BLOCK, "desenvolvimento bloqueado",
                f"Bloqueado aguardando instrução — {payload.get('reason') or ''}",
                step=step, status="blocked",
            ))
            continue

        if kind == "user_intervention":
            instruction = str(payload.get("instruction") or "")
            timeline.append(_event_base(
                ev, payload, EV_USER, "intervenção do usuário",
                f"👤 \"{instruction[:200]}\"",
                step=step,
            ))
            continue

        if kind == "execution_resumed":
            timeline.append(_event_base(
                ev, payload, EV_USER, "execução retomada",
                "▶ execução retomada após intervenção do usuário",
                step=step,
            ))
            continue

        if kind == "human_gate_approved":
            timeline.append(_event_base(
                ev, payload, EV_USER, "aprovação humana",
                f"👤 fase {payload.get('position') or '?'} aprovada pelo usuário",
                step=step,
            ))
            continue

        if kind == "phase_done":
            nxt = payload.get("next")
            subs = " (subtarefas)" if payload.get("subtasks") else ""
            pos = step.position if step else payload.get("_position", "?")
            timeline.append(_event_base(
                ev, payload, EV_PHASE_DONE, "etapa concluída",
                f"✓ etapa {pos} concluída{subs}"
                + (f" → próxima {nxt}" if nxt is not None else " → desenvolvimento concluído"),
                step=step, status="completed",
            ))
            continue

        if kind == "bounce_back":
            timeline.append(_event_base(
                ev, payload, EV_WARNING, "bounce-back",
                f"↩️ fase {payload.get('from_position') or '?'} reprovou — a fase anterior volta para corrigir: {payload.get('reason') or ''}",
                step=step, status="warning",
            ))
            continue

        if kind == "subtask_bounce_back":
            timeline.append(_event_base(
                ev, payload, EV_WARNING, "bounce-back de tarefas",
                f"↩️ subtarefas {payload.get('positions') or '?'} reprovadas — voltam para implement",
                step=step, status="warning",
            ))
            continue

        if kind == "merged":
            timeline.append(_event_base(
                ev, payload, EV_PHASE_DONE, "merge realizado",
                f"🔀 merge + push na default concluído: {payload.get('detail') or ''}",
                step=step, status="completed",
            ))
            continue

        if kind == "merge_failed":
            timeline.append(_event_base(
                ev, payload, EV_ERROR, "merge falhou",
                f"⚠️ merge falhou: {payload.get('detail') or ''}",
                step=step, status="error",
            ))
            continue

        if kind == "budget_hit":
            timeline.append(_event_base(
                ev, payload, EV_ERROR, "orçamento estourado",
                f"💰 orçamento estourado: {payload.get('reason') or ''}",
                step=step, status="error",
            ))
            continue

        if kind == "post_merge_failed":
            timeline.append(_event_base(
                ev, payload, EV_ERROR, "falha pós-merge",
                f"⚠️ falha pós-merge (código já integrado): {payload.get('reason') or ''}",
                step=step, status="error",
            ))
            continue

        if kind == "arch_metric":
            timeline.append(_event_base(
                ev, payload, EV_SYSTEM, "métrica de arquitetura",
                f"📐 arquitetura {payload.get('level') or '?'} ({payload.get('score') or '?'}/100)",
                step=step,
            ))
            continue

        if kind in ("subtask_start", "subtask_implemented", "subtask_verified", "subtask_failed", "subtask_marked_done"):
            position = int(payload.get("position", -1)) + 1
            title = str(payload.get("title") or "?")
            phase = "implementação" if kind == "subtask_implemented" else (
                "verificação" if kind == "subtask_verified" else "execução")
            if kind == "subtask_start":
                summary = f"tarefa {position}: {title} iniciada ({phase})"
                name = "tarefa iniciada"
            elif kind == "subtask_implemented":
                summary = f"tarefa {position}: {title} implementada"
                name = "tarefa implementada"
            elif kind == "subtask_verified":
                summary = f"✓ tarefa {position}: {title} concluída (PASS)"
                name = "tarefa concluída"
            elif kind == "subtask_marked_done":
                summary = f"tarefa {position}: {title} marcada como implementada (código já presente)"
                name = "tarefa implementada"
            else:
                summary = f"tarefa {position}: {title} falhou — {payload.get('reason') or ''}"
                name = "tarefa falhou"
            timeline.append(_event_base(
                ev, payload, EV_TASK_EVENT, name, summary,
                step=step, status="completed" if kind != "subtask_failed" else "error",
            ))
            continue

        if kind == "pm_decision":
            timeline.append(_event_base(
                ev, payload, EV_SYSTEM, "decisão do PM",
                f"🤖 PM decidiu: {payload.get('action') or '?'} — {payload.get('reason') or ''}",
                step=step,
            ))
            continue

        if kind == "subtasks_generated":
            titles = ", ".join(str(t) for t in (payload.get("titles") or []))
            timeline.append(_event_base(
                ev, payload, EV_TASK_EVENT, "tarefas propostas pelo agente",
                f"🧩 {payload.get('count') or 0} tarefa(s) proposta(s): {titles}",
                step=step,
            ))
            continue

        if kind == "task_paused":
            timeline.append(_event_base(ev, payload, EV_USER, "tarefa pausada", "⏸ tarefa pausada", step=step))
            continue
        if kind == "task_resumed":
            timeline.append(_event_base(ev, payload, EV_USER, "tarefa retomada", "▶ tarefa retomada", step=step))
            continue
        if kind == "task_cancelled":
            timeline.append(_event_base(ev, payload, EV_USER, "tarefa cancelada", "✕ tarefa cancelada — pipeline encerrado", step=step, status="error"))
            continue

        # Evento genérico do worker (worker_recovered, summary_generated, etc.)
        timeline.append(_event_base(
            ev, payload, EV_SYSTEM, kind,
            f"{kind}: {str(payload)[:140]}",
            step=step,
        ))

    # Início/fim do desenvolvimento: com base na primeira/última fase executada.
    if first_step_ts is not None:
        timeline.insert(0, {
            "seq": -1,
            "ts": _fmt_ts(first_step_ts),
            "type": EV_DEV_START,
            "name": "desenvolvimento iniciado",
            "summary": "Desenvolvimento iniciado",
            "status": "completed",
            "duration_ms": None,
            "input": None,
            "output": None,
            "raw": {"kind": "development_started", "payload": {}},
            "step_id": None,
            "step_position": None,
            "step_robot": None,
            "step_role": None,
        })

    # Se a task terminou (done/failed/cancelled), fecha a timeline.
    if task.status in ("done", "failed", "cancelled") and last_step_ts is not None:
        timeline.append({
            "seq": 10**9,
            "ts": _fmt_ts(last_step_ts),
            "type": EV_DEV_DONE,
            "name": "desenvolvimento concluído" if task.status == "done" else f"desenvolvimento {task.status}",
            "summary": (
                "✓ Desenvolvimento concluído" if task.status == "done"
                else f"Desenvolvimento terminou com status {task.status}"
            ),
            "status": "completed" if task.status == "done" else "error",
            "duration_ms": None,
            "input": None,
            "output": None,
            "raw": {"kind": "development_finished", "payload": {"status": task.status}},
            "step_id": None,
            "step_position": None,
            "step_robot": None,
            "step_role": None,
        })

    # Ordena por ts (e seq) — tool_calls pareadas preservam a ordem original.
    return sorted(timeline, key=lambda e: (e["ts"], e["seq"]))


# Kinds que encerram uma ocorrência de fase com estado terminal.
_TERMINAL_DONE = {"phase_done", "merged"}
_TERMINAL_BLOCK = {"task_blocked", "guardrail_blocked"}
_TERMINAL_FAIL = {
    "timeout", "exec_exit", "verdict", "git_error", "merge_error",
    "merge_failed", "post_merge_failed", "budget_hit",
}

# Tipos de evento que representam "atividade do sistema" (marcos discretos) dentro
# de uma ocorrência de fase — o resto (text, tool_call) é o trabalho técnico.
_SYSTEM_ACTIVITY_TYPES = ("system", "task", "warning", "user", "error", "blocked")


def _stop_reason(kind: str, payload: dict) -> str:
    """Motivo determinístico da parada de uma ocorrência de fase."""
    if kind == "task_blocked":
        return str(payload.get("reason") or payload.get("question") or "")
    if kind == "guardrail_blocked":
        return str(payload.get("detail") or payload.get("pattern") or "")
    return str(payload.get("reason") or "")


def _new_occurrence(step: TaskStep, attempt: int) -> dict:
    return {
        "step_id": step.id,
        "position": step.position,
        "robot": {"name": step.robot.name, "role": step.robot.role} if step.robot else None,
        "attempt": attempt,
        "status": "pending",
        "goal": step.goal,
        "started_at": None,
        "finished_at": None,
        "last_activity": None,
        "delivered_text": None,
        "stop": None,
        "system_activity": [],
        "events": [],
    }


def _finalize_occurrence(occ: dict, step: TaskStep | None) -> None:
    events = sorted(occ["events"], key=lambda e: (e["ts"], e["seq"]))
    occ["events"] = events
    if events:
        occ["started_at"] = events[0]["ts"]
        occ["finished_at"] = events[-1]["ts"]

    last_terminal = None
    for ev in events:
        kind = ev["raw"]["kind"]
        if kind in _TERMINAL_DONE:
            last_terminal = ("done", kind, ev["raw"]["payload"])
        elif kind in _TERMINAL_BLOCK:
            last_terminal = ("blocked", kind, ev["raw"]["payload"])
        elif kind in _TERMINAL_FAIL:
            last_terminal = ("failed", kind, ev["raw"]["payload"])
        if ev["type"] == "tool_call":
            occ["last_activity"] = ev["summary"]
        if ev["type"] == "text":
            occ["delivered_text"] = str(ev["raw"]["payload"].get("content") or "")
        if ev["type"] in _SYSTEM_ACTIVITY_TYPES:
            occ["system_activity"].append({
                "ts": ev["ts"],
                "type": ev["type"],
                "name": ev["name"],
                "summary": ev["summary"],
                "status": ev["status"],
            })

    if last_terminal is not None:
        occ["status"], kind, payload = last_terminal
        if occ["status"] != "done":
            occ["stop"] = {"kind": kind, "reason": _stop_reason(kind, payload)}
        return
    if step is not None and step.attempt > occ["attempt"]:
        # Execução MAIS ANTIGA que não terminou (ex.: restart do worker no meio):
        # sem veredicto/merge — o histórico continua, mas esta tentativa foi interrompida.
        occ["status"] = "interrupted"
        return
    st = step.status if step else "pending"
    occ["status"] = "blocked" if st == STEP_GUARDRAIL_BLOCKED else st


def derive_task_occurrences(session: Session, task: Task) -> list[dict]:
    """Deriva as OCOrrências de fase ("execuções") da task a partir da timeline.

    Cada fase vira UMA ocorrência por execução/tentativa, na ordem cronológica
    (da mais antiga para a mais nova). Um evento `attempt_started` com `attempt>1`
    (ou manual retry/bounce-back) marca uma NOVA ocorrência da mesma fase — o
    histórico anterior nunca é apagado. Determinístico, sem LLM.
    """
    steps = {st.id: st for st in task.steps}
    tl = derive_task_timeline(session, task)

    groups: dict[tuple[int, int], dict] = {}
    current_key: dict[int, tuple[int, int]] = {}
    order: list[tuple[int, int]] = []

    for ev in tl:
        raw_kind = ev["raw"]["kind"]
        step_id = ev["step_id"]
        if step_id is None:
            continue
        step = steps.get(step_id)
        if step is None:
            continue
        if raw_kind == "attempt_started":
            payload = ev["raw"]["payload"]
            try:
                attempt = max(int(payload.get("attempt") or 1), 1)
            except (TypeError, ValueError):
                attempt = 1
            key = (step_id, attempt)
            if key not in groups:
                groups[key] = _new_occurrence(step, attempt)
                order.append(key)
            current_key[step_id] = key
        else:
            key = current_key.get(step_id)
            if key is None or key not in groups:
                # Eventos ancorados na fase ANTES do primeiro `attempt_started`
                # (ex.: `summary_generated`/`pm_decision` no último step da task,
                # `task_paused`, `worker_recovered`) são de nível da TASK, não de
                # uma execução real — NÃO criam ocorrência nem deslocam a posição
                # cronológica da fase.
                continue
            groups[key]["events"].append(ev)

    for key in order:
        _finalize_occurrence(groups[key], steps.get(key[0]))
    return [groups[key] for key in order]


def timeline_summary_text(timeline: list[dict]) -> str:
    """Texto compacto da timeline para alimentar o prompt do resumo (sem truncar)."""
    lines = []
    for ev in timeline:
        stamp = ev["ts"][11:19] if isinstance(ev["ts"], str) and len(ev["ts"]) >= 19 else str(ev["ts"])
        lines.append(f"{stamp} [{ev['type']}] {ev['summary']}")
    return "\n".join(lines)
