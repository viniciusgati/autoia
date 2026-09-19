import type { TaskListItem, TaskStepListItem, User } from "../types";

/** Tooltip dos botões de ação sem permissão (não é responsável nem admin). */
export const MSG_SEM_PERMISSAO = "Somente o responsável ou admin do projeto pode atuar";

/** Permissão de atuação numa tarefa (mutações). Com auth OFF (user null) libera —
 *  comportamento legado. Com responsável definido: só ele, admin do projeto ou
 *  admin global; sem responsável, qualquer autenticado atua. */
export function podeAtuar(
  user: User | null,
  responsibleId: number | null,
  isRepoAdmin: boolean,
): boolean {
  if (!user) return true;
  if (responsibleId == null) return true;
  if (user.id === responsibleId) return true;
  if (user.role === "admin") return true;
  if (isRepoAdmin) return true;
  return false;
}

const ATTENTION_STATUSES = new Set(["needs_review", "waiting_approval", "blocked", "failed"]);
const FAILED_STEP_STATUSES = new Set(["failed", "guardrail_blocked", "blocked"]);

/** O estado da tarefa prevalece sobre falhas históricas de fases já retomadas. */
export function taskNeedsAttention(task: TaskListItem): boolean {
  return ATTENTION_STATUSES.has(task.status);
}

export function taskStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: "Não iniciada", queued: "Na fila", in_progress: "Em andamento",
    done: "Concluída", failed: "Falhou", blocked: "Bloqueada",
    needs_review: "Precisa de revisão", waiting_approval: "Aguardando aprovação",
    paused: "Pausada", cancelled: "Cancelada", open: "Aguardando orientação",
    pending: "Pendente", running: "Em execução", guardrail_blocked: "Execução interrompida",
    skipped: "Dispensada",
  };
  return labels[status] ?? status;
}

export interface TaskAttention {
  title: string;
  detail: string;
  nextStep: string;
  tone: "error" | "warning";
  step: TaskStepListItem | null;
}

function failedStep(task: TaskListItem): TaskStepListItem | null {
  const failures = task.steps.filter((step) => FAILED_STEP_STATUSES.has(step.status));
  return failures.find((step) => step.position === task.current_step)
    ?? failures.sort((a, b) => (b.finished_at ?? b.started_at ?? "").localeCompare(a.finished_at ?? a.started_at ?? ""))[0]
    ?? null;
}

function attentionStep(task: TaskListItem): TaskStepListItem | null {
  return task.steps.find((step) => step.position === task.current_step && step.status !== "done")
    ?? failedStep(task);
}

/** Diagnóstico usa apenas dados já disponíveis na listagem, sem inventar a causa. */
export function taskAttention(task: TaskListItem): TaskAttention | null {
  if (!taskNeedsAttention(task)) return null;
  const step = attentionStep(task);
  const taskError = task.error?.trim() ?? "";
  const stepError = step?.error?.trim() ?? "";
  const detail = taskError && stepError && taskError !== stepError
    ? `${taskError}\n\n${stepError}` : taskError || stepError;
  const isSubtask = /subtarefa/i.test(detail);
  const isBudget = /orçamento|budget|limite de custo/i.test(detail);
  const title = task.status === "waiting_approval" ? "Sua aprovação é necessária"
    : task.status === "blocked" ? "A execução precisa de orientação"
      : isSubtask ? "Uma subtarefa precisa de atenção"
        : isBudget ? "O orçamento precisa de revisão"
          : step ? `A fase ${step.position + 1} não foi concluída`
            : task.status === "failed" ? "A execução falhou" : "A tarefa precisa de revisão";
  const nextStep = task.status === "waiting_approval" ? "Revise a entrega no workspace para decidir se ela pode continuar."
    : task.status === "blocked" ? "Leia o motivo do bloqueio e envie uma orientação no workspace."
      : isSubtask ? "Confira o relatório da subtarefa no workspace e oriente a correção."
        : step?.post_merge ? "Revise a falha após a integração e indique a correção no workspace."
          : isBudget ? "Confira o consumo e decida como retomar a tarefa no workspace."
            : "Confira a evidência da falha no workspace antes de orientar a próxima execução.";
  return {
    title,
    detail: detail || (task.status === "waiting_approval"
      ? "A tarefa está aguardando sua decisão para avançar."
      : "O motivo detalhado não veio na listagem. Abra o workspace para consultar as fases e subtarefas."),
    nextStep,
    tone: task.status === "failed" || task.status === "blocked" ? "error" : "warning",
    step,
  };
}

