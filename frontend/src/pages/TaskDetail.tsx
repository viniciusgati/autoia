import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import type { RunEvent, Task, TaskStep } from "../types";

function eventSummary(event: RunEvent): string {
  const payload = event.payload as Record<string, unknown>;
  if (event.kind === "assistant_text") {
    return String(payload.content ?? "");
  }
  if (event.kind === "tool_call") {
    const call = payload.tool_call as { function?: { name?: string; arguments?: string } } | undefined;
    const fn = call?.function;
    return `${fn?.name ?? "?"} ${fn?.arguments ?? ""}`;
  }
  if (event.kind === "tool_result") {
    const content = String(payload.content ?? "");
    return content.length > 300 ? content.slice(0, 300) + "…" : content;
  }
  if (event.kind === "guardrail_blocked") {
    return `⛔ ${String(payload.detail ?? payload.pattern ?? "")}`;
  }
  if (event.kind === "pm_decision") {
    return `🤖 PM: ${String(payload.action ?? "")} — ${String(payload.reason ?? "")}`;
  }
  if (event.kind === "bounce_back") {
    return `↩️ voltou da fase ${String(payload.from_position ?? "?")}: ${String(payload.reason ?? "")}`;
  }
  return JSON.stringify(payload);
}

function eventFull(event: RunEvent): string {
  return JSON.stringify(event.payload, null, 2);
}

export default function TaskDetail() {
  const { id } = useParams();
  const taskId = Number(id);
  const [task, setTask] = useState<Task | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [log, setLog] = useState("");
  const [view, setView] = useState<"timeline" | "transcript">("timeline");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [error, setError] = useState("");
  const [extraBudget, setExtraBudget] = useState(5);
  const [cancelNote, setCancelNote] = useState("");
  const [pmBusy, setPmBusy] = useState(false);

  useEffect(() => {
    const load = () =>
      api
        .getTask(taskId)
        .then((t) => {
          setTask(t);
          if (t.steps.length > 0) {
            setSelected((current) => current ?? t.steps[0].id);
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
      await api.retryStep(taskId, position);
      await refresh();
    } catch (e) {
      setError(String(e));
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

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p>Carregando…</p>;

  const active = ["queued", "in_progress", "needs_review"].includes(task.status);
  const selectedStep = task.steps.find((s) => s.id === selected) ?? null;
  const pmCandidates = ["failed", "blocked", "needs_review"].includes(task.status);

  const toggleExpanded = (eventId: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(eventId)) next.delete(eventId);
      else next.add(eventId);
      return next;
    });
  };

  return (
    <div>
      <p>
        <Link to="/tasks">← tarefas</Link>
      </p>
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
      {task.description && <p className="muted">{task.description}</p>}
      {task.error && <div className="error">{task.error}</div>}      {task.acceptance_criteria && (
        <div className="card">
          <div className="card-title">
            <strong>Critérios de aceite</strong>
          </div>
          <ul className="criteria">
            {task.acceptance_criteria.split("\n").map((line, i) => {
              const match = line.match(/^\s*-?\s*\[( |x|X)\]\s*(.*)$/);
              if (!match) return <li key={i}>{line}</li>;
              return (
                <li key={i}>
                  <span className={match[1] !== " " ? "criteria-done" : ""}>
                    {match[1] !== " " ? "☑" : "☐"} {match[2]}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {task.status === "needs_review" && (
        <div className="card warn">
          <div className="card-title">
            <strong>Aguardando revisão humana</strong>
          </div>
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

      <h3>Fases</h3>
      <ol className="steps">
        {task.steps.map((step: TaskStep) => (
          <li key={step.id}>
            <button
              className={`step ${selected === step.id ? "step-selected" : ""}`}
              onClick={() => setSelected(step.id)}
            >
              <span className="step-pos">{step.position}</span>
              <span>
                <strong>{step.robot?.name ?? "robô"}</strong> <StatusBadge status={step.status} />
                {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
                <span className="muted">· tentativa {step.attempt}</span>
                {step.verdict && <span className="verdict">· veredicto: {step.verdict}</span>}
              </span>
              {step.summary && <p className="summary">{step.summary}</p>}
              {step.error && <p className="step-error">{step.error}</p>}
            </button>
            {(step.status === "failed" || step.status === "guardrail_blocked") && (
              <button className="danger" onClick={() => retry(step.position)}>
                repetir fase
              </button>
            )}
          </li>
        ))}
      </ol>

      {selectedStep && (
        <>
          <div className="meta">
            <h3>Fase {selectedStep.position} — {selectedStep.robot?.name}</h3>
            <div className="view-toggle">
              <button
                className={view === "timeline" ? "view-active" : ""}
                onClick={() => setView("timeline")}
              >
                resumo
              </button>
              <button
                className={view === "transcript" ? "view-active" : ""}
                onClick={() => setView("transcript")}
              >
                transcript completo
              </button>
            </div>
          </div>

          {events.length === 0 && <p className="muted">Sem eventos ainda{active ? " — aguardando execução…" : ""}.</p>}

          {view === "timeline" && (
            <ul className="timeline">
              {events.map((event) => {
                const isOpen = expanded.has(event.id);
                return (
                  <li key={event.id} className={`timeline-${event.kind}`}>
                    <span className="mono time">{new Date(event.ts).toLocaleTimeString()}</span>
                    <span className="kind">{event.kind}</span>
                    {event.cost > 0 && <span className="muted">+{event.cost.toFixed(2)} US$</span>}
                    <button className="link-btn" onClick={() => toggleExpanded(event.id)}>
                      {isOpen ? "recolher" : "expandir"}
                    </button>
                    <pre className="event-payload">{isOpen ? eventFull(event) : eventSummary(event)}</pre>
                  </li>
                );
              })}
            </ul>
          )}

          {view === "transcript" && (
            <div className="transcript">
              {events.map((event) => (
                <div key={event.id} className={`transcript-block transcript-${event.kind}`}>
                  <div className="mono time">
                    [{event.seq}] {new Date(event.ts).toLocaleTimeString()} · {event.kind}
                    {event.cost > 0 ? ` · +${event.cost.toFixed(2)} US$` : ""}
                  </div>
                  <pre className="transcript-body">{eventFull(event)}</pre>
                </div>
              ))}
            </div>
          )}

          <h3>Log bruto</h3>
          <pre className="raw-log">{log || "(vazio)"}</pre>
        </>
      )}
    </div>
  );
}
