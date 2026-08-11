import type { RunEvent, Task, TaskProposal } from "../types";
import { diffSummary } from "./tasks";

/** Constrói a conversa (timeline-chat) de uma task a partir do TaskOut + eventos
 *  de cada fase. Sem mudança de schema: o worker é estritamente sequencial, então
 *  a ordem global dos eventos (RunEvent.id) reproduz a cronologia real.
 *
 *  - mensagem 0: 📋 a tarefa (descrição + critérios de aceite + propostas);
 *  - por fase, um turno por tentativa (conteúdo = resumo da última ou o último
 *    `assistant_text` do intervalo);
 *  - turnos system inline: bounce_back, merged/merge_failed, guardrail_blocked,
 *    budget_hit, subtasks_generated, task_spawned, pm_decision, human_gate, etc.
 */

export interface TaskTurn {
  id: string;
  kind: "task";
  title: string;
  description: string;
  acceptanceCriteria: string | null;
  proposals: TaskProposal[];
}

export interface PhaseTurn {
  id: string;
  kind: "phase";
  stepId: number;
  position: number;
  robotName: string;
  robotRole: string;
  attempt: number;
  verdict: string | null;
  summary: string;
  error: string | null;
  diffStat: string | null;
  diffSummaryText: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  postMerge: boolean;
  status: string;
  running: boolean;
  cost: number;
}

export interface SystemTurn {
  id: string;
  kind: "system";
  type: string;
  text: string;
  payload: Record<string, unknown>;
}

export type ChatTurn = TaskTurn | PhaseTurn | SystemTurn;

const SYSTEM_KINDS = new Set([
  "bounce_back",
  "merged",
  "merge_failed",
  "guardrail_blocked",
  "budget_hit",
  "subtasks_generated",
  "task_spawned",
  "pm_decision",
  "pm_skip",
  "human_gate",
  "human_gate_approved",
  "proposal_accepted",
  "proposal_rejected",
  "post_merge_failed",
  "task_paused",
  "task_resumed",
  "task_cancelled",
  "subtask_bounce_back",
  "subtask_marked_done",
]);

function systemTurnFromEvent(event: RunEvent): SystemTurn | null {
  if (!SYSTEM_KINDS.has(event.kind)) return null;
  const payload = event.payload as Record<string, unknown>;
  let text = "";
  switch (event.kind) {
    case "bounce_back":
      text = `↩️ fase ${String(payload.from_position ?? "?")} reprovou — a fase anterior volta para corrigir: ${String(payload.reason ?? "")}`;
      break;
    case "merged":
      text = `🔀 merge + push na main concluído: ${String(payload.detail ?? "")}`;
      break;
    case "merge_failed":
      text = `⚠️ merge falhou: ${String(payload.detail ?? "")}`;
      break;
    case "guardrail_blocked":
      text = `⛔ guardrail bloqueou a execução: ${String(payload.detail ?? payload.pattern ?? "")}`;
      break;
    case "budget_hit":
      text = `💰 orçamento estourado: ${String(payload.reason ?? "")}`;
      break;
    case "subtasks_generated":
      text = `🧩 ${String(payload.count ?? 0)} subtarefa(s) gerada(s): ${String((payload.titles as string[] ?? []).join(", "))}`;
      break;
    case "task_spawned":
      text = `🧩 ${String(payload.count ?? 0)} proposta(s) de task criada(s) — aguardando aprovação humana`;
      break;
    case "pm_decision":
      text = `🤖 PM decidiu: ${String(payload.action ?? "")} — ${String(payload.reason ?? "")}`;
      break;
    case "pm_skip":
      text = `🤖 PM ignorado: ${String(payload.reason ?? "")}`;
      break;
    case "human_gate":
      text = `⏸ parada para aprovação humana antes da fase ${String(payload.position ?? "?")} · ${String(payload.robot ?? "")}`;
      break;
    case "human_gate_approved":
      text = `👤 humano aprovou a fase ${String(payload.position ?? "?")}`;
      break;
    case "proposal_accepted":
      text = `✅ proposta "${String(payload.title ?? "")}" aceita — task #${String(payload.child_task_id ?? "?")} criada`;
      break;
    case "proposal_rejected":
      text = `✕ proposta "${String(payload.title ?? "")}" rejeitada`;
      break;
    case "post_merge_failed":
      text = `⚠️ falha pós-merge (código já integrado — decisão do PM/humano): ${String(payload.reason ?? "")}`;
      break;
    case "task_paused":
      text = "⏸ tarefa pausada — o pipeline não avança até retomar";
      break;
    case "task_resumed":
      text = "▶️ tarefa retomada";
      break;
    case "task_cancelled":
      text = "✕ tarefa cancelada — pipeline encerrado";
      break;
    case "subtask_bounce_back":
      text = `↩️ subtarefas reprovadas (${String(payload.positions ?? "")}) — voltam para implement`;
      break;
    case "subtask_marked_done":
      text = `✅ subtarefa ${Number(payload.position ?? -1) + 1} marcada como implementada pelo agente: ${String(payload.title ?? "")}`;
      break;
    default:
      text = `${event.kind}: ${JSON.stringify(payload)}`;
  }
  return {
    id: `sys-${event.id}`,
    kind: "system",
    type: event.kind,
    text,
    payload,
  };
}

interface TurnItem {
  order: number;
  tie: number; // 0 = turno de fase, 1 = sistema (empate: fase primeiro)
  turn: ChatTurn;
}

export function buildTurns(task: Task, eventsByStep: Record<number, RunEvent[]>): ChatTurn[] {
  const items: TurnItem[] = [];

  items.push({
    order: 0,
    tie: 0,
    turn: {
      id: "task",
      kind: "task",
      title: task.title,
      description: task.description,
      acceptanceCriteria: task.acceptance_criteria,
      proposals: task.proposals ?? [],
    },
  });

  const steps = [...(task.steps ?? [])].sort((a, b) => a.position - b.position);

  for (const step of steps) {
    const events = (eventsByStep[step.id] ?? []).slice().sort((a, b) => a.seq - b.seq);
    if (events.length === 0 && !step.summary) continue;

    // Separa em tentativas usando `attempt_started` como marcador de início.
    const attempts: RunEvent[][] = [];
    let current: RunEvent[] = [];
    for (const ev of events) {
      if (ev.kind === "attempt_started") {
        if (current.length > 0) attempts.push(current);
        current = [ev];
      } else {
        current.push(ev);
      }
    }
    if (current.length > 0) attempts.push(current);
    if (attempts.length === 0) attempts.push(events);

    // Turnos system inline desta fase, ordenados globalmente pelo id do evento.
    for (const ev of events) {
      const sys = systemTurnFromEvent(ev);
      if (sys) items.push({ order: ev.id, tie: 1, turn: sys });
    }

    attempts.forEach((group, i) => {
      const isLast = i === attempts.length - 1;
      const attemptStart = group.find((e) => e.kind === "attempt_started");
      const attemptNum =
        (attemptStart?.payload as { attempt?: number })?.attempt ?? i + 1;

      // Conteúdo: na última tentativa prefere o relatório completo (step.summary);
      // senão, o último `assistant_text` do intervalo.
      let content = "";
      if (isLast && step.summary) {
        content = step.summary;
      } else {
        const lastAssistant = [...group].reverse().find((e) => e.kind === "assistant_text");
        content = String((lastAssistant?.payload as { content?: unknown })?.content ?? "");
      }

      const maxOrder =
        group.length > 0 ? Math.max(...group.map((e) => e.id)) : undefined;
      const cost = group.reduce((acc, e) => acc + (e.cost ?? 0), 0);

      items.push({
        // Fases sem eventos (dados legados com summary): caem no fim, em ordem.
        order: maxOrder ?? Number.MAX_SAFE_INTEGER - step.position,
        tie: 0,
        turn: {
          id: `phase-${step.id}-${attemptNum}`,
          kind: "phase",
          stepId: step.id,
          position: step.position,
          robotName: step.robot?.name ?? "?",
          robotRole: step.robot?.role ?? "?",
          attempt: attemptNum,
          verdict: isLast ? step.verdict : null,
          summary: content,
          error: isLast ? step.error : null,
          diffStat: isLast ? step.diff_stat : null,
          diffSummaryText: isLast ? diffSummary(step.diff_stat) : null,
          startedAt: step.started_at,
          finishedAt: step.finished_at,
          postMerge: step.post_merge,
          status: isLast ? step.status : "done",
          running: step.status === "running",
          cost,
        },
      });
    });
  }

  return [...items]
    .sort((a, b) => a.order - b.order || a.tie - b.tie)
    .map((item) => item.turn);
}
