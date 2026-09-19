import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { CheckIcon, EyeIcon, PauseIcon, PlayIcon, UndoIcon, WorkspaceIcon, XIcon } from "./Icons";
import PhaseStepper from "./PhaseStepper";
import StatusIcon from "./StatusIcon";
import { fmtBudget } from "../lib/money";
import { faseAtual, formatDuration, taskAttention, taskNeedsAttention, taskStatusLabel } from "../lib/tasks";
import type { TaskListItem } from "../types";

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

  const attention = taskAttention(task);
  const workspacePath = `${detailPath}/${task.id}/workspace`;
  const step = faseAtual(task);
  const etapa = step
    ? `Fase ${step.position + 1} · ${step.robot?.name ?? "Agente"}${step.post_merge ? " · pós-merge" : ""}`
    : task.status === "done"
      ? "concluída"
      : "—";
  const costPct = task.budget_limit > 0 ? task.cost_spent / task.budget_limit : 0;
  const costHigh = costPct >= 0.8 && !["done", "cancelled", "failed"].includes(task.status);
  const completed = task.steps.filter((item) => item.status === "done").length;
  const detailPreview = attention?.detail.replace(/\s+/g, " ").trim() ?? "";
  const hasLongDetail = detailPreview.length > 220;
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
    <article className={`task-card${attention ? (attention.tone === "error" ? " task-card-err" : " task-card-warn") : ""}${isMine ? " task-card-mine" : ""}`}>
      <div className="task-card-eyebrow">
        <span>#{task.id}{repoName && ` · ${repoName}`}</span>
        {isMine && <span className="badge badge-mine">Sua tarefa</span>}
      </div>
      <div className="task-card-head">
        <Link to={workspacePath} className="task-card-title" title={`#${task.id} ${task.title}`}>
          {task.title}
        </Link>
      </div>
      <div className="task-card-progress-label">
        <span className={`task-card-status task-card-status-${task.status}`}>
          <StatusIcon status={task.status} />
          {taskStatusLabel(task.status)}
        </span>
        <span className="muted small">{completed}/{task.steps.length} fases</span>
      </div>

      {attention && (
        <div className={`task-card-diagnosis task-card-diagnosis-${attention.tone}`}>
          <span className="task-card-diagnosis-label">{attention.title}</span>
          <p>{hasLongDetail ? `${detailPreview.slice(0, 220)}…` : detailPreview}</p>
          {hasLongDetail && (
            <details className="task-card-diagnosis-details">
              <summary>Ver motivo completo</summary>
              <div>{attention.detail}</div>
            </details>
          )}
          <div className="task-card-next"><strong>Próximo passo</strong>{attention.nextStep}</div>
        </div>
      )}

      <div className="task-card-stage">
        <span className="muted small">{task.status === "done" ? "Última fase concluída" : attention?.step ? "Onde parou" : "Etapa atual"}</span>
        <span className="task-card-stage-name">{etapa}</span>
        {step && task.status !== "done" && (
          <span className="muted small">{taskStatusLabel(step.status)}{step.attempt > 1 ? ` · tentativa ${step.attempt}` : ""}</span>
        )}
      </div>

      <div className="task-card-progress"><PhaseStepper task={task} muted showLabels /></div>

      <div className="task-card-meta">
        <span className="task-card-responsible" title="responsável pela tarefa">
          {task.responsible?.name ?? "Sem responsável"}
        </span>
        <span className="task-card-executor" title={`executor: ${task.executor}`}>
          {task.executor === "codex" ? "codex" : task.executor === "opencode" ? "opencode" : "kimi"}
        </span>
        {hasTimestamps && (
          <span className="task-card-duration" title="tempo total de execução">
            {formatDuration(totalMs)}
          </span>
        )}
      </div>
      <div className={`task-card-budget${costHigh ? " task-card-cost-warn" : ""}`}>
        <div className="task-card-progress-label"><span>Orçamento{costHigh ? " · atenção ao limite" : ""}</span><span className="mono">{fmtBudget(task.cost_spent, task.budget_limit)}</span></div>
        {task.budget_limit > 0 && <div className="task-card-budget-track" aria-label={`${Math.round(costPct * 100)}% do orçamento utilizado`}><span style={{ width: `${Math.min(100, Math.max(0, costPct * 100))}%` }} /></div>}
      </div>

      <Link to={`${workspacePath}${attention ? "#diagnostico" : ""}`} className="task-card-primary">
        <WorkspaceIcon size={16} />{attention ? "Entender e resolver" : task.status === "done" ? "Ver entrega" : "Abrir workspace"}<span aria-hidden="true">→</span>
      </Link>

      <div className="task-card-actions">
        {task.status === "created" && (
          <button className="icon-btn" title="iniciar tarefa" onClick={() => run(() => api.startTask(task.id))} disabled={busy}>
            <PlayIcon size={15} />
          </button>
        )}
        {(task.status === "queued" || task.status === "in_progress") && (
          <>
            <button className="icon-btn" title="pausar" onClick={() => run(() => api.pauseTask(task.id))} disabled={busy}>
              <PauseIcon size={15} />
            </button>
            <button
              className="icon-btn icon-btn-danger"
              title="cancelar tarefa"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              <XIcon size={15} />
            </button>
          </>
        )}
        {task.status === "paused" && (
          <>
            <button className="icon-btn" title="retomar" onClick={() => run(() => api.resumeTask(task.id))} disabled={busy}>
              <PlayIcon size={15} />
            </button>
            <button
              className="icon-btn icon-btn-danger"
              title="cancelar tarefa"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              <XIcon size={15} />
            </button>
          </>
        )}
        {(task.status === "failed" || task.status === "done") && (
          <button
            className="icon-btn icon-btn-danger"
            title="cancelar tarefa"
            onClick={() => {
              if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                run(() => api.cancelTask(task.id));
              }
            }}
            disabled={busy}
          >
            <XIcon size={15} />
          </button>
        )}
        {task.status === "needs_review" && (
          <>
            <button
              className="icon-btn icon-btn-ok"
              title="aprovar e continuar"
              onClick={() => run(() => api.reviewTask(task.id, { action: "approve", extra_budget: 0 }))}
              disabled={busy}
            >
              <CheckIcon size={15} />
            </button>
            <button
              className="icon-btn icon-btn-warn"
              title="retornar ao dev"
              onClick={() => run(retornarAoDev)}
              disabled={busy}
            >
              <UndoIcon size={15} />
            </button>
            <button
              className="icon-btn icon-btn-danger"
              title="cancelar tarefa"
              onClick={() => {
                if (window.confirm(`Cancelar a tarefa #${task.id}?`)) {
                  run(() => api.cancelTask(task.id));
                }
              }}
              disabled={busy}
            >
              <XIcon size={15} />
            </button>
          </>
        )}
        <Link to={`${detailPath}/${task.id}`} className="task-card-technical" title="Abrir detalhes técnicos">
          <EyeIcon size={15} />
          Detalhes técnicos
        </Link>
      </div>
    </article>
  );
}

