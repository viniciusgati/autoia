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
                to={`/${notice.task_id}/tasks/${notice.task_id}`}
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
            const attentionCount = counts
              ? (counts.byStatus["needs_review"] || 0) +
                (counts.byStatus["blocked"] || 0)
              : 0;
            const recentTasks = repoTasks.slice(0, 3);

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
                  <div className="project-card-stats">
                    <span>{counts?.total ?? 0} tarefas</span>
                    {activeCount > 0 && (
                      <span className="project-card-active">{activeCount} ativas</span>
                    )}
                    {attentionCount > 0 && (
                      <span className="project-card-attention">⚠ {attentionCount}</span>
                    )}
                  </div>
                </Link>

                {/* últimas tarefas do projeto */}
                {recentTasks.length > 0 && (
                  <div className="project-tasks-mini">
                    {recentTasks.map((task) => {
                      const step = faseAtual(task);
                      const etapa = step
                        ? `F${step.position} · ${step.robot?.name ?? "?"}`
                        : "—";
                      const isAttention =
                        task.status === "needs_review" ||
                        task.status === "blocked" ||
                        (task.steps.some(
                          (s) => s.status === "guardrail_blocked",
                        ) &&
                          task.status !== "done" &&
                          task.status !== "failed");
                      const rowClass = isAttention
                        ? task.status === "needs_review"
                          ? " project-task-row-warn"
                          : " project-task-row-err"
                        : "";
                      return (
                        <Link
                          key={task.id}
                          to={`/${repo.id}/tasks/${task.id}`}
                          className={`project-task-row${rowClass}`}
                        >
                          <span className="project-task-title">
                            {isAttention && <span className="project-task-alert">⚠ </span>}
                            #{task.id} {task.title}
                          </span>
                          <span className="project-task-meta">
                            <StatusBadge status={task.status} />
                            <span className="muted small">{etapa}</span>
                            <span className="muted small mono">
                              {task.cost_spent.toFixed(2)} US$
                            </span>
                          </span>
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
