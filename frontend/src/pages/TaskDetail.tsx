import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import PhaseStepper from "../components/PhaseStepper";
import StatusBadge from "../components/StatusBadge";
import { formatToolCall } from "../lib/events";
import Markdown, { inlineMarkdown } from "../lib/markdown";
import type { RunEvent, Task, TaskStep } from "../types";

/** Página de detalhe da task: o pipeline de fases (cards horizontais) no topo; ao
 * clicar numa fase, aparece embaixo "O que aconteceu" (chat com os papéis
 * worker/kimi/ferramenta) + o relatório da fase. Quando aguardando revisão humana,
 * o header fixo avisa e leva ao card. */

const FILTROS = [
  { id: "todos", label: "todos" },
  { id: "comandos", label: "comandos" },
  { id: "textos", label: "textos" },
  { id: "alertas", label: "alertas" },
] as const;

type Filtro = (typeof FILTROS)[number]["id"];

const KINDS_COMANDOS = new Set(["tool_call", "tool_result"]);
const KINDS_ALERTAS = new Set([
  "guardrail_blocked",
  "budget_hit",
  "bounce_back",
  "pm_decision",
  "pm_skip",
  "system",
  "phase_done",
  "merged",
  "merge_failed",
  "arch_metric",
  "post_merge_failed",
  "worker_recovered",
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
    case "assistant_text":
      return String(payload.content ?? "");
    case "tool_call":
      return formatToolCall(event);
    case "tool_result": {
      const content = String(payload.content ?? "");
      return content.length > 300 ? content.slice(0, 300) + "…" : content;
    }
    case "guardrail_blocked":
      return `⛔ guardrail: ${String(payload.detail ?? payload.pattern ?? "")}`;
    case "pm_decision":
      return `🤖 PM: ${String(payload.action ?? "")} — ${String(payload.reason ?? "")}`;
    case "bounce_back":
      return `↩️ voltou da fase ${String(payload.from_position ?? "?")}: ${String(
        payload.reason ?? "",
      )}`;
    case "phase_done":
      return `✅ fase concluída → próxima ${String(payload.next ?? "?")}`;
    case "merged":
      return `🔀 merge realizado: ${String(payload.detail ?? "")}`;
    case "merge_failed":
      return `⚠️ merge falhou: ${String(payload.detail ?? "")}`;
    case "budget_hit":
      return `💰 orçamento estourado: ${String(payload.reason ?? "")}`;
    case "arch_metric":
      return `📐 arquitetura ${String(payload.level ?? "?")} (${String(payload.score ?? "?")}/100)`;
    case "prompt":
      return `prompt da fase (${String(payload.robot ?? "robô")})`;
    default:
      return JSON.stringify(payload);
  }
}

function eventFull(event: RunEvent): string {
  return JSON.stringify(event.payload, null, 2);
}

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

interface Tentativa {
  attempt: number | null;
  eventos: RunEvent[];
}

/** Separa os eventos da fase por tentativa de execução, usando os marcadores
 * `attempt_started` gravados pelo worker (bounce-back re-executa o mesmo step). */
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

/** Uma "mensagem" do chat da etapa: bolha para worker/kimi/ferramenta, linha para marcadores. */
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

  // marcadores (system, phase_done, merged, guardrail, ...)
  return (
    <div className={`chat-marker chat-marker-${event.kind}`}>
      <span className="mono time">{hora}</span> {eventSummary(event)}
    </div>
  );
}

