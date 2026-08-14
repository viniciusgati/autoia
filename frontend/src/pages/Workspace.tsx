import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import DiffView from "../components/DiffView";
import ResponsavelControl from "../components/ResponsavelControl";
import { usePolling } from "../lib/polling";
import { MSG_SEM_PERMISSAO, podeAtuar } from "../lib/tasks";
import Markdown from "../lib/markdown";
import { fmtCost } from "../lib/money";
import type { RepositoryMember, StepDiff, Task, TaskProposal, Workspace, WorkspaceOccurrence } from "../types";

/** Estados do workspace (mapeamento dos status do sistema para os 7 do blueprint). */
function statusMeta(status: string): { label: string; cls: string } {
  switch (status) {
    case "created":
      return { label: "Não iniciada", cls: "badge-muted" };
    case "queued":
    case "in_progress":
      return { label: "Em execução", cls: "badge-run" };
    case "paused":
      return { label: "Pausada", cls: "badge-warn" };
    case "waiting_approval":
    case "needs_review":
      return { label: "Aguardando decisão", cls: "badge-warn" };
    case "blocked":
      return { label: "Bloqueada", cls: "badge-err" };
    case "failed":
      return { label: "Erro", cls: "badge-err" };
    case "done":
      return { label: "Concluída", cls: "badge-ok" };
    case "cancelled":
      return { label: "Cancelada", cls: "badge-err" };
    default:
      return { label: status, cls: "badge-muted" };
  }
}

function occStatusMeta(status: string): { label: string; cls: string } {
  switch (status) {
    case "done":
      return { label: "Concluído", cls: "badge-ok" };
    case "failed":
      return { label: "Falhou", cls: "badge-err" };
    case "blocked":
      return { label: "Parado", cls: "badge-warn" };
    case "guardrail_blocked":
      return { label: "Guardrail", cls: "badge-err" };
    case "running":
      return { label: "Em andamento", cls: "badge-run" };
    default:
      return { label: "Interrompido", cls: "badge-warn" };
  }
}

/** Metadados da parada de uma ocorrência (motivo da falha/bloqueio). */
function stopMeta(kind: string): { label: string; cls: string } {
  switch (kind) {
    case "verdict":
      return { label: "❌ REPROVAÇÃO — a revisão não aprovou esta etapa", cls: "ws-stop-verdict" };
    case "guardrail_blocked":
      return { label: "⛔ GUARDRAIL BLOQUEOU A EXECUÇÃO", cls: "ws-stop-guardrail" };
    case "timeout":
      return { label: "⏱ TIMEOUT — o robô não respondeu", cls: "ws-stop-timeout" };
    case "exec_exit":
      return { label: "❌ ERRO DO EXECUTOR", cls: "" };
    case "git_error":
      return { label: "❌ ERRO DE GIT", cls: "" };
    case "merge_error":
    case "merge_failed":
      return { label: "❌ MERGE FALHOU", cls: "" };
    case "budget_hit":
      return { label: "💰 ORÇAMENTO ESTOURADO", cls: "" };
    case "post_merge_failed":
      return { label: "❌ FALHA PÓS-MERGE (código já integrado)", cls: "" };
    case "task_blocked":
      return { label: "🟠 ETAPA PARADA — AGUARDANDO DECISÃO/INSTRUÇÃO", cls: "" };
    case "subtask_bounce_back":
      return { label: "↩️ TAREFAS REPROVADAS NA VERIFICAÇÃO — voltam para o developer", cls: "ws-stop-timeout" };
    case "subtask_failed":
      return { label: "❌ SUBTAREFA FALHOU", cls: "" };
    default:
      return { label: "❌ ETAPA PARADA", cls: "" };
  }
}

