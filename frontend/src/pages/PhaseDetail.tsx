import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import ArtifactGallery from "../components/ArtifactGallery";
import StatusBadge from "../components/StatusBadge";
import { formatToolCall } from "../lib/events";
import Markdown from "../lib/markdown";
import type { RunEvent, Task } from "../types";

/* ═══════════════════════════════════════════
   Constantes e helpers (extraídos do TaskDetail)
   ═══════════════════════════════════════════ */

const FILTROS = [
  { id: "todos", label: "todos" },
  { id: "comandos", label: "comandos" },
  { id: "textos", label: "textos" },
  { id: "alertas", label: "alertas" },
] as const;

type Filtro = (typeof FILTROS)[number]["id"];

const KINDS_COMANDOS = new Set(["tool_call", "tool_result"]);
const KINDS_ALERTAS = new Set([
  "guardrail_blocked", "budget_hit", "bounce_back", "pm_decision", "pm_skip",
  "system", "phase_done", "merged", "merge_failed", "arch_metric",
  "post_merge_failed", "worker_recovered",
]);

function matchFiltro(kind: string, filtro: Filtro): boolean {
  if (filtro === "comandos") return KINDS_COMANDOS.has(kind);
  if (filtro === "textos") return kind === "assistant_text" || kind === "prompt";
  if (filtro === "alertas") return KINDS_ALERTAS.has(kind);
  return true;
}

function eventSummary(event: RunEvent): string {
  const payload = event.payload as Record<string, unknown>;
  switch (event.kind) {
    case "assistant_text": return String(payload.content ?? "");
    case "tool_call": return formatToolCall(event);
    case "tool_result": {
      const content = String(payload.content ?? "");
      return content.length > 300 ? content.slice(0, 300) + "…" : content;
    }
    case "guardrail_blocked": return `⛔ guardrail: ${String(payload.detail ?? payload.pattern ?? "")}`;
    case "pm_decision": return `🤖 PM: ${String(payload.action ?? "")} — ${String(payload.reason ?? "")}`;
    case "bounce_back": return `↩️ voltou da fase ${String(payload.from_position ?? "?")}: ${String(payload.reason ?? "")}`;
    case "phase_done": return `✅ fase concluída → próxima ${String(payload.next ?? "?")}`;
    case "merged": return `🔀 merge realizado: ${String(payload.detail ?? "")}`;
    case "merge_failed": return `⚠️ merge falhou: ${String(payload.detail ?? "")}`;
    case "budget_hit": return `💰 orçamento estourado: ${String(payload.reason ?? "")}`;
    case "arch_metric": return `📐 arquitetura ${String(payload.level ?? "?")} (${String(payload.score ?? "?")}/100)`;
    case "prompt": return `prompt da fase (${String(payload.robot ?? "robô")})`;
    default: return JSON.stringify(payload);
  }
}

function eventFull(event: RunEvent): string {
  return JSON.stringify(event.payload, null, 2);
}

interface Tentativa {
  attempt: number | null;
  eventos: RunEvent[];
}

function separarTentativas(eventos: RunEvent[]): Tentativa[] {
  const tentativas: Tentativa[] = [];
  let atual: RunEvent[] = [];
  let attempt: number | null = null;
  const flush = () => {
    if (atual.length) tentativas.push({ attempt, eventos: atual });
    atual = [];
    attempt = null;
  };
  for (const e of eventos) {
    if (e.kind === "attempt_started") {
      flush();
      attempt = (e.payload as { attempt?: number }).attempt ?? null;
      continue;
    }
    atual.push(e);
  }
  flush();
  return tentativas;
}

/* ═══════════════════════════════════════════
   ChatMessage
   ═══════════════════════════════════════════ */

