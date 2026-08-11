import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api } from "./api";
import { DashboardIcon, PipelinesIcon, ProjectsIcon, RobotsIcon, TasksIcon, TerminalIcon } from "./components/Icons";
import Home from "./pages/Home";
import Repositories from "./pages/Repositories";
import Robots from "./pages/Robots";
import Pipelines from "./pages/Pipelines";
import RepoDashboard from "./pages/RepoDashboard";
import RepoTasks from "./pages/RepoTasks";
import PhaseDetail from "./pages/PhaseDetail";
import TaskDetail from "./pages/TaskDetail";
import Execution from "./pages/Execution";
import Notifications from "./components/Notifications";
import type { Repository } from "./types";

/** Extrai o repoId numérico do pathname atual, ou null se não for um projeto. */
function useRepoId(): number | null {
  const location = useLocation();
  const segments = location.pathname.split("/").filter(Boolean);
  if (segments.length > 0 && /^\d+$/.test(segments[0])) {
    return Number(segments[0]);
  }
  return null;
}

/** Renderiza Robots/Pipelines no escopo de um projeto (repoId do path). */
function RepoScoped({ route }: { route: "/robots" | "/pipelines" }) {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);
  if (route === "/robots") return <Robots repoId={repoId} />;
  return <Pipelines repoId={repoId} />;
}

export default function App() {
  const repoId = useRepoId();
  const location = useLocation();
  const [repos, setRepos] = useState<Repository[]>([]);
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
  }, [location.pathname]);

  useEffect(() => {
    const check = () => {
      api.getWorkerStatus()
        .then((s) => setWorkerAlive(s.alive))
        .catch(() => setWorkerAlive(false));
    };
    check();
    const timer = setInterval(check, 5000);
    return () => clearInterval(timer);
  }, []);

  const currentRepo = repoId != null ? repos.find((r) => r.id === repoId) : null;

  /** Se o path atual pertence ao projeto (ex.: /1, /1/tasks, /1/robots). */
  const inRepo = (id: number) => {
    const prefix = `/${id}`;
    return (
      location.pathname === prefix ||
      location.pathname.startsWith(`${prefix}/`)
    );
  };

  return (
    <div className="layout">
      <header className="topbar">
        <span className="topbar-brand">autoia</span>
        <div className="topbar-right">
          <Notifications />
          <div className="worker-status">
            <span className={`worker-dot${workerAlive === null ? "" : workerAlive ? " worker-dot-on" : " worker-dot-off"}`} />
            <span className="worker-label">
              {workerAlive === null ? "verificando…" : workerAlive ? "worker ativo" : "worker offline"}
            </span>
          </div>
        </div>
      </header>
      <div className="layout-body">
      <nav className="sidebar">
        <div className="sidebar-section">
          <div className="sidebar-label">Projetos</div>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            <ProjectsIcon size={16} /> Todos os projetos
          </NavLink>
          <NavLink to="/execucao" className={({ isActive }) => (isActive ? "active" : "")}>
            <TerminalIcon size={16} /> Execução
          </NavLink>

          <div className="sidebar-projects">
            {repos.map((repo) => (
              <NavLink
                key={repo.id}
                to={`/${repo.id}`}
                title={repo.name}
                className={inRepo(repo.id) ? "active" : ""}
              >
                <span className="sidebar-project-dot" />
                <span className="sidebar-project-link-name">{repo.name}</span>
              </NavLink>
            ))}
          </div>

          {currentRepo && (
            <div className="sidebar-sub">
              <div className="sidebar-project-name">{currentRepo.name}</div>
              <NavLink
                to={`/${currentRepo.id}`}
                end
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                <DashboardIcon size={16} /> Dashboard
              </NavLink>
              <NavLink
                to={`/${currentRepo.id}/tasks`}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                <TasksIcon size={16} /> Tarefas
              </NavLink>
              <NavLink
                to={`/${currentRepo.id}/robots`}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                <RobotsIcon size={16} /> Robôs
              </NavLink>
              <NavLink
                to={`/${currentRepo.id}/pipelines`}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                <PipelinesIcon size={16} /> Pipelines
              </NavLink>
            </div>
          )}
        </div>

        <div className="sidebar-section">
          <div className="sidebar-label">Configuração</div>
          <NavLink to="/robots" className={({ isActive }) => (isActive ? "active" : "")}>
            <RobotsIcon size={16} /> Robôs
          </NavLink>
          <NavLink to="/pipelines" className={({ isActive }) => (isActive ? "active" : "")}>
            <PipelinesIcon size={16} /> Pipelines
          </NavLink>
        </div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/execucao" element={<Execution />} />
          <Route path="/robots" element={<Robots />} />
          <Route path="/pipelines" element={<Pipelines />} />
          <Route path="/repositories" element={<Repositories />} />
          <Route path="/:repoId" element={<RepoDashboard />} />
          <Route path="/:repoId/tasks" element={<RepoTasks />} />
          <Route path="/:repoId/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/:repoId/tasks/:taskId/phase/:stepId" element={<PhaseDetail />} />
          <Route
            path="/:repoId/robots"
            element={<RepoScoped route="/robots" />}
          />
          <Route
            path="/:repoId/pipelines"
            element={<RepoScoped route="/pipelines" />}
          />
        </Routes>
      </main>
      </div>
    </div>
  );
}
