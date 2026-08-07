import { useEffect, useState } from "react";
import { api } from "../api";
import type { Dashboard as DashboardData } from "../types";

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState("");

  const load = () =>
    api
      .getDashboard()
      .then(setData)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>Carregando…</p>;

  const statuses = Object.entries(data.tasks_by_status);
  return (
    <div>
      <h2>Dashboard</h2>
      <div className="cards">
        <div className="card">
          <div className="card-value">{data.total_tasks}</div>
          <div className="card-label">tarefas</div>
        </div>
        <div className="card">
          <div className="card-value">{data.total_cost.toFixed(2)}</div>
          <div className="card-label">custo estimado (US$)</div>
        </div>
        <div className="card">
          <div className="card-value">{data.guardrail_events}</div>
          <div className="card-label">bloqueios de guardrail</div>
        </div>
        <div className="card">
          <div className="card-value">
            {statuses.map(([status, count]) => (
              <span key={status} className="status-inline">
                {status}: {count}
              </span>
            ))}
            {statuses.length === 0 && <span className="status-inline">—</span>}
          </div>
          <div className="card-label">tarefas por status</div>
        </div>
      </div>

      <h3>Últimos bloqueios de guardrail</h3>
      {data.recent_guardrails.length === 0 ? (
        <p className="muted">Nenhum bloqueio registrado.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Quando</th>
              <th>Motivo</th>
            </tr>
          </thead>
          <tbody>
            {data.recent_guardrails.map((event) => (
              <tr key={event.id}>
                <td>{new Date(event.ts).toLocaleString()}</td>
                <td className="mono">
                  {String((event.payload as { pattern?: string }).pattern ?? event.payload.detail)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