function ChatMessage({
  event,
  isOpen,
  onToggle,
}: {
  event: RunEvent;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const payload = event.payload as Record<string, unknown>;
  const hora = new Date(event.ts).toLocaleTimeString();

  if (event.kind === "prompt") {
    return (
      <div className="chat-msg">
        <span className="chat-avatar chat-avatar-worker">w</span>
        <div className="chat-bubble">
          <div className="chat-who">
            worker pede <span className="muted small">· {hora}</span>
          </div>
          <details className="chat-prompt">
            <summary>
              prompt da fase — {String(payload.robot ?? "robô")} ·{" "}
              {String(payload.prompt ?? "").length} chars
            </summary>
            <pre className="chat-body">{String(payload.prompt ?? "")}</pre>
          </details>
        </div>
      </div>
    );
  }

  if (event.kind === "assistant_text") {
    const conteudo = String(payload.content ?? "");
    return (
      <div className="chat-msg">
        <span className="chat-avatar chat-avatar-kimi">k</span>
        <div className="chat-bubble chat-bubble-kimi">
          <div className="chat-who">
            kimi diz <span className="muted small">· {hora}</span>
            {event.cost > 0 ? <span className="muted small"> · +{event.cost.toFixed(2)} US$</span> : null}
          </div>
          <div className="chat-body">
            {conteudo ? <Markdown text={conteudo} /> : "(sem texto)"}
          </div>
          <button className="link-btn" onClick={onToggle}>
            {isOpen ? "recolher" : "ver JSON"}
          </button>
          {isOpen && <pre className="event-payload">{eventFull(event)}</pre>}
        </div>
      </div>
    );
  }

  if (event.kind === "tool_call") {
    return (
      <div className="chat-msg">
        <span className="chat-avatar chat-avatar-tool">⌘</span>
        <div className="chat-bubble">
          <div className="chat-who">
            kimi executa <span className="muted small">· {hora}</span>
            {event.cost > 0 ? <span className="muted small"> · +{event.cost.toFixed(2)} US$</span> : null}
          </div>
          <div className="chat-body mono">{formatToolCall(event)}</div>
          <button className="link-btn" onClick={onToggle}>
            {isOpen ? "recolher" : "ver JSON"}
          </button>
          {isOpen && <pre className="event-payload">{eventFull(event)}</pre>}
        </div>
      </div>
    );
  }

  if (event.kind === "tool_result") {
    const content = String(payload.content ?? "");
    return (
      <div className="chat-msg">
        <span className="chat-avatar chat-avatar-tool">→</span>
        <div className="chat-bubble">
          <div className="chat-who">
            resultado <span className="muted small">· {hora}</span>
          </div>
          <div className="chat-body chat-body-result">
            {content.length > 400 ? content.slice(0, 400) + "…" : content || "(vazio)"}
          </div>
          <button className="link-btn" onClick={onToggle}>
            {isOpen ? "recolher" : "ver completo"}
          </button>
          {isOpen && <pre className="event-payload">{eventFull(event)}</pre>}
        </div>
      </div>
    );
  }

  return (
    <div className={`chat-marker chat-marker-${event.kind}`}>
      <span className="mono time">{hora}</span> {eventSummary(event)}
    </div>
  );
}

/* ═══════════════════════════════════════════
   PhaseDetail page
   ═══════════════════════════════════════════ */

export default function PhaseDetail() {
  const { repoId, taskId: taskIdStr, stepId: stepIdStr } = useParams<{
    repoId: string;
    taskId: string;
    stepId: string;
  }>();
  const taskId = Number(taskIdStr);
  const stepId = Number(stepIdStr);

  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [log, setLog] = useState("");
  const [view, setView] = useState<"chat" | "transcript">("chat");
  const [filtro, setFiltro] = useState<Filtro>("todos");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [error, setError] = useState("");

  const step = task?.steps.find((s) => s.id === stepId) ?? null;
  const active = task ? ["queued", "in_progress", "needs_review", "blocked"].includes(task.status) : false;

  useEffect(() => {
    api.getTask(taskId)
      .then(setTask)
      .catch((e) => setError(String(e)));
    const timer = setInterval(() => {
      api.getTask(taskId).then(setTask).catch(() => {});
    }, 1500);
    return () => clearInterval(timer);
  }, [taskId]);

  useEffect(() => {
    const load = () => {
      api.listEvents(stepId).then(setEvents).catch(() => {});
      api.getLog(stepId).then(setLog).catch(() => {});
    };
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [stepId]);

  const refresh = () =>
    api.getTask(taskId).then(setTask).catch((e) => setError(String(e)));

  const retry = async (position: number) => {
    try {
      await api.retryStep(taskId, position);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const subtaskRetry = async (position: number) => {
    try {
      await api.retrySubtask(taskId, position);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const toggleExpanded = (eventId: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(eventId)) next.delete(eventId);
      else next.add(eventId);
      return next;
    });
  };

  const eventosVisiveis = events.filter((e) => matchFiltro(e.kind, filtro));

  if (error) return <p className="error">{error}</p>;
  if (!task || !step) return <p>Carregando…</p>;

  const canRetry = step.status === "failed" || step.status === "guardrail_blocked";
  const canBounceBack = task.status !== "created" && step.status === "done";

  return (
    <div>
      {/* Breadcrumb */}
      <Link to={`/${repoId}/tasks/${taskId}`} className="phase-detail-back">
        ← voltar para a tarefa
      </Link>

      {/* Header */}
      <h2>
        Fase {step.position} · {step.robot?.name ?? `robô ${step.robot?.id ?? "?"}`}
      </h2>
      <div className="meta">
        <StatusBadge status={step.status} />
        <span>tentativa {step.attempt}</span>
        {step.verdict && <span>· veredicto: {step.verdict}</span>}
        {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
        {step.started_at && <span className="muted">início: {new Date(step.started_at).toLocaleString()}</span>}
        {step.finished_at && <span className="muted">fim: {new Date(step.finished_at).toLocaleString()}</span>}
      </div>

      {/* Actions */}
      <div style={{ display: "flex", gap: 10, marginBottom: 18 }}>
        {canRetry && (
          <button className="danger" onClick={() => retry(step.position)}>
            repetir fase {step.position}
          </button>
        )}
        {canBounceBack && (
          <button className="warn-btn" onClick={() => retry(step.position)}>
            ← voltar para esta fase
          </button>
        )}
      </div>

      {/* Diff stat */}
      {step.diff_stat && (
        <>
          <h3>Alterações</h3>
          <pre className="diff-stat">{step.diff_stat}</pre>
        </>
      )}

      {/* Error */}
      {step.error && (
        <>
          <h3>Erro</h3>
          <pre className="step-error">{step.error}</pre>
        </>
      )}

      {/* Summary / Relatório completo */}
      <h3>Relatório do robô</h3>
      {step.summary ? (
        <div className="card">
          <Markdown text={step.summary} />
        </div>
      ) : (
        <p className="muted">
          {step.status === "running"
            ? "Robô está executando — o relatório será gerado ao final da fase."
            : step.status === "pending"
              ? "Fase aguardando execução."
              : "Nenhum relatório gerado."}
        </p>
      )}

      <ArtifactGallery stepId={step.id} />      {/* Subtarefas */}
      {task.subtasks && task.subtasks.length > 0 &&
       (step.robot?.role === "implement" || step.robot?.role === "verify") && (
        <>
          <h3>Subtarefas</h3>
          <div className="subtask-list">
            {[...task.subtasks].sort((a, b) => a.position - b.position).map((st) => (
              <div key={st.id} className={`subtask-card subtask-${st.status}`}>
                <span className="subtask-pos">{st.position + 1}</span>
                <div className="subtask-body">
                  <div className="subtask-head">
                    <strong>{st.title}</strong>
                    <StatusBadge status={st.status} />
                    <span className="muted small">tentativa {st.attempt}</span>
                    {st.verdict && <span className="muted small">· veredicto: {st.verdict}</span>}
                  </div>
                  {st.description && <p className="muted small">{st.description}</p>}
                  {st.summary && (
                    <details className="subtask-summary">
                      <summary>resumo da fase</summary>
                      <Markdown text={st.summary} />
                    </details>
                  )}
                  {st.error && <div className="error small">{st.error}</div>}
                  {st.acceptance_criteria && (
                    <details className="muted small" style={{ marginTop: 4 }}>
                      <summary>critérios ({st.acceptance_criteria.length} chars)</summary>
                      <pre className="step-error">{st.acceptance_criteria}</pre>
                    </details>
                  )}
                </div>
                <div className="subtask-actions">
                  {(st.status === "failed" || (st.status === "pending" && st.attempt > 1)) && (
                    <button className="danger small" onClick={() => subtaskRetry(st.position)}>
                      repetir
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Execução detalhada */}
      <h3>Execução detalhada</h3>

      <div className="meta">
        <div className="view-toggle">
          <button className={view === "chat" ? "view-active" : ""} onClick={() => setView("chat")}>
            chat
          </button>
          <button className={view === "transcript" ? "view-active" : ""} onClick={() => setView("transcript")}>
            transcript
          </button>
        </div>
        <div className="view-toggle">
          {FILTROS.map((f) => (
            <button
              key={f.id}
              className={filtro === f.id ? "view-active" : ""}
              onClick={() => setFiltro(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {view === "chat" && (
        <div className="chat">
          {separarTentativas(events).map((tent, ti) => {
            const visiveis = tent.eventos.filter((e) => matchFiltro(e.kind, filtro));
            if (visiveis.length === 0) return null;
            return (
              <div key={ti} className="tentativa">
                <div className="tentativa-head">
                  <span className="tentativa-n">tentativa {tent.attempt ?? "?"}</span>
                  <span className="muted small">
                    fase {step.position} · {step.robot?.name ?? "?"}
                  </span>
                </div>
                {visiveis.map((e) => (
                  <ChatMessage
                    key={e.id}
                    event={e}
                    isOpen={expanded.has(e.id)}
                    onToggle={() => toggleExpanded(e.id)}
                  />
                ))}
              </div>
            );
          })}
          {eventosVisiveis.length === 0 && (
            <p className="muted">
              Sem eventos neste filtro{active ? " — aguardando execução…" : ""}.
            </p>
          )}
        </div>
      )}

      {view === "transcript" && (
        <div className="transcript">
          {eventosVisiveis.map((event) => (
            <div key={event.id} className={`transcript-block transcript-${event.kind}`}>
              <div className="mono time">
                [{event.seq}] {new Date(event.ts).toLocaleTimeString()} · {event.kind}
                {event.cost > 0 ? ` · +${event.cost.toFixed(2)} US$` : ""}
              </div>
              <pre className="transcript-body">{eventFull(event)}</pre>
            </div>
          ))}
          {eventosVisiveis.length === 0 && (
            <p className="muted">
              Sem eventos neste filtro{active ? " — aguardando execução…" : ""}.
            </p>
          )}
        </div>
      )}

      <details className="raw-log-box">
        <summary>log bruto da fase (últimas ~5000 linhas)</summary>
        <pre className="raw-log">{log || "(vazio)"}</pre>
      </details>
    </div>
  );
}
