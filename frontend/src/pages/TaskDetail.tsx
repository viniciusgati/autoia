import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import ArtifactThumbs from "../components/ArtifactThumbs";
import BlockedPanel from "../components/BlockedPanel";
import ExecTimeline from "../components/ExecTimeline";
import FluxoSteps from "../components/FluxoSteps";
import PhasePanel from "../components/PhasePanel";
import PhaseStepper from "../components/PhaseStepper";
import StatusBadge from "../components/StatusBadge";
import TaskChat from "../components/TaskChat";
import TaskSummaryCard from "../components/TaskSummaryCard";
import Timeline from "../components/Timeline";
import { buildTurns } from "../lib/chat";
import { formatToolCall } from "../lib/events";
import Markdown from "../lib/markdown";
import { diffSummary, etapaAtualLabel, tempoDecorrido } from "../lib/tasks";
import type { Repository, RunEvent, Task, TaskStep, TimelineEvent } from "../types";

/** Detalhe da task em 3 níveis de acompanhamento:
 *  - Resumo (Nível 1): "o que aconteceu?" — resumo LLM + timeline compacta;
 *  - Acompanhamento (Nível 2): "como foi feito e qual o estado atual?" — solicitação,
 *    contexto, tarefas, fluxo, arquivos, testes, timeline completa e intervenções;
 *  - Técnico (Nível 3): "o que exatamente aconteceu tecnicamente?" — chat/logs/
 *    payloads (conteúdo completo, nada escondido). */

export default function TaskDetail() {
  const { repoId, taskId: taskIdStr } = useParams<{ repoId: string; taskId: string }>();
  const taskId = Number(taskIdStr);
  const repoIdNum = Number(repoId);

  const [task, setTask] = useState<Task | null>(null);
  const [view, setView] = useState<"resumo" | "acompanhamento" | "tecnico">("resumo");
  const [eventsByStep, setEventsByStep] = useState<Record<number, RunEvent[]>>({});
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [panelStep, setPanelStep] = useState<TaskStep | null>(null);
  const [runningSubtask, setRunningSubtask] = useState<{position: number; title: string} | null>(null);
  const [repoNames, setRepoNames] = useState<Record<number, string>>({});
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
  const [actionBusy, setActionBusy] = useState(false);

  // Aprovação humana (gate do pipeline)
  const [approvalNote, setApprovalNote] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [voltarTarget, setVoltarTarget] = useState(0);
  // Edição da história (só em waiting_approval)
  const [storyDesc, setStoryDesc] = useState("");
  const [storyCriteria, setStoryCriteria] = useState("");
  const [storyBusy, setStoryBusy] = useState(false);
  // Detalhes da implementação (contexto adicional do usuário)
  const [detailsText, setDetailsText] = useState("");
  const [detailsBusy, setDetailsBusy] = useState(false);
  // Resumo do desenvolvimento (LLM dedicada)
  const [summaryBusy, setSummaryBusy] = useState(false);
  // Retomada de fase bloqueada
  const [continueBusy, setContinueBusy] = useState(false);
  const storyInit = useRef(false);
  const prevStatus = useRef<string | null>(null);

  useEffect(() => {
    api.listRepositories().then((repos: Repository[]) => {
      const m: Record<number, string> = {};
      for (const r of repos) m[r.id] = r.name;
      setRepoNames(m);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const load = () =>
      api
        .getTask(taskId)
        .then((t) => {
          setTask(t);
          setFeedbackText((current) => current || t.feedback || "");
          setBouncebackTarget((prev) => prev || suggestedBouncebackTarget(t));
          setDetailsText((current) => current || t.details || "");
          const gated = t.steps.find((s) => s.status === "pending" && s.pause_before);
          if (
            !storyInit.current ||
            (t.status === "waiting_approval" && prevStatus.current !== "waiting_approval")
          ) {
            setStoryDesc(t.description ?? "");
            setStoryCriteria(t.acceptance_criteria ?? "");
            storyInit.current = true;
            if (gated) {
              const prev = [...t.steps]
                .filter((s) => s.position < gated.position)
                .sort((a, b) => b.position - a.position)[0];
              setVoltarTarget(prev ? prev.position : 0);
            }
          }
          prevStatus.current = t.status;

          // Subtarefa atual (evento subtask_start da fase running)
          const running = t.steps.find((s) => s.status === "running");
          if (running && t.subtasks && t.subtasks.length > 0) {
            api
              .listEvents(running.id, "subtask_start", "desc")
              .then((evs) => {
                if (evs.length > 0) {
                  const p = evs[0].payload as {position?: number; title?: string};
                  setRunningSubtask({position: p.position ?? -1, title: p.title ?? "?"});
                } else setRunningSubtask(null);
              })
              .catch(() => setRunningSubtask(null));
          } else {
            setRunningSubtask(null);
          }

          // Eventos das fases (para o chat): fases executadas ou em execução.
          const toLoad = t.steps.filter(
            (s) => s.summary || s.status === "running",
          );
          if (toLoad.length > 0) {
            Promise.allSettled(
              toLoad.map((s) => api.listEvents(s.id)),
            ).then((results) => {
              const next: Record<number, RunEvent[]> = {};
              toLoad.forEach((s, i) => {
                const r = results[i];
                if (r.status === "fulfilled") next[s.id] = r.value;
              });
              setEventsByStep((current) => ({ ...current, ...next }));
            });
          }
        })
        .catch((e) => setError(String(e)));
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [taskId]);

  useEffect(() => {
    const load = () => api.getTaskTimeline(taskId).then(setTimeline).catch(() => {});
    load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [taskId]);

  const refresh = () => api.getTask(taskId).then(setTask).catch((e) => setError(String(e)));

  const turns = useMemo(
    () => (task ? buildTurns(task, eventsByStep) : []),
    [task, eventsByStep],
  );

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

  /** Sugere a fase alvo para bounceback. */
  function suggestedBouncebackTarget(task: Task): number {
    const steps = [...task.steps].sort((a, b) => a.position - b.position);
    const failedStep = steps.find((s) => s.status === "failed" || s.status === "guardrail_blocked");
    if (failedStep) {
      const prev = steps.filter((s) => s.position < failedStep.position).pop();
      return prev ? prev.position : failedStep.position;
    }
    const implement = steps.find((s) => s.robot?.role === "implement" && !s.post_merge);
    if (implement) return implement.position;
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

  const taskAction = async (action: "pause" | "resume" | "cancel") => {
    setActionBusy(true);
    try {
      if (action === "pause") await api.pauseTask(taskId);
      else if (action === "resume") await api.resumeTask(taskId);
      else {
        if (!window.confirm(`Cancelar a tarefa #${taskId}?`)) return;
        await api.cancelTask(taskId);
      }
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setActionBusy(false);
    }
  };

  const approveGate = async (position: number) => {
    setApprovalBusy(true);
    try {
      await api.approveStep(taskId, position, approvalNote.trim() || undefined);
      setApprovalNote("");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setApprovalBusy(false);
    }
  };

  const voltarFase = async (position: number) => {
    setApprovalBusy(true);
    try {
      await api.retryStep(taskId, position, approvalNote.trim() || undefined);
      setApprovalNote("");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setApprovalBusy(false);
    }
  };

  const saveStory = async () => {
    setStoryBusy(true);
    try {
      await api.updateTaskStory(taskId, {
        description: storyDesc,
        acceptance_criteria: storyCriteria,
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setStoryBusy(false);
    }
  };

  const saveDetails = async () => {
    setDetailsBusy(true);
    try {
      await api.updateTaskStory(taskId, { details: detailsText });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setDetailsBusy(false);
    }
  };

  const regenerateSummary = async () => {
    setSummaryBusy(true);
    try {
      await api.regenerateSummary(taskId);
      window.setTimeout(refresh, 2500);
    } catch (e) {
      setError(String(e));
    } finally {
      setSummaryBusy(false);
    }
  };

  const continueBlocked = async (instruction: string) => {
    setContinueBusy(true);
    try {
      await api.continueBlocked(taskId, instruction);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setContinueBusy(false);
    }
  };

  const scrollToBlocked = () => {
    document.getElementById("bloqueio-instrucao")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const scrollToApproval = () => {
    document.getElementById("aprovacao-humana")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const scrollToReview = () => {
    document.getElementById("revisao-humana")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p>Carregando…</p>;

  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const runningStep = steps.find((s) => s.status === "running") ?? null;
  const runningEvents = runningStep ? eventsByStep[runningStep.id] ?? [] : [];
  const runningToolCall = [...runningEvents].reverse().find((e) => e.kind === "tool_call") ?? null;
  const live = runningStep
    ? { step: runningStep, toolCall: runningToolCall, events: runningEvents }
    : null;
  const blocked = task.status === "blocked" && task.block_reason != null;

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
  const subtasksDone = (task.subtasks || []).filter((s) => s.status === "done").length;
  const verifySteps = steps.filter((s) => s.robot?.role === "verify" && (s.summary || s.error));
  const changedSteps = steps.filter((s) => s.diff_stat);

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
          <span className="task-detail-executor">
            executor: {task.executor === "opencode" ? "opencode" : "kimi code"}
          </span>
          <span>
            branch: <code>{task.branch ?? "—"}</code>
          </span>
          <span>
            orçamento: <strong>{task.budget_limit.toFixed(2)}</strong> US$ · gasto:{" "}
            <strong>{task.cost_spent.toFixed(2)}</strong> US$
          </span>
          <span className="muted">decisões PM: {task.pm_decisions}</span>
        </div>
        {["created", "queued", "in_progress", "paused", "needs_review", "waiting_approval", "blocked"].includes(
          task.status,
        ) && (
          <div className="meta" style={{ marginTop: 4 }}>
            {task.status === "paused" ? (
              <button onClick={() => taskAction("resume")} disabled={actionBusy}>
                retomar
              </button>
            ) : task.status === "queued" || task.status === "in_progress" ? (
              <button onClick={() => taskAction("pause")} disabled={actionBusy}>
                pausar
              </button>
            ) : null}
            <button
              className="danger"
              onClick={() => taskAction("cancel")}
              disabled={actionBusy}
            >
              cancelar tarefa
            </button>
          </div>
        )}
        {task.subtasks && task.subtasks.length > 0 && (
          <div className="meta" style={{ marginTop: 4 }}>
            <span>
              Tarefas:{" "}
              <strong>
                {subtasksDone}/{task.subtasks.length}
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
                <span className="session-label">Tarefa atual</span>
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
        {task.status === "waiting_approval" && (
          <div className="sticky-alert">
            <span>⚠ Aguardando aprovação humana — o pipeline parou na fase {etapaAtualLabel(task)}</span>
            <button onClick={scrollToApproval}>ir para a aprovação ↓</button>
          </div>
        )}
        {blocked && (
          <div className="sticky-alert sticky-alert-critical">
            <span>⛔ Desenvolvimento bloqueado — aguardando sua instrução para continuar</span>
            <button onClick={scrollToBlocked}>dar instrução ↓</button>
          </div>
        )}
        {task.status === "paused" && (
          <div className="sticky-alert">
            <span>⏸ Tarefa pausada — o pipeline não avança até retomar</span>
          </div>
        )}
        {task.status === "cancelled" && (
          <div className="sticky-alert sticky-alert-critical">
            <span>✕ Tarefa cancelada — pipeline encerrado</span>
          </div>
        )}
        {subAlert && (
          <div className={`sticky-alert ${subAlert.level === "critical" ? "sticky-alert-critical" : ""}`}>
            <span>{subAlert.level === "critical" ? "⛔" : "⚠"} {subAlert.message}</span>
          </div>
        )}
      </div>

      {task.error && <div className="error">{task.error}</div>}

      {/* Níveis de acompanhamento */}
      <div className="meta" style={{ margin: "12px 0" }}>
        <div className="view-toggle">
          <button
            className={view === "resumo" ? "view-active" : ""}
            onClick={() => setView("resumo")}
            title="O que aconteceu?"
          >
            resumo
          </button>
          <button
            className={view === "acompanhamento" ? "view-active" : ""}
            onClick={() => setView("acompanhamento")}
            title="Como o trabalho foi realizado e qual é o estado atual?"
          >
            acompanhamento
          </button>
          <button
            className={view === "tecnico" ? "view-active" : ""}
            onClick={() => setView("tecnico")}
            title="O que exatamente aconteceu tecnicamente (auditoria)?"
          >
            técnico
          </button>
        </div>
      </div>

      {/* ─────────────── Nível 1: Resumo ─────────────── */}
      {view === "resumo" && (
        <div className="resumo-view">
          <SituacaoCard
            task={task}
            runningStep={runningStep}
            runningToolCall={runningToolCall}
            onGoAcompanhamento={() => setView("acompanhamento")}
          />

          {blocked && (
            <BlockedPanel task={task} onContinue={continueBlocked} busy={continueBusy} />
          )}

          <TaskSummaryCard summary={task.summary} onRegenerate={regenerateSummary} busy={summaryBusy} />

          <div className="card">
            <div className="card-title">
              <strong>Estado atual</strong>
            </div>
            <div className="resumo-kpis">
              <div>
                <span className="form-label">Resultado</span>
                <StatusBadge status={task.status} />
              </div>
              <div>
                <span className="form-label">Etapa atual</span>
                <span>{etapaAtualLabel(task) || "—"}</span>
              </div>
              {task.subtasks.length > 0 && (
                <div>
                  <span className="form-label">Tarefas</span>
                  <span>{subtasksDone} concluídas · {task.subtasks.length - subtasksDone} pendentes</span>
                </div>
              )}
              <div>
                <span className="form-label">Custo</span>
                <span>{task.cost_spent.toFixed(2)} / {task.budget_limit.toFixed(2)} US$</span>
              </div>
            </div>
            {changedSteps.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <span className="form-label">Principais alterações</span>
                <ul className="summary-list">
                  {changedSteps.map((s) => (
                    <li key={s.id}>
                      Fase {s.position} ({s.robot?.name ?? "?"}) · {diffSummary(s.diff_stat) ?? "arquivos alterados"}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {/* Etapas com retorno (re-executa a partir da fase escolhida) */}
          <div className="card">
            <div className="card-title">
              <strong>Etapas</strong>
              <button className="link-btn" onClick={() => setView("acompanhamento")}>
                ver acompanhamento →
              </button>
            </div>
            <PhaseStepper task={task} showLabels />
            <FluxoSteps
              task={task}
              steps={steps}
              onRetry={retry}
              repoId={repoIdNum}
              taskId={taskId}
            />
            <p className="muted small" style={{ margin: "8px 0 0" }}>
              "Voltar para esta fase" re-executa a partir dela: a nova execução aparece
              no fim da timeline e o histórico anterior é preservado.
            </p>
          </div>

          {/* Screenshots por fase (thumbs expandíveis) */}
          {steps.some((s) => (s.artifacts ?? []).length > 0) && (
            <div className="card">
              <div className="card-title">
                <strong>Screenshots</strong>
                <span className="muted small">evidência visual gerada pelos robôs · clique para ampliar</span>
              </div>
              {steps.map((s) => (
                <ArtifactThumbs
                  key={s.id}
                  artifacts={s.artifacts ?? []}
                  label={`Fase ${s.position} · ${s.robot?.name ?? "?"}`}
                />
              ))}
            </div>
          )}

          <div className="card">
            <div className="card-title">
              <strong>O que aconteceu (timeline)</strong>
              <button className="link-btn" onClick={() => setView("acompanhamento")}>
                ver detalhes →
              </button>
            </div>
            <ExecTimeline events={timeline} level={1} />
          </div>

          {/* Ações de intervenção humana disponíveis direto no resumo */}
          <HumanIntervention
            task={task}
            steps={steps}
            extraBudget={extraBudget}
            setExtraBudget={setExtraBudget}
            bouncebackTarget={bouncebackTarget}
            setBouncebackTarget={setBouncebackTarget}
            bouncebackNote={bouncebackNote}
            setBouncebackNote={setBouncebackNote}
            reviewedBy={reviewedBy}
            setReviewedBy={setReviewedBy}
            bouncebackBusy={bouncebackBusy}
            bounceback={bounceback}
            review={review}
            approvalNote={approvalNote}
            setApprovalNote={setApprovalNote}
            approvalBusy={approvalBusy}
            approveGate={approveGate}
            voltarTarget={voltarTarget}
            setVoltarTarget={setVoltarTarget}
            voltarFase={voltarFase}
            pmBusy={pmBusy}
            pmDecide={pmDecide}
            storyDesc={storyDesc}
            setStoryDesc={setStoryDesc}
            storyCriteria={storyCriteria}
            setStoryCriteria={setStoryCriteria}
            storyBusy={storyBusy}
            saveStory={saveStory}
          />
        </div>
      )}

      {/* ─────────────── Nível 2: Acompanhamento ─────────────── */}
      {view === "acompanhamento" && (
        <div className="acompanhamento-view">
          {blocked && (
            <BlockedPanel task={task} onContinue={continueBlocked} busy={continueBusy} />
          )}

          {/* Solicitação */}
          <div className="card">
            <div className="card-title">
              <strong>Solicitação</strong>
              <span className="muted small">contexto original</span>
            </div>
            {task.description ? (
              <Markdown text={task.description} />
            ) : (
              <p className="muted">Sem descrição.</p>
            )}
            {task.acceptance_criteria && (
              <>
                <div className="form-label" style={{ marginTop: 8 }}>Critérios de aceite</div>
                <Markdown text={task.acceptance_criteria} />
              </>
            )}
          </div>

          {/* Contexto adicional (detalhes do usuário) */}
          <div className="card">
            <div className="card-title">
              <strong>Detalhes adicionados pelo usuário</strong>
              <span className="muted small">contexto da implementação — entra no handoff das próximas fases</span>
            </div>
            <div className="form-field">
              <textarea
                rows={3}
                value={detailsText}
                onChange={(e) => setDetailsText(e.target.value)}
                placeholder="Complemente ou corrija o contexto antes ou durante o desenvolvimento…"
              />
            </div>
            <div className="form-actions">
              <button disabled={detailsBusy} onClick={saveDetails}>
                {detailsBusy ? "salvando…" : "salvar detalhes"}
              </button>
            </div>
          </div>

          {/* Fluxo de execução */}
          <div className="card">
            <div className="card-title">
              <strong>Fluxo de execução</strong>
              <span className="muted small">etapa atual destacada</span>
            </div>
            <PhaseStepper task={task} showLabels />
            <FluxoSteps
              task={task}
              steps={steps}
              onRetry={retry}
              repoId={repoIdNum}
              taskId={taskId}
            />
          </div>

          {/* Tarefas */}
          {task.subtasks.length > 0 && (
            <div className="card">
              <div className="card-title">
                <strong>Tarefas</strong>
                <span className="muted small">origem: plano do PO · o agente pode sugerir outras</span>
              </div>
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
              {task.proposals.filter((p) => p.status === "pending").length > 0 && (
                <p className="muted small" style={{ marginTop: 8 }}>
                  🧩 {task.proposals.filter((p) => p.status === "pending").length} proposta(s) de tarefa
                  sugerida(s) pelo agente aguardando aprovação — veja na aba técnica.
                </p>
              )}
            </div>
          )}

          {/* Arquivos alterados */}
          {changedSteps.length > 0 && (
            <div className="card">
              <div className="card-title">
                <strong>Arquivos alterados</strong>
              </div>
              {changedSteps.map((s) => (
                <details key={s.id} className="diff-item">
                  <summary>
                    Fase {s.position} ({s.robot?.name ?? "?"}) · {diffSummary(s.diff_stat) ?? "diff"}
                  </summary>
                  <pre className="step-error">{s.diff_stat}</pre>
                </details>
              ))}
            </div>
          )}

          {/* Resultado dos testes */}
          {verifySteps.length > 0 && (
            <div className="card">
              <div className="card-title">
                <strong>Resultado dos testes</strong>
              </div>
              {verifySteps.map((s) => (
                <div key={s.id} className="teste-item">
                  <div className="teste-head">
                    <strong>{s.robot?.name ?? "Tester"}</strong>
                    <StatusBadge status={s.status} />
                    {s.verdict && <span className="muted small">veredicto: {s.verdict}</span>}
                  </div>
                  {s.summary && <p className="muted small">{s.summary.slice(0, 400)}{s.summary.length > 400 ? "…" : ""}</p>}
                  {s.error && <div className="error small">{s.error}</div>}
                </div>
              ))}
            </div>
          )}

          {/* Timeline completa */}
          <div className="card">
            <div className="card-title">
              <strong>Timeline da execução</strong>
              <span className="muted small">todas as ações do agente</span>
            </div>
            <ExecTimeline events={timeline} level={2} />
          </div>

          {/* Intervenções humanas (revisão/aprovação/PM) + feedback */}
          <HumanIntervention
            task={task}
            steps={steps}
            extraBudget={extraBudget}
            setExtraBudget={setExtraBudget}
            bouncebackTarget={bouncebackTarget}
            setBouncebackTarget={setBouncebackTarget}
            bouncebackNote={bouncebackNote}
            setBouncebackNote={setBouncebackNote}
            reviewedBy={reviewedBy}
            setReviewedBy={setReviewedBy}
            bouncebackBusy={bouncebackBusy}
            bounceback={bounceback}
            review={review}
            approvalNote={approvalNote}
            setApprovalNote={setApprovalNote}
            approvalBusy={approvalBusy}
            approveGate={approveGate}
            voltarTarget={voltarTarget}
            setVoltarTarget={setVoltarTarget}
            voltarFase={voltarFase}
            pmBusy={pmBusy}
            pmDecide={pmDecide}
            storyDesc={storyDesc}
            setStoryDesc={setStoryDesc}
            storyCriteria={storyCriteria}
            setStoryCriteria={setStoryCriteria}
            storyBusy={storyBusy}
            saveStory={saveStory}
          />
        </div>
      )}

      {/* ─────────────── Nível 3: Técnico ─────────────── */}
      {view === "tecnico" && (
        <>
          <div className="card">
            <div className="card-title">
              <strong>Execução técnica</strong>
              <span className="muted small">
                prompt completo, respostas, tool calls, retornos, logs — auditoria e debug
              </span>
            </div>
            <TaskChat
              task={task}
              turns={turns}
              repoNames={repoNames}
              live={live}
              onProposalsChanged={refresh}
              onError={setError}
            />
            <h3 style={{ marginTop: 18 }}>Fases (pipeline)</h3>
            <Timeline
              steps={steps}
              selectedId={panelStep?.id ?? null}
              onSelect={(id) => {
                const step = steps.find((s) => s.id === id) ?? null;
                setPanelStep(step);
              }}
              onRetry={retry}
            />
            <PhasePanel
              step={panelStep}
              repoId={repoIdNum}
              taskId={taskId}
              taskStatus={task.status}
              onClose={() => setPanelStep(null)}
              onRetry={retry}
            />
          </div>
        </>
      )}

      {/* Relações */}
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

/** Card de situação no Nível 1: deixa claro se a tarefa está em execução, na fila
 *  ou parada (e por quê), com atalho para as ações de intervenção. */
function SituacaoCard({
  task,
  runningStep,
  runningToolCall,
  onGoAcompanhamento,
}: {
  task: Task;
  runningStep: TaskStep | null;
  runningToolCall: RunEvent | null;
  onGoAcompanhamento: () => void;
}) {
  const running = task.status === "in_progress" && runningStep != null;
  const queued = task.status === "queued";
  const parada = ["needs_review", "waiting_approval", "blocked", "paused"].includes(task.status);

  if (running) {
    return (
      <div className="card situation-running">
        <div className="card-title">
          <strong>● Em execução</strong>
          <StatusBadge status={task.status} />
        </div>
        <p className="muted small" style={{ margin: 0 }}>
          A tarefa <strong>não está parada</strong> — o robô está trabalhando agora.
        </p>
        <div className="resumo-kpis" style={{ marginTop: 10 }}>
          <div>
            <span className="form-label">Etapa em execução</span>
            <span>{etapaAtualLabel(task)}</span>
          </div>
          <div>
            <span className="form-label">Tempo em execução</span>
            <span>{tempoDecorrido(runningStep)}</span>
          </div>
          <div>
            <span className="form-label">Comando atual</span>
            <span className="mono">
              {runningToolCall ? formatToolCall(runningToolCall) : "aguardando interação…"}
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (queued) {
    return (
      <div className="card situation-wait">
        <div className="card-title">
          <strong>● Na fila</strong>
          <StatusBadge status={task.status} />
        </div>
        <p className="muted small" style={{ margin: 0 }}>
          A tarefa está aguardando uma vaga de worker. Com{" "}
          <span className="mono">--workers N</span> em execução, ela roda quando uma fase
          ativa terminar. <span className="mono">Etapa: {etapaAtualLabel(task)}</span>
        </p>
      </div>
    );
  }

  if (parada) {
    const msg =
      task.status === "needs_review"
        ? "parada aguardando revisão humana"
        : task.status === "waiting_approval"
          ? "parada aguardando aprovação humana (gate do pipeline)"
          : task.status === "blocked"
            ? "parada: o agente não conseguiu continuar sozinho"
            : "pausada pelo usuário";
    return (
      <div className="card situation-stopped">
        <div className="card-title">
          <strong>⏸ {msg}</strong>
          <StatusBadge status={task.status} />
        </div>
        {task.error && <div className="error">{task.error}</div>}
        <p className="muted small" style={{ margin: 0 }}>
          Veja o motivo e a ação disponível abaixo ou no acompanhamento.
        </p>
        <div style={{ marginTop: 8 }}>
          <button className="link-btn" onClick={onGoAcompanhamento}>
            ir para o acompanhamento ↓
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-title">
        <strong>Situação</strong>
        <StatusBadge status={task.status} />
      </div>
      {task.error && <div className="error">{task.error}</div>}
      <p className="muted small" style={{ margin: 0 }}>
        Status final: <span className="mono">{task.status}</span>
      </p>
    </div>
  );
}

/** Intervenções humanas (revisão de needs_review, aprovação de gate, PM, edição
 *  da história) — reagrupadas na aba Acompanhamento. */
function HumanIntervention(props: {
  task: Task;
  steps: TaskStep[];
  extraBudget: number;
  setExtraBudget: (n: number) => void;
  bouncebackTarget: number;
  setBouncebackTarget: (n: number) => void;
  bouncebackNote: string;
  setBouncebackNote: (s: string) => void;
  reviewedBy: string;
  setReviewedBy: (s: string) => void;
  bouncebackBusy: boolean;
  bounceback: (e: FormEvent) => void;
  review: (e: FormEvent, action: "approve" | "cancel") => void;
  approvalNote: string;
  setApprovalNote: (s: string) => void;
  approvalBusy: boolean;
  approveGate: (position: number) => void;
  voltarTarget: number;
  setVoltarTarget: (n: number) => void;
  voltarFase: (position: number) => void;
  pmBusy: boolean;
  pmDecide: () => void;
  storyDesc: string;
  setStoryDesc: (s: string) => void;
  storyCriteria: string;
  setStoryCriteria: (s: string) => void;
  storyBusy: boolean;
  saveStory: () => void;
}) {
  const {
    task, steps, extraBudget, setExtraBudget,
    bouncebackTarget, setBouncebackTarget, bouncebackNote, setBouncebackNote,
    reviewedBy, setReviewedBy, bouncebackBusy, bounceback, review,
    approvalNote, setApprovalNote, approvalBusy, approveGate,
    voltarTarget, setVoltarTarget, voltarFase,
    pmBusy, pmDecide, storyDesc, setStoryDesc, storyCriteria, setStoryCriteria,
    storyBusy, saveStory,
  } = props;

  return (
    <>
      {/* Revisão humana */}
      {task.status === "needs_review" && (() => {
        const sorted = [...steps].sort((a, b) => a.position - b.position);
        const lastExecuted = sorted.filter((s) => s.status !== "pending").pop();
        const maxPos = lastExecuted ? lastExecuted.position : sorted.length - 1;
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

      {/* Aprovação humana (gate do pipeline) */}
      {task.status === "waiting_approval" && (() => {
        const gated = steps.find((s) => s.status === "pending" && s.pause_before) ?? null;
        const anteriores = gated
          ? steps.filter((s) => s.position < gated.position)
          : [];
        return (
          <div className="card warn" id="aprovacao-humana">
            <div className="card-title">
              <strong>⏸ Aguardando aprovação humana</strong>
              {gated && (
                <span className="badge badge-warn">
                  F{gated.position} · {gated.robot?.name ?? "?"} ({gated.robot?.role ?? "?"})
                </span>
              )}
            </div>
            <p className="muted">
              O pipeline parou antes da fase acima (gate configurado no pipeline).
              Revise o trabalho das fases anteriores na aba técnica e decida:
              aprovar para liberar o robô, ou voltar uma fase para refazer com
              ajustes. Você também pode editar a história abaixo.
            </p>
            <div className="form-field" style={{ marginBottom: 10 }}>
              <label className="form-label">Nota / instruções para a fase aprovada (opcional)</label>
              <textarea
                rows={2}
                value={approvalNote}
                onChange={(e) => setApprovalNote(e.target.value)}
                placeholder="ex.: confirmar nomenclatura das rotas antes de implementar…"
              />
            </div>
            <div className="form-inline" style={{ gap: 8 }}>
              <button
                disabled={approvalBusy || !gated}
                onClick={() => gated && approveGate(gated.position)}
              >
                {approvalBusy ? "liberando…" : "aprovar e liberar o robô"}
              </button>
              {anteriores.length > 0 && (
                <>
                  <select
                    value={voltarTarget}
                    onChange={(e) => setVoltarTarget(Number(e.target.value))}
                    style={{ maxWidth: 260 }}
                  >
                    {anteriores.map((s) => (
                      <option key={s.position} value={s.position}>
                        F{s.position} · {s.robot?.name ?? "?"} ({s.robot?.role ?? "?"})
                        {s.attempt > 1 ? ` · tent. ${s.attempt}` : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    className="warn-btn"
                    disabled={approvalBusy}
                    onClick={() => voltarFase(voltarTarget)}
                  >
                    voltar para fase anterior
                  </button>
                </>
              )}
            </div>
            <p className="muted small" style={{ marginTop: 8 }}>
              Nota preenchida entra no feedback da task e vai no handoff das próximas fases.
            </p>
          </div>
        );
      })()}

      {/* PM decide */}
      {["failed", "blocked", "needs_review"].includes(task.status) && (
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

      {/* Edição da história (só em waiting_approval) */}
      {task.status === "waiting_approval" && (
        <div className="card">
          <div className="card-title">
            <strong>Editar história antes de aprovar</strong>
            <span className="muted small">descrição e critérios de aceite (refinado pelo PO/QA)</span>
          </div>
          <div className="form-stack">
            <div className="form-field">
              <label className="form-label">Descrição</label>
              <textarea
                rows={5}
                value={storyDesc}
                onChange={(e) => setStoryDesc(e.target.value)}
              />
            </div>
            <div className="form-field">
              <label className="form-label">Critérios de aceite</label>
              <textarea
                rows={4}
                value={storyCriteria}
                onChange={(e) => setStoryCriteria(e.target.value)}
              />
            </div>
            <div className="form-actions">
              <button disabled={storyBusy} onClick={saveStory}>
                {storyBusy ? "salvando…" : "salvar alterações"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
