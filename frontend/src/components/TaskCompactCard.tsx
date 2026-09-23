import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { CheckIcon, PlayIcon, UndoIcon, WorkspaceIcon, XIcon } from "./Icons";
import StatusBadge from "./StatusBadge";
import { fmtBudget } from "../lib/money";
import { etapaAtualLabel, podeAtuar, taskAttention, taskNeedsAttention } from "../lib/tasks";
import type { TaskListItem } from "../types";

/**
 * Card COMPACTO para grids de acompanhamento (Execução/Dashboard).
 * Mostra o status no topo do grid e a etapa atual em uma linha — sem stepper,
 * diagnóstico longo ou ações de revisão, mantendo o essencial para "o que está
 * executando agora?".
 */
export default function TaskCompactCard({
  task,
  detailPath,
  repoName,
  onChanged,
  onError,
}: {
  task: TaskListItem;
  detailPath: string;
  repoName?: string;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const { user } = useAuth();
  const attention = taskAttention(task);
  const workspacePath = `${detailPath}/${task.id}/workspace`;
  const step = task.steps.find((s) => s.status === "running")
    ?? [...task.steps].sort((a, b) => a.position - b.position).find((s) => s.status === "pending");
  const costPct = task.budget_limit > 0 ? task.cost_spent / task.budget_limit : 0;
  const costHigh = costPct >= 0.8 && !["done", "cancelled", "failed"].includes(task.status);
  const canAct = podeAtuar(user, task.responsible_id, false);

  const run = async (action: () => Promise<unknown>) => {
    try {
      await action();
      onChanged();
    } catch (e) {
      onError(String(e));
    }
  };

  return (
    <article className={`task-compact${attention ? (attention.tone === "error" ? " task-compact-err" : " task-compact-warn") : ""}`}>
      <div className="task-compact-top">
        <span className="task-compact-id">#{task.id}{repoName ? ` · ${repoName}` : ""}</span>
        <span className="task-compact-status">
          <StatusBadge status={task.status} />
        </span>
      </div>
      <Link to={workspacePath} className="task-compact-title" title={`#${task.id} ${task.title}`}>
        {task.title}
      </Link>
      <div className="task-compact-stage" title={step ? etapaAtualLabel(task) : undefined}>
        <span className="task-compact-stage-label">{task.status === "done" ? "entrega" : step ? (step.status === "running" ? "executando agora" : "próxima fase") : "—"}</span>
        <span className="task-compact-stage-name">
          {step ? `Fase ${step.position + 1} · ${step.robot?.name ?? "Agente"}` : task.status === "done" ? "concluída" : "aguardando"}
        </span>
      </div>
      <div className="task-compact-meta">
        <span className="mono">{fmtBudget(task.cost_spent, task.budget_limit)}</span>
        <span className={`task-compact-executor${costHigh ? " task-compact-cost-warn" : ""}`}>
          {task.executor === "codex" ? "codex" : task.executor === "opencode" ? "opencode" : "kimi"}
        </span>
      </div>
      <div className="task-compact-actions">
        <Link to={workspacePath} className="task-compact-open">
          <WorkspaceIcon size={14} />{attention ? "Resolver" : task.status === "done" ? "Ver entrega" : "Acompanhar"}<span aria-hidden="true">→</span>
        </Link>
        {canAct && task.status === "created" && (
          <button className="icon-btn" title="iniciar tarefa" onClick={() => run(() => api.startTask(task.id))}>
            <PlayIcon size={14} />
          </button>
        )}
        {canAct && task.status === "needs_review" && (
          <>
            <button className="icon-btn icon-btn-ok" title="aprovar e continuar" onClick={() => run(() => api.reviewTask(task.id, { action: "approve", extra_budget: 0 }))}>
              <CheckIcon size={14} />
            </button>
            <button
              className="icon-btn icon-btn-warn"
              title="retornar ao dev"
              onClick={() => {
                const implement = [...task.steps].sort((a, b) => a.position - b.position).find((s) => s.robot?.role === "implement" && !s.post_merge);
                run(() => api.bouncebackTask(task.id, implement ? implement.position : 0));
              }}
            >
              <UndoIcon size={14} />
            </button>
          </>
        )}
        {(canAct && ["failed", "done"].includes(task.status)) || (canAct && ["queued", "in_progress"].includes(task.status)) ? (
          <button
            className="icon-btn icon-btn-danger"
            title="cancelar tarefa"
            onClick={() => {
              if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                run(() => api.cancelTask(task.id));
              }
            }}
          >
            <XIcon size={14} />
          </button>
        ) : null}
      </div>
    </article>
  );
}

export function needsAttentionCompact(task: TaskListItem): boolean {
  return taskNeedsAttention(task);
}