import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import ProposalCard from "../components/ProposalCard";
import StatusBadge from "../components/StatusBadge";
import StatusIcon from "../components/StatusIcon";
import { fmtCost } from "../lib/money";
import { usePolling } from "../lib/polling";
import type { Dashboard as DashboardData, MyProject, MyTask, Repository, TaskListItem, TaskProposal, TaskStepListItem } from "../types";

/** Seção de propostas de tasks filhas no dashboard: aparecem enquanto não forem
 *  rejeitadas (aceitas seguem com link para a task criada). */
function ProposalsSection({
  proposals,
  repos,
  onChanged,
  onError,
}: {
  proposals: TaskProposal[];
  repos: Repository[];
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const repoNames = Object.fromEntries(repos.map((r) => [r.id, r.name]));
  if (proposals.length === 0) return null;
  return (
    <section style={{ marginTop: 22 }}>
      <h3>Propostas aguardando decisão</h3>
      <div className="proposal-list">
        {proposals.map((p) => (
          <ProposalCard
            key={p.id}
            proposal={p}
            repoNames={repoNames}
            parentRepoName={p.repository_id != null ? repoNames[p.repository_id] : undefined}
            parentDetailPath={
              p.repository_id != null ? `/${p.repository_id}/tasks` : undefined
            }
            onChanged={onChanged}
            onError={onError}
          />
        ))}
      </div>
    </section>
  );
}

/** Fase em destaque da task: a que está rodando, senão a próxima da fila. */
function faseAtual(task: TaskListItem): TaskStepListItem | null {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    steps.find((s) => s.status === "running") ??
    steps.find((s) => s.status === "pending") ??
    steps[task.current_step] ??
    null
  );
}

/** Tarefa minha que precisa de ação humana (vai primeiro, com selo). */
function aguardaMinha(t: MyTask): boolean {
  return (
    t.status === "needs_review" ||
    t.status === "waiting_approval" ||
    t.status === "blocked"
  );
}

