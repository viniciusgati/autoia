import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api";
import { DashboardIcon, PipelinesIcon, ProjectsIcon, RobotsIcon, TasksIcon } from "./components/Icons";
import Home from "./pages/Home";
import Repositories from "./pages/Repositories";
import Robots from "./pages/Robots";
import Pipelines from "./pages/Pipelines";
import RepoDashboard from "./pages/RepoDashboard";
import RepoTasks from "./pages/RepoTasks";
import PhaseDetail from "./pages/PhaseDetail";
import TaskDetail from "./pages/TaskDetail";
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

export default function App() {
  const repoId = useRepoId();
  const [repos, setRepos] = useState<Repository[]>([]);
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
  }, []);

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

  return (
    <div className="layout">
      <nav className="sidebar">
        <h1 className="brand">autoia</h1>

        <div className="sidebar-section">
          <div className="sidebar-label">Projetos</div>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            <ProjectsIcon size={16} /> Todos os projetos
          </NavLink>

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

        <div className="sidebar-section" style={{ marginTop: "auto" }}>
          <div className="sidebar-label">Sistema</div>
          <div className="worker-status">
            <span className={`worker-dot${workerAlive === null ? "" : workerAlive ? " worker-dot-on" : " worker-dot-off"}`} />
            <span className="worker-label">
              {workerAlive === null ? "verificando…" : workerAlive ? "worker ativo" : "worker offline"}
            </span>
          </div>
        </div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/robots" element={<Robots />} />
          <Route path="/pipelines" element={<Pipelines />} />
          <Route path="/repositories" element={<Repositories />} />
          <Route path="/:repoId" element={<RepoDashboard />} />
          <Route path="/:repoId/tasks" element={<RepoTasks />} />
          <Route path="/:repoId/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/:repoId/tasks/:taskId/phase/:stepId" element={<PhaseDetail />} />
        </Routes>
      </main>
    </div>
  );
}
