import assert from "node:assert/strict";
import test from "node:test";
import { currentFailureOccurrence, diagnoseSubtasks, suggestedCorrectionStep } from "../src/lib/workspaceDiagnosis.ts";
import type { SubTask, TaskStep, TimelineEvent, Workspace, WorkspaceOccurrence } from "../src/types.ts";

const report = `FAIL\n\nO formulário aceita um valor inválido.\n${"Evidência completa, sem cortes.\n".repeat(200)}Última linha preservada.`;

function event(seq: number, kind: string, payload: Record<string, unknown>, input: Record<string, unknown> | null = null): TimelineEvent {
  return { seq, ts: `2026-09-18T12:00:${String(seq).padStart(2, "0")}`, type: kind === "assistant_text" ? "text" : "task", name: kind, summary: kind, status: null, duration_ms: null, input, output: null, raw: { kind, payload }, step_id: 2, step_position: 1, step_robot: "validador", step_role: "verify" };
}

function occurrence(events: TimelineEvent[], patch: Partial<WorkspaceOccurrence> = {}): WorkspaceOccurrence {
  return { step_id: 2, position: 1, robot: { name: "validador", role: "verify" }, attempt: 1, run: 1, is_rerun: false, status: "failed", goal: null, mission: null, mission_source: null, started_at: null, finished_at: null, duration_ms: null, cost: 0, last_activity: null, delivered_text: null, delivered: null, stop: { kind: "subtask_failed", reason: "veredicto FAIL" }, proposals: [], files: [], file_count: 0, tests: null, system_activity: [], events, branch: null, ...patch };
}

function subtask(patch: Partial<SubTask> = {}): SubTask {
  return { id: 10, position: 0, title: "Validar o formulário", description: "Escopo", acceptance_criteria: "Rejeitar valor inválido", status: "pending", attempt: 1, summary: report, verdict: null, error: "veredicto FAIL", started_at: null, finished_at: null, ...patch };
}

function workspace(subtasks = [subtask()], occurrences = [occurrence([
  event(1, "subtask_start", { position: 0, phase: "verify" }),
  event(2, "subtask_failed", { position: 0, phase: "verify", reason: "veredicto FAIL", class: "fail" }),
])]): Workspace {
  return {
    task: { id: 1, status: "needs_review", subtasks, steps: [
      { id: 1, position: 0, robot: { role: "implement", name: "developer" }, status: "done" },
      { id: 2, position: 1, robot: { role: "verify", name: "validador" }, status: "failed" },
    ] as TaskStep[] }, occurrences,
  } as Workspace;
}

test("validação reprovada e devolvida a pending mantém o relatório completo e sugere implementação", () => {
  const ws = workspace();
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.needsAttention, true);
  assert.equal(diagnosis.report, report);
  assert.equal(diagnosis.reportSource, "Relatório salvo na subtarefa");
  assert.equal(diagnosis.failure?.occurrence.position, 1);
  assert.equal(suggestedCorrectionStep(ws.task, diagnosis)?.position, 0);
});

test("nova implementação não transforma FAIL antigo em falha atual nem usa seu resumo como relatório", () => {
  const ws = workspace([subtask({ status: "implemented", verdict: "FAIL", error: null, summary: "Código corrigido" })]);
  ws.task.status = "in_progress";
  ws.occurrences.push(occurrence([event(3, "subtask_implemented", { position: 0 })], { step_id: 1, position: 0, robot: { name: "developer", role: "implement" }, status: "done" }));
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.needsAttention, false);
  assert.equal(diagnosis.recovering, true);
  assert.equal(diagnosis.report, null);
  assert.equal(currentFailureOccurrence(ws), null);
});

test("subtarefa em correção não é apresentada como falha atual mesmo com error ainda salvo", () => {
  const ws = workspace([subtask({ status: "implementing" })]);
  ws.task.status = "in_progress";
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.needsAttention, false);
  assert.equal(diagnosis.recovering, true);
});

test("PASS posterior preserva histórico sem exigir correção", () => {
  const ws = workspace([subtask({ status: "done", verdict: "PASS", error: null })]);
  ws.occurrences.push(occurrence([event(4, "subtask_verified", { position: 0, verdict: "PASS" })], { run: 2, status: "done" }));
  ws.task.steps[1].status = "done";
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.needsAttention, false);
  assert.equal(diagnosis.recovering, false);
  assert.equal(diagnosis.failure?.event.seq, 2);
  assert.equal(currentFailureOccurrence(ws), null);
});