/** A fase atual é identificada pela posição, nunca pelo índice no array. */
export function faseAtual(task: TaskListItem): TaskStepListItem | null {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  if (task.status === "done") return steps.filter((step) => step.status === "done").slice(-1)[0] ?? null;
  return (
    steps.find((s) => s.status === "running") ??
    (taskNeedsAttention(task) ? attentionStep(task) : null) ??
    steps.find((s) => s.position === task.current_step && s.status !== "done") ??
    steps.find((s) => s.status === "pending") ??
    steps.find((s) => s.position === task.current_step) ??
    null
  );
}

/** Rótulo curto da etapa atual: "Fase 3/7 · developer (tentativa 2) · rodando". */
export function etapaAtualLabel(task: TaskListItem): string {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const step = faseAtual(task);
  if (!step) return "";
  const nome = step.robot?.name ?? "?";
  const estado =
    step.status === "running"
      ? "rodando"
      : step.status === "pending"
        ? "na fila"
        : taskStatusLabel(step.status).toLowerCase();
  return `Fase ${step.position + 1}/${steps.length} · ${nome} (tentativa ${step.attempt}) · ${estado}`;
}

/** Tempo decorrido desde o início da fase, legível ("3m 12s"). */
export function tempoDecorrido(step: { started_at: string | null }): string {
  if (!step.started_at) return "";
  const ms = Date.now() - new Date(step.started_at).getTime();
  if (ms < 0) return "0s";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Formata uma duração em ms como texto curto: "42s" · "3m 12s" · "1h 23m". */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  if (m < 60) return `${m}m ${total % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** True se o timestamp (ISO/UTC) cai no dia de HOJE no fuso local do navegador. */
export function isToday(iso: string): boolean {
  const d = new Date(iso);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

/** Métricas rápidas de um conjunto de tasks para o dashboard:
 *  concluídas hoje (`status === "done"` com `updated_at` de hoje) + custo acumulado. */
export function taskStats(tasks: TaskListItem[]): { doneToday: number; spent: number } {
  let doneToday = 0;
  let spent = 0;
  for (const t of tasks) {
    spent += t.cost_spent ?? 0;
    if (t.status === "done" && isToday(t.updated_at)) doneToday += 1;
  }
  return { doneToday, spent };
}

/** Extrai resumo legível de um diff_stat (git --stat): "3 arquivos, +45/-12". */
export function diffSummary(diffStat: string | null): string | null {
  if (!diffStat) return null;
  const lines = diffStat.trim().split("\n");
  const last = lines[lines.length - 1];
  const match = last.match(
    /(\d+)\s+files?\s+changed(?:,\s*(\d+)\s+insertions?\(\+\))?(?:,\s*(\d+)\s+deletions?\(\-\))?/,
  );
  if (!match) {
    // fallback: conta linhas com "|" (uma por arquivo)
    const fileLines = lines.filter((l) => l.includes("|")).length;
    if (fileLines > 0) return `${fileLines} arquivo(s) alterado(s)`;
    return null;
  }
  const files = match[1];
  const plus = match[2] ? `+${match[2]}` : "+0";
  const minus = match[3] ? `-${match[3]}` : "-0";
  if (plus === "+0" && minus === "-0") return null;
  return `${files} arquivo(s) · ${plus} ${minus}`;
}