/** Ícone do evento de subtarefa pelo nome do marcador (da timeline). */
function subtaskIcon(name: string): { icon: string; cls: string } {
  if (name.includes("falhou")) return { icon: "✕", cls: "ws-sub-err" };
  if (name.includes("concluída")) return { icon: "✓", cls: "ws-sub-ok" };
  if (name.includes("implementada")) return { icon: "✅", cls: "ws-sub-ok" };
  if (name.includes("iniciada")) return { icon: "▸", cls: "ws-sub-run" };
  return { icon: "•", cls: "" };
}

/** Subtarefas com atividade nesta ocorrência (dos eventos da timeline). */
function occurrenceSubtasks(occ: WorkspaceOccurrence) {
  // marcadores de EXECUÇÃO de subtarefa ("tarefa iniciada/implementada/..."),
  // excluindo o resumo "tarefas propostas pelo agente" (plural).
  return occ.system_activity.filter((a) => a.name.startsWith("tarefa "));
}

const SUB_LABELS: Record<string, { label: string; cls: string }> = {
  pending: { label: "pendente", cls: "badge-muted" },
  implementing: { label: "implementando", cls: "badge-run" },
  implemented: { label: "implementada", cls: "badge-ok" },
  verifying: { label: "verificando", cls: "badge-run" },
  done: { label: "concluída", cls: "badge-ok" },
  failed: { label: "falhou", cls: "badge-err" },
};

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

