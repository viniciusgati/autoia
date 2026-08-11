import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import PhaseStepper from "../components/PhaseStepper";
import ProposalCard from "../components/ProposalCard";
import StatusBadge from "../components/StatusBadge";
import TaskCard from "../components/TaskCard";
import { formatToolCall, sessionEventLine } from "../lib/events";
import { etapaAtualLabel, tempoDecorrido } from "../lib/tasks";
import type { Execution, Repository, RunEvent, Task } from "../types";

/** Página global "Execução": sessões ativas ao vivo, atenção humana, propostas
 *  de tasks filhas (aprovação) e tasks paradas — um request/poll por vez.
 */

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

  const load = async () => {
    try {
      setData(await api.getExecution(filter ?? undefined));
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

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

  const sections = (
    <>
      {/* Propostas de tasks filhas */}
      {data.proposals.length > 0 && (
        <>
          <h3 className="resumo-section">Propostas (aprovação humana)</h3>
          <div className="proposal-list">
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
        </>
      )}

      {/* Atenção humana */}
      {atencao.length > 0 && (
        <>
          <h3 className="resumo-section">Atenção humana</h3>
          <div className="task-grid">
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
        </>
      )}

      {/* Na fila */}
      {emFila.length > 0 && (
        <>
          <h3 className="resumo-section">Na fila</h3>
          <div className="task-grid">
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
        </>
      )}

      {/* Paradas */}
      {paradas.length > 0 && (
        <>
          <h3 className="resumo-section">Paradas</h3>
          <div className="task-grid">
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
        </>
      )}

      {data.tasks.length === 0 && data.proposals.length === 0 && (
        <p className="muted">Nada em execução neste momento.</p>
      )}
    </>
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

      {running.length > 0 ? (
        <div className="exec-cols">
          {/* Trilho esquerdo (~25%): sessões ativas */}
          <div className="exec-rail">
            <h3 className="resumo-section">Sessões ativas</h3>
            {running.map((task) => {
              const step = task.steps.find((s) => s.status === "running");
              const events = step ? data.current_events[String(step.id)] ?? [] : [];
              return (
                <RunningSession key={task.id} task={task} events={events} repoNames={repoNames} />
              );
            })}
          </div>

          {/* Conteúdo principal (75%): propostas, atenção humana, fila, paradas */}
          <div className="exec-main">{sections}</div>
        </div>
      ) : (
        sections
      )}
    </div>
  );
}

/** Card de sessão ativa: etapa atual, comando atual, feed ao vivo, próximos passos. */
function RunningSession({
  task,
  events,
  repoNames,
}: {
  task: Task;
  events: RunEvent[];
  repoNames: Record<number, string>;
}) {
  const runningStep = task.steps.find((s) => s.status === "running") ?? null;
  const toolCall = [...events].find((e) => e.kind === "tool_call");
  const comando = toolCall ? formatToolCall(toolCall) : "";
  const ahead = proximosPassos(task);
  const repoName = repoNames[task.repository_id];

  return (
    <div className="session-card">
      <div className="session-head">
        <div className="session-title-wrap">
          <span className="session-eyebrow">
            sessão ativa{runningStep ? ` · ${runningStep.robot?.name ?? "?"}` : ""}
          </span>
          <Link to={`/${task.repository_id}/tasks/${task.id}`} className="resumo-title">
            #{task.id} {task.title}
          </Link>
        </div>
        <StatusBadge status={task.status} />
      </div>

      {repoName && <div className="muted small">projeto: {repoName}</div>}

      <div className="session-grid">
        <div className="session-field">
          <span className="session-label">Etapa atual</span>
          <span className="session-value" title={etapaAtualLabel(task)}>
            {etapaAtualLabel(task)}
          </span>
        </div>
        <div className="session-field">
          <span className="session-label">Comando atual</span>
          <span className="session-value mono" title={comando}>
            {comando || "aguardando interação…"}
          </span>
        </div>
      </div>

      <PhaseStepper task={task} />

      {ahead.length > 0 && (
        <div className="session-field" style={{ marginTop: 8 }}>
          <span className="session-label">Próximos passos</span>
          <span className="session-value">
            {ahead.join(" · ")}
          </span>
        </div>
      )}

      <div className="session-foot">
        <span className="muted small">
          gasto {task.cost_spent.toFixed(2)} / {task.budget_limit.toFixed(2)} US$
          {runningStep ? ` · ${tempoDecorrido(runningStep)}` : ""}
        </span>
        <Link to={`/${task.repository_id}/tasks/${task.id}`} className="link-btn">
          ver detalhes →
        </Link>
      </div>

      {events.length > 0 && (
        <ul className="session-events">
          {events.map((event) => (
            <li key={event.id} className={`session-event session-event-${event.kind}`}>
              <span className="mono time">{new Date(event.ts).toLocaleTimeString()}</span>
              <span className="kind">{event.kind}</span>
              <span className="session-event-text">{sessionEventLine(event)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Fases pendentes à frente da fase atual + marco de merge/push na main. */
function proximosPassos(task: Task): string[] {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const running = steps.find((s) => s.status === "running");
  const cur = running ?? steps.find((s) => s.status === "pending") ?? steps[task.current_step];
  if (!cur) return [];
  const ahead = steps.filter(
    (s) => s.position > cur.position && s.status === "pending",
  );
  const lines: string[] = [];
  const firstPost = ahead.find((s) => s.post_merge);
  if (firstPost) {
    lines.push("merge + push na main");
  }
  for (const s of ahead) {
    lines.push(`F${s.position} ${s.robot?.name ?? "?"}`);
  }
  return lines;
}