/** Opções de filtro por status/grupo de status. */
const FILTROS: { value: string; label: string; match: (t: TaskListItem) => boolean }[] = [
  { value: "todas", label: "Todas", match: () => true },
  { value: "humano", label: "Precisam de atenção", match: taskNeedsAttention },
  {
    value: "ativas",
    label: "Em andamento",
    match: (t) => t.status === "queued" || t.status === "in_progress",
  },
  { value: "paradas", label: "Aguardando início", match: (t) => ["created", "paused", "open"].includes(t.status) },
  { value: "concluidas", label: "Concluídas", match: (t) => t.status === "done" },
  { value: "falharam", label: "Falharam", match: (t) => t.status === "failed" },
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
  const [search, setSearch] = useState("");
  if (tasks.length === 0) return null;
  const sorted = [...tasks].sort((a, b) => Number(taskNeedsAttention(b)) - Number(taskNeedsAttention(a)) || b.id - a.id);
  const ativo = FILTROS.find((f) => f.value === filtro) ?? FILTROS[0];
  const term = search.trim().toLocaleLowerCase("pt-BR");
  const searched = sorted.filter((task) => !term || `${task.id} ${task.title} ${task.error ?? ""} ${repoNames?.[task.repository_id] ?? ""}`.toLocaleLowerCase("pt-BR").includes(term));
  const filtrados = searched.filter(ativo.match);
  return (
    <>
      <div className="task-card-grid-toolbar">
        <label className="task-card-search"><span>Buscar tarefas</span><input type="search" placeholder="Título, número ou motivo da falha" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <span className="muted small">{filtrados.length} de {tasks.length} tarefas · atenção primeiro</span>
      </div>
      <div className="task-filters">
        {FILTROS.map((f) => {
          const count = searched.filter(f.match).length;
          const selected = f.value === ativo.value;
          return (
            <button
              key={f.value}
              className={`task-filter${selected ? " task-filter-active" : ""}`}
              aria-pressed={selected}
              onClick={() => setFiltro(f.value)}
            >
              {f.label} <span className="task-filter-count">{count}</span>
            </button>
          );
        })}
      </div>
      {filtrados.length === 0 ? (
        <div className="exec-empty">Nenhuma tarefa encontrada. Tente outro filtro ou termo de busca.</div>
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