export default function TaskDetail() {
  const { id } = useParams();
  const taskId = Number(id);
  const [task, setTask] = useState<Task | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [log, setLog] = useState("");
  const [view, setView] = useState<"chat" | "transcript">("chat");
  const [filtro, setFiltro] = useState<Filtro>("todos");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [runningToolCall, setRunningToolCall] = useState<RunEvent | null>(null);
  const [error, setError] = useState("");
  const [extraBudget, setExtraBudget] = useState(5);
  const [cancelNote, setCancelNote] = useState("");
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const [pmBusy, setPmBusy] = useState(false);

  useEffect(() => {
    const load = () =>
      api
        .getTask(taskId)
        .then((t) => {
          setTask(t);
          setFeedbackText((current) => current || t.feedback || "");
          if (t.steps.length > 0) {
            setSelected((current) => current ?? t.steps[0].id);
          }
          const running = t.steps.find((s) => s.status === "running");
          if (running) {
            api
              .listEvents(running.id, "tool_call", "desc")
              .then((evs) => setRunningToolCall(evs[0] ?? null))
              .catch(() => setRunningToolCall(null));
          } else {
            setRunningToolCall(null);
          }
        })
        .catch((e) => setError(String(e)));
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [taskId]);

  useEffect(() => {
    if (selected == null) return;
    const load = () => {
      api.listEvents(selected).then(setEvents).catch(() => undefined);
      api.getLog(selected).then(setLog).catch(() => undefined);
    };
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [selected]);

  const refresh = () => api.getTask(taskId).then(setTask).catch((e) => setError(String(e)));

  const retry = async (position: number) => {
    try {
      await api.retryStep(taskId, position, feedbackText.trim() || undefined);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const saveFeedback = async () => {
    setFeedbackBusy(true);
    try {
      await api.setFeedback(taskId, feedbackText);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setFeedbackBusy(false);
    }
  };

  const clearFeedback = async () => {
    setFeedbackBusy(true);
    try {
      await api.clearFeedback(taskId);
      setFeedbackText("");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setFeedbackBusy(false);
    }
  };

  const review = async (event: FormEvent, action: "approve" | "cancel") => {
    event.preventDefault();
    try {
      await api.reviewTask(taskId, { action, extra_budget: extraBudget, note: cancelNote || undefined });
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const pmDecide = async () => {
    setPmBusy(true);
    try {
      await api.pmDecide(taskId);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setPmBusy(false);
    }
  };

  const scrollToReview = () => {
    document.getElementById("revisao-humana")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const toggleExpanded = (eventId: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(eventId)) next.delete(eventId);
      else next.add(eventId);
      return next;
    });
  };

  const subtaskRetry = async (position: number) => {
    try {
      await api.retrySubtask(taskId, position);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p>Carregando…</p>;

  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const selectedStep = steps.find((s) => s.id === selected) ?? null;
  const active = ["queued", "in_progress", "needs_review"].includes(task.status);
  const pmCandidates = ["failed", "blocked", "needs_review"].includes(task.status);
  const runningStep = steps.find((s) => s.status === "running") ?? null;
  const eventosVisiveis = events.filter((e) => matchFiltro(e.kind, filtro));

  return (
    <div>
      <p>
        <Link to="/tasks">← tarefas</Link>
      </p>

      {/* Header fixo: estado da task sempre visível */}
      <div className="task-sticky">
        <h2>
          #{task.id} {task.title}
        </h2>
        <div className="meta">
          <StatusBadge status={task.status} />
          <span>
            branch: <code>{task.branch ?? "—"}</code>
          </span>
          <span>
            orçamento: <strong>{task.budget_limit.toFixed(2)}</strong> US$ · gasto:{" "}
            <strong>{task.cost_spent.toFixed(2)}</strong> US$
          </span>
          <span className="muted">decisões PM: {task.pm_decisions}</span>
        </div>
        <PhaseStepper task={task} />
        {runningStep && (
          <div className="task-live">
            <div>
              <span className="session-label">Etapa atual</span>
              <span className="task-live-value">{etapaAtualLabel(task)}</span>
            </div>
            <div>
              <span className="session-label">Comando atual</span>
              <span className="task-live-value mono">
                {runningToolCall ? formatToolCall(runningToolCall) : "aguardando interação…"}
              </span>
            </div>
          </div>
        )}
        {task.status === "needs_review" && (
          <div className="sticky-alert">
            <span>⚠ Aguardando revisão humana — o pipeline parou</span>
            <button onClick={scrollToReview}>ir para a revisão ↓</button>
          </div>
        )}
      </div>

      {task.error && <div className="error">{task.error}</div>}

      {task.status === "needs_review" && (
        <div className="card warn" id="revisao-humana">
          <div className="card-title">
            <strong>Aguardando revisão humana</strong>
          </div>
          <p className="muted small prewrap">
            O pipeline parou e precisa de você. <strong>Aprovar</strong> continua a
            execução (a próxima fase pendente roda; adicione orçamento se o limite estiver
            perto). <strong>Cancelar</strong> encerra a tarefa. Se o erro for de uma fase
            específica, use "repetir fase" abaixo.
          </p>
          <form className="form-inline" onSubmit={(e) => review(e, "approve")}>
            <label>
              + orçamento (US$):{" "}
              <input
                type="number"
                min={0}
                step={0.5}
                value={extraBudget}
                onChange={(e) => setExtraBudget(Number(e.target.value))}
                className="short"
              />
            </label>
            <button type="submit">aprovar e continuar</button>
          </form>
          <form className="form-inline" onSubmit={(e) => review(e, "cancel")}>
            <input
              placeholder="motivo da recusa (opcional)"
              value={cancelNote}
              onChange={(e) => setCancelNote(e.target.value)}
            />
            <button type="submit" className="danger">
              cancelar tarefa
            </button>
          </form>
        </div>
      )}

      {pmCandidates && (
        <div className="card">
          <div className="card-title">
            <strong>Robô PM (controle do projeto)</strong>
            <span className="muted">decide: retry · continuar (orçamento) · escalar para humano</span>
          </div>
          <button onClick={pmDecide} disabled={pmBusy}>
            {pmBusy ? "PM analisando…" : "🤖 PM decide agora"}
          </button>
        </div>
      )}

      {/* Pipeline de fases: cards horizontais clicáveis */}
      <h3>Fases do pipeline</h3>
      <div className="phase-cards">
        {steps.map((step) => {
          const estado =
            step.status === "running"
              ? "running"
              : step.status === "failed" || step.status === "guardrail_blocked"
                ? "failed"
                : step.status === "done"
                  ? "done"
                  : "pending";
          return (
            <button
              key={step.id}
              className={`phase-card phase-card-${estado} ${selected === step.id ? "phase-card-selected" : ""}`}
              onClick={() => setSelected(step.id)}
              title={`Fase ${step.position} · ${step.robot?.name ?? "?"} · ${step.status} · tentativa ${step.attempt}`}
            >
              <span className="phase-card-pos">{step.position}</span>
              <span className="phase-card-robot">{step.robot?.name ?? "?"}</span>
              <StatusBadge status={step.status} />
              <span className="phase-card-meta">
                tentativa {step.attempt}
                {step.verdict ? ` · ${step.verdict}` : ""}
              </span>
            </button>
          );
        })}
      </div>

      {selectedStep && (
        <>
          {(selectedStep.status === "failed" || selectedStep.status === "guardrail_blocked") && (
            <button className="danger" onClick={() => retry(selectedStep.position)}>
              repetir fase {selectedStep.position}
            </button>
          )}
          {task.status !== "created" && selectedStep.status === "done" && (
            <button className="warn-btn" onClick={() => retry(selectedStep.position)}>
              ← voltar para esta fase
            </button>
          )}
        </>
      )}

      {/* Subtarefas: mostradas quando a task tem subtarefas e a fase selecionada é implement/verify */}
      {task.subtasks && task.subtasks.length > 0 && selectedStep &&
       (selectedStep.robot?.role === "implement" || selectedStep.robot?.role === "verify") && (
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
                  {st.description && (
                    <p className="muted small">{st.description}</p>
                  )}
                  {st.summary && (
                    <details className="subtask-summary">
                      <summary>resumo da fase</summary>
                      <Markdown text={st.summary} />
                    </details>
                  )}
                  {st.error && <div className="error small">{st.error}</div>}
                  {st.acceptance_criteria && (
                    <details className="muted small" style={{marginTop: 4}}>
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

      {/* O que aconteceu: chat da fase selecionada */}
      <div className="task-section-head">
        <h3>
          O que aconteceu
          {selectedStep ? (
            <span className="muted">
              {" "}
              — fase {selectedStep.position} · {selectedStep.robot?.name ?? "?"} · tentativa
              atual {selectedStep.attempt} · {selectedStep.status}
              {selectedStep.verdict ? ` · veredicto: ${selectedStep.verdict}` : ""}
            </span>
          ) : null}
        </h3>
      </div>

      {selectedStep && (
        <>
          <div className="meta">
            <span className="muted small">
              cada tentativa aparece separada abaixo · os filtros valem para o chat
            </span>
            <div className="view-toggle">
              <button
                className={view === "chat" ? "view-active" : ""}
                onClick={() => setView("chat")}
              >
                chat
              </button>
              <button
                className={view === "transcript" ? "view-active" : ""}
                onClick={() => setView("transcript")}
              >
                transcript completo
              </button>
            </div>
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
                        fase {selectedStep.position} · {selectedStep.robot?.name ?? "?"}
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
                  Sem eventos neste filtro
                  {active ? " — aguardando execução…" : ""}.
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
                  Sem eventos neste filtro
                  {active ? " — aguardando execução…" : ""}.
                </p>
              )}
            </div>
          )}

          <details className="raw-log-box">
            <summary>log bruto da fase (últimas ~5000 linhas)</summary>
            <pre className="raw-log">{log || "(vazio)"}</pre>
          </details>

          {selectedStep.summary && (
            <>
              <h3>Relatório da fase {selectedStep.position}</h3>
              <div className="card">
                <Markdown text={selectedStep.summary} />
              </div>
            </>
          )}
          {selectedStep.error && (
            <>
              <h3>Erro da fase {selectedStep.position}</h3>
              <pre className="step-error">{selectedStep.error}</pre>
            </>
          )}
        </>
      )}

      {/* História */}
      <h3>História</h3>
      {task.description && (
        <div className="card">
          <div className="card-title">
            <strong>Descrição</strong>
          </div>
          <Markdown text={task.description} />
        </div>
      )}
      {task.acceptance_criteria && (
        <div className="card">
          <div className="card-title">
            <strong>Critérios de aceite</strong>
          </div>
          <ul className="criteria">
            {task.acceptance_criteria.split("\n").map((line, i) => {
              const match = line.match(/^\s*-?\s*\[( |x|X)\]\s*(.*)$/);
              if (!match) {
                return <li key={i}>{inlineMarkdown(line, `c${i}`)}</li>;
              }
              return (
                <li key={i}>
                  <span className={match[1] !== " " ? "criteria-done" : ""}>
                    {match[1] !== " " ? "☑" : "☐"}{" "}
                    {inlineMarkdown(match[2], `c${i}`)}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Feedback externo */}
      <h3>Feedback externo</h3>
      <div className="feedback-box">
        <textarea
          className="feedback-input"
          rows={3}
          placeholder="Erro de deploy, pedido de ajuste, info do ambiente… (entra no handoff das próximas fases)"
          value={feedbackText}
          onChange={(e) => setFeedbackText(e.target.value)}
        />
        <div className="form-inline">
          <button disabled={feedbackBusy || !feedbackText.trim()} onClick={saveFeedback}>
            salvar nota
          </button>
          <button className="danger" disabled={feedbackBusy || !task.feedback} onClick={clearFeedback}>
            limpar
          </button>
          <span className="muted small">
            {task.feedback ? "a nota entra no handoff das próximas fases" : "sem nota ativa"}
          </span>
        </div>
      </div>
    </div>
  );
}
