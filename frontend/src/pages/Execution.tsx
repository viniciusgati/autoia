import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import ProposalCard from "../components/ProposalCard";
import StatusIcon from "../components/StatusIcon";
import TaskCard from "../components/TaskCard";
import { formatToolCall } from "../lib/events";
import { fmtBudget } from "../lib/money";
import { usePolling } from "../lib/polling";
import type { Execution, Repository, RunEvent, TaskListItem } from "../types";

/** Página global "Execução": seções em colunas horizontais ocupando 100% da tela.
 *  Sessões ativas mostram apenas o comando atual + hora da chamada; propostas,
 *  atenção humana, fila e paradas ficam em colunas ao lado. */

const ATENCAO = ["needs_review", "waiting_approval", "blocked"];
const PARADAS = ["paused", "created"];

export default function ExecutionPage() {
  const [data, setData] = useState<Execution | null>(null);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [filter, setFilter] = useState<number | null>(null);
  const [error, setError] = useState("");

  const repoNames = useMemo(() => {
    const m: Record<number, string> = {};
    for (const r of repos) m[r.id] = r.name;
    return m;
  }, [repos]);

  const load = async (signal?: AbortSignal) => {
    try {
      setData(await api.getExecution(filter ?? undefined, signal));
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    }
  };

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
  }, []);

  usePolling(load, 5000, [filter]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>Carregando…</p>;

  const running = data.tasks.filter((t) => t.steps.some((s) => s.status === "running"));
  const atencao = data.tasks.filter((t) => ATENCAO.includes(t.status));
  const paradas = data.tasks.filter(
    (t) => PARADAS.includes(t.status) && !running.some((r) => r.id === t.id),
  );
  const emFila = data.tasks.filter(
    (t) =>
      t.status === "queued" &&
      !running.some((r) => r.id === t.id) &&
      !atencao.some((a) => a.id === t.id),
  );

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Execução</h2>
        <span className="muted">
          {data.worker.alive ? (
            <span className="worker-status" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <span className="worker-dot worker-dot-on" /> worker ativo
            </span>
          ) : (
            <span className="worker-status" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <span className="worker-dot worker-dot-off" /> worker offline
            </span>
          )}
          {" · "}
          {running.length} em execução · {atencao.length} precisam de humano ·{" "}
          {data.proposals.length} propostas
        </span>
      </div>

      <div className="form-inline" style={{ marginBottom: 14, alignItems: "center" }}>
        <label className="form-label" style={{ margin: 0 }}>Projeto:</label>
        <select
          value={filter ?? ""}
          onChange={(e) => setFilter(e.target.value ? Number(e.target.value) : null)}
          style={{ maxWidth: 260 }}
        >
          <option value="">— todos os projetos —</option>
          {repos.map((r) => (
            <option key={r.id} value={r.id}>{r.name}</option>
          ))}
        </select>
      </div>

      <div className="exec-board">
        {/* Sessões ativas */}
        <h3 className="resumo-section">Sessões ativas</h3>
        {running.length === 0 ? (
          <p className="muted small">Nada em execução.</p>
        ) : (
          <div className="exec-cards">
            {running.map((task) => {
              const step = task.steps.find((s) => s.status === "running");
              const events = step ? data.current_events[String(step.id)] ?? [] : [];
              return (
                <RunningSession key={task.id} task={task} events={events} repoNames={repoNames} />
              );
            })}
          </div>
        )}

        {/* Propostas de tasks filhas */}
        <h3 className="resumo-section">Propostas</h3>
        {data.proposals.length === 0 ? (
          <p className="muted small">Sem propostas aguardando aprovação.</p>
        ) : (
          <div className="exec-cards proposal-list">
            {data.proposals.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                repoNames={repoNames}
                onChanged={load}
                onError={setError}
              />
            ))}
          </div>
        )}

        {/* Atenção humana */}
        <h3 className="resumo-section">Atenção humana</h3>
        {atencao.length === 0 ? (
          <p className="muted small">Nada precisa de humano.</p>
        ) : (
          <div className="exec-cards task-grid">
            {atencao.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                detailPath={`/${task.repository_id}/tasks`}
                repoName={repoNames[task.repository_id]}
                onChanged={load}
                onError={setError}
              />
            ))}
          </div>
        )}

        {/* Na fila */}
        <h3 className="resumo-section">Na fila</h3>
        {emFila.length === 0 ? (
          <p className="muted small">Nada aguardando worker.</p>
        ) : (
          <div className="exec-cards task-grid">
            {emFila.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                detailPath={`/${task.repository_id}/tasks`}
                repoName={repoNames[task.repository_id]}
                onChanged={load}
                onError={setError}
              />
            ))}
          </div>
        )}

        {/* Paradas */}
        <h3 className="resumo-section">Paradas</h3>
        {paradas.length === 0 ? (
          <p className="muted small">Nenhuma tarefa parada.</p>
        ) : (
          <div className="exec-cards task-grid">
            {paradas.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                detailPath={`/${task.repository_id}/tasks`}
                repoName={repoNames[task.repository_id]}
                onChanged={load}
                onError={setError}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Card de sessão ativa (compacto): só o comando atual com a hora da chamada. */
function RunningSession({
  task,
  events,
  repoNames,
}: {
  task: TaskListItem;
  events: RunEvent[];
  repoNames: Record<number, string>;
}) {
  const runningStep = task.steps.find((s) => s.status === "running") ?? null;
  // events vêm em ordem decrescente — o primeiro tool_call é o mais recente.
  const toolCall = [...events].find((e) => e.kind === "tool_call") ?? null;
  const comando = toolCall ? formatToolCall(toolCall) : "aguardando interação…";
  const hora = toolCall
    ? new Date(toolCall.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : null;
  const repoName = repoNames[task.repository_id];

  return (
    <div className="session-card">
      <div className="session-head">
        <Link to={`/${task.repository_id}/tasks/${task.id}`} className="resumo-title">
          #{task.id} {task.title}
        </Link>
        <StatusIcon status={task.status} />
      </div>

      {repoName && <div className="muted small">projeto: {repoName}</div>}

      <div className="session-grid" style={{ gridTemplateColumns: "1fr" }}>
        <div className="session-field">
          <span className="session-label">Etapa</span>
          <span className="session-value">{runningStep?.robot?.name ?? etapaAtual(task)}</span>
        </div>
        <div className="session-field">
          <span className="session-label">Comando atual</span>
          <span className="session-value mono" title={comando}>
            {comando}
          </span>
        </div>
      </div>

      {hora && (
        <div className="session-command-time">
          <span className="mono">{hora}</span> — chamada do comando
        </div>
      )}

      <div className="session-foot">
        <span className="muted small">gasto {fmtBudget(task.cost_spent, task.budget_limit)}</span>
        <Link to={`/${task.repository_id}/tasks/${task.id}`} className="link-btn">
          ver detalhes →
        </Link>
      </div>
    </div>
  );
}

function etapaAtual(task: TaskListItem): string {
  const step = task.steps.find((s) => s.status === "running")
    ?? [...task.steps].sort((a, b) => a.position - b.position)
      .find((s) => s.status === "pending");
  return step?.robot?.name ?? "—";
}