test("task concluída ou cancelada não mostra falha residual como pendência atual", () => {
  const ws = workspace();
  for (const status of ["done", "cancelled"]) {
    ws.task.status = status;
    assert.equal(diagnoseSubtasks(ws)[0].needsAttention, false);
    assert.equal(currentFailureOccurrence(ws), null);
  }
});

test("última execução prevalece mesmo quando attempt repete e a lista chega fora de ordem", () => {
  const ws = workspace();
  const oldFailure = ws.occurrences[0];
  const newSuccess = occurrence([event(5, "subtask_verified", { position: 0 })], { run: 2, attempt: 1, status: "done" });
  ws.occurrences = [newSuccess, oldFailure];
  assert.equal(currentFailureOccurrence(ws), null);
  ws.occurrences = [oldFailure];
  assert.equal(currentFailureOccurrence(ws)?.run, 1);
});

test("relatório e resposta são associados à subtarefa correta e preservados no histórico", () => {
  const events = [
    event(1, "subtask_start", { position: 0, phase: "verify" }),
    event(2, "tool_call", {}, { path: "/checkout/autoia_verdict.txt", content: report }),
    event(3, "assistant_text", { content: "Resposta integral da primeira subtarefa" }),
    event(4, "subtask_failed", { position: 0, phase: "verify", reason: "veredicto FAIL" }),
    event(5, "subtask_start", { position: 1, phase: "verify" }),
    event(6, "tool_call", {}, { filePath: "autoia_verdict.txt", content: "FAIL\nRelatório da segunda subtarefa" }),
    event(7, "subtask_failed", { position: 1, phase: "verify", reason: "veredicto FAIL" }),
  ];
  const ws = workspace([subtask({ status: "done", summary: "Resumo novo", error: null }), subtask({ id: 11, position: 1 })], [occurrence(events)]);
  const [first, second] = diagnoseSubtasks(ws);
  assert.equal(first.report, report);
  assert.equal(first.agentResponse, "Resposta integral da primeira subtarefa");
  assert.equal(first.needsAttention, false);
  assert.equal(second.report, "FAIL\nRelatório da segunda subtarefa");
  assert.equal(second.agentResponse, null);
});

test("validação inconclusiva sugere repetir validação sem atribuir falha ao código", () => {
  const ws = workspace([subtask({ error: "inconclusive: timeout", summary: "Implementação entregue anteriormente" })], [occurrence([
    event(1, "subtask_failed", { position: 0, phase: "verify", reason: "timeout", class: "inconclusive" }),
  ])]);
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.inconclusive, true);
  assert.equal(diagnosis.report, null);
  assert.equal(suggestedCorrectionStep(ws.task, diagnosis)?.position, 1);
});

test("falha de implementação não apresenta um resumo antigo como relatório dessa falha", () => {
  const ws = workspace([subtask({ status: "failed", error: "codex saiu com código 1", summary: "Entrega anterior" })], [occurrence([
    event(1, "subtask_failed", { position: 0, phase: "implement", reason: "codex saiu com código 1" }),
  ], { position: 0 })]);
  assert.equal(diagnoseSubtasks(ws)[0].report, null);
});

test("ordem entre fases usa data, pois seq é um contador local da fase", () => {
  const previous = event(300, "subtask_failed", { position: 0, phase: "verify", reason: "veredicto FAIL" });
  previous.ts = "2026-09-18T12:00:00Z";
  const newer = event(2, "subtask_failed", { position: 0, phase: "implement", reason: "erro de execução" });
  newer.ts = "2026-09-18T13:00:00Z";
  newer.step_id = 1;
  const ws = workspace([subtask({ status: "failed", error: "erro de execução" })], [
    occurrence([previous]),
    occurrence([newer], { step_id: 1, position: 0, robot: { name: "developer", role: "implement" } }),
  ]);
  ws.task.steps[0].status = "failed";
  assert.equal(diagnoseSubtasks(ws)[0].failure?.occurrence.step_id, 1);
  assert.equal(currentFailureOccurrence(ws)?.step_id, 1);
});

test("timeout da implementação não é apresentado como validação inconclusiva", () => {
  const ws = workspace([subtask({ status: "failed", error: "timeout" })], [occurrence([
    event(1, "subtask_failed", { position: 0, phase: "implement", reason: "timeout" }),
  ], { position: 0 })]);
  const diagnosis = diagnoseSubtasks(ws)[0];
  assert.equal(diagnosis.inconclusive, false);
  assert.equal(suggestedCorrectionStep(ws.task, diagnosis)?.position, 0);
});