/** Tarefa precisa de iteração humana (revisão, aprovação, bloqueada ou guardrail). */
function precisaHumano(task: TaskListItem): boolean {
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
  const { user } = useAuth();
  const [dash, setDash] = useState<DashboardData | null>(null);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("main");

  // Dashboard pessoal (auth ON): tarefas do usuário + participações.
  const [myTasks, setMyTasks] = useState<MyTask[] | null>(null);
  const [myProjects, setMyProjects] = useState<MyProject[] | null>(null);
  const [myTasksError, setMyTasksError] = useState("");
  const [myProjectsError, setMyProjectsError] = useState("");
  const [myTasksLoading, setMyTasksLoading] = useState(false);
  const [myProjectsLoading, setMyProjectsLoading] = useState(false);

  const load = (signal?: AbortSignal) =>
    Promise.all([api.getDashboard(undefined, signal), api.listRepositories(signal), api.listTasks(undefined, signal)])
      .then(([d, r, t]) => {
        setDash(d);
        setRepos(r);
        setTasks(t);
        setError("");
      })
      .catch((e) => {
        if (!signal?.aborted) setError(String(e));
      });

  const loadMyTasks = (signal?: AbortSignal) => {
    setMyTasksLoading(true);
    return api
      .getMyTasks(signal)
      .then((t) => {
        setMyTasks(t);
        setMyTasksError("");
      })
      .catch((e) => {
        if (!signal?.aborted) setMyTasksError(String(e));
      })
      .finally(() => setMyTasksLoading(false));
  };

  const loadMyProjects = (signal?: AbortSignal) => {
    setMyProjectsLoading(true);
    return api
      .getMyProjects(signal)
      .then((p) => {
        setMyProjects(p);
        setMyProjectsError("");
      })
      .catch((e) => {
        if (!signal?.aborted) setMyProjectsError(String(e));
      })
      .finally(() => setMyProjectsLoading(false));
  };

  const loggedIn = user != null;

  // Poll global da Home: 10s (dados agregados + listas leves).
  usePolling(load, 10000, []);

  // Poll das seções pessoais (só com usuário logado).
  usePolling(
    (signal) => {
      if (!loggedIn) return;
      void loadMyTasks(signal);
      void loadMyProjects(signal);
    },
    10000,
    [loggedIn],
  );

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

  // ── Dashboard pessoal (auth ON): tarefas do usuário + participações ──
  if (user) {
    const sortedMyTasks = [...(myTasks ?? [])].sort((a, b) => {
      const aW = aguardaMinha(a) ? 1 : 0;
      const bW = aguardaMinha(b) ? 1 : 0;
      if (aW !== bW) return bW - aW;
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
    });
    const awaitCount = (myTasks ?? []).filter(aguardaMinha).length;

    return (
      <div>
        <h2>Olá, {user.name}</h2>

        <div className="cards">
          <div className="card">
            <div className="card-value">{myProjects?.length ?? "…"}</div>
            <div className="card-label">projetos</div>
          </div>
          <div className="card">
            <div className="card-value">{myTasks?.length ?? "…"}</div>
            <div className="card-label">tarefas</div>
          </div>
          <div className="card">
            <div className="card-value">{awaitCount}</div>
            <div className="card-label">aguardando você</div>
          </div>
        </div>

        {/* Minhas tarefas */}
        <h3>Minhas tarefas</h3>
        {myTasksLoading && myTasks === null ? (
          <div className="my-list-skeleton">
            <div className="skeleton" style={{ height: 52 }} />
            <div className="skeleton" style={{ height: 52 }} />
            <div className="skeleton" style={{ height: 52 }} />
          </div>
        ) : myTasksError ? (
          <div className="section-error">
            <span>{myTasksError}</span>
            <button onClick={() => void loadMyTasks()}>Tentar novamente</button>
          </div>
        ) : myTasks === null ? (
          <p className="muted">carregando…</p>
        ) : myTasks.length === 0 ? (
          <p className="muted">Nenhuma tarefa atribuída a você</p>
        ) : (
          <div className="my-tasks">
            {sortedMyTasks.map((t) => (
              <Link
                key={t.id}
                to={`/${t.repository_id}/tasks/${t.id}`}
                className="my-task-row"
              >
                <div className="my-task-line">
                  <span className="my-task-title">#{t.id} {t.title}</span>
                  {aguardaMinha(t) && (
                    <span className="badge badge-warn my-task-await">aguardando você</span>
                  )}
                  <StatusIcon status={t.status} />
                </div>
                <div className="my-task-sub">
                  <span>{t.repository_name}</span>
                  <span className="mono">{fmtCost(t.cost_spent)}</span>
                  <span className="muted">{tempoRelativo(t.updated_at)}</span>
                </div>
              </Link>
            ))}
          </div>
        )}

        {/* Meus projetos */}
        <h3>Meus projetos</h3>
        {myProjectsLoading && myProjects === null ? (
          <div className="my-list-skeleton">
            <div className="skeleton" style={{ height: 88 }} />
            <div className="skeleton" style={{ height: 88 }} />
          </div>
        ) : myProjectsError ? (
          <div className="section-error">
            <span>{myProjectsError}</span>
            <button onClick={() => void loadMyProjects()}>Tentar novamente</button>
          </div>
        ) : myProjects === null ? (
          <p className="muted">carregando…</p>
        ) : myProjects.length === 0 ? (
          <p className="muted">Você ainda não participa de nenhum projeto</p>
        ) : (
          <div className="project-grid">
            {myProjects.map((p) => (
              <Link key={p.id} to={`/${p.id}`} className="project-card">
                <div className="project-card-head">
                  <span className="project-card-name">{p.name}</span>
                  <span className="project-card-arrow">→</span>
                </div>
                <div className="project-card-meta">
                  <span className="muted small">
                    {p.role === "admin" ? "admin do projeto" : "membro"}
                  </span>
                </div>
                <div className="project-card-stats">
                  <span>{p.my_tasks_total} tarefas</span>
                  {p.my_tasks_active > 0 && (
                    <span className="project-card-active">{p.my_tasks_active} ativas</span>
                  )}
                  {p.my_tasks_pending > 0 && (
                    <span className="project-card-active">{p.my_tasks_pending} aguardando</span>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}

        {/* Propostas de tasks filhas (do dashboard pessoal, escopado aos meus projetos) */}
        <ProposalsSection
          proposals={dash?.proposals ?? []}
          repos={repos}
          onChanged={() => void load()}
          onError={setError}
        />
      </div>
    );
  }

  if (!dash || !repos.length === undefined) return <p>Carregando…</p>;

  // Agrupa tarefas por repositório, ordenadas por id decrescente (mais recentes primeiro)
  const tasksByRepo: Record<number, TaskListItem[]> = {};
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
          <div className="card-value">{fmtCost(dash.total_cost)}</div>
          <div className="card-label">custo estimado (R$)</div>
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

      {/* Propostas de tasks filhas aguardando decisão humana */}
      <ProposalsSection
        proposals={dash.proposals}
        repos={repos}
        onChanged={() => void load()}
        onError={setError}
      />

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
                            <StatusIcon status={task.status} />
                          </div>
                          <div className="project-task-sub">
                            <span>{etapa}</span>
                            <span className="mono">{fmtCost(task.cost_spent)}</span>
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
