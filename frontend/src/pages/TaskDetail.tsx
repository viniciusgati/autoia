import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import ArtifactThumbs from "../components/ArtifactThumbs";
import BlockedPanel from "../components/BlockedPanel";
import ExecTimeline from "../components/ExecTimeline";
import FluxoSteps from "../components/FluxoSteps";
import MergeConflictPanel from "../components/MergeConflictPanel";
import PhasePanel from "../components/PhasePanel";
import PhaseStepper from "../components/PhaseStepper";
import ResponsavelControl from "../components/ResponsavelControl";
import StatusBadge from "../components/StatusBadge";
import TaskChat from "../components/TaskChat";
import TaskSummaryCard from "../components/TaskSummaryCard";
import Timeline from "../components/Timeline";
import { buildTurns } from "../lib/chat";
import { formatToolCall } from "../lib/events";
import Markdown from "../lib/markdown";
import { fmtBudget, fmtCost } from "../lib/money";
import { diffSummary, etapaAtualLabel, formatDuration, MSG_SEM_PERMISSAO, podeAtuar, tempoDecorrido } from "../lib/tasks";
import { useAdaptivePolling } from "../lib/polling";
import type { Epic, Pipeline, Project, Repository, RepositoryMember, RunEvent, Task, TaskStep, TimelineEvent } from "../types";

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

  const { user } = useAuth();
  const [members, setMembers] = useState<RepositoryMember[]>([]);

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
  // Troca de pipeline (reiniciar o trabalho com outra pipeline)
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [pipelineSel, setPipelineSel] = useState<number | null>(null);
  const [pipelineBusy, setPipelineBusy] = useState(false);
  // Associação organizacional Projeto > Épico (editável em qualquer status —
  // metadados; nomes no resumo independentes da edição).
  const [editProjects, setEditProjects] = useState<Project[]>([]);
  const [editProjectsLoading, setEditProjectsLoading] = useState(true);
  const [projEditSel, setProjEditSel] = useState("");
  const [editEpics, setEditEpics] = useState<Epic[]>([]);
  const [editEpicsLoading, setEditEpicsLoading] = useState(false);
  const [epicEditSel, setEpicEditSel] = useState("");
  const [assocBusy, setAssocBusy] = useState(false);
  const [taskEpics, setTaskEpics] = useState<Epic[]>([]);
  const assocInit = useRef(false);
  const storyInit = useRef(false);
  const prevStatus = useRef<string | null>(null);

  useEffect(() => {
    api.listRepositories().then((repos: Repository[]) => {
      const m: Record<number, string> = {};
      for (const r of repos) m[r.id] = r.name;
      setRepoNames(m);
    }).catch(() => {});
  }, []);

  // Membros do projeto: admin do projeto (permissão de atuação) + alimenta o
  // controle de atribuição de responsável.
  useEffect(() => {
    let active = true;
    api
      .listMembers(repoIdNum)
      .then((m) => {
        if (active) setMembers(m);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [repoIdNum]);

  // Pipelines disponíveis para trocar o pipeline da task (reiniciar o trabalho).
  useEffect(() => {
    let active = true;
    api
      .listPipelines(repoIdNum)
      .then((list) => {
        if (active) {
          setPipelines(list);
          if (list.length > 0) setPipelineSel((prev) => prev ?? list[0].id);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [repoIdNum]);

  // Projetos do repositório: alimentam a edição da associação e os nomes no resumo.
  useEffect(() => {
    let active = true;
    setEditProjectsLoading(true);
    api
      .listProjects(repoIdNum)
      .then((list) => {
        if (active) setEditProjects(list);
      })
      .catch(() => {
        if (active) setEditProjects([]);
      })
      .finally(() => {
        if (active) setEditProjectsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [repoIdNum]);

  // Épicos do projeto selecionado na edição (não reseta o valor: a inicialização
  // sincroniza o select com a associação atual da task).
  useEffect(() => {
    if (projEditSel === "") {
      setEditEpics([]);
      setEditEpicsLoading(false);
      return;
    }
    let active = true;
    setEditEpicsLoading(true);
    api
      .listEpics(Number(projEditSel))
      .then((list) => {
        if (active) setEditEpics(list);
      })
      .catch(() => {
        if (active) setEditEpics([]);
      })
      .finally(() => {
        if (active) setEditEpicsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projEditSel]);

  // Épicos do projeto ATUAL da task (nomes exibidos no resumo).
  useEffect(() => {
    if (task?.project_id == null) {
      setTaskEpics([]);
      return;
    }
    let active = true;
    api
      .listEpics(task.project_id)
      .then((list) => {
        if (active) setTaskEpics(list);
      })
      .catch(() => {
        if (active) setTaskEpics([]);
      });
    return () => {
      active = false;
    };
  }, [task?.project_id]);

  // Polling adaptativo: a task "ativa" (fila ou fase rodando) mantém 1,5 s;
  // ociosa (done/needs_review/open/bloqueada) reduz a frequência (backoff até 10 s).
  const taskActive = task?.status === "in_progress" || task?.status === "queued";

  useAdaptivePolling(
    (signal) =>
      api
        .getTask(taskId, signal)
        .then((t) => {
          setTask(t);
          setFeedbackText((current) => current || t.feedback || "");
          setBouncebackTarget((prev) => prev || suggestedBouncebackTarget(t));
          setDetailsText((current) => current || t.details || "");
          // Associação Projeto > Épico: sincroniza os selects na primeira carga
          // (depois disso o usuário assume o controle da edição).
          if (!assocInit.current) {
            assocInit.current = true;
            setProjEditSel(t.project_id == null ? "" : String(t.project_id));
            setEpicEditSel(t.epic_id == null ? "" : String(t.epic_id));
          }
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
              .listEvents(running.id, "subtask_start", "desc", signal)
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
              toLoad.map((s) => api.listEvents(s.id, undefined, undefined, signal)),
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
        .catch((e) => {
          if (!signal.aborted) setError(String(e));
        }),
    { activeIntervalMs: 1500, idleIntervalMs: 10000, isActive: taskActive, deps: [taskId] },
  );

    useAdaptivePolling(
      (signal) => api.getTaskTimeline(taskId, signal).then(setTimeline).catch(() => {}),
      { activeIntervalMs: 1500, idleIntervalMs: 10000, isActive: taskActive, deps: [taskId] },
    );

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

  const changePipeline = async () => {
    if (pipelineSel == null) return;
    if (
      !window.confirm(
        "Trocar a pipeline desta tarefa? As fases atuais são arquivadas (histórico preservado) " +
          "e o trabalho reinicia do zero com a nova pipeline. Você pode trocar mesmo se a tarefa " +
          "já rodou — serve para corrigir algo.",
      )
    ) {
      return;
    }
    setPipelineBusy(true);
    try {
      const updated = await api.changePipeline(taskId, pipelineSel);
      setTask(updated);
      setPipelineSel(updated.pipeline_id);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setPipelineBusy(false);
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

  /** Troca o projeto na edição: reseta o épico (dependência) e recarrega a lista. */
  const onProjEditChange = (value: string) => {
    setProjEditSel(value);
    setEpicEditSel("");
  };

  /** Salva a associação Projeto > Épico (PATCH em qualquer status). Opções "Sem
   *  projeto"/"Sem épico" enviam `null` (remoção); erro 400/404 vira banner no
   *  topo via `setError`, preservando os valores dos selects. */
  const saveAssociation = async () => {
    setAssocBusy(true);
    try {
      await api.updateTaskStory(taskId, {
        project_id: projEditSel === "" ? null : Number(projEditSel),
        epic_id: epicEditSel === "" ? null : Number(epicEditSel),
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setAssocBusy(false);
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

  const instructAndRetry = async (instruction: string, position: number) => {
    setContinueBusy(true);
    try {
      await api.retryStep(taskId, position, instruction.trim() || undefined);
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
  const scrollToMerge = () => {
    document.getElementById("conflito-merge")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const scrollToApproval = () => {
    document.getElementById("aprovacao-humana")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const scrollToReview = () => {
    document.getElementById("revisao-humana")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  if (!task) return <p>Carregando…</p>;

  // Permissão de atuação: sem responsável qualquer autenticado atua; com
  // responsável, só ele, admin do projeto ou admin global (auth OFF libera).
  const isRepoAdmin =
    user != null && members.some((m) => m.role === "admin" && m.user_id === user.id);
  const canAct = podeAtuar(user, task.responsible_id, isRepoAdmin);
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;

  // Nomes da associação atual Projeto > Épico (exibidos no resumo; "—" se vazio).
  const projectNameFor = (id: number | null) =>
    id == null ? null : (editProjects.find((p) => p.id === id)?.name ?? null);
  const epicNameFor = (id: number | null) =>
    id == null ? null : (taskEpics.find((e) => e.id === id)?.name ?? null);

  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  const runningStep = steps.find((s) => s.status === "running") ?? null;
  const runningEvents = runningStep ? eventsByStep[runningStep.id] ?? [] : [];
  const runningToolCall = [...runningEvents].reverse().find((e) => e.kind === "tool_call") ?? null;
  const live = runningStep
    ? { step: runningStep, toolCall: runningToolCall, events: runningEvents }
    : null;
  // Bloqueio por agente (aguardando instrução via autoia_blocked.json) vs. bloqueio
  // por conflito de merge (integração) — painéis e ações diferentes.
  const agentBlocked = task.status === "blocked" && task.block_reason != null;
  const mergeConflict =
    task.status === "blocked" && (task.error ?? "").toLowerCase().includes("conflito");

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
  // Tempo total de execução da tarefa (soma das fases com timestamps completos).
  const totalMs = steps.reduce((acc, s) => {
    if (s.started_at && s.finished_at) {
      acc += Math.max(0, new Date(s.finished_at).getTime() - new Date(s.started_at).getTime());
    }
    return acc;
  }, 0);
  const hasTimestamps = steps.some((s) => s.started_at && s.finished_at);

  return (
    <div>
      {/* Erro (ex.: 403 sem permissão) exibido sem sair da tela — descartável. */}
      {error && (
        <div className="sticky-alert sticky-alert-critical">
          <span>{error}</span>
          <button className="link-btn" onClick={() => setError("")}>×</button>
        </div>
      )}
      <p>
        <Link to={`/${repoId}`}>← projeto</Link>
        {" · "}
        <Link to={`/${repoId}/tasks`}>tarefas</Link>
        {" · "}
        <Link to={`/${repoId}/tasks/${task.id}/workspace`}>workspace ↗</Link>
      </p>

      {/* Cabeçalho da tarefa (não fixo): dados organizados e espaçados, com o
          conteúdo da solicitação visível no resumo. */}
      <div className="task-head">
        <div className="task-head-top">
          <h2>
            #{task.id} {task.title}
          </h2>
          <StatusBadge status={task.status} />
        </div>

        <div className="task-head-grid">
          <div>
            <span className="task-head-label">Executor</span>
            <span>{task.executor === "codex" ? "codex" : task.executor === "opencode" ? "opencode" : "kimi code"}</span>
          </div>
          <div>
            <span className="task-head-label">Responsável</span>
            <span>{task.responsible?.name ?? "Não atribuída"}</span>
          </div>
          <div>
            <span className="task-head-label">Branch</span>
            <code>{task.branch ?? "—"}</code>
          </div>
          <div>
            <span className="task-head-label">Orçamento</span>
            <span>{fmtBudget(task.cost_spent, task.budget_limit)}</span>
          </div>
          <div>
            <span className="task-head-label">Decisões PM</span>
            <span>{task.pm_decisions}</span>
          </div>
          {task.subtasks.length > 0 && (
            <div>
              <span className="task-head-label">Subtarefas</span>
              <span>
                {subtasksDone}/{task.subtasks.length} concluídas
              </span>
            </div>
          )}
          {hasTimestamps && (
            <div>
              <span className="task-head-label">Tempo total</span>
              <span>{formatDuration(totalMs)}</span>
            </div>
          )}
        </div>

        <div className="task-head-row">
          <ResponsavelControl task={task} repoId={repoIdNum} onAssigned={setTask} />
          {["created", "queued", "in_progress", "paused", "needs_review", "waiting_approval", "blocked"].includes(
            task.status,
          ) && (
            <div className="task-head-actions">
              {task.status === "paused" ? (
                <button onClick={() => taskAction("resume")} disabled={actionBusy || !canAct} title={actTitle}>
                  retomar
                </button>
              ) : task.status === "queued" || task.status === "in_progress" ? (
                <button onClick={() => taskAction("pause")} disabled={actionBusy || !canAct} title={actTitle}>
                  pausar
                </button>
              ) : null}
              <button
                className="danger"
                onClick={() => taskAction("cancel")}
                disabled={actionBusy || !canAct}
                title={actTitle}
              >
                cancelar tarefa
              </button>
            </div>
          )}
          {pipelines.length > 0 && (
            <div className="task-head-actions" style={{ marginLeft: "auto" }}>
              <select
                value={pipelineSel ?? ""}
                onChange={(e) => setPipelineSel(e.target.value ? Number(e.target.value) : null)}
                title="Trocar a pipeline da tarefa (reinicia o trabalho do zero, arquivando o histórico)"
              >
                {pipelines.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.repository_id == null ? " (global)" : ""}
                    {p.id === task.pipeline_id ? " — atual" : ""}
                  </option>
                ))}
              </select>
              <button
                onClick={() => void changePipeline()}
                disabled={pipelineBusy || !canAct || pipelineSel == null || pipelineSel === task.pipeline_id}
                title={actTitle}
              >
                {pipelineBusy ? "…" : "trocar pipeline"}
              </button>
            </div>
          )}
        </div>

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
              <span
                className="task-live-value mono task-cmd"
                title={runningToolCall ? formatToolCall(runningToolCall) : "aguardando interação…"}
              >
                {runningToolCall ? formatToolCall(runningToolCall) : "aguardando interação…"}
              </span>
            </div>
          </div>
        )}

        {task.status === "needs_review" && (
          <div className="sticky-alert">
            <span>
              ⚠ Aguardando revisão humana — o pipeline parou
              {task.error ? ` · ${task.error}` : ""}
            </span>
            <button onClick={scrollToReview}>ir para a revisão ↓</button>
          </div>
        )}
        {task.status === "waiting_approval" && (
          <div className="sticky-alert">
            <span>⚠ Aguardando aprovação humana — o pipeline parou na fase {etapaAtualLabel(task)}</span>
            <button onClick={scrollToApproval}>ir para a aprovação ↓</button>
          </div>
        )}
        {agentBlocked && (
          <div className="sticky-alert sticky-alert-critical">
            <span>⛔ Desenvolvimento bloqueado — aguardando sua instrução para continuar</span>
            <button onClick={scrollToBlocked}>dar instrução ↓</button>
          </div>
        )}
        {mergeConflict && (
          <div className="sticky-alert sticky-alert-critical">
            <span>⚠ Conflito de merge — instrua o robô e re-execute a fase para resolver</span>
            <button onClick={scrollToMerge}>resolver ↓</button>
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
          {/* Conteúdo da tarefa (solicitação) — o que foi pedido, no topo. */}
          <div className="card">
            <div className="card-title">
              <strong>Solicitação</strong>
              <span className="muted small">o que foi pedido</span>
            </div>
            {task.description ? (
              <Markdown text={task.description} />
            ) : (
              <p className="muted">Sem descrição.</p>
            )}
            {task.acceptance_criteria && (
              <>
                <div className="form-label" style={{ marginTop: 10 }}>Critérios de aceite</div>
                <Markdown text={task.acceptance_criteria} />
              </>
            )}
            {/* Associação organizacional Projeto > Épico (sempre visível; "—" sem associação) */}
            <div className="muted small" style={{ marginTop: 10 }}>
              Projeto: <b>{projectNameFor(task.project_id) ?? "—"}</b>
              {" · "}Épico: <b>{epicNameFor(task.epic_id) ?? "—"}</b>
            </div>
          </div>

          <SituacaoCard
            task={task}
            runningStep={runningStep}
            runningToolCall={runningToolCall}
            onGoAcompanhamento={() => setView("acompanhamento")}
          />

          {agentBlocked && (
            <BlockedPanel task={task} onContinue={continueBlocked} busy={continueBusy} canAct={canAct} />
          )}
          {mergeConflict && (
            <MergeConflictPanel task={task} steps={steps} onInstructAndRetry={instructAndRetry} busy={continueBusy} canAct={canAct} />
          )}

          <TaskSummaryCard summary={task.summary} onRegenerate={regenerateSummary} busy={summaryBusy} />

          {changedSteps.length > 0 && (
            <div className="card">
              <div className="card-title">
                <strong>Principais alterações</strong>
              </div>
              <ul className="summary-list">
                {changedSteps.map((s) => (
                  <li key={s.id}>
                    Fase {s.position} ({s.robot?.name ?? "?"}) · {diffSummary(s.diff_stat) ?? "arquivos alterados"}
                  </li>
                ))}
              </ul>
            </div>
          )}

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
              canAct={canAct}
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
            canAct={canAct}
          />
        </div>
      )}

      {/* ─────────────── Nível 2: Acompanhamento ─────────────── */}
      {view === "acompanhamento" && (
        <div className="acompanhamento-view">
          {agentBlocked && (
            <BlockedPanel task={task} onContinue={continueBlocked} busy={continueBusy} canAct={canAct} />
          )}
          {mergeConflict && (
            <MergeConflictPanel task={task} steps={steps} onInstructAndRetry={instructAndRetry} busy={continueBusy} canAct={canAct} />
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
              <button disabled={detailsBusy || !canAct} title={actTitle} onClick={saveDetails}>
                {detailsBusy ? "salvando…" : "salvar detalhes"}
              </button>
            </div>
          </div>

          {/* Associação Projeto > Épico (metadados organizacionais) */}
          <div className="card">
            <div className="card-title">
              <strong>Projeto / Épico</strong>
              <span className="muted small">associação organizacional — não afeta a execução</span>
            </div>
            <div className="form-inline">
              <div className="form-field">
                <label className="form-label">Projeto</label>
                <select
                  value={projEditSel}
                  onChange={(e) => onProjEditChange(e.target.value)}
                  disabled={editProjectsLoading}
                >
                  {editProjectsLoading ? (
                    <option value="">Carregando…</option>
                  ) : editProjects.length === 0 ? (
                    <option value="">Nenhum projeto cadastrado</option>
                  ) : (
                    <>
                      <option value="">Sem projeto</option>
                      {editProjects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </>
                  )}
                </select>
              </div>
              <div className="form-field">
                <label className="form-label">Épico</label>
                <select
                  value={epicEditSel}
                  onChange={(e) => setEpicEditSel(e.target.value)}
                  disabled={editProjectsLoading || projEditSel === "" || editEpicsLoading}
                >
                  {projEditSel === "" ? (
                    <option value="">Selecione um projeto</option>
                  ) : editEpicsLoading ? (
                    <option value="">Carregando…</option>
                  ) : editEpics.length === 0 ? (
                    <option value="">Nenhum épico deste projeto</option>
                  ) : (
                    <>
                      <option value="">Sem épico</option>
                      {editEpics.map((ep) => (
                        <option key={ep.id} value={ep.id}>
                          {ep.name}
                        </option>
                      ))}
                    </>
                  )}
                </select>
              </div>
            </div>
            <div className="form-actions">
              <button disabled={assocBusy || !canAct} title={actTitle} onClick={() => void saveAssociation()}>
                {assocBusy ? "Salvando…" : "salvar associação"}
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
              canAct={canAct}
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
                        <button
                          className="danger small"
                          onClick={() => subtaskRetry(st.position)}
                          disabled={!canAct}
                          title={actTitle}
                        >
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
            canAct={canAct}
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
              canAct={canAct}
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
              canAct={canAct}
            />
            <PhasePanel
              step={panelStep}
              repoId={repoIdNum}
              taskId={taskId}
              taskStatus={task.status}
              onClose={() => setPanelStep(null)}
              onRetry={retry}
              canAct={canAct}
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
                  {child.kind} · {fmtCost(child.cost_spent)}
                </div>
                {child.status === "created" && (
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button
                      disabled={!canAct}
                      title={actTitle}
                      onClick={async () => {
                        try { await api.startTask(child.id); await refresh(); }
                        catch (e) { setError(String(e)); }
                      }}
                    >
                      aprovar e iniciar
                    </button>
                    <button
                      className="danger"
                      disabled={!canAct}
                      title={actTitle}
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
          <button
            disabled={feedbackBusy || !feedbackText.trim() || !canAct}
            title={actTitle}
            onClick={saveFeedback}
          >
            salvar nota
          </button>
          <button
            className="danger"
            disabled={feedbackBusy || !task.feedback || !canAct}
            title={actTitle}
            onClick={clearFeedback}
          >
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
            <span className="mono task-cmd" title={runningToolCall ? formatToolCall(runningToolCall) : "aguardando interação…"}>
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
    const mergeConflict =
      task.status === "blocked" && (task.error ?? "").toLowerCase().includes("conflito");
    const msg =
      task.status === "needs_review"
        ? "parada aguardando revisão humana"
        : task.status === "waiting_approval"
          ? "parada aguardando aprovação humana (gate do pipeline)"
          : task.status === "blocked"
            ? mergeConflict
              ? "parada por conflito de merge (integração)"
              : "parada: o agente não conseguiu continuar sozinho"
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
 *  da história) — reagrupadas na aba Acompanhamento. `canAct` desabilita as ações
 *  quando o usuário não é responsável/admin (tooltip explica o motivo). */
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
  canAct: boolean;
}) {
  const {
    task, steps, extraBudget, setExtraBudget,
    bouncebackTarget, setBouncebackTarget, bouncebackNote, setBouncebackNote,
    reviewedBy, setReviewedBy, bouncebackBusy, bounceback, review,
    approvalNote, setApprovalNote, approvalBusy, approveGate,
    voltarTarget, setVoltarTarget, voltarFase,
    pmBusy, pmDecide, storyDesc, setStoryDesc, storyCriteria, setStoryCriteria,
    storyBusy, saveStory, canAct,
  } = props;
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;

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

          {/* Resumo do problema (LLM) para entender o que aconteceu sem abrir logs */}
          {task.summary && (
            <div className="review-summary" style={{ marginBottom: 12 }}>
              <div className="form-label">Resumo do problema</div>
              <ul className="summary-list summary-list-warn">
                {(task.summary.issues.length > 0 ? task.summary.issues : [task.summary.summary])
                  .slice(0, 5)
                  .map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
              </ul>
              {task.summary.tasks_summary && (
                <p className="muted small" style={{ marginTop: 6, marginBottom: 0 }}>
                  {task.summary.tasks_summary}
                </p>
              )}
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
                <button type="submit" disabled={bouncebackBusy || !canAct} title={actTitle}>
                  {bouncebackBusy ? "retornando…" : "confirmar retorno"}
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={!canAct}
                  title={actTitle}
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
                <label className="form-label">Orçamento extra (R$)</label>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={extraBudget}
                  onChange={(e) => setExtraBudget(Number(e.target.value))}
                  className="short"
                />
              </div>
              <button type="submit" disabled={!canAct} title={actTitle}>aprovar e continuar</button>
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
                disabled={approvalBusy || !gated || !canAct}
                title={actTitle}
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
                    disabled={approvalBusy || !canAct}
                    title={actTitle}
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
          <button onClick={pmDecide} disabled={pmBusy || !canAct} title={actTitle}>
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
              <button disabled={storyBusy || !canAct} title={actTitle} onClick={saveStory}>
                {storyBusy ? "salvando…" : "salvar alterações"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
