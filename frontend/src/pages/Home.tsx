import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { AlertIcon, CheckIcon, PlusIcon, ProjectsIcon } from "../components/Icons";
import ProposalCard from "../components/ProposalCard";
import StatusBadge from "../components/StatusBadge";
import TaskCard from "../components/TaskCard";
import { fmtCost } from "../lib/money";
import { usePolling } from "../lib/polling";
import { faseAtual, isToday, taskAttention, taskNeedsAttention, taskStats } from "../lib/tasks";
import type { Dashboard as DashboardData, MyProject, MyTask, Repository, TaskListItem, TaskProposal } from "../types";

function ProposalsSection({ proposals, repos, onChanged, onError }: {
  proposals: TaskProposal[];
  repos: Repository[];
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const repoNames = Object.fromEntries(repos.map((repo) => [repo.id, repo.name]));
  if (proposals.length === 0) return null;
  return (
    <section className="dash-section">
      <div className="dash-section-heading"><div><h3>Propostas de próximas tarefas <span className="dash-count">{proposals.length}</span></h3><p>Revise as sugestões e escolha o que entra no desenvolvimento.</p></div></div>
      <div className="proposal-list">
        {proposals.map((proposal) => (
          <ProposalCard key={proposal.id} proposal={proposal} repoNames={repoNames}
            parentRepoName={proposal.repository_id != null ? repoNames[proposal.repository_id] : undefined}
            parentDetailPath={proposal.repository_id != null ? `/${proposal.repository_id}/tasks` : undefined}
            onChanged={onChanged} onError={onError} />
        ))}
      </div>
    </section>
  );
}

function aguardaMinha(task: MyTask): boolean {
  return ["needs_review", "waiting_approval", "blocked", "failed"].includes(task.status);
}

function tempoRelativo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 0) return "";
  const min = Math.floor(ms / 60000);
  if (min < 1) return "agora";
  if (min < 60) return `há ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `há ${h} h`;
  return `há ${Math.floor(h / 24)} d`;
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
  const [showAllAttention, setShowAllAttention] = useState(false);
  const [showAllMyTasks, setShowAllMyTasks] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [myTasks, setMyTasks] = useState<MyTask[] | null>(null);
  const [myProjects, setMyProjects] = useState<MyProject[] | null>(null);
  const [myTasksError, setMyTasksError] = useState("");
  const [myProjectsError, setMyProjectsError] = useState("");
  const [myTasksLoading, setMyTasksLoading] = useState(false);
  const [myProjectsLoading, setMyProjectsLoading] = useState(false);

  const load = (signal?: AbortSignal) =>
    Promise.all([api.getDashboard(undefined, signal), api.listRepositories(signal), api.listTasks(undefined, signal)])
      .then(([dashboard, repositories, taskList]) => {
        setDash(dashboard);
        setRepos(repositories);
        setTasks(taskList);
        setUpdatedAt(new Date());
        setError("");
      })
      .catch((e) => { if (!signal?.aborted) setError(String(e)); });

  const loadMyTasks = (signal?: AbortSignal) => {
    setMyTasksLoading(true);
    return api.getMyTasks(signal)
      .then((list) => { setMyTasks(list); setMyTasksError(""); })
      .catch((e) => { if (!signal?.aborted) setMyTasksError(String(e)); })
      .finally(() => setMyTasksLoading(false));
  };

  const loadMyProjects = (signal?: AbortSignal) => {
    setMyProjectsLoading(true);
    return api.getMyProjects(signal)
      .then((list) => { setMyProjects(list); setMyProjectsError(""); })
      .catch((e) => { if (!signal?.aborted) setMyProjectsError(String(e)); })
      .finally(() => setMyProjectsLoading(false));
  };

  const loggedIn = user != null;
  usePolling(load, 10000, []);
  usePolling((signal) => {
    if (!loggedIn) return;
    void loadMyTasks(signal);
    void loadMyProjects(signal);
  }, 10000, [loggedIn]);

  const addRepo = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createRepository({ name, url, default_branch: branch });
      setName("");
      setUrl("");
      setBranch("main");
      await load();
    } catch (err) { setError(String(err)); }
  };

  const refreshTasks = () => {
    void load();
    if (loggedIn) void loadMyTasks();
  };
  const renderMyTask = (task: MyTask) => {
    const fullTask = tasks.find((item) => item.id === task.id);
    if (fullTask) return <TaskCard key={task.id} task={fullTask} detailPath={`/${task.repository_id}/tasks`} repoName={task.repository_name} onChanged={refreshTasks} onError={setError} />;
    return (
      <Link key={task.id} to={`/${task.repository_id}/tasks/${task.id}/workspace${aguardaMinha(task) ? "#diagnostico" : ""}`} className="card dash-history-card">
        <div className="resumo-line"><span className="muted small">#{task.id} · {task.repository_name}</span><StatusBadge status={task.status} /></div>
        <strong>{task.title}</strong>
        {aguardaMinha(task) && <p className="dash-task-reason">Abra o workspace para conferir o motivo e decidir como continuar.</p>}
        <div className="resumo-line small"><span>{fmtCost(task.cost_spent)}</span><span>{tempoRelativo(task.updated_at)}</span><span>Abrir workspace →</span></div>
      </Link>
    );
  };

  if (user) {
    const sortedMyTasks = [...(myTasks ?? [])].sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
    const attention = sortedMyTasks.filter(aguardaMinha);
    const otherTasks = sortedMyTasks.filter((task) => !aguardaMinha(task)).sort((a, b) => {
      const active = (task: MyTask) => ["in_progress", "queued", "open"].includes(task.status) ? 0 : ["done", "cancelled"].includes(task.status) ? 2 : 1;
      return active(a) - active(b);
    });
    const activeCount = sortedMyTasks.filter((task) => ["queued", "in_progress"].includes(task.status)).length;
    return (
      <div className="dash-page">
        <header className="dash-header"><div><span className="dash-eyebrow">SEU PAINEL</span><h2>Olá, {user.name}</h2><p>Suas prioridades, suas entregas e os projetos que você acompanha.</p></div><div className="dash-header-actions"><Link to="/execucao" className="link-btn">Acompanhar execução →</Link></div></header>
        {error && <div className="section-error" role="alert"><span>{error}</span><button onClick={() => void load()}>Tentar novamente</button></div>}
        <div className="dash-metrics">
          <a href="#minha-atencao" className={`card dash-metric${attention.length ? " dash-metric-alert" : ""}`}><span className="dash-metric-label">Precisam de você</span><strong className="dash-metric-value">{myTasks ? attention.length : "…"}</strong><span className="dash-metric-note">Falhas, bloqueios e decisões</span></a>
          <a href="#minhas-tarefas" className="card dash-metric dash-metric-run"><span className="dash-metric-label">Em andamento</span><strong className="dash-metric-value">{myTasks ? activeCount : "…"}</strong><span className="dash-metric-note">Suas tarefas em execução ou na fila</span></a>
          <div className="card dash-metric dash-metric-ok"><span className="dash-metric-label">Concluídas hoje</span><strong className="dash-metric-value">{myTasks ? myTasks.filter((task) => task.status === "done" && isToday(task.updated_at)).length : "…"}</strong><span className="dash-metric-note">{myTasks?.length ?? 0} tarefas atribuídas a você</span></div>
          <a href="#meus-projetos" className="card dash-metric"><span className="dash-metric-label">Meus projetos</span><strong className="dash-metric-value">{myProjects?.length ?? "…"}</strong><span className="dash-metric-note">Seu espaço de trabalho</span></a>
        </div>

        {myTasksLoading && myTasks === null ? <div className="task-grid" aria-label="Carregando tarefas"><div className="skeleton card" style={{ height: 220 }} /><div className="skeleton card" style={{ height: 220 }} /></div> : myTasksError ? <div className="section-error" role="alert"><span>{myTasksError}</span><button onClick={() => void loadMyTasks()}>Tentar novamente</button></div> : myTasks !== null && (
          <>
            <section id="minha-atencao" className="dash-section dash-attention">
              <div className="dash-section-heading"><div><h3><AlertIcon size={19} /> Sua próxima ação <span className="dash-count">{attention.length}</span></h3><p>Veja o que impediu a entrega e escolha como seguir.</p></div>{attention.length > 6 && <button className="dash-section-action" onClick={() => setShowAllAttention(!showAllAttention)}>{showAllAttention ? "Mostrar recentes" : `Ver todas (${attention.length})`}</button>}</div>
              {attention.length ? <div className="task-grid">{(showAllAttention ? attention : attention.slice(0, 6)).map(renderMyTask)}</div> : <div className="card dash-empty dash-empty-ok"><CheckIcon size={24} /><div><strong>Nenhuma pendência com você</strong><p>As tarefas que precisarem de uma decisão aparecerão aqui.</p></div></div>}
            </section>
            <section id="minhas-tarefas" className="dash-section">
              <div className="dash-section-heading"><div><h3>Minhas tarefas <span className="dash-count">{otherTasks.length}</span></h3><p>Acompanhe o que está em curso e consulte suas entregas.</p></div>{otherTasks.length > 6 && <button className="dash-section-action" onClick={() => setShowAllMyTasks(!showAllMyTasks)}>{showAllMyTasks ? "Mostrar recentes" : `Ver todas (${otherTasks.length})`}</button>}</div>
              {otherTasks.length ? <div className="task-grid">{(showAllMyTasks ? otherTasks : otherTasks.slice(0, 6)).map(renderMyTask)}</div> : <div className="card dash-empty"><p>{myTasks.length ? "Suas outras tarefas aparecerão aqui." : "Nenhuma tarefa atribuída a você."}</p></div>}
            </section>
          </>
        )}

        <section id="meus-projetos" className="dash-section">
          <div className="dash-section-heading"><div><h3>Meus projetos</h3><p>Entre em um projeto para acompanhar todas as suas tarefas.</p></div></div>
          {myProjectsLoading && myProjects === null ? <div className="project-grid"><div className="skeleton card" style={{ height: 150 }} /><div className="skeleton card" style={{ height: 150 }} /></div> : myProjectsError ? <div className="section-error" role="alert"><span>{myProjectsError}</span><button onClick={() => void loadMyProjects()}>Tentar novamente</button></div> : myProjects === null ? <p className="muted">Carregando projetos…</p> : myProjects.length === 0 ? <div className="card dash-empty"><p>Você ainda não participa de nenhum projeto.</p></div> : (
            <div className="project-grid dash-project-grid">{myProjects.map((project) => <Link key={project.id} to={`/${project.id}`} className="project-card"><div className="project-card-head"><span className="dash-project-mark"><ProjectsIcon size={21} /></span><span className="project-card-arrow">→</span></div><div className="project-card-name">{project.name}</div><div className="project-card-meta"><span className="muted small">{project.role === "admin" ? "Administrador do projeto" : "Membro"}</span></div><div className="project-card-stats"><span>{project.my_tasks_total} tarefas</span><span className="project-card-active">{project.my_tasks_active} ativas</span>{project.my_tasks_pending > 0 && <span>{project.my_tasks_pending} aguardando</span>}</div></Link>)}</div>
          )}
        </section>
        <ProposalsSection proposals={dash?.proposals ?? []} repos={repos} onChanged={() => void load()} onError={setError} />
      </div>
    );
  }

  if (!dash) return <div className="dash-page"><header className="dash-header"><div><span className="dash-eyebrow">CENTRAL DE ACOMPANHAMENTO</span><h2>Visão geral</h2></div></header>{error ? <div className="section-error" role="alert"><span>{error}</span><button onClick={() => void load()}>Tentar novamente</button></div> : <div className="dash-metrics" aria-label="Carregando painel">{[1, 2, 3, 4].map((item) => <div key={item} className="card skeleton" style={{ height: 130 }} />)}</div>}</div>;

  const attention = tasks.filter(taskNeedsAttention).sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
  const running = tasks.filter((task) => ["queued", "in_progress"].includes(task.status));
  const stats = taskStats(tasks);
  const tasksByRepo: Record<number, TaskListItem[]> = {};
  for (const task of tasks) (tasksByRepo[task.repository_id] ??= []).push(task);
  const sortedRepos = [...repos].sort((a, b) => (tasksByRepo[b.id] ?? []).filter(taskNeedsAttention).length - (tasksByRepo[a.id] ?? []).filter(taskNeedsAttention).length);
  const repoNames = Object.fromEntries(repos.map((repo) => [repo.id, repo.name]));

  return (
    <div className="dash-page">
      <header className="dash-header"><div><span className="dash-eyebrow">CENTRAL DE ACOMPANHAMENTO</span><h2>Visão geral</h2><p>Encontre o que precisa de atenção e acompanhe cada entrega.</p></div><div className="dash-header-actions"><Link to="/execucao" className="link-btn">Acompanhar execução →</Link><a className="link-btn primary" href="#novo-projeto" onClick={() => { const form = document.getElementById("novo-projeto"); if (form instanceof HTMLDetailsElement) form.open = true; }}><PlusIcon size={16} /> Novo projeto</a></div></header>
      {error && <div className="section-error" role="alert"><span>{error}</span><button onClick={() => void load()}>Tentar novamente</button></div>}
      <div className="dash-metrics">
        <a className={`card dash-metric${attention.length ? " dash-metric-alert" : ""}`} href="#atencao"><span className="dash-metric-label">Precisam de atenção</span><strong className="dash-metric-value">{attention.length}</strong><span className="dash-metric-note">Falhas, bloqueios e decisões</span></a>
        <Link className="card dash-metric dash-metric-run" to="/execucao"><span className="dash-metric-label">Em andamento</span><strong className="dash-metric-value">{running.length}</strong><span className="dash-metric-note">{running.filter((task) => task.status === "in_progress").length} em execução · {running.filter((task) => task.status === "queued").length} na fila</span></Link>
        <div className="card dash-metric dash-metric-ok"><span className="dash-metric-label">Concluídas hoje</span><strong className="dash-metric-value">{stats.doneToday}</strong><span className="dash-metric-note">{repos.length} projetos · {dash.total_tasks} tarefas no total</span></div>
        <div className="card dash-metric"><span className="dash-metric-label">Investimento acumulado</span><strong className="dash-metric-value">{fmtCost(dash.total_cost)}</strong><span className="dash-metric-note">Custo das tarefas em todos os projetos</span></div>
      </div>
      <div className="dash-updated">{updatedAt && `Atualizado às ${updatedAt.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · atualização automática`}{tasks.length >= 100 && " · Atividade das 100 tarefas mais recentes"}</div>

      <section id="atencao" className="dash-section dash-attention">
        <div className="dash-section-heading"><div><h3><AlertIcon size={19} /> Sua próxima ação <span className="dash-count">{attention.length}</span></h3><p>O motivo da interrupção e o caminho para retomar, em um só lugar.</p></div>{attention.length > 6 && <button className="dash-section-action" onClick={() => setShowAllAttention(!showAllAttention)}>{showAllAttention ? "Mostrar recentes" : `Ver todas (${attention.length})`}</button>}</div>
        {attention.length ? <div className="task-grid">{(showAllAttention ? attention : attention.slice(0, 6)).map((task) => <TaskCard key={task.id} task={task} detailPath={`/${task.repository_id}/tasks`} repoName={repoNames[task.repository_id]} onChanged={() => void load()} onError={setError} />)}</div> : <div className="card dash-empty dash-empty-ok"><CheckIcon size={24} /><div><strong>Nenhuma tarefa precisa da sua atenção</strong><p>As falhas, os bloqueios e os pedidos de decisão aparecerão aqui.</p></div></div>}
      </section>

      {dash.notices.length > 0 && <details className="card dash-notices"><summary><AlertIcon size={17} /> Alertas de execução e orçamento <span className="dash-count">{dash.notices.length}</span></summary><div className="notices">{dash.notices.map((notice, index) => <Link key={`${notice.kind}-${notice.task_id}-${index}`} to={`/${notice.repository_id}/tasks/${notice.task_id}/workspace`} className={`notice notice-${notice.level}`}><div className="notice-line"><strong>#{notice.task_id} {notice.task_title}</strong><StatusBadge status={notice.task_status} /></div><p>{notice.message}</p><span className="small">Conferir no workspace →</span></Link>)}</div></details>}

      <section className="dash-section">
        <div className="dash-section-heading"><div><h3>Seus projetos <span className="dash-count">{repos.length}</span></h3><p>Os projetos com pendências aparecem primeiro.</p></div></div>
        {repos.length === 0 ? <div className="card dash-empty"><ProjectsIcon size={28} /><div><strong>Seu próximo projeto começa aqui</strong><p>Adicione um repositório abaixo para criar e acompanhar suas tarefas.</p></div></div> : (
          <div className="project-grid dash-project-grid">
            {sortedRepos.map((repo) => {
              const repoTasks = tasksByRepo[repo.id] ?? [];
              const activeCount = repoTasks.filter((task) => ["queued", "in_progress"].includes(task.status)).length;
              const humanCount = repoTasks.filter(taskNeedsAttention).length;
              const repoStats = taskStats(repoTasks);
              const recentTasks = [...repoTasks].sort((a, b) => Number(taskNeedsAttention(b)) - Number(taskNeedsAttention(a)) || new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()).slice(0, 3);
              return (
                <div key={repo.id} className="project-card-wrapper dash-project-card">
                  <Link to={`/${repo.id}`} className="project-card">
                    <div className="project-card-head"><span className="dash-project-mark"><ProjectsIcon size={21} /></span><span className="project-card-arrow">→</span></div>
                    <div className="project-card-name">{repo.name}</div>
                    <div className="project-card-meta"><span className="muted mono small">{repo.url}</span></div>
                    {humanCount > 0 && <div className="project-card-human"><AlertIcon size={15} />{humanCount} {humanCount === 1 ? "tarefa precisa" : "tarefas precisam"} de atenção</div>}
                    <div className="project-card-stats"><span>{repoTasks.length} tarefas</span><span className="project-card-active">{activeCount} ativas</span>{repoStats.doneToday > 0 && <span>{repoStats.doneToday} {repoStats.doneToday === 1 ? "entrega" : "entregas"} hoje</span>}<span className="mono">{fmtCost(repoStats.spent)}</span></div>
                  </Link>
                  {recentTasks.length > 0 && <div className="project-tasks-mini">{recentTasks.map((task) => {
                    const concern = taskAttention(task);
                    const step = concern?.step ?? faseAtual(task);
                    return <Link key={task.id} to={`/${repo.id}/tasks/${task.id}/workspace${concern ? "#diagnostico" : ""}`} className={`project-task-row${concern ? concern.tone === "error" ? " project-task-row-err" : " project-task-row-warn" : ""}`}><div className="project-task-line"><span className="project-task-title">#{task.id} {task.title}</span><StatusBadge status={task.status} /></div>{concern && <p className="dash-task-reason" title={concern.detail}>{concern.detail}</p>}<div className="project-task-sub"><span>{task.status === "done" ? "Entrega concluída" : step ? `Fase ${step.position + 1} · ${step.robot?.name ?? "Agente"}` : "Ainda não iniciada"}</span><span className="muted">{tempoRelativo(task.updated_at)}</span>{concern && <span>Ver diagnóstico →</span>}</div></Link>;
                  })}</div>}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <ProposalsSection proposals={dash.proposals} repos={repos} onChanged={() => void load()} onError={setError} />
      <details id="novo-projeto" className="card add-section dash-config">
        <summary><PlusIcon size={17} /> Novo projeto</summary>
        <form className="form-stack" onSubmit={addRepo}>
          <div className="form-field"><label className="form-label" htmlFor="repo-name">Nome do projeto</label><input id="repo-name" value={name} onChange={(event) => setName(event.target.value)} required placeholder="Ex.: Portal de clientes" /></div>
          <div className="form-field"><label className="form-label" htmlFor="repo-url">URL do repositório</label><input id="repo-url" value={url} onChange={(event) => setUrl(event.target.value)} required placeholder="https://github.com/organização/projeto.git" /></div>
          <div className="form-field"><label className="form-label" htmlFor="repo-branch">Branch principal</label><input id="repo-branch" value={branch} onChange={(event) => setBranch(event.target.value)} /></div>
          <div className="form-actions"><button type="submit">Adicionar projeto</button></div>
        </form>
      </details>
    </div>
  );
}
