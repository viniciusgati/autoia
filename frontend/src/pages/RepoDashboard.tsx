import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { AlertIcon, CheckIcon, PlusIcon, SettingsIcon } from "../components/Icons";
import PhaseStepper from "../components/PhaseStepper";
import StatusBadge from "../components/StatusBadge";
import TaskCard from "../components/TaskCard";
import { fmtCost } from "../lib/money";
import { usePolling } from "../lib/polling";
import { taskNeedsAttention, taskStats } from "../lib/tasks";
import type { Pipeline, Repository, TaskListItem } from "../types";

export default function RepoDashboard() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [showAllFinished, setShowAllFinished] = useState(false);

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

  usePolling(load, 10000, [repoId]);

  useEffect(() => {
    setTasks([]);
    setUpdatedAt(null);
    setRepo(null);
    setShowAllFinished(false);
    const controller = new AbortController();
    api.listRepositories(controller.signal).then((repos) => {
      setRepo(repos.find((r) => r.id === repoId) ?? null);
    }).catch(() => {});
    api.listPipelines(repoId, controller.signal).then(setPipelines).catch(() => {});
    return () => controller.abort();
  }, [repoId]);

  const sorted = [...tasks].sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
  const attention = sorted.filter(taskNeedsAttention);
  const running = sorted.filter((task) => !taskNeedsAttention(task) && ["queued", "in_progress"].includes(task.status));
  const waiting = sorted.filter((task) => !taskNeedsAttention(task) && !["queued", "in_progress", "done", "cancelled"].includes(task.status));
  const finished = sorted.filter((task) => ["done", "cancelled"].includes(task.status));
  const stats = taskStats(tasks);
  const renderCards = (items: TaskListItem[]) => (
    <div className="task-grid">
      {items.map((task) => (
        <TaskCard key={task.id} task={task} detailPath={`/${repoId}/tasks`} onChanged={() => void load()} onError={setError} />
      ))}
    </div>
  );

  return (
    <div className="resumo dash-page">
      <header className="dash-header">
        <div>
          <span className="dash-eyebrow">PAINEL DO PROJETO</span>
          <h2>{repo?.name ?? "Dashboard do projeto"}</h2>
          <p>Acompanhe as entregas e veja onde sua decisão faz diferença.</p>
        </div>
        <div className="dash-header-actions">
          <Link to={`/${repoId}/config`} className="link-btn"><SettingsIcon size={16} /> Configuração</Link>
          <Link to={`/${repoId}/tasks`} className="link-btn primary"><PlusIcon size={16} /> Nova tarefa</Link>
        </div>
      </header>

      {error && <div className="section-error" role="alert"><span>{error}</span><button onClick={() => void load()}>Tentar novamente</button></div>}

      <div className="dash-metrics">
        <a className={`card dash-metric${attention.length ? " dash-metric-alert" : ""}`} href="#atencao">
          <span className="dash-metric-label">Precisam de atenção</span>
          <strong className="dash-metric-value">{updatedAt ? attention.length : "…"}</strong>
          <span className="dash-metric-note">Falhas, bloqueios e decisões</span>
        </a>
        <a className="card dash-metric dash-metric-run" href="#andamento">
          <span className="dash-metric-label">Em andamento</span>
          <strong className="dash-metric-value">{updatedAt ? running.length : "…"}</strong>
          <span className="dash-metric-note">{running.filter((task) => task.status === "in_progress").length} em execução · {running.filter((task) => task.status === "queued").length} na fila</span>
        </a>
        <a className="card dash-metric dash-metric-ok" href="#entregas">
          <span className="dash-metric-label">Concluídas hoje</span>
          <strong className="dash-metric-value">{updatedAt ? stats.doneToday : "…"}</strong>
          <span className="dash-metric-note">{tasks.filter((task) => task.status === "done").length} concluídas no projeto</span>
        </a>
        <div className="card dash-metric">
          <span className="dash-metric-label">Investimento acumulado</span>
          <strong className="dash-metric-value">{updatedAt ? fmtCost(stats.spent) : "…"}</strong>
          <span className="dash-metric-note">Em {tasks.length} {tasks.length === 1 ? "tarefa" : "tarefas"}</span>
        </div>
      </div>
      <div className="dash-updated">{updatedAt ? `Atualizado às ${updatedAt.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · atualização automática` : "Carregando tarefas…"}{tasks.length >= 100 && " · Indicadores das 100 tarefas mais recentes"}</div>

      {!updatedAt ? (
        <div className="task-grid" aria-label="Carregando tarefas"><div className="card skeleton" style={{ height: 220 }} /><div className="card skeleton" style={{ height: 220 }} /></div>
      ) : tasks.length === 0 ? (
        <div className="card dash-empty"><PlusIcon size={28} /><div><h3>Comece pela sua primeira ideia</h3><p>Crie uma tarefa para acompanhar o desenvolvimento neste painel.</p><Link to={`/${repoId}/tasks`} className="link-btn">Criar tarefa →</Link></div></div>
      ) : (
        <>
          <section id="atencao" className="dash-section dash-attention">
            <div className="dash-section-heading"><div><h3><AlertIcon size={19} /> Sua próxima ação <span className="dash-count">{attention.length}</span></h3><p>Abra o diagnóstico, confira o motivo e oriente a próxima tentativa.</p></div></div>
            {attention.length > 0 ? renderCards(attention) : <div className="card dash-empty dash-empty-ok"><CheckIcon size={24} /><div><strong>Nenhuma tarefa precisa da sua atenção</strong><p>As falhas e os pedidos de decisão aparecerão aqui.</p></div></div>}
          </section>
          <section id="andamento" className="dash-section">
            <div className="dash-section-heading"><div><h3>Em andamento <span className="dash-count">{running.length}</span></h3><p>Execuções em curso e tarefas aguardando sua vez.</p></div></div>
            {running.length > 0 ? renderCards(running) : <div className="card dash-empty"><p>Nenhuma tarefa em execução ou na fila neste momento.</p></div>}
          </section>
          {waiting.length > 0 && <section className="dash-section"><div className="dash-section-heading"><div><h3>Para continuar <span className="dash-count">{waiting.length}</span></h3><p>Rascunhos e tarefas pausadas, prontos para quando você quiser.</p></div></div>{renderCards(waiting)}</section>}
          <section id="entregas" className="dash-section">
            <div className="dash-section-heading"><div><h3>Histórico de entregas <span className="dash-count">{finished.length}</span></h3><p>Resultados concluídos e tarefas canceladas.</p></div>{finished.length > 6 && <button className="dash-section-action" onClick={() => setShowAllFinished(!showAllFinished)}>{showAllFinished ? "Mostrar recentes" : `Ver todas (${finished.length})`}</button>}</div>
            {finished.length === 0 ? <div className="card dash-empty"><p>As primeiras entregas aparecerão aqui.</p></div> : <div className="dash-history-grid">{(showAllFinished ? finished : finished.slice(0, 6)).map((task) => <Link to={`/${repoId}/tasks/${task.id}/workspace`} className="card dash-history-card" key={task.id}><div className="resumo-line"><span className="muted small">#{task.id}</span><StatusBadge status={task.status} /></div><strong>{task.title}</strong><PhaseStepper task={task} showLabels /><div className="resumo-line small"><span>{new Date(task.updated_at).toLocaleDateString("pt-BR")}</span><span>{fmtCost(task.cost_spent)}</span><span>Ver entrega →</span></div></Link>)}</div>}
          </section>
        </>
      )}

      {repo && (
        <details className="card dash-config">
          <summary><SettingsIcon size={17} /> Configuração do projeto <span className="muted small">Limites e preferências</span></summary>
          <div className="config-summary">
            <div><span className="task-head-label">Sandbox</span><span>{repo.sandbox ? repo.sandbox : "global (off)"}{repo.sandbox_profile ? ` · ${repo.sandbox_profile}` : ""}</span></div>
            <div><span className="task-head-label">Orçamento por tarefa</span><span>{repo.task_budget != null ? fmtCost(repo.task_budget) : "global"}</span></div>
            <div><span className="task-head-label">Timeout por fase</span><span>{repo.run_timeout != null ? `${repo.run_timeout}s` : "global"}</span></div>
            <div><span className="task-head-label">Sem progresso</span><span>{repo.no_progress_timeout != null ? `${repo.no_progress_timeout}s` : "global (300)"}</span></div>
            <div><span className="task-head-label">Max tentativas</span><span>{repo.max_attempts ?? "global"}</span></div>
            <div><span className="task-head-label">Max decisões PM</span><span>{repo.max_pm_decisions ?? "global"}</span></div>
            <div><span className="task-head-label">Pipeline padrão</span><span>{pipelines.find((pipeline) => pipeline.id === repo.default_pipeline_id)?.name ?? (repo.default_pipeline_id != null ? `#${repo.default_pipeline_id}` : "—")}</span></div>
            <div><span className="task-head-label">Resumo automático</span><span>{repo.auto_summary ? "sim" : "não"}</span></div>
            <div><span className="task-head-label">Tasks externas</span><span>{repo.allow_external_tasks ? "permitidas" : "não"}</span></div>
          </div>
          <Link to={`/${repoId}/config`} className="link-btn">Editar configuração →</Link>
        </details>
      )}
    </div>
  );
}
