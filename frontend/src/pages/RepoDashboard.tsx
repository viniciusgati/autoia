import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import HelpTip from "../components/HelpTip";
import PhaseStepper from "../components/PhaseStepper";
import ProjectSkills from "../components/ProjectSkills";
import StatusBadge from "../components/StatusBadge";
import { fmtBudget, fmtCost } from "../lib/money";
import { diffSummary } from "../lib/tasks";
import { usePolling } from "../lib/polling";
import type { Pipeline, Repository, RepositoryMember, TaskListItem, TaskStepListItem } from "../types";

const ATIVOS = ["queued", "in_progress", "needs_review", "waiting_approval", "blocked"];

/** Campos editáveis nas Configurações (usados para o badge "alterações não salvas"). */
const SETTINGS_FIELDS = [
  "name",
  "max_attempts",
  "max_pm_decisions",
  "run_timeout",
  "task_budget",
  "cost_per_interaction",
  "risky_patterns_extra",
  "db_rule",
  "allow_external_tasks",
  "auto_summary",
  "default_pipeline_id",
] as const;

/** Mensagem de erro da API sem o prefixo de status ("400: detalhe" → "detalhe"). */
function apiErrorMsg(e: unknown): string {
  return String(e).replace(/^\d+: /, "");
}

/** O form diverge do último valor salvo? */
function repoDirty(repo: Repository, base: Repository): boolean {
  return SETTINGS_FIELDS.some((f) => repo[f] !== base[f]);
}