/** Formata uma duração em ms como "Xh Ym Zs" (ou "Ym Zs"/"Zs" para tempos curtos). */
function fmtDur(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "";
  const total = Math.max(0, Math.round(ms));
  const s = Math.floor((total / 1000) % 60);
  const m = Math.floor((total / 60000) % 60);
  const h = Math.floor(total / 3600000);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Seletor de "continuar a partir de" (fases já executadas ou todas). */
function availablePositions(task: Task): { position: number; label: string }[] {
  const sorted = [...task.steps].sort((a, b) => a.position - b.position);
  const maxExecuted = Math.max(
    ...sorted.filter((s) => s.status !== "pending").map((s) => s.position),
    -1,
  );
  const include = task.status === "done" ? sorted : sorted.filter((s) => s.position <= maxExecuted);
  return include.map((s) => ({
    position: s.position,
    label: `Fase ${s.position + 1} · ${s.robot?.name ?? "?"}`,
  }));
}

function ProposalRow({ proposal, onChanged, onError, canAct }: {
  proposal: TaskProposal;
  onChanged: () => void;
  onError: (msg: string) => void;
  canAct: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      onChanged();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ws-proposal">
      <div className="ws-proposal-title">{proposal.title}</div>
      {proposal.description && <div className="ws-proposal-desc">{proposal.description}</div>}
      {proposal.status === "pending" ? (
        <div className="ws-proposal-actions">
          <button disabled={busy || !canAct} title={actTitle} onClick={() => run(() => api.acceptProposal(proposal.task_id, proposal.id))}>
            {busy ? "…" : "Aceitar"}
          </button>
          <button className="danger" disabled={busy || !canAct} title={actTitle} onClick={() => run(() => api.rejectProposal(proposal.task_id, proposal.id))}>
            Recusar
          </button>
        </div>
      ) : (
        <span className={`ws-proposal-status ${proposal.status === "accepted" ? "ws-ok" : "ws-err"}`}>
          {proposal.status === "accepted" ? "✓ Tarefa aceita" : "✕ Tarefa recusada"}
        </span>
      )}
    </div>
  );
}

function DiffModal({ taskId, position, onClose }: {
  taskId: number;
  position: number;
  onClose: () => void;
}) {
  const [diff, setDiff] = useState<StepDiff | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .getStepDiff(taskId, position)
      .then((d) => active && setDiff(d))
      .catch((e) => active && setError(String(e)));
    return () => {
      active = false;
    };
  }, [taskId, position]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal ws-diff-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>Diff da fase {position + 1}</span>
          <button className="link-btn" onClick={onClose}>fechar</button>
        </div>
        <div className="modal-body">
          {error && <div className="step-error">{error}</div>}
          {!diff && !error && <div className="muted">carregando diff…</div>}
          {diff && diff.diff ? (
            <DiffView code={diff.diff} />
          ) : diff ? (
            <div className="muted">Sem diff para esta fase (nenhum commit com alterações encontrado).</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function OccurrenceCard({ occ, onChanged, onError, onOpenDiff, canAct }: {
  occ: WorkspaceOccurrence;
  onChanged: () => void;
  onError: (msg: string) => void;
  onOpenDiff: (position: number) => void;
  canAct: boolean;
}) {
  const meta = occStatusMeta(occ.status);
  const running = occ.status === "running";
  const deliveredText = occ.delivered?.summary || occ.delivered_text;
  const subtasks = occurrenceSubtasks(occ);
  // Mensagens que o usuário enviou no workspace e motivaram esta execução da fase.
  const interventions = occ.events.filter((e) => e.raw?.kind === "user_intervention");

  // Duração da execução: a do backend quando concluída; em andamento, o tempo
  // decorrido até agora (atualiza a cada poll de 1,5s da página).
  const durationMs =
    occ.duration_ms ??
    (running && occ.started_at ? Date.now() - new Date(occ.started_at).getTime() : null);

  return (
    <article className={`ws-occ ws-occ-${occ.status}`}>
      {running && (
        <div className="ws-occ-running-banner">
          <span className="ws-pulse" /> ETAPA EM EXECUÇÃO — {occ.robot?.name} · Fase {occ.position + 1}
        </div>
      )}
      <header className="ws-occ-head">
        <span className="ws-occ-pos">FASE {occ.position + 1}</span>
        <span className="ws-occ-robot">{occ.robot?.name ?? "?"}</span>
        {occ.is_rerun && (
          <span className="badge badge-warn" title="Nova execução da mesma fase — o histórico anterior foi preservado">
            ↻ Nova execução · tentativa {occ.attempt}
          </span>
        )}
        <span className={`badge ${meta.cls}`}>{meta.label}</span>
        <span className="ws-occ-time">
          {fmtTime(occ.started_at)} {occ.finished_at ? `→ ${fmtTime(occ.finished_at)}` : ""}
          {durationMs != null && (
            <span className={`ws-occ-duration${running ? " ws-occ-duration-run" : ""}`}>
              ⏱ {fmtDur(durationMs)}
            </span>
          )}
        </span>
      </header>

      {/* 2. MISSÃO desta execução — o conteúdo principal do card. */}
      <section className="ws-occ-section ws-mission-box">
        <h4 className="ws-section-title">
          Missão desta execução
          {occ.mission_source === "llm" && (
            <span className="badge ws-badge-delivered">resumo LLM</span>
          )}
        </h4>
        <p className="ws-mission" title={occ.mission || occ.goal || undefined}>
          {occ.mission || occ.goal || "Execução em preparação…"}
        </p>
      </section>

      {/* 4. EM ANDAMENTO — atividade atual da execução. */}
      {running && occ.last_activity && (
        <section className="ws-occ-section">
          <h4 className="ws-section-title">Em andamento</h4>
          <p className="ws-live">
            <span className="ws-pulse" />
            <span className="ws-live-text" title={occ.last_activity}>{occ.last_activity}</span>
          </p>
        </section>
      )}

      {/* 4. MOTIVO DA PARADA — imediatamente depois da missão. */}
      {!running && occ.stop && (
        <section className={`ws-occ-section ws-stop${occ.stop.detail ? " ws-stop-with-detail" : ""}`}>
          <h4 className="ws-section-title">
            <span className={stopMeta(occ.stop.kind).cls}>
              {stopMeta(occ.stop.kind).label}
            </span>
          </h4>
          <p className="ws-stop-reason">{occ.stop.reason || "execução interrompida"}</p>
          {occ.stop.detail && (
            <div className="ws-stop-detail">
              <Markdown text={occ.stop.detail} />
            </div>
          )}
        </section>
      )}

      {/* 3. RESULTADO — o que esta execução resolveu/entregou. */}
      {!running && deliveredText && (
        <section className="ws-occ-section ws-result">
          <h4 className="ws-section-title">
            O que foi resolvido
            {occ.delivered ? (
              <span className="badge ws-badge-delivered">resumo LLM</span>
            ) : (
              <span className="badge badge-muted ws-badge-delivered">texto do robô</span>
            )}
          </h4>
          <div className="ws-delivered">
            <Markdown text={deliveredText} />
          </div>
        </section>
      )}

      {interventions.length > 0 && (
        <section className="ws-occ-section ws-intervention">
          <h4 className="ws-section-title">👤 Sua mensagem para o robô</h4>
          {interventions.map((e, i) => (
            <div key={i} className="ws-intervention-item">
              <span className="muted small">{fmtTime(e.ts)}</span>
              <p className="ws-intervention-text">
                {String(e.raw?.payload?.instruction ?? "") || e.summary}
              </p>
            </div>
          ))}
        </section>
      )}

      {subtasks.length > 0 && (
        <section className="ws-occ-section">
          <h4 className="ws-section-title">Subtarefas desta execução</h4>
          <ul className="ws-subtasks">
            {subtasks.map((a, i) => {
              const si = subtaskIcon(a.name);
              return (
                <li key={i} className={`ws-subtask-item ${si.cls}`}>
                  <span className="ws-sub-icon">{si.icon}</span>
                  <span>{a.summary}</span>
                  <span className="muted small">{fmtTime(a.ts)}</span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {occ.tests && (
        <section className="ws-occ-section">
          <h4 className="ws-section-title">Resultado de testes</h4>
          <div className="ws-tests">
            {occ.tests.passed != null && <span className="ws-test-ok">✓ {occ.tests.passed} testes passaram</span>}
            {occ.tests.failed != null && <span className="ws-test-err">✕ {occ.tests.failed} testes falharam</span>}
            {occ.tests.verdict && <span>Veredicto: <b>{occ.tests.verdict}</b></span>}
          </div>
        </section>
      )}

      {occ.proposals.length > 0 && (
        <section className="ws-occ-section">
          <h4 className="ws-section-title">Tarefas propostas</h4>
          <div className="ws-proposals">
            {occ.proposals.map((p) => (
              <ProposalRow key={p.id} proposal={p} onChanged={onChanged} onError={onError} canAct={canAct} />
            ))}
          </div>
        </section>
      )}

      {occ.file_count > 0 && (
        <section className="ws-occ-section">
          <h4 className="ws-section-title">Arquivos alterados</h4>
          <ul className="ws-files">
            {occ.files.slice(0, 10).map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
          <div className="ws-file-actions">
            {occ.file_count > 10 && <span className="muted small">Ver todos os {occ.file_count} arquivos…</span>}
            <button className="link-btn" onClick={() => onOpenDiff(occ.position)}>
              Ver diff
            </button>
          </div>
        </section>
      )}

      {occ.system_activity.length > 0 && (
        <details className="ws-occ-section ws-collapse">
          <summary className="ws-section-title">Atividade do sistema ({occ.system_activity.length})</summary>
          <ul className="ws-sysact">
            {occ.system_activity.map((a, i) => (
              <li key={i} className="ws-sysact-item">
                <span className="muted small">{fmtTime(a.ts)}</span>
                <span className="ws-sysact-text" title={a.summary}>{a.summary}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {occ.events.length > 0 && (
        <details className="ws-occ-section ws-collapse">
          <summary className="ws-section-title">Detalhes técnicos ({occ.events.length} eventos)</summary>
          <ul className="ws-sysact">
            {occ.events.map((e, i) => (
              <li key={i} className="ws-sysact-item">
                <span className="muted small">{fmtTime(e.ts)}</span>
                <span className="ws-sysact-text" title={`${e.type} — ${e.summary}`}>
                  [{e.type}] {e.name} — {e.summary}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

export default function Workspace() {
  const { repoId: repoIdStr, taskId: taskIdStr } = useParams<{ repoId: string; taskId: string }>();
  const taskId = Number(taskIdStr);
  const repoId = Number(repoIdStr);

  const { user } = useAuth();
  const [members, setMembers] = useState<RepositoryMember[]>([]);

  const [ws, setWs] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [resumePos, setResumePos] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [diffPos, setDiffPos] = useState<number | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const timelineRef = useRef<HTMLElement>(null);
  // Segue automaticamente o fim da timeline conforme a execução avança; pausa
  // quando o usuário rola para cima (ler histórico) e volta quando chega ao fim.
  const [followLatest, setFollowLatest] = useState(true);

  // Membros do projeto: define admin do projeto (permissão de atuação) e
  // alimenta o controle de atribuição de responsável.
  useEffect(() => {
    let active = true;
    api
      .listMembers(repoId)
      .then((m) => {
        if (active) setMembers(m);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [repoId]);

  const refresh = () => {
    api
      .getWorkspace(taskId)
      .then(setWs)
      .catch((e) => setError(String(e)));
  };

  usePolling(
    (signal) => {
      api
        .getWorkspace(taskId, signal)
        .then(setWs)
        .catch((e) => {
          if (!signal.aborted) setError(String(e));
        });
    },
    1500,
    [taskId],
  );

  // Mantém o fim da timeline sempre visível enquanto a execução evolui.
  useEffect(() => {
    if (!followLatest) return;
    const el = timelineRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ws, followLatest]);

  const onTimelineScroll = () => {
    const el = timelineRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
    if (nearBottom !== followLatest) setFollowLatest(nearBottom);
  };

  const task = ws?.task ?? null;
  const meta = task ? statusMeta(task.status) : null;
  const positions = useMemo(() => (task ? availablePositions(task) : []), [task]);
  const runningOcc = ws?.occurrences.find((o) => o.status === "running") ?? null;
  const nextStep = useMemo(() => {
    if (!task) return null;
    return [...task.steps].sort((a, b) => a.position - b.position).find((s) => s.status === "pending") ?? null;
  }, [task]);

  // Permissão de atuação: sem responsável qualquer autenticado atua; com
  // responsável, só ele, admin do projeto ou admin global (auth OFF libera).
  const isRepoAdmin =
    user != null && members.some((m) => m.role === "admin" && m.user_id === user.id);
  const canAct = podeAtuar(user, task?.responsible_id ?? null, isRepoAdmin);
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const focusInput = () => inputRef.current?.focus();

  /** Atribuição de responsável salva: atualiza o header sem recarregar a página. */
  const handleAssigned = (updated: Task) => {
    setWs((prev) => (prev ? { ...prev, task: updated } : prev));
  };

  const summarize = () => {
    setSummaryBusy(true);
    api
      .regenerateSummary(taskId)
      .catch((e) => setError(String(e)))
      .finally(() => {
        setTimeout(() => {
          refresh();
          setSummaryBusy(false);
        }, 2500);
      });
  };

  const send = async () => {
    if (!instruction.trim()) return;
    const position = resumePos === "" ? undefined : Number(resumePos);
    await act(() => api.sendInstruction(taskId, { instruction: instruction.trim(), position }));
    setInstruction("");
  };

  const chooseDecision = (option: string) => {
    setInstruction(option);
    setTimeout(() => sendDecision(option), 0);
  };

  const sendDecision = (text: string) => {
    api
      .sendInstruction(taskId, { instruction: text })
      .then(() => {
        setInstruction("");
        refresh();
      })
      .catch((e) => setError(String(e)));
  };

  const taskStatus = task?.status ?? "created";
  const controls = (() => {
    const list: { label: string; cls?: string; onClick: () => void }[] = [];
    if (taskStatus === "created") list.push({ label: "Iniciar", onClick: () => act(() => api.startTask(taskId)) });
    if (taskStatus === "queued" || taskStatus === "in_progress")
      list.push({ label: "Pausar", onClick: () => act(() => api.pauseTask(taskId)) });
    if (taskStatus === "paused") list.push({ label: "Continuar", onClick: () => act(() => api.resumeTask(taskId)) });
    if (["blocked", "needs_review", "waiting_approval", "failed"].includes(taskStatus))
      list.push({ label: "Continuar", onClick: focusInput });
    if (taskStatus === "done" && task && task.steps.length > 0) {
      const last = [...task.steps].sort((a, b) => a.position - b.position)[task.steps.length - 1];
      list.push({ label: "Reexecutar", onClick: () => act(() => api.retryStep(taskId, last.position)) });
    }
    if (taskStatus !== "created") list.push({ label: "Resumir", cls: "ws-btn-summary", onClick: summarize });
    return list;
  })();

  return (
    <div className="ws">
      {error && (
        <div className="sticky-alert sticky-alert-critical ws-error">
          <span>{error}</span>
          <button className="link-btn" onClick={() => setError(null)}>×</button>
        </div>
      )}

      <header className="ws-header">
        <div className="ws-header-top">
          <Link to={`/${repoId}/tasks`} className="link-btn ws-back">← tarefas</Link>
          <Link to={`/${repoId}/tasks/${taskId}`} className="link-btn" title="ver tela de detalhes (auditoria)">
            detalhes técnicos ↗
          </Link>
        </div>

        <div className="ws-header-title">
          <h2>{task ? `#${task.id} ${task.title}` : "…"}</h2>
          {meta && (
            <span className={`badge ${meta.cls} ws-status-badge`}>● {meta.label}</span>
          )}
        </div>

        <div className="ws-header-meta">
          {task && (
            <>
              <span className="ws-cost">
                Custo total: <b>{fmtCost(task.cost_spent)}</b>
                <span className="muted small"> / {fmtCost(task.budget_limit)}</span>
              </span>
              <span className="ws-responsavel" title="responsável pela tarefa">
                responsável: <b>{task.responsible?.name ?? "Não atribuída"}</b>
              </span>
              <span className="muted small">{task.executor === "opencode" ? "opencode" : "kimi code"}</span>
              {runningOcc ? (
                <span className="ws-etapa-atual ws-etapa-running" title="fase em execução agora">
                  <span className="ws-pulse" />
                  <b>Etapa em execução:</b> {runningOcc.robot?.name} · Fase {runningOcc.position + 1}
                  {runningOcc.last_activity && (
                    <span className="ws-etapa-activity" title={runningOcc.last_activity}>
                      — {runningOcc.last_activity}
                    </span>
                  )}
                </span>
              ) : taskStatus === "queued" && nextStep ? (
                <span className="ws-etapa-atual" title="próxima fase da fila">
                  <b>Próxima etapa:</b> {nextStep.robot?.name} · Fase {nextStep.position + 1}
                </span>
              ) : null}
            </>
          )}
          <div className="ws-controls">
            {controls.map((c) => (
              <button
                key={c.label}
                className={c.cls ?? ""}
                disabled={busy || summaryBusy || !canAct}
                title={actTitle}
                onClick={c.onClick}
              >
                {summaryBusy && c.label === "Resumir" ? "resumindo…" : c.label}
              </button>
            ))}
          </div>
        </div>

        {task && (
          <ResponsavelControl
            task={task}
            repoId={repoId}
            onAssigned={handleAssigned}
          />
        )}

        {(task?.status === "blocked" || task?.status === "failed" || task?.status === "needs_review") && task.error && (
          <div className={`ws-header-alert ${task.status === "blocked" ? "ws-alert-critical" : ""}`}>
            <span>
              {task.status === "blocked" && task.block_reason_type === "decision_request"
                ? "🟠 Decisão necessária:"
                : task.status === "blocked"
                  ? "⛔ Bloqueada:"
                  : task.status === "failed"
                    ? "❌ Erro:"
                    : "⚠ Revisão:"}{" "}
              {task.error}
            </span>
            <button className="link-btn" onClick={focusInput}>responder ↓</button>
          </div>
        )}
      </header>

      <main className="ws-timeline" ref={timelineRef} onScroll={onTimelineScroll}>
        {!ws && <div className="muted ws-loading">carregando workspace…</div>}
        {ws && ws.occurrences.length === 0 && (
          <div className="ws-empty">
            <p className="muted">Nenhuma etapa executada ainda.</p>
            {taskStatus === "created" && (
              <button
                onClick={() => act(() => api.startTask(taskId))}
                disabled={!canAct}
                title={actTitle}
              >
                Iniciar tarefa
              </button>
            )}
          </div>
        )}

        {ws?.decisions.length ? (
          <section className="ws-decision">
            <h3>🟠 ETAPA PARADA — DECISÃO NECESSÁRIA</h3>
            <p className="ws-decision-question">{ws.decisions[0].question}</p>
            {ws.decisions[0].context && <p className="muted small">{ws.decisions[0].context}</p>}
            <div className="ws-decision-options">
              {ws.decisions[0].options.map((opt) => (
                <button
                  key={opt}
                  className="ws-option-chip"
                  disabled={!canAct}
                  title={actTitle}
                  onClick={() => chooseDecision(opt)}
                >
                  {opt}
                </button>
              ))}
            </div>
            <p className="muted small">ou responda no campo abaixo e envie.</p>
          </section>
        ) : null}

        {ws?.task.subtasks.length ? (
          <section className="ws-subtasks-panel">
            <h4 className="ws-section-title">
              Subtarefas
              <span className="muted small">
                ({ws.task.subtasks.filter((s) => s.status === "done").length}/{ws.task.subtasks.length} concluídas)
              </span>
            </h4>
            <ul className="ws-subtasks-global">
              {[...ws.task.subtasks].sort((a, b) => a.position - b.position).map((s) => {
                const lb = SUB_LABELS[s.status] ?? { label: s.status, cls: "badge-muted" };
                return (
                  <li key={s.position} className="ws-subtask-global-item">
                    <span className="ws-sub-pos">{s.position + 1}</span>
                    <span className="ws-sub-title">{s.title}</span>
                    <span className={`badge ${lb.cls}`}>{lb.label}</span>
                    {s.attempt > 1 && <span className="muted small">tentativa {s.attempt}</span>}
                    {s.verdict && <span className="muted small">{s.verdict}</span>}
                    {s.error && <span className="ws-sub-error" title={s.error}>{s.error}</span>}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {ws?.occurrences.map((occ) => (
          <OccurrenceCard
            key={`${occ.step_id}-${occ.attempt}`}
            occ={occ}
            onChanged={refresh}
            onError={setError}
            onOpenDiff={setDiffPos}
            canAct={canAct}
          />
        ))}
      </main>

      <footer className="ws-input">
        <div className="ws-input-row">
          <textarea
            ref={inputRef}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="Escreva uma instrução para o agente… (corrigir decisão, mudar abordagem, destravar etapa, nova execução…)"
            rows={2}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <button
            className="ws-send"
            disabled={busy || !instruction.trim() || !canAct}
            title={actTitle}
            onClick={() => void send()}
          >
            Enviar
          </button>
        </div>
        <div className="ws-input-foot">
          <label className="ws-resume-label">
            Continuar a partir de:
            <select value={resumePos} onChange={(e) => setResumePos(e.target.value)}>
              <option value="">Etapa atual</option>
              {positions.map((p) => (
                <option key={p.position} value={p.position}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <span className="muted small">Escolher uma etapa anterior cria uma nova execução dela — o histórico permanece intacto.</span>
        </div>
      </footer>

      {diffPos != null && (
        <DiffModal taskId={taskId} position={diffPos} onClose={() => setDiffPos(null)} />
      )}
    </div>
  );
}
