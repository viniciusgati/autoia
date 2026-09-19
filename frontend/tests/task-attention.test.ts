import assert from "node:assert/strict";
import test from "node:test";
import { faseAtual, taskAttention, taskNeedsAttention } from "../src/lib/tasks.ts";
import type { TaskListItem, TaskStepListItem } from "../src/types.ts";

function task(status: string): TaskListItem {
  return { status, current_step: 2, error: "subtarefas reprovadas", steps: [
    { id: 1, position: 0, status: "done", robot: {role: "implement"}, error: null },
    { id: 2, position: 2, status: "failed", robot: {role: "verify"}, error: "veredicto FAIL" },
    { id: 3, position: 3, status: "pending", robot: {role: "assess"}, error: null },
  ] as TaskStepListItem[] } as TaskListItem;
}

test("tarefas que exigem decisão ganham motivo e caminho para o relatório", () => {
  for (const status of ["needs_review", "failed", "blocked", "waiting_approval"]) {
    assert.equal(taskNeedsAttention(task(status)), true);
    assert.ok(taskAttention(task(status))?.detail.includes("veredicto FAIL"));
  }
  assert.match(taskAttention(task("needs_review"))!.nextStep, /relatório da subtarefa/);
});

test("falhas históricas não tornam tarefa concluída ou retomada em alerta", () => {
  for (const status of ["done", "in_progress", "queued", "cancelled", "paused"]) {
    assert.equal(taskNeedsAttention(task(status)), false);
    assert.equal(taskAttention(task(status)), null);
  }
});

test("fase atual usa posição real mesmo em lista esparsa e prioriza a parada", () => {
  assert.equal(faseAtual(task("needs_review"))?.id, 2);
  assert.equal(faseAtual(task("done"))?.id, 1);
  const running = task("in_progress");
  running.steps[0].status = "running";
  assert.equal(faseAtual(running)?.id, 1);
});

test("o card preserva o motivo integral e informa quando não há detalhes", () => {
  const t = task("needs_review");
  t.error = "Falha detalhada\n".repeat(500);
  assert.ok(taskAttention(t)!.detail.startsWith(t.error.trim()));
  t.error = null;
  t.steps[1].error = null;
  assert.match(taskAttention(t)!.detail, /não veio na listagem/);
});
