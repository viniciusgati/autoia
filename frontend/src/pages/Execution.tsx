import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import StatusIcon from "../components/StatusIcon";
import TaskCompactCard from "../components/TaskCompactCard";
import { formatToolCall, sessionEventLine } from "../lib/events";
import { fmtBudget } from "../lib/money";
import { usePolling } from "../lib/polling";
import { faseAtual, taskNeedsAttention, tempoAtras, tempoDecorrido } from "../lib/tasks";
import type { Execution, Repository, RunEvent, TaskListItem } from "../types";

const STOPPED_STATUSES = ["paused", "created", "open"];
const SUPPLEMENTAL_STATUSES = ["failed", ...STOPPED_STATUSES];

export default function ExecutionPage() {
  const [data, setData] = useState<Execution | null>(null);
  const [recentTasks, setRecentTasks] = useState<TaskListItem[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [filter, setFilter] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");

  const repoNames = useMemo(() => {
    const names: Record<number, string> = {};
    for (const repo of repos) names[repo.id] = repo.name;
    return names;
  }, [repos]);

  const load = async (signal?: AbortSignal) => {
    const results = await Promise.allSettled([
      api.getExecution(filter ?? undefined, signal),
      api.listTasks(filter ?? undefined, signal),
    ]);
    if (signal?.aborted) return;
    const [execution, tasks] = results;
    if (execution.status === "fulfilled") setData(execution.value);
    if (tasks.status === "fulfilled") setRecentTasks(tasks.value);
    setError(execution.status === "rejected" ? `Não foi possível atualizar as execuções: ${String(execution.reason)}`
      : tasks.status === "rejected" ? "Não foi possível atualizar as falhas recentes. Tente novamente."
        : "");
  };

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
  }, []);

  usePolling(load, 5000, [filter]);

  if (!data) return <div className="exec-empty">{error ? <p className="error" role="alert">{error}</p> : "Carregando execuções…"}</div>;

  // O endpoint de execução entrega apenas estados ativos. A listagem existente
  // complementa com falhas recentes e tarefas manuais, sem alterar o backend.
  const merged = new Map(recentTasks.filter((task) => SUPPLEMENTAL_STATUSES.includes(task.status)).map((task) => [task.id, task]));
  for (const task of data.tasks) merged.set(task.id, task);
  const term = search.trim().toLocaleLowerCase("pt-BR");
  const tasks = [...merged.values()]
    .filter((task) => filter == null || task.repository_id === filter)
    .filter((task) => !term || `${task.id} ${task.title} ${task.error ?? ""} ${repoNames[task.repository_id] ?? ""}`.toLocaleLowerCase("pt-BR").includes(term))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id);
  const atencao = tasks.filter(taskNeedsAttention);
  const running = tasks.filter((task) => !taskNeedsAttention(task) && task.steps.some((step) => step.status === "running"));
  const runningIds = new Set(running.map((task) => task.id));
  const emFila = tasks.filter((task) => ["queued", "in_progress"].includes(task.status) && !runningIds.has(task.id));
  const paradas = tasks.filter((task) => STOPPED_STATUSES.includes(task.status) && !runningIds.has(task.id));

  const cards = (items: TaskListItem[]) => (
    <div className="exec-cards exec-cards-compact">
      {items.map((task) => <TaskCompactCard key={task.id} task={task} detailPath={`/${task.repository_id}/tasks`} repoName={repoNames[task.repository_id]} onChanged={() => void load()} onError={setError} />)}
    </div>
  );

  return (
    <div className="resumo exec-page">
      <header className="exec-hero">
        <div>
          <span className="task-card-eyebrow">CENTRAL DE ACOMPANHAMENTO</span>
          <h2>Execução</h2>
          <p className="muted">Veja o que precisa de uma decisão e acompanhe o trabalho em andamento.</p>
        </div>
        <span className="worker-status">
          <span className={`worker-dot ${data.worker.alive ? "worker-dot-on" : "worker-dot-off"}`} />
          {data.worker.alive ? "Execução automática disponível" : "Worker sem sinal recente"}
        </span>
      </header>

      {error && <div className="section-error" role="alert">{error} <button className="btn-sm" onClick={() => void load()}>Atualizar</button></div>}

      <nav className="exec-summary" aria-label="Resumo das execuções">
        <a className="exec-metric exec-metric-attention" href="#exec-attention"><span>Precisam de atenção</span><strong>{atencao.length}</strong><small>Revisões, bloqueios e falhas recentes</small></a>
        <a className="exec-metric" href="#exec-running"><span>Em execução</span><strong>{running.length}</strong><small>Fases trabalhando agora</small></a>
        <a className="exec-metric" href="#exec-queue"><span>Na fila</span><strong>{emFila.length}</strong><small>Aguardando uma nova execução</small></a>
        <a className="exec-metric" href="#exec-stopped"><span>Para continuar</span><strong>{paradas.length}</strong><small>Pausadas, novas ou em modo manual</small></a>
      </nav>

      <div className="exec-toolbar">
        <label><span>Projeto</span><select value={filter ?? ""} onChange={(event) => setFilter(event.target.value ? Number(event.target.value) : null)}>
          <option value="">Todos os projetos</option>
          {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
        </select></label>
        <label className="task-card-search"><span>Buscar tarefas</span><input type="search" placeholder="Título, número ou motivo da falha" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <span className="muted small">Atualização automática · 5 s</span>
      </div>

      {!data.worker.alive && emFila.length > 0 && <div className="exec-recovery-note">Há tarefas na fila e o worker está sem sinal recente. Elas continuam aguardando execução.</div>}

      <div className="exec-board">
        <section className="exec-section" id="exec-attention">
          <div className="exec-section-head"><div><h3>Precisa da sua atenção <span>{atencao.length}</span></h3><p>Comece pelo motivo da parada. No workspace, confira a evidência e oriente a correção.</p></div></div>
          {atencao.length ? cards(atencao) : <div className="exec-empty">Nenhuma pendência ou falha encontrada nas tarefas carregadas{term ? " para esta busca" : ""}.</div>}
        </section>

        <section className="exec-section" id="exec-running">
          <div className="exec-section-head"><div><h3>Em execução <span>{running.length}</span></h3><p>A atividade mais recente de cada fase, com acesso direto ao workspace.</p></div></div>
          {running.length ? <div className="exec-cards">{running.map((task) => {
            const step = task.steps.find((item) => item.status === "running");
            return <RunningSession key={task.id} task={task} events={step ? data.current_events[String(step.id)] ?? [] : []} repoNames={repoNames} />;
          })}</div> : <div className="exec-empty">Nenhuma fase em execução neste momento{term ? " para esta busca" : ""}.</div>}
        </section>

        <section className="exec-section" id="exec-queue">
          <div className="exec-section-head"><div><h3>Na fila <span>{emFila.length}</span></h3><p>O trabalho está aguardando o início da próxima fase.</p></div></div>
          {emFila.length ? cards(emFila) : <div className="exec-empty">Nenhuma tarefa aguardando execução.</div>}
        </section>

        <section className="exec-section" id="exec-stopped">
          <div className="exec-section-head"><div><h3>Para continuar <span>{paradas.length}</span></h3><p>Tarefas recentes que aguardam início, retomada ou uma orientação sua.</p></div></div>
          {paradas.length ? cards(paradas) : <div className="exec-empty">Nenhuma tarefa aguardando retomada.</div>}
        </section>
      </div>
    </div>
  );
}

