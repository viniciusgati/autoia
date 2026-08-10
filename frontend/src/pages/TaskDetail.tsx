import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import PhasePanel from "../components/PhasePanel";
import StatusBadge from "../components/StatusBadge";
import Timeline from "../components/Timeline";
import { formatToolCall } from "../lib/events";
import Markdown from "../lib/markdown";
import type { RunEvent, Task, TaskStep } from "../types";

/** Página de detalhe da task com timeline vertical e painel lateral. */

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

export default function TaskDetail() {
  const { repoId, taskId: taskIdStr } = useParams<{ repoId: string; taskId: string }>();
  const taskId = Number(taskIdStr);
  const repoIdNum = Number(repoId);

  const [task, setTask] = useState<Task | null>(null);
  const [panelStep, setPanelStep] = useState<TaskStep | null>(null);
  const [runningToolCall, setRunningToolCall] = useState<RunEvent | null>(null);
  const [runningSubtask, setRunningSubtask] = useState<{position: number; title: string} | null>(null);
  const [error, setError] = useState("");
  const [extraBudget, setExtraBudget] = useState(5);
  const [cancelNote] = useState("");
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const [pmBusy, setPmBusy] = useState(false);
  const [bouncebackTarget, setBouncebackTarget] = useState(0);
  const [bouncebackNote, setBouncebackNote] = useState("");
  const [reviewedBy, setReviewedBy] = useState("humano");
  const [bouncebackBusy, setBouncebackBusy] = useState(false);

  useEffect(() => {
    const load = () =>
      api
        .getTask(taskId)
        .then((t) => {
          setTask(t);
          setFeedbackText((current) => current || t.feedback || "");
          // Só inicializa o alvo na primeira carga
          setBouncebackTarget((prev) => prev || suggestedBouncebackTarget(t));
          const running = t.steps.find((s) => s.status === "running");
          if (running) {
            api
              .listEvents(running.id, "tool_call", "desc")
              .then((evs) => setRunningToolCall(evs[0] ?? null))
              .catch(() => setRunningToolCall(null));
            // Subtarefa atual: busca o último evento subtask_start
            if (t.subtasks && t.subtasks.length > 0) {
              api
                .listEvents(running.id, "subtask_start", "desc")
                .then((evs) => {
                  if (evs.length > 0) {
                    const p = evs[0].payload as {position?: number; title?: string};
                    setRunningSubtask({position: p.position ?? -1, title: p.title ?? "?"});
                  } else {
                    setRunningSubtask(null);
                  }
                })
                .catch(() => setRunningSubtask(null));
            } else {
              setRunningSubtask(null);
            }
          } else {
            setRunningToolCall(null);
            setRunningSubtask(null);
          }
        })
        .catch((e) => setError(String(e)));
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [taskId]);

  const refresh = () => api.getTask(taskId).then(setTask).catch((e) => setError(String(e)));

  const retry = async (position: number) => {
    try {
      await api.retryStep(taskId, position, feedbackText.trim() || undefined);
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

  /** Sugere a fase alvo para bounceback:
   *  - Se há step com falha, volta para o step anterior a ele.
   *  - Se é falha pós-merge, volta para a fase implement (developer).
   *  - Fallback: primeiro step pré-merge com status done. */
  function suggestedBouncebackTarget(task: Task): number {
    const steps = [...task.steps].sort((a, b) => a.position - b.position);
    const failedStep = steps.find((s) => s.status === "failed" || s.status === "guardrail_blocked");
    if (failedStep) {
      const prev = steps.filter((s) => s.position < failedStep.position).pop();
      return prev ? prev.position : failedStep.position;
    }
    // Pós-merge: sugere o implement (developer)
    const implement = steps.find((s) => s.robot?.role === "implement" && !s.post_merge);
    if (implement) return implement.position;
    // Fallback: primeiro step pré-merge com status done
    const firstDone = steps.find((s) => s.status === "done" && !s.post_merge);
    return firstDone ? firstDone.position : 0;
  }

  const bounceback = async (event: FormEvent) => {
    event.preventDefault();
    setBouncebackBusy(true);
    try {
      await api.bouncebackTask(taskId, bouncebackTarget, bouncebackNote.trim() || undefined, reviewedBy.trim() || "humano");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBouncebackBusy(false);
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

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p>Carregando…</p>;

  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const runningStep = steps.find((s) => s.status === "running") ?? null;
  const pmCandidates = ["failed", "blocked", "needs_review"].includes(task.status);

  function subtaskAlert(task: Task): {message: string; level: "critical" | "warning"} | null {
    const subs = task.subtasks || [];
    const failed = subs.find((s) => s.status === "failed");
    if (failed) {
      return {
        message: `Subtarefa ${failed.position + 1} "${failed.title}" falhou: ${failed.error || "sem detalhes"}`,
        level: "critical",
      };
    }
    const stuck = subs.find((s) => (s.status === "implementing" || s.status === "verifying") && s.error);
    if (stuck) {
      return {
        message: `Subtarefa ${stuck.position + 1} "${stuck.title}" com erro: ${stuck.error}`,
        level: "warning",
      };
    }
    return null;
  }

  const subAlert = subtaskAlert(task);

  return (
    <div>
      <p>
        <Link to={`/${repoId}`}>← projeto</Link>
        {" · "}
        <Link to={`/${repoId}/tasks`}>tarefas</Link>
      </p>

      {/* Header fixo */}
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
        {task.subtasks && task.subtasks.length > 0 && (
          <div className="meta" style={{ marginTop: 4 }}>
            <span>
              Subtarefas:{" "}
              <strong>
                {task.subtasks.filter((s) => s.status === "done").length}/{task.subtasks.length}
              </strong>{" "}
              concluídas
            </span>
            {(() => {
              const counts: Record<string, number> = {};
              for (const s of task.subtasks) {
                if (s.status !== "done") {
                  counts[s.status] = (counts[s.status] || 0) + 1;
                }
              }
              return Object.entries(counts).map(([status, n]) => (
                <span key={status} className="muted small">
                  {n} <StatusBadge status={status} />
                </span>
              ));
            })()}
          </div>
        )}
        {runningStep && (
          <div className="task-live">
            <div>
              <span className="session-label">Etapa atual</span>
              <span className="task-live-value">{etapaAtualLabel(task)}</span>
            </div>
            {runningSubtask && (
              <div>
                <span className="session-label">Subtarefa atual</span>
                <span className="task-live-value">
                  {runningSubtask.position + 1}/{task.subtasks?.length ?? "?"} · {runningSubtask.title}
                </span>
              </div>
            )}
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
        {subAlert && (
          <div className={`sticky-alert ${subAlert.level === "critical" ? "sticky-alert-critical" : ""}`}>
            <span>{subAlert.level === "critical" ? "⛔" : "⚠"} {subAlert.message}</span>
          </div>
        )}
      </div>

      {task.error && <div className="error">{task.error}</div>}

      {/* Timeline vertical das fases */}
      <h3>Pipeline</h3>
      <Timeline
        steps={steps}
        selectedId={panelStep?.id ?? null}
        onSelect={(id) => {
          const step = steps.find((s) => s.id === id) ?? null;
          setPanelStep(step);
        }}
        onRetry={retry}
      />

      {/* Painel lateral */}
      <PhasePanel
        step={panelStep}
        repoId={repoIdNum}
        taskId={taskId}
        taskStatus={task.status}
        onClose={() => setPanelStep(null)}
        onRetry={retry}
      />

      {/* Revisão humana */}
      {task.status === "needs_review" && (() => {
        const sorted = [...task.steps].sort((a, b) => a.position - b.position);
        const lastExecuted = sorted.filter((s) => s.status !== "pending").pop();
        const maxPos = lastExecuted ? lastExecuted.position : sorted.length - 1;
        // Candidatos: fases anteriores à última executada (exclui pós-merge se falha foi nelas)
        const candidates = sorted.filter(
          (s) => s.position < maxPos && !(lastExecuted?.post_merge && s.post_merge)
        );
        return (
        <div className="card warn" id="revisao-humana">
          <div className="card-title">
            <strong>⚠ Aguardando revisão humana</strong>
          </div>
          {task.error && (
            <div style={{ marginBottom: 12 }}>
              <div className="form-label">Motivo da parada</div>
              <pre className="review-error">{task.error}</pre>
            </div>
          )}

          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12, marginBottom: 12 }}>
            <div className="form-label" style={{ marginBottom: 4 }}>
              ▸ Retornar pipeline para a fase:
            </div>
            <form onSubmit={bounceback}>
              <div className="form-field" style={{ marginBottom: 8 }}>
                <select
                  value={bouncebackTarget}
                  onChange={(e) => setBouncebackTarget(Number(e.target.value))}
                >
                  {candidates.map((s) => (
                    <option key={s.position} value={s.position}>
                      F{s.position} · {s.robot?.name ?? "?"} ({s.robot?.role ?? "?"})
                      {s.attempt > 1 ? ` · tent. ${s.attempt}` : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-inline" style={{ gap: 8, marginBottom: 8 }}>
                <div className="form-field" style={{ flex: 1 }}>
                  <label className="form-label">Nota (opcional)</label>
                  <input
                    value={bouncebackNote}
                    onChange={(e) => setBouncebackNote(e.target.value)}
                    placeholder="ex.: feature não foi deployada, revisar implementação"
                  />
                </div>
                <div className="form-field" style={{ maxWidth: 160 }}>
                  <label className="form-label">Revisado por</label>
                  <input
                    value={reviewedBy}
                    onChange={(e) => setReviewedBy(e.target.value)}
                  />
                </div>
              </div>
              <div className="form-inline" style={{ gap: 8 }}>
                <button type="submit" disabled={bouncebackBusy}>
                  {bouncebackBusy ? "retornando…" : "confirmar retorno"}
                </button>
                <button
                  type="button"
                  className="danger"
                  onClick={(e) => review(e as unknown as FormEvent, "cancel")}
                >
                  cancelar tarefa
                </button>
              </div>
            </form>
          </div>

          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
            <div className="form-label" style={{ marginBottom: 4 }}>
              — ou —
            </div>
            <form className="form-inline" onSubmit={(e) => review(e, "approve")}>
              <div className="form-field">
                <label className="form-label">Orçamento extra (US$)</label>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={extraBudget}
                  onChange={(e) => setExtraBudget(Number(e.target.value))}
                  className="short"
                />
              </div>
              <button type="submit">aprovar e continuar</button>
            </form>
          </div>
        </div>
        );
      })()}

      {/* PM decide */}
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

      {/* Subtarefas */}
      {task.subtasks && task.subtasks.length > 0 && (
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
                  {(st.status === "failed" || st.status === "implementing" || st.status === "verifying" || (st.status === "pending" && st.attempt > 1)) && (
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

      {/* Tasks filhas / pai */}
      {(task.children && task.children.length > 0) || task.parent_task_id ? (
        <>
          <h3>Relações</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 18 }}>
            {task.parent_task_id && (
              <div className="muted small">
                ← tarefa pai:{" "}
                <Link to={`/${repoId}/tasks/${task.parent_task_id}`}>
                  #{task.parent_task_id}
                </Link>
              </div>
            )}
            {task.children && task.children.length > 0 && task.children.map((child) => (
              <div
                key={child.id}
                className="resumo-card"
                style={{ margin: 0, padding: "10px 14px" }}
              >
                <div className="resumo-line">
                  <span className="resumo-title">
                    {child.status === "created" && "📝 "}
                    #{child.id} {child.title}
                  </span>
                  <StatusBadge status={child.status} />
                </div>
                <div className="muted small" style={{ marginTop: 4 }}>
                  {child.repository_id !== task.repository_id && (
                    <span>repo #{child.repository_id} · </span>
                  )}
                  {child.kind} · {child.cost_spent.toFixed(2)} US$
                </div>
                {child.status === "created" && (
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button
                      onClick={async () => {
                        try { await api.startTask(child.id); await refresh(); }
                        catch (e) { setError(String(e)); }
                      }}
                    >
                      aprovar e iniciar
                    </button>
                    <button
                      className="danger"
                      onClick={async () => {
                        if (!confirm(`Recusar tarefa #${child.id} "${child.title}"?`)) return;
                        try { await api.deleteTask(child.id); await refresh(); }
                        catch (e) { setError(String(e)); }
                      }}
                    >
                      recusar
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      ) : null}

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

      {/* Feedback externo */}
      <h3>Feedback externo</h3>
      <div className="feedback-box">
        <div className="form-field">
          <label className="form-label">Nota de feedback</label>
          <textarea
            className="feedback-input"
            rows={3}
            placeholder="Erro de deploy, pedido de ajuste, info do ambiente… (entra no handoff das próximas fases)"
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
          />
        </div>
        <div className="form-actions">
          <button disabled={feedbackBusy || !feedbackText.trim()} onClick={saveFeedback}>
            salvar nota
          </button>
          <button className="danger" disabled={feedbackBusy || !task.feedback} onClick={clearFeedback}>
            limpar
          </button>
          <span className="muted small" style={{ alignSelf: "center" }}>
            {task.feedback ? "a nota entra no handoff das próximas fases" : "sem nota ativa"}
          </span>
        </div>
      </div>
    </div>
  );
}
