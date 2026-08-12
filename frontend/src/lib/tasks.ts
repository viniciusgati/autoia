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

/** Fase em destaque da task: a que está rodando, senão a próxima da fila. */
export function faseAtual(task: TaskListItem): TaskStepListItem | null {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    steps.find((s) => s.status === "running") ??
    steps.find((s) => s.status === "pending") ??
    steps[task.current_step] ??
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
        : step.status;
  return `Fase ${step.position}/${steps.length} · ${nome} (tentativa ${step.attempt}) · ${estado}`;
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
