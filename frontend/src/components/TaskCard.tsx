import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import PhaseStepper from "./PhaseStepper";
import StatusBadge from "./StatusBadge";
import { fmtBudget } from "../lib/money";
import { faseAtual, formatDuration } from "../lib/tasks";
import type { TaskListItem } from "../types";

/** Tarefa que precisa de ação humana (revisão/aprovação/bloqueio). */
function precisaHumano(task: TaskListItem): string | null {
  if (task.status === "needs_review") return "aguardando revisão humana";
  if (task.status === "waiting_approval") return "aguardando aprovação humana";
  if (task.status === "blocked") return "bloqueada — requer atenção";
  if (
    task.status !== "done" &&
    task.status !== "failed" &&
    task.steps.some((s) => s.status === "guardrail_blocked")
  ) {
    return "guardrail bloqueou execução";
  }
  return null;
}

export default function TaskCard({
  task,
  detailPath,
  repoName,
  onChanged,
  onError,
}: {
  task: TaskListItem;
  /** Caminho base do detalhe (ex.: `/tasks` ou `/:repoId/tasks`). */
  detailPath: string;
  repoName?: string;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const { user } = useAuth();

  const alert = precisaHumano(task);
  const hasGuardrail = task.steps.some((s) => s.status === "guardrail_blocked");
  const isErr = task.status === "blocked" || (hasGuardrail && !alert?.includes("revisão"));
  const step = faseAtual(task);
  const etapa = step
    ? `F${step.position} · ${step.robot?.name ?? "?"}${step.post_merge ? " · pós-merge" : ""}`
    : task.status === "done"
      ? "concluída"
      : "—";
  const costPct = task.budget_limit > 0 ? task.cost_spent / task.budget_limit : 0;
  const costHigh = costPct >= 0.8 && task.status !== "done";
  // Tempo total de execução: soma das durações das fases com timestamps completos.
  const totalMs = task.steps.reduce((acc, s) => {
    if (s.started_at && s.finished_at) {
      acc += Math.max(0, new Date(s.finished_at).getTime() - new Date(s.started_at).getTime());
    }
    return acc;
  }, 0);
  const hasTimestamps = task.steps.some((s) => s.started_at && s.finished_at);
  // Minha tarefa (auth ON): destaque visual + selo "sua tarefa".
  const isMine = user != null && task.responsible_id === user.id;

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      onChanged();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const retornarAoDev = () => {
    const steps = [...task.steps].sort((a, b) => a.position - b.position);
    const implement = steps.find((s) => s.robot?.role === "implement" && !s.post_merge);
    return api.bouncebackTask(task.id, implement ? implement.position : 0);
  };

  return (
    <div className={`task-card${alert ? (isErr ? " task-card-err" : " task-card-warn") : ""}${isMine ? " task-card-mine" : ""}`}>
      <div className="task-card-head">
        <Link to={`${detailPath}/${task.id}`} className="task-card-title" title={`#${task.id} ${task.title}`}>
          #{task.id} {task.title}
        </Link>
        <div className="task-card-head-right">
          {isMine && (
            <span className="badge badge-mine" title="você é o responsável por esta tarefa">
              sua tarefa
            </span>
          )}
          <StatusBadge status={task.status} />
        </div>
      </div>

      {alert ? (
        <div className={`task-card-alert${isErr ? " task-card-alert-err" : ""}`}>⚠ {alert}</div>
      ) : (
        <div className="task-card-alert task-card-alert-empty" aria-hidden="true" />
      )}

      <div className="task-card-stage">
        <span className="muted small">etapa atual</span>
        <span className="task-card-stage-name">{etapa}</span>
      </div>

      <PhaseStepper task={task} muted showLabels />

      <div className="task-card-meta">
        {repoName && <span className="muted">{repoName}</span>}
        <span className="task-card-responsible" title="responsável pela tarefa">
          {task.responsible?.name ?? "Não atribuída"}
        </span>
        <span className="task-card-executor" title={`executor: ${task.executor}`}>
          {task.executor === "opencode" ? "opencode" : "kimi"}
        </span>
        {hasTimestamps && (
          <span className="task-card-duration" title="tempo total de execução">
            {formatDuration(totalMs)}
          </span>
        )}
        <span className={`mono small${costHigh ? " task-card-cost-warn" : " muted"}`}>
          {fmtBudget(task.cost_spent, task.budget_limit)}
        </span>
      </div>

      <div className="task-card-actions">
        {task.status === "created" && (
          <button onClick={() => run(() => api.startTask(task.id))} disabled={busy}>
            iniciar
          </button>
        )}
        {(task.status === "queued" || task.status === "in_progress") && (
          <>
            <button onClick={() => run(() => api.pauseTask(task.id))} disabled={busy}>
              pausar
            </button>
            <button
              className="danger"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              cancelar
            </button>
          </>
        )}
        {task.status === "paused" && (
          <>
            <button onClick={() => run(() => api.resumeTask(task.id))} disabled={busy}>
              retomar
            </button>
            <button
              className="danger"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              cancelar
            </button>
          </>
        )}
        {task.status === "needs_review" && (
          <>
            <button
              onClick={() => run(() => api.reviewTask(task.id, { action: "approve", extra_budget: 0 }))}
              disabled={busy}
            >
              aprovar
            </button>
            <button
              className="warn-btn"
              onClick={() => run(retornarAoDev)}
              disabled={busy}
            >
              retornar ao dev
            </button>
            <button
              className="danger"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              cancelar
            </button>
          </>
        )}
        <Link to={`${detailPath}/${task.id}/workspace`} className="link-btn">
          workspace ↗
        </Link>
        <Link to={`${detailPath}/${task.id}`} className="link-btn">
          ver detalhes →
        </Link>
      </div>
    </div>
  );
}

/** Opções de filtro por status/grupo de status. */
const FILTROS: { value: string; label: string; match: (t: TaskListItem) => boolean }[] = [
  { value: "todas", label: "todas", match: () => true },
  {
    value: "ativas",
    label: "em andamento",
    match: (t) => t.status === "queued" || t.status === "in_progress",
  },
  { value: "humano", label: "precisam de humano", match: (t) => precisaHumano(t) !== null },
  { value: "criadas", label: "criadas", match: (t) => t.status === "created" },
  { value: "concluidas", label: "concluídas", match: (t) => t.status === "done" },
  { value: "falharam", label: "falharam", match: (t) => t.status === "failed" },
];

export function TaskCardGrid({
  tasks,
  detailPath,
  repoNames,
  onChanged,
  onError,
}: {
  tasks: TaskListItem[];
  detailPath: string;
  repoNames?: Record<number, string>;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [filtro, setFiltro] = useState("todas");
  if (tasks.length === 0) return null;
  const sorted = [...tasks].sort((a, b) => b.id - a.id);
  const ativo = FILTROS.find((f) => f.value === filtro) ?? FILTROS[0];
  const filtrados = sorted.filter(ativo.match);
  return (
    <>
      <div className="task-filters">
        {FILTROS.map((f) => {
          const count = sorted.filter(f.match).length;
          const selected = f.value === ativo.value;
          return (
            <button
              key={f.value}
              className={`task-filter${selected ? " task-filter-active" : ""}`}
              onClick={() => setFiltro(f.value)}
            >
              {f.label} <span className="task-filter-count">{count}</span>
            </button>
          );
        })}
      </div>
      {filtrados.length === 0 ? (
        <p className="muted">Nenhuma tarefa neste filtro.</p>
      ) : (
        <div className="task-grid">
          {filtrados.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              detailPath={detailPath}
              repoName={repoNames?.[task.repository_id]}
              onChanged={onChanged}
              onError={onError}
            />
          ))}
        </div>
      )}
    </>
  );
}
