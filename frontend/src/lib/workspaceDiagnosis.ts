import type { SubTask, Task, TaskStep, TimelineEvent, Workspace, WorkspaceOccurrence } from "../types";

export interface SubtaskFailure {
  occurrence: WorkspaceOccurrence;
  event: TimelineEvent;
}

export interface SubtaskDiagnosis {
  subtask: SubTask;
  failure: SubtaskFailure | null;
  needsAttention: boolean;
  recovering: boolean;
  inconclusive: boolean;
  reason: string | null;
  report: string | null;
  reportSource: string | null;
  agentResponse: string | null;
}

const SUBTASK_EVENTS = new Set([
  "subtask_start", "subtask_failed", "subtask_verified", "subtask_implemented", "subtask_marked_done",
]);

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/** Relatórios pertencem a uma execução, nunca à subtarefa vizinha. */
function failureEvidence(failure: SubtaskFailure | null) {
  if (!failure) return { report: null, agentResponse: null };
  const events = failure.occurrence.events;
  const end = events.findIndex((event) => event.seq === failure.event.seq);
  const position = failure.event.raw.payload.position;
  let start = end;
  for (let i = end - 1; i >= 0; i--) {
    if (events[i].raw.kind === "subtask_start") {
      if (events[i].raw.payload.position === position) start = i;
      break;
    }
  }
  let report = text(failure.event.raw.payload.detail);
  let agentResponse: string | null = null;
  for (const event of events.slice(start, end + 1)) {
    const input = event.input;
    const path = String(input?.path ?? input?.file_path ?? input?.filePath ?? "");
    if (path.split(/[\\/]/).pop() === "autoia_verdict.txt") {
      report = text(input?.content) ?? report;
    }
    if (event.raw.kind === "assistant_text") {
      agentResponse = text(event.raw.payload.content) ?? agentResponse;
    }
  }
  return { report, agentResponse };
}

export function diagnoseSubtasks(ws: Workspace): SubtaskDiagnosis[] {
  const terminal = ["done", "cancelled"].includes(ws.task.status);
  const events = ws.occurrences.flatMap((occurrence) =>
    occurrence.events
      .filter((event) => SUBTASK_EVENTS.has(event.raw.kind))
      .map((event) => ({ occurrence, event })),
  ).sort((a, b) => a.event.ts.localeCompare(b.event.ts) || a.event.seq - b.event.seq);

  return [...ws.task.subtasks].sort((a, b) => a.position - b.position).map((subtask) => {
    const history = events.filter(({ event }) => event.raw.payload.position === subtask.position);
    const latest = history[history.length - 1];
    const failure = [...history].reverse().find(({ event }) => event.raw.kind === "subtask_failed") ?? null;
    const latestFailed = latest?.event.raw.kind === "subtask_failed";
    // FAIL pode sobreviver a uma implementação bem-sucedida: o estado e evento
    // mais recentes prevalecem para não reapresentar uma falha antiga.
    const settled = subtask.status === "done" || subtask.status === "implemented";
    const running = ["implementing", "verifying"].includes(subtask.status);
    const needsAttention = !terminal && !settled && !running && (
      subtask.status === "failed" || !!subtask.error || latestFailed ||
      (!latest && ["FAIL", "AUSENTE"].includes(subtask.verdict ?? ""))
    );
    const recovering = !terminal && subtask.status !== "done" && !!failure && !needsAttention;
    const currentFailure = needsAttention && (!latest || latestFailed);
    const reason = currentFailure
      ? subtask.error || text(failure?.event.raw.payload.reason)
      : text(failure?.event.raw.payload.reason);
    const evidence = failureEvidence(failure);
    const verifying = failure ? failure.event.raw.payload.phase === "verify" : ["FAIL", "AUSENTE"].includes(subtask.verdict ?? "");
    const inconclusive = currentFailure && verifying && (
      failure?.event.raw.payload.class === "inconclusive" ||
      /inconclusive|ausente|timeout|sem progresso|saiu com código/i.test(reason ?? "")
    );
    // A implementação sobrescreve summary. Ele só representa o relatório de
    // falha enquanto a reprovação ainda é o estado atual da subtarefa.
    const validationReport = failure
      ? failure.event.raw.payload.phase === "verify" && /veredicto/i.test(String(failure.event.raw.payload.reason ?? ""))
      : ["FAIL", "AUSENTE"].includes(subtask.verdict ?? "");
    const savedReport = currentFailure && validationReport ? text(subtask.summary) : null;
    const report = evidence.report ?? savedReport;
    return {
      subtask, failure, needsAttention, recovering, inconclusive, reason, report,
      reportSource: evidence.report
        ? "Relatório registrado nesta execução"
        : savedReport ? "Relatório salvo na subtarefa" : null,
      agentResponse: evidence.agentResponse,
    };
  });
}

export function currentFailureOccurrence(ws: Workspace): WorkspaceOccurrence | null {
  if (!["blocked", "failed", "needs_review", "paused", "waiting_approval"].includes(ws.task.status)) return null;
  const latestByStep = new Map<number, WorkspaceOccurrence>();
  for (const occurrence of ws.occurrences) {
    if ((latestByStep.get(occurrence.step_id)?.run ?? 0) <= occurrence.run) {
      latestByStep.set(occurrence.step_id, occurrence);
    }
  }
  return [...latestByStep.values()]
    .filter((occurrence) => ["failed", "blocked", "guardrail_blocked"].includes(occurrence.status))
    .filter((occurrence) => ws.task.steps.some((step) =>
      step.id === occurrence.step_id && ["failed", "blocked", "guardrail_blocked"].includes(step.status),
    ))
    .sort((a, b) => (b.finished_at ?? b.events[b.events.length - 1]?.ts ?? b.started_at ?? "")
      .localeCompare(a.finished_at ?? a.events[a.events.length - 1]?.ts ?? a.started_at ?? ""))[0] ?? null;
}

export function suggestedCorrectionStep(task: Task, diagnosis: SubtaskDiagnosis): TaskStep | null {
  const failure = diagnosis.failure?.occurrence;
  const role = diagnosis.inconclusive ? "verify" : "implement";
  const candidates = task.steps.filter((step) => step.robot?.role === role &&
    (failure == null || step.position <= failure.position));
  return candidates.sort((a, b) => b.position - a.position)[0] ?? null;
}
