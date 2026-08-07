import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Repositories from "./pages/Repositories";
import Resumo from "./pages/Resumo";
import Robots from "./pages/Robots";
import Pipelines from "./pages/Pipelines";
import Tasks from "./pages/Tasks";
import TaskDetail from "./pages/TaskDetail";

const navItems = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/resumo", label: "Resumo", end: false },
  { to: "/repositories", label: "Repositórios" },
  { to: "/robots", label: "Robôs" },
  { to: "/pipelines", label: "Pipelines" },
  { to: "/tasks", label: "Tarefas" },
];

export default function App() {
  return (
    <div className="layout">
      <nav className="sidebar">
        <h1 className="brand">autoia</h1>
        <ul>
          {navItems.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/resumo" element={<Resumo />} />
          <Route path="/repositories" element={<Repositories />} />
          <Route path="/robots" element={<Robots />} />
          <Route path="/pipelines" element={<Pipelines />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/tasks/:id" element={<TaskDetail />} />
        </Routes>
      </main>
    </div>
  );
}
