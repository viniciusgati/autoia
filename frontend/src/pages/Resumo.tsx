import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import type { Task } from "../types";

/** Tela concisa de monitoramento (mobile-first): a evolução das tarefas num relance. */
export default function Resumo() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .listTasks()
        .then((list) => {
          setTasks(list);
          setUpdatedAt(new Date());
        })
        .catch((e) => setError(String(e)));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  if (error) return <p className="error">{error}</p>;

  const ativos = tasks.filter((t) => ["queued", "in_progress", "needs_review"].includes(t.status));
  const finalizados = tasks.filter((t) => !ativos.includes(t));

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Resumo</h2>
        <span className="muted">
          {updatedAt ? `atualizado ${updatedAt.toLocaleTimeString()}` : "carregando…"} ·{" "}
          {ativos.length} ativa(s)
        </span>
      </div>

      {tasks.length === 0 && <p className="muted">Nenhuma tarefa ainda.</p>}

      {ativos.map((task) => (
        <Link to={`/tasks/${task.id}`} className="resumo-card" key={task.id}>
          <div className="resumo-line">
            <span className="resumo-title">
              #{task.id} {task.title}
            </span>
            <StatusBadge status={task.status} />
          </div>
          <div className="resumo-line muted small">
            <span>{faseAtual(task)}</span>
            <span>US$ {task.cost_spent.toFixed(2)} / {task.budget_limit.toFixed(2)}</span>
          </div>
          {task.status === "needs_review" && (
            <div className="resumo-line small warn-text">⚠ aguardando revisão: {(task.error ?? "").slice(0, 80)}</div>
          )}
        </Link>
      ))}

      {finalizados.length > 0 && (
        <>
          <h3 className="resumo-section">Finalizadas</h3>
          {finalizados.map((task) => (
            <Link to={`/tasks/${task.id}`} className="resumo-card muted" key={task.id}>
              <div className="resumo-line">
                <span className="resumo-title">
                  #{task.id} {task.title}
                </span>
                <StatusBadge status={task.status} />
              </div>
              <div className="resumo-line small">
                {task.status === "done" ? `concluída por ${task.cost_spent.toFixed(2)} US$` : (task.error ?? "").slice(0, 90)}
              </div>
            </Link>
          ))}
        </>
      )}
    </div>
  );
}

function faseAtual(task: Task): string {
  const step = [...task.steps].sort((a, b) => a.position - b.position)[task.current_step] ?? task.steps[0];
  if (!step) return "";
  const nome = step.robot?.name ?? "?";
  const estado = step.status === "running" ? "rodando" : step.status === "pending" ? "na fila" : step.status;
  return `${nome} (tentativa ${step.attempt}) · ${estado}`;
}
