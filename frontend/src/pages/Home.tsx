import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import type { Dashboard as DashboardData, Repository, Task, TaskStep } from "../types";

/** Fase em destaque da task: a que está rodando, senão a próxima da fila. */
function faseAtual(task: Task): TaskStep | null {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    steps.find((s) => s.status === "running") ??
    steps.find((s) => s.status === "pending") ??
    steps[task.current_step] ??
    null
  );
}

/** Tarefa precisa de iteração humana (revisão, aprovação, bloqueada ou guardrail). */
function precisaHumano(task: Task): boolean {
  if (
    task.status === "needs_review" ||
    task.status === "waiting_approval" ||
    task.status === "blocked"
  ) {
    return true;
  }
  return (
    task.status !== "done" &&
    task.status !== "failed" &&
    task.steps.some((s) => s.status === "guardrail_blocked")
  );
}

/** "há 5 min", "há 3 h", etc. */
function tempoRelativo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 0) return "";
  const min = Math.floor(ms / 60000);
  if (min < 1) return "agora";
  if (min < 60) return `há ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `há ${h} h`;
  const d = Math.floor(h / 24);
  return `há ${d} d`;
}

export default function Home() {
  const [dash, setDash] = useState<DashboardData | null>(null);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("main");

  const load = () =>
    Promise.all([api.getDashboard(), api.listRepositories(), api.listTasks()])
      .then(([d, r, t]) => {
        setDash(d);
        setRepos(r);
        setTasks(t);
        setError("");
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  const addRepo = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api.createRepository({ name, url, default_branch: branch });
      setName("");
      setUrl("");
      setBranch("main");
      await load();
    } catch (err) {
      setError(String(err));
    }
  };

  if (!dash || !repos.length === undefined) return <p>Carregando…</p>;

  // Agrupa tarefas por repositório, ordenadas por id decrescente (mais recentes primeiro)
  const tasksByRepo: Record<number, Task[]> = {};
  for (const t of tasks) {
    if (!tasksByRepo[t.repository_id]) tasksByRepo[t.repository_id] = [];
    tasksByRepo[t.repository_id].push(t);
  }
  for (const arr of Object.values(tasksByRepo)) {
    arr.sort((a, b) => b.id - a.id);
  }

  // Contagem de tarefas por repositório
  const taskCounts: Record<number, { total: number; byStatus: Record<string, number> }> = {};
  for (const t of tasks) {
    if (!taskCounts[t.repository_id]) {
      taskCounts[t.repository_id] = { total: 0, byStatus: {} };
    }
    taskCounts[t.repository_id].total++;
    taskCounts[t.repository_id].byStatus[t.status] =
      (taskCounts[t.repository_id].byStatus[t.status] || 0) + 1;
  }

  return (
    <div>
      <h2>Projetos</h2>

      {error && <p className="error">{error}</p>}

      {/* métricas globais */}
      <div className="cards">
        <div className="card">
          <div className="card-value">{repos.length}</div>
          <div className="card-label">projetos</div>
        </div>
        <div className="card">
          <div className="card-value">{dash.total_tasks}</div>
          <div className="card-label">tarefas</div>
        </div>
        <div className="card">
          <div className="card-value">{dash.total_cost.toFixed(2)}</div>
          <div className="card-label">custo estimado (US$)</div>
        </div>
        <div className="card">
          <div className="card-value">{dash.guardrail_events}</div>
          <div className="card-label">bloqueios de guardrail</div>
        </div>
      </div>

      {/* avisos globais */}
      {dash.notices.length > 0 && (
        <>
          <h3>Requer atenção</h3>
          <div className="notices">
            {dash.notices.map((notice, i) => (
              <Link
                key={`${notice.kind}-${notice.task_id}-${i}`}
                to={`/${notice.repository_id}/tasks/${notice.task_id}`}
                className={`notice notice-${notice.level}`}
              >
                <div className="notice-line">
                  <span className="resumo-title">
                    #{notice.task_id} {notice.task_title}
                  </span>
                  <StatusBadge status={notice.task_status} />
                </div>
                <div className="notice-line small">
                  <span className={`notice-kind ${notice.level}`}>{notice.kind}</span>
                  <span className="muted">{notice.message}</span>
                </div>
              </Link>
            ))}
          </div>
        </>
      )}

      {/* lista de projetos */}
      <h3>Todos os projetos</h3>
      {repos.length === 0 ? (
        <p className="muted">Nenhum projeto cadastrado.</p>
      ) : (
        <div className="project-grid">
          {repos.map((repo) => {
            const counts = taskCounts[repo.id];
            const repoTasks = tasksByRepo[repo.id] ?? [];
            const activeCount = counts
              ? (counts.byStatus["queued"] || 0) +
                (counts.byStatus["in_progress"] || 0)
              : 0;
            const humanCount = repoTasks.filter(precisaHumano).length;
            const recentTasks = [...repoTasks]
              .sort((a, b) => {
                const aH = precisaHumano(a) ? 1 : 0;
                const bH = precisaHumano(b) ? 1 : 0;
                if (aH !== bH) return bH - aH;
                return b.id - a.id;
              })
              .slice(0, 3);

            return (
              <div key={repo.id} className="project-card-wrapper">
                <Link to={`/${repo.id}`} className="project-card">
                  <div className="project-card-head">
                    <span className="project-card-name">{repo.name}</span>
                    <span className="project-card-arrow">→</span>
                  </div>
                  <div className="project-card-meta">
                    <span className="muted mono small">{repo.url}</span>
                  </div>
                  {humanCount > 0 && (
                    <div className="project-card-human">
                      <span className="project-card-human-icon">👀</span>
                      {humanCount} {humanCount === 1 ? "tarefa aguardando" : "tarefas aguardando"} você
                    </div>
                  )}
                  <div className="project-card-stats">
                    <span>{counts?.total ?? 0} tarefas</span>
                    {activeCount > 0 && (
                      <span className="project-card-active">{activeCount} ativas</span>
                    )}
                  </div>
                </Link>

                {/* últimas tarefas do projeto (prioriza as que precisam de humano) */}
                {recentTasks.length > 0 && (
                  <div className="project-tasks-mini">
                    {recentTasks.map((task) => {
                      const step = faseAtual(task);
                      const etapa = step
                        ? `F${step.position} · ${step.robot?.name ?? "?"}`
                        : "—";
                      const precisa = precisaHumano(task);
                      const rowClass = precisa
                        ? task.status === "blocked"
                          ? " project-task-row-err"
                          : " project-task-row-warn"
                        : "";
                      return (
                        <Link
                          key={task.id}
                          to={`/${repo.id}/tasks/${task.id}`}
                          className={`project-task-row${rowClass}`}
                        >
                          <div className="project-task-line">
                            <span className="project-task-title">
                              #{task.id} {task.title}
                            </span>
                            {precisa && (
                              <span className="project-task-await">👀 aguarda você</span>
                            )}
                            <StatusBadge status={task.status} />
                          </div>
                          <div className="project-task-sub">
                            <span>{etapa}</span>
                            <span className="mono">{task.cost_spent.toFixed(2)} US$</span>
                            <span className="muted">{tempoRelativo(task.updated_at)}</span>
                          </div>
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* adicionar projeto */}
      <details className="add-section">
        <summary>+ Novo projeto</summary>
        <form className="form-stack" onSubmit={addRepo}>
          <div className="form-field">
            <label className="form-label">Nome do projeto</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="form-field">
            <label className="form-label">URL do repositório</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
            />
          </div>
          <div className="form-field">
            <label className="form-label">Branch default</label>
            <input
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
          </div>
          <div className="form-actions">
            <button type="submit">adicionar projeto</button>
          </div>
        </form>
      </details>
    </div>
  );
}