function RunningSession({ task, events, repoNames }: { task: TaskListItem; events: RunEvent[]; repoNames: Record<number, string> }) {
  const runningStep = faseAtual(task);
  const currentEvents = [...events].sort((a, b) => b.seq - a.seq);
  const boundary = currentEvents.find((event) => event.kind === "attempt_started");
  const eventsThisRun = boundary ? currentEvents.filter((event) => event.seq >= boundary.seq) : currentEvents.filter((event) => !runningStep?.started_at || new Date(event.ts).getTime() >= new Date(runningStep.started_at).getTime());
  const toolCall = eventsThisRun.find((event) => event.kind === "tool_call");
  const activity = eventsThisRun.find((event) => ["assistant_text", "tool_call", "subtask_start", "subtask_implemented", "subtask_verified", "subtask_failed", "subtask_bounce_back"].includes(event.kind));
  const command = toolCall ? formatToolCall(toolCall) : null;
  const lastAt = activity ? new Date(activity.ts).getTime() : runningStep?.started_at ? new Date(runningStep.started_at).getTime() : null;
  const lastAgo = lastAt ? tempoAtras(new Date(lastAt).toISOString()) : null;
  const stale = lastAt ? Date.now() - lastAt > 120_000 : false;
  const workspacePath = `/${task.repository_id}/tasks/${task.id}/workspace`;

  return (
    <article className="session-card exec-session-card">
      <div className="task-card-eyebrow"><span>#{task.id} · {repoNames[task.repository_id] ?? "Projeto"}</span><span className="task-card-status"><StatusIcon status="running" />Em execução</span></div>
      <div className="session-head"><Link to={workspacePath} className="resumo-title">{task.title}</Link></div>
      <div className="task-card-stage">
        <span className="muted small">Fase em andamento</span>
        <strong>{runningStep ? `Fase ${runningStep.position + 1} · ${runningStep.robot?.name ?? "Agente"}` : "Preparando execução"}</strong>
        {runningStep && <span className="muted small">Tentativa {runningStep.attempt}{runningStep.started_at ? ` · ${tempoDecorrido(runningStep)}` : ""}</span>}
      </div>
      {runningStep && runningStep.attempt > 1 && <div className="exec-recovery-note">Nova tentativa em andamento. Acompanhe o resultado desta execução no workspace.</div>}
      <div className="exec-session-activity"><span className="session-label">Última atividade{lastAgo ? ` · ${lastAgo}` : ""}</span><p>{activity ? sessionEventLine(activity) || "O agente está trabalhando." : "Aguardando a primeira atividade desta execução…"}</p>{stale && <p className="exec-session-stale">Sem atividade há mais de 2 minutos — verifique se o agente não travou.</p>}</div>
      {command && <details className="exec-session-command"><summary>Ver última ferramenta utilizada</summary><pre>{command}</pre></details>}
      <div className="session-foot"><span className="muted small">Orçamento {fmtBudget(task.cost_spent, task.budget_limit)}</span><Link to={workspacePath} className="link-btn">Acompanhar →</Link></div>
    </article>
  );
}
