import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import PhaseStepper from "../components/PhaseStepper";
import StatusIcon from "../components/StatusIcon";
import TaskCard from "../components/TaskCard";
import { fmtCost } from "../lib/money";
import { usePolling } from "../lib/polling";
import { taskStats } from "../lib/tasks";
import type { Pipeline, Repository, TaskListItem } from "../types";

const ATIVOS = ["queued", "in_progress", "needs_review", "waiting_approval", "blocked"];

export default function RepoDashboard() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const load = async (signal?: AbortSignal) => {
    try {
      const list = await api.listTasks(repoId, signal);
      setTasks(list);
      setUpdatedAt(new Date());
      setError("");
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    }
  };

  // Poll do dashboard do projeto: 10s é suficiente (sem chat ao vivo).
  usePolling(load, 10000, [repoId]);

  useEffect(() => {
    api.listRepositories().then((repos) => {
      setRepo(repos.find((r) => r.id === repoId) ?? null);
    }).catch(() => {});
    api.listPipelines(repoId).then(setPipelines).catch(() => {});
  }, [repoId]);

  if (error) return <p className="error">{error}</p>;

  const ativas = tasks.filter((t) => ATIVOS.includes(t.status));
  const finalizadas = tasks.filter((t) => !ativas.includes(t));
  const stats = taskStats(tasks);

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Dashboard do projeto</h2>
        <span className="muted">
          {updatedAt ? `atualizado ${updatedAt.toLocaleTimeString()}` : "carregando…"} ·{" "}
          {ativas.length} ativa(s)
        </span>
      </div>

      <div className="cards" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-value">{stats.doneToday}</div>
          <div className="card-label">concluídas hoje</div>
        </div>
        <div className="card">
          <div className="card-value">{fmtCost(stats.spent)}</div>
          <div className="card-label">gasto total</div>
        </div>
        <div className="card">
          <div className="card-value">{ativas.length}</div>
          <div className="card-label">tarefas ativas</div>
        </div>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}/tasks`} className="link-btn">
          + Nova tarefa
        </Link>
        <Link to={`/${repoId}/config`} className="link-btn">
          ⚙ Configuração
        </Link>
      </div>

      {error && <p className="error">{error}</p>}
      {tasks.length === 0 && <p className="muted">Nenhuma tarefa neste projeto.</p>}

      {/* Tarefas ativas — mesmo card compacto da página Execução. */}
      {ativas.length > 0 && (
        <div className="task-grid" style={{ marginTop: 14 }}>
          {ativas.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              detailPath={`/${repoId}/tasks`}
              onChanged={load}
              onError={setError}
            />
          ))}
        </div>
      )}

      {/* Configuração em resumo (somente leitura) — edição na página própria */}
      {repo && (
        <div className="card" style={{ marginTop: 24 }}>
          <div className="card-title">
            <strong>Configuração do projeto</strong>
            <Link to={`/${repoId}/config`} className="link-btn">
              editar configuração →
            </Link>
          </div>
          <div className="config-summary">
            <div>
              <span className="task-head-label">Sandbox</span>
              <span>{repo.sandbox ? repo.sandbox : "global (off)"}</span>
            </div>
            <div>
              <span className="task-head-label">Orçamento por tarefa</span>
              <span>{repo.task_budget != null ? fmtCost(repo.task_budget) : "global"}</span>
            </div>
            <div>
              <span className="task-head-label">Timeout por fase</span>
              <span>{repo.run_timeout != null ? `${repo.run_timeout}s` : "global"}</span>
            </div>
            <div>
              <span className="task-head-label">Max tentativas</span>
              <span>{repo.max_attempts ?? "global"}</span>
            </div>
            <div>
              <span className="task-head-label">Max decisões PM</span>
              <span>{repo.max_pm_decisions ?? "global"}</span>
            </div>
            <div>
              <span className="task-head-label">Pipeline padrão</span>
              <span>
                {pipelines.find((p) => p.id === repo.default_pipeline_id)?.name ??
                  (repo.default_pipeline_id != null ? `#${repo.default_pipeline_id}` : "—")}
              </span>
            </div>
            <div>
              <span className="task-head-label">Resumo automático</span>
              <span>{repo.auto_summary ? "sim" : "não"}</span>
            </div>
            <div>
              <span className="task-head-label">Tasks externas</span>
              <span>{repo.allow_external_tasks ? "permitidas" : "não"}</span>
            </div>
          </div>
        </div>
      )}

      {/* Tarefas finalizadas */}
      {finalizadas.length > 0 && (
        <>
          <h3 className="resumo-section">Finalizadas</h3>
          {finalizadas.map((task) => (
            <Link
              to={`/${repoId}/tasks/${task.id}`}
              className="resumo-card muted"
              key={task.id}
            >
              <div className="resumo-line">
                <span className="resumo-title">
                  #{task.id} {task.title}
                </span>
                <StatusIcon status={task.status} />
              </div>
              <PhaseStepper task={task} showLabels />
              <div className="resumo-line small">
                {task.status === "done" ? (
                  `concluída · ${fmtCost(task.cost_spent)}`
                ) : (
                  <span className="resumo-error" title={task.error ?? undefined}>
                    {task.error || "sem detalhes"}
                  </span>
                )}
              </div>
            </Link>
          ))}
        </>
      )}
    </div>
  );
}