export default function RepoDashboard() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [baseRepo, setBaseRepo] = useState<Repository | null>(null);
  const [members, setMembers] = useState<RepositoryMember[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState("");
  const savedTimer = useRef<number | null>(null);
  const { user, authEnabled } = useAuth();

  const load = async (signal?: AbortSignal) => {
    try {
      const list = await api.listTasks(repoId, signal);
      setTasks(list);
      setUpdatedAt(new Date());
    } catch (e) {
      setError(String(e));
    }
  };

  // Poll do dashboard do projeto: 10s é suficiente (sem chat ao vivo).
  usePolling(load, 10000, [repoId]);

  useEffect(() => {
    api.listRepositories().then((repos) => {
      const r = repos.find((r) => r.id === repoId) ?? null;
      setRepo(r);
      setBaseRepo(r);
    }).catch(() => {});
    api.listPipelines(repoId).then(setPipelines).catch(() => {});
    // Membros do projeto: define quem é admin do projeto (permissão de skills).
    api.listMembers(repoId).then(setMembers).catch(() => {});
  }, [repoId]);

  /** Atualiza um campo do form e limpa o erro de salvamento anterior. */
  const updateRepo = (next: Repository) => {
    setRepo(next);
    if (saveError) setSaveError("");
  };

  const review = async (task: TaskListItem, action: "approve" | "cancel") => {
    setBusy(task.id);
    setError("");
    try {
      if (action === "cancel") {
        if (!window.confirm(`Cancelar a tarefa #${task.id}?`)) return;
        await api.cancelTask(task.id);
      } else {
        await api.reviewTask(task.id, { action, extra_budget: 0 });
      }
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const quickBounceback = async (task: TaskListItem) => {
    const steps = [...task.steps].sort((a, b) => a.position - b.position);
    const implement = steps.find((s) => s.robot?.role === "implement" && !s.post_merge);
    const target = implement ? implement.position : 0;
    setBusy(task.id);
    setError("");
    try {
      await api.bouncebackTask(task.id, target);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const saveSettings = async () => {
    if (!repo || !formValid) return;
    setSaving(true);
    setSaved(false);
    setSaveError("");
    try {
      const updated = await api.updateRepository(repoId, {
        name: repo.name,
        max_attempts: repo.max_attempts,
        max_pm_decisions: repo.max_pm_decisions,
        run_timeout: repo.run_timeout,
        task_budget: repo.task_budget,
        cost_per_interaction: repo.cost_per_interaction,
        risky_patterns_extra: repo.risky_patterns_extra,
        db_rule: repo.db_rule,
        allow_external_tasks: repo.allow_external_tasks,
        auto_summary: repo.auto_summary,
        default_pipeline_id: repo.default_pipeline_id,
      });
      setRepo(updated);
      setBaseRepo(updated);
      setSaved(true);
      if (savedTimer.current != null) window.clearTimeout(savedTimer.current);
      savedTimer.current = window.setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setSaveError(apiErrorMsg(e));
    } finally {
      setSaving(false);
    }
  };

  if (error) return <p className="error">{error}</p>;

  // Validação inline: nome obrigatório; timeout > 0; budget >= 0; tentativas >= 1
  // (valores vazios = "global" → válidos).
  const nameValid = (repo?.name ?? "").trim().length > 0;
  const attemptsValid = repo == null || repo.max_attempts == null || repo.max_attempts >= 1;
  const timeoutValid = repo == null || repo.run_timeout == null || repo.run_timeout > 0;
  const budgetValid = repo == null || repo.task_budget == null || repo.task_budget >= 0;
  const formValid = nameValid && attemptsValid && timeoutValid && budgetValid;
  const dirty = repo != null && baseRepo != null && repoDirty(repo, baseRepo);

  // Permissão de skills: admin global ou admin do projeto (auth OFF → libera).
  const isRepoAdmin =
    user != null && members.some((m) => m.role === "admin" && m.user_id === user.id);
  const canManageSkills = !authEnabled || user == null || user.role === "admin" || isRepoAdmin;

  const ativas = tasks.filter((t) => ATIVOS.includes(t.status));
  const finalizadas = tasks.filter((t) => !ativas.includes(t));

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Dashboard do projeto</h2>
        <span className="muted">
          {updatedAt ? `atualizado ${updatedAt.toLocaleTimeString()}` : "carregando…"} ·{" "}
          {ativas.length} ativa(s)
        </span>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}/tasks`} className="link-btn">
          + Nova tarefa
        </Link>
      </div>

      {error && <p className="error">{error}</p>}
      {tasks.length === 0 && <p className="muted">Nenhuma tarefa neste projeto.</p>}

      {/* Tarefas ativas */}
      {ativas.map((task) => (
        <TaskCard
          key={task.id}
          task={task}
          repoId={repoId}
          busy={busy}
          onReview={review}
          onBounceback={quickBounceback}
        />
      ))}

      {/* Configurações do projeto (5 seções accordion) */}
      {repo && (
        <details className="config-root" style={{ marginTop: 28 }} open>
          <summary style={{ cursor: "pointer", color: "var(--accent)", fontWeight: 600, fontSize: 15, padding: "8px 0" }}>
            ⚙ Configurações do projeto
            {dirty && (
              <span className="badge badge-warn" style={{ marginLeft: 10, textTransform: "lowercase" }}>
                alterações não salvas
              </span>
            )}
          </summary>
          <div className="card" style={{ marginTop: 10 }}>
            <div className="config-sections">
              {/* ── Geral ── */}
              <details className="config-section" open>
                <summary>Geral</summary>
                <div className="form-stack">
                  <div className={`form-field ${nameValid ? "" : "form-field-invalid"}`}>
                    <label className="form-label">Nome do projeto <HelpTip>Nome exibido nas listas e dashboards. Obrigatório.</HelpTip></label>
                    <input type="text"
                      value={repo.name}
                      onChange={(e) => updateRepo({ ...repo, name: e.target.value })}
                    />
                    {!nameValid && <div className="form-error">O nome do projeto é obrigatório</div>}
                  </div>
                  <div className="form-field">
                    <label className="form-label">Pipeline padrão <HelpTip>Pipeline usado quando uma tarefa é criada sem especificar qual pipeline utilizar.</HelpTip></label>
                    <select
                      value={repo.default_pipeline_id ?? ""}
                      onChange={(e) => updateRepo({ ...repo, default_pipeline_id: e.target.value ? Number(e.target.value) : null })}
                    >
                      <option value="">— global (escolher na criação) —</option>
                      {pipelines.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                          {p.repository_id == null ? " (global)" : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="form-inline">
                    <label className="post-merge-label">
                      <input type="checkbox"
                        checked={repo.auto_summary}
                        onChange={(e) => updateRepo({ ...repo, auto_summary: e.target.checked })}
                      />
                      gerar resumo automaticamente
                      <HelpTip>Gera (e regenera) o resumo do desenvolvimento por LLM a cada avanço de fase e quando a task para (final, revisão, bloqueio). O resumo é opcional — nunca afeta a execução.</HelpTip>
                    </label>
                  </div>
                </div>
              </details>

              {/* ── Execução ── */}
              <details className="config-section">
                <summary>Execução</summary>
                <div className="form-stack">
                  <div className="form-inline">
                    <div className={`form-field ${attemptsValid ? "" : "form-field-invalid"}`} style={{ flex: 1, minWidth: 120 }}>
                      <label className="form-label">Max tentativas <HelpTip>Quantas vezes uma fase pode falhar e ser re-executada antes de travar a task. Config global: variável AUTOIA_MAX_ATTEMPTS.</HelpTip></label>
                      <input type="number" min={1} max={10}
                        value={repo.max_attempts ?? ""}
                        placeholder="global"
                        onChange={(e) => updateRepo({ ...repo, max_attempts: e.target.value ? Number(e.target.value) : null })}
                      />
                      {!attemptsValid && <div className="form-error">Máx. tentativas deve ser ao menos 1</div>}
                    </div>
                    <div className="form-field" style={{ flex: 1, minWidth: 120 }}>
                      <label className="form-label">Max decisões PM <HelpTip>Limite de vezes que o robô PM pode decidir (retry/continuar/escalar) antes de travar. Config global: AUTOIA_MAX_PM_DECISIONS.</HelpTip></label>
                      <input type="number" min={0} max={10}
                        value={repo.max_pm_decisions ?? ""}
                        placeholder="global"
                        onChange={(e) => updateRepo({ ...repo, max_pm_decisions: e.target.value ? Number(e.target.value) : null })}
                      />
                    </div>
                    <div className={`form-field ${timeoutValid ? "" : "form-field-invalid"}`} style={{ flex: 1, minWidth: 120 }}>
                      <label className="form-label">Timeout (seg) <HelpTip>Tempo máximo que o kimi pode rodar por fase. Se estourar, a fase falha. Config global: AUTOIA_RUN_TIMEOUT.</HelpTip></label>
                      <input type="number" min={60} step={60}
                        value={repo.run_timeout ?? ""}
                        placeholder="global"
                        onChange={(e) => updateRepo({ ...repo, run_timeout: e.target.value ? Number(e.target.value) : null })}
                      />
                      {!timeoutValid && <div className="form-error">Timeout deve ser maior que zero</div>}
                    </div>
                  </div>
                  <div className="form-inline">
                    <label className="post-merge-label">
                      <input type="checkbox"
                        checked={repo.allow_external_tasks}
                        onChange={(e) => updateRepo({ ...repo, allow_external_tasks: e.target.checked })}
                      />
                      receber tasks de outros projetos
                      <HelpTip>Outros repositórios podem criar tarefas neste projeto. Ex: o repo de código cria uma task de documentação no repo de docs.</HelpTip>
                    </label>
                  </div>
                </div>
              </details>

              {/* ── Orçamento ── */}
              <details className="config-section">
                <summary>Orçamento</summary>
                <div className="form-stack">
                  <div className="form-inline">
                    <div className={`form-field ${budgetValid ? "" : "form-field-invalid"}`} style={{ flex: 1, minWidth: 120 }}>
                      <label className="form-label">Orçamento (R$) <HelpTip>Limite de gasto por tarefa. Se estourar, a task vai para needs_review. Config global: AUTOIA_TASK_BUDGET.</HelpTip></label>
                      <input type="number" min={0} step={0.5}
                        value={repo.task_budget ?? ""}
                        placeholder="global"
                        onChange={(e) => updateRepo({ ...repo, task_budget: e.target.value ? Number(e.target.value) : null })}
                      />
                      {!budgetValid && <div className="form-error">Orçamento não pode ser negativo</div>}
                    </div>
                    <div className="form-field" style={{ flex: 1, minWidth: 120 }}>
                      <label className="form-label">Custo/interação (R$) <HelpTip>Custo estimado por chamada ao kimi (tool_call + resposta). Usado para calcular gasto acumulado. Config global: AUTOIA_COST_PER_INTERACTION.</HelpTip></label>
                      <input type="number" min={0} step={0.001}
                        value={repo.cost_per_interaction ?? ""}
                        placeholder="global"
                        onChange={(e) => updateRepo({ ...repo, cost_per_interaction: e.target.value ? Number(e.target.value) : null })}
                      />
                    </div>
                  </div>
                </div>
              </details>

              {/* ── Regras e ambiente ── */}
              <details className="config-section">
                <summary>Regras e ambiente</summary>
                <div className="form-stack">
                  <div className="form-field">
                    <label className="form-label">Regra de banco de dados <HelpTip>Instrução sobre qual banco usar nos testes. Ex: "PostgreSQL 15 local (host: localhost, porta: 5432, banco: test, user: test, senha: test)". Config global: AUTOIA_DB_RULE.</HelpTip></label>
                    <textarea rows={2}
                      value={repo.db_rule ?? ""}
                      placeholder="global (PostgreSQL padrão)"
                      onChange={(e) => updateRepo({ ...repo, db_rule: e.target.value || null })}
                    />
                  </div>
                  <div className="form-field">
                    <label className="form-label">Padrões de risco extras (JSON array) <HelpTip>Comandos adicionais bloqueados pelo guardrail. Ex: ["rm -rf /var", "DROP DATABASE"]. São somados aos padrões globais (AUTOIA_RISKY_PATTERNS).</HelpTip></label>
                    <textarea rows={2}
                      value={repo.risky_patterns_extra ?? ""}
                      placeholder='ex: ["rm -rf /var", "DROP DATABASE"]'
                      onChange={(e) => updateRepo({ ...repo, risky_patterns_extra: e.target.value || null })}
                    />
                  </div>
                </div>
              </details>

              {/* ── Skills ── */}
              <details className="config-section">
                <summary>Skills</summary>
                <div className="form-stack">
                  <ProjectSkills repoId={repoId} isAdmin={canManageSkills} />
                </div>
              </details>
            </div>

            <div className="form-actions" style={{ alignItems: "center", marginTop: 12 }}>
              <button
                onClick={saveSettings}
                disabled={!dirty || saving || !formValid}
                className={saved ? "btn-save-ok" : saveError ? "btn-save-err" : undefined}
              >
                {saving ? (
                  <><span className="spinner" /> Salvando…</>
                ) : saved ? (
                  "✓ Salvo"
                ) : saveError ? (
                  "✕ Falha ao salvar"
                ) : (
                  "Salvar"
                )}
              </button>
              {saveError && (
                <span className="muted small" style={{ color: "var(--err)" }}>{saveError}</span>
              )}
            </div>
          </div>
        </details>
      )}

      {/* Tarefas finalizadas */}
      {finalizadas.length > 0 && (
        <>
          <h3 className="resumo-section">Finalizadas</h3>
          {finalizadas.map((task) => (
            <Link
              to={`/${repoId}/tasks/${task.id}`}
              className="resumo-card muted"
              key={task.id}
            >
              <div className="resumo-line">
                <span className="resumo-title">
                  #{task.id} {task.title}
                </span>
                <StatusBadge status={task.status} />
              </div>
              <PhaseStepper task={task} showLabels />
              <div className="resumo-line small">
                {task.status === "done" ? (
                  `concluída · ${fmtCost(task.cost_spent)}`
                ) : (
                  <span className="resumo-error" title={task.error ?? undefined}>
                    {task.error || "sem detalhes"}
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

/* ── Card de tarefa expandido com etapas ── */

function TaskCard({
  task,
  repoId,
  busy,
  onReview,
  onBounceback,
}: {
  task: TaskListItem;
  repoId: number;
  busy: number | null;
  onReview: (t: TaskListItem, action: "approve" | "cancel") => void;
  onBounceback: (t: TaskListItem) => void;
}) {
  const needsReview = task.status === "needs_review";
  const isBlocked = task.status === "blocked";
  const isRunning = task.steps.some((s) => s.status === "running");
  const hasGuardrail = task.steps.some((s) => s.status === "guardrail_blocked");
  const steps = [...task.steps].sort((a, b) => a.position - b.position);

  const cardClass = [
    "resumo-card",
    "task-expanded",
    needsReview || isBlocked ? "resumo-card-review" : "",
    hasGuardrail && !needsReview && !isBlocked ? "resumo-card-guardrail" : "",
    isRunning ? "task-expanded-running" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cardClass}>
      {/* Cabeçalho */}
      <div className="resumo-line">
        <Link to={`/${repoId}/tasks/${task.id}`} className="resumo-title">
          {(hasGuardrail || isBlocked) && (
            <span className="project-task-alert">⚠ </span>
          )}
          {isRunning && <span className="running-dot" />}
          #{task.id} {task.title}
        </Link>
        <StatusBadge status={task.status} />
      </div>

      {/* Pipeline compacto */}
      <PhaseStepper task={task} showLabels />

      {/* Grid de etapas com mini-relatórios */}
      <div className="stage-grid">
        {steps.map((step) => (
          <StageSummary key={step.id} step={step} />
        ))}
      </div>

      {/* Rodapé: custo + diff */}
      <div className="task-card-foot">
        <span className="muted small">
          {fmtBudget(task.cost_spent, task.budget_limit)}
        </span>
        {steps.find((s) => s.diff_stat)?.diff_stat && (
          <span className="diff-summary">
            {diffSummary(steps.find((s) => s.diff_stat)!.diff_stat)}
          </span>
        )}
        <Link to={`/${repoId}/tasks/${task.id}`} className="link-btn">
          ver detalhes →
        </Link>
      </div>

      {/* Alertas */}
      {isBlocked && (
        <div className="guardrail-inline-warn">
          ⚠ Tarefa bloqueada: {task.error || "sem detalhes"}
        </div>
      )}
      {hasGuardrail && !needsReview && !isBlocked && (
        <div className="guardrail-inline-warn">
          ⛔ Guardrail bloqueou execução — veja os detalhes
        </div>
      )}
      {needsReview && (
        <div className="review-box">
          <div className="review-title">⚠ Aguardando revisão humana</div>
          <pre className="review-error" title={task.error ?? undefined}>
            {task.error || "sem detalhes"}
          </pre>
          <div className="review-actions">
            <button onClick={() => onReview(task, "approve")} disabled={busy === task.id}>
              aprovar e continuar
            </button>
            <button
              onClick={() => onBounceback(task)}
              disabled={busy === task.id}
            >
              retornar ao dev
            </button>
            <button
              className="danger"
              onClick={() => onReview(task, "cancel")}
              disabled={busy === task.id}
            >
              cancelar tarefa
            </button>
            <Link to={`/${repoId}/tasks/${task.id}`} className="link-btn">
              ver detalhes →
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Mini-card de etapa ── */

function StageSummary({ step }: { step: TaskStepListItem }) {
  const state =
    step.status === "done"
      ? "done"
      : step.status === "running"
        ? "running"
        : step.status === "failed" || step.status === "guardrail_blocked"
          ? "failed"
          : "pending";

  const summaryText = step.summary_preview
    ? extractSummary(step.summary_preview)
    : state === "running"
      ? "Em execução…"
      : state === "pending"
        ? "Aguardando…"
        : step.error
          ? `Erro: ${step.error.slice(0, 120)}`
          : "—";

  return (
    <div className={`stage-card stage-${state}`}>
      <div className="stage-head">
        <span className="stage-pos">{step.position}</span>
        <span className="stage-robot">{step.robot?.name ?? `Fase ${step.position}`}</span>
        <StatusBadge status={step.status} />
        {step.verdict && <span className="stage-verdict">{step.verdict}</span>}
        {step.verdict && (
          <HelpTip>
            {step.verdict === "READY" && "QA aprovou a história — está clara e implementável."}
            {step.verdict === "PASS" && "Tester/avaliador aprovou — código atende os critérios."}
            {step.verdict === "FAIL" && "Fase reprovada — o trabalho volta para correção."}
            {step.verdict === "NEEDS_WORK" && "Precisa de ajustes antes de prosseguir."}
            {!["READY", "PASS", "FAIL", "NEEDS_WORK"].includes(step.verdict) && `Veredicto: ${step.verdict}`}
          </HelpTip>
        )}
        {step.attempt > 1 && (
          <span className="muted small">tentativa {step.attempt}</span>
        )}
        {step.attempt > 1 && (
          <HelpTip>
            Esta fase foi re-executada {step.attempt - 1} vez(es). O limite de tentativas é
            configurado em <strong>Configurações do projeto → Max tentativas</strong>.
          </HelpTip>
        )}
      </div>
      <div className="stage-body">{summaryText}</div>
      {step.diff_stat && (
        <div className="stage-diff">{diffSummary(step.diff_stat)}</div>
      )}
    </div>
  );
}

/* ── Helpers ── */

/** Extrai as primeiras ~150 letras do summary, até um limite razoável. */
function extractSummary(text: string): string {
  // tenta pegar o primeiro parágrafo significativo
  const cleaned = text.replace(/^#+\s*.*$/gm, "").trim(); // remove headings
  const limit = 180;
  if (cleaned.length <= limit) return cleaned;
  // corta na palavra mais próxima
  const cut = cleaned.slice(0, limit);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 100 ? cut.slice(0, lastSpace) : cut) + "…";
}
