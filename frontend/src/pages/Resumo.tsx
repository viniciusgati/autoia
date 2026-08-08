import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import PhaseStepper from "../components/PhaseStepper";
import StatusBadge from "../components/StatusBadge";
import { formatToolCall } from "../lib/events";
import type { RunEvent, Task, TaskStep } from "../types";

/** Tela concisa de monitoramento (mobile-first): a evolução das tarefas num relance.
 *
 * Quando há uma fase rodando, um card fixo (sticky) mostra a sessão ativa: etapa
 * atual, comando atual (último tool_call do kimi) e os eventos ao vivo, além da
 * trilha de fases (caminho percorrido). Tarefas aguardando revisão humana trazem
 * as ações de aprovar/cancelar direto no card. As demais aparecem em cards enxutos.
 */

interface SessionData {
  taskId: number;
  stepId: number;
  toolCalls: RunEvent[]; // mais recente primeiro
  events: RunEvent[]; // mais recentes primeiro
}

const ATIVOS = ["queued", "in_progress", "needs_review"];

export default function Resumo() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [session, setSession] = useState<SessionData | null>(null);
  const [showSession, setShowSession] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const load = async () => {
    try {
      const list = await api.listTasks();
      setTasks(list);
      setUpdatedAt(new Date());

      const runningTask =
        list.find((t) => t.steps.some((s) => s.status === "running")) ?? null;
      const runningStep = runningTask?.steps.find((s) => s.status === "running") ?? null;
      if (!runningTask || !runningStep) {
        setSession(null);
        return;
      }
      try {
        const [toolCalls, events] = await Promise.all([
          api.listEvents(runningStep.id, "tool_call", "desc"),
          api.listEvents(runningStep.id, undefined, "desc"),
        ]);
        setSession({ taskId: runningTask.id, stepId: runningStep.id, toolCalls, events });
      } catch {
        setSession(null);
      }
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const review = async (task: Task, action: "approve" | "cancel") => {
    setBusy(task.id);
    setError("");
    try {
      await api.reviewTask(task.id, { action, extra_budget: 0 });
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (error) return <p className="error">{error}</p>;

  const ativas = tasks.filter((t) => ATIVOS.includes(t.status));
  const sessionTask = ativas.find((t) => t.steps.some((s) => s.status === "running")) ?? null;
  const listaAtivas = ativas.filter((t) => t !== sessionTask);
  const finalizadas = tasks.filter((t) => !ativas.includes(t));

  const runningStep = sessionTask?.steps.find((s) => s.status === "running") ?? null;
  const comandoAtual = session?.toolCalls[0] ? formatToolCall(session.toolCalls[0]) : "";

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Resumo</h2>
        <span className="muted">
          {updatedAt ? `atualizado ${updatedAt.toLocaleTimeString()}` : "carregando…"} ·{" "}
          {ativas.length} ativa(s)
        </span>
      </div>

      {tasks.length === 0 && <p className="muted">Nenhuma tarefa ainda.</p>}

      {sessionTask && runningStep && (
        <div className="session-card">
          <div className="session-head">
            <div className="session-title-wrap">
              <span className="session-eyebrow">sessão ativa</span>
              <Link to={`/tasks/${sessionTask.id}`} className="resumo-title">
                #{sessionTask.id} {sessionTask.title}
              </Link>
            </div>
            <StatusBadge status={sessionTask.status} />
          </div>

          <div className="session-grid">
            <div className="session-field">
              <span className="session-label">Etapa atual</span>
              <span className="session-value" title={etapaAtualLabel(sessionTask)}>
                {etapaAtualLabel(sessionTask)}
              </span>
            </div>
            <div className="session-field">
              <span className="session-label">Comando atual</span>
              <span className="session-value mono" title={comandoAtual}>
                {comandoAtual || "aguardando interação…"}
              </span>
            </div>
          </div>

          <PhaseStepper task={sessionTask} />

          <div className="session-foot">
            <span className="muted small">
              gasto {sessionTask.cost_spent.toFixed(2)} / {sessionTask.budget_limit.toFixed(2)}{" "}
              US$ · {tempoDecorrido(runningStep)}
            </span>
            {faseComDiff(sessionTask)?.diff_stat && (
              <span className="diff-summary" title={faseComDiff(sessionTask)!.diff_stat ?? undefined}>
                {diffSummary(faseComDiff(sessionTask)!.diff_stat)}
              </span>
            )}
            <div className="session-actions">
              <button className="link-btn" onClick={() => setShowSession((s) => !s)}>
                {showSession ? "ocultar dados" : "ver dados da sessão"}
              </button>
              <Link to={`/tasks/${sessionTask.id}`} className="link-btn">
                ver detalhes →
              </Link>
            </div>
          </div>

          {showSession && session && session.stepId === runningStep.id && (
            <ul className="session-events">
              {[...session.events].reverse().map((event) => (
                <li key={event.id} className={`session-event session-event-${event.kind}`}>
                  <span className="mono time">{new Date(event.ts).toLocaleTimeString()}</span>
                  <span className="kind">{event.kind}</span>
                  <span className="session-event-text">{sessionEventLine(event)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {listaAtivas.map((task) => {
        const needsReview = task.status === "needs_review";
        return (
          <div className={`resumo-card ${needsReview ? "resumo-card-review" : ""}`} key={task.id}>
            <div className="resumo-line">
              <Link to={`/tasks/${task.id}`} className="resumo-title">
                #{task.id} {task.title}
              </Link>
              <StatusBadge status={task.status} />
            </div>
            <PhaseStepper task={task} />
            <div className="resumo-line muted small">
              <span>{etapaAtualLabel(task)}</span>
              <span>
                {task.cost_spent.toFixed(2)} / {task.budget_limit.toFixed(2)} US$
                {faseComDiff(task)?.diff_stat && (
                  <> · <span className="diff-summary" title={faseComDiff(task)!.diff_stat ?? undefined}>{diffSummary(faseComDiff(task)!.diff_stat)}</span></>
                )}
              </span>
            </div>
            {needsReview && (
              <div className="review-box">
                <div className="review-title">⚠ Aguardando revisão humana</div>
                <pre className="review-error" title={task.error ?? undefined}>
                  {task.error || "sem detalhes"}
                </pre>
                <div className="review-actions">
                  <button onClick={() => review(task, "approve")} disabled={busy === task.id}>
                    aprovar e continuar
                  </button>
                  <button
                    className="danger"
                    onClick={() => review(task, "cancel")}
                    disabled={busy === task.id}
                  >
                    cancelar tarefa
                  </button>
                  <Link to={`/tasks/${task.id}`} className="link-btn">
                    ver detalhes →
                  </Link>
                </div>
              </div>
            )}
          </div>
        );
      })}

      {finalizadas.length > 0 && (
        <>
          <h3 className="resumo-section">Finalizadas</h3>
          {finalizadas.map((task) => (
            <Link to={`/tasks/${task.id}`} className="resumo-card muted" key={task.id}>
              <div className="resumo-line">
                <span className="resumo-title">
                  #{task.id} {task.title}
                </span>
                <StatusBadge status={task.status} />
              </div>
              <PhaseStepper task={task} />
              <div className="resumo-line small">
                {task.status === "done" ? (
                  `concluída · ${task.cost_spent.toFixed(2)} US$`
                ) : (
                  <span className="resumo-error" title={task.error ?? undefined}>
                    {task.error || "sem detalhes"}
                  </span>
                )}
                {faseComDiff(task)?.diff_stat && (
                  <span className="diff-summary" title={faseComDiff(task)!.diff_stat ?? undefined}>
                    {" · "}{diffSummary(faseComDiff(task)!.diff_stat)}
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

/** Fase em destaque da task: a que está rodando, senão a próxima da fila. */
function faseAtual(task: Task): TaskStep | null {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    steps.find((s) => s.status === "running") ??
    steps.find((s) => s.status === "pending") ??
    steps[task.current_step] ??
    null
  );
}

function etapaAtualLabel(task: Task): string {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const step = faseAtual(task);
  if (!step) return "";
  const nome = step.robot?.name ?? "?";
  const estado =
    step.status === "running"
      ? "rodando"
      : step.status === "pending"
        ? "na fila"
        : step.status;
  return `Fase ${step.position}/${steps.length} · ${nome} (tentativa ${step.attempt}) · ${estado}`;
}

/** Resumo de uma linha para os eventos ao vivo da sessão. */
function sessionEventLine(event: RunEvent): string {
  const payload = event.payload as Record<string, unknown>;
  switch (event.kind) {
    case "assistant_text":
      return String(payload.content ?? "").replace(/\s+/g, " ").trim().slice(0, 140);
    case "tool_call":
      return formatToolCall(event);
    case "tool_result": {
      const content = String(payload.content ?? "").replace(/\s+/g, " ").trim();
      return content.slice(0, 120) + (content.length > 120 ? "…" : "");
    }
    case "guardrail_blocked":
      return `⛔ ${String(payload.detail ?? payload.pattern ?? "")}`;
    case "bounce_back":
      return `↩️ voltou da fase ${String(payload.from_position ?? "?")}: ${String(
        payload.reason ?? "",
      )}`;
    case "phase_done":
      return `fase concluída → próxima ${String(payload.next ?? "?")}`;
    case "budget_hit":
      return `orçamento estourado: ${String(payload.reason ?? "")}`;
    case "system":
      return String(payload.reason ?? JSON.stringify(payload)).slice(0, 140);
    default:
      return JSON.stringify(payload).slice(0, 140);
  }
}

function tempoDecorrido(step: TaskStep): string {
  if (!step.started_at) return "";
  const ms = Date.now() - new Date(step.started_at).getTime();
  if (ms < 0) return "0s";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Extrai resumo legível de um diff_stat (git --stat): "3 arquivos, +45/-12". */
function diffSummary(diffStat: string | null): string | null {
  if (!diffStat) return null;
  const lines = diffStat.trim().split("\n");
  const last = lines[lines.length - 1];
  const match = last.match(/(\d+)\s+files?\s+changed(?:,\s*(\d+)\s+insertions?\(\+\))?(?:,\s*(\d+)\s+deletions?\(\-\))?/);
  if (!match) {
    // fallback: conta linhas com "|" (uma por arquivo)
    const fileLines = lines.filter((l) => l.includes("|")).length;
    if (fileLines > 0) return `${fileLines} arquivo(s) alterado(s)`;
    return null;
  }
  const files = match[1];
  const plus = match[2] ? `+${match[2]}` : "+0";
  const minus = match[3] ? `-${match[3]}` : "-0";
  if (plus === "+0" && minus === "-0") return null;
  return `${files} arquivo(s) · ${plus} ${minus}`;
}

/** Encontra a fase implement (primeira com diff_stat) para mostrar alterações. */
function faseComDiff(task: Task): TaskStep | null {
  return [...task.steps].sort((a, b) => a.position - b.position).find((s) => s.diff_stat) ?? null;
}
