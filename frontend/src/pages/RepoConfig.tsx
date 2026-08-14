import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import HelpTip from "../components/HelpTip";
import ProjectSkills from "../components/ProjectSkills";
import type { Pipeline, Repository, RepositoryMember } from "../types";

/** Campos editáveis (usados no badge "alterações não salvas"). */
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
  "sandbox",
  "task_targets",
  "external_context",
] as const;

function apiErrorMsg(e: unknown): string {
  return String(e).replace(/^\d+: /, "");
}

function repoDirty(repo: Repository, base: Repository): boolean {
  return SETTINGS_FIELDS.some((f) => repo[f] !== base[f]);
}

export default function RepoConfig() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const { user, authEnabled } = useAuth();
  const [repo, setRepo] = useState<Repository | null>(null);
  const [baseRepo, setBaseRepo] = useState<Repository | null>(null);
  const [members, setMembers] = useState<RepositoryMember[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [allRepos, setAllRepos] = useState<Repository[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState("");
  const savedTimer = useRef<number | null>(null);

  useEffect(() => {
    api.listRepositories().then((repos) => {
      const r = repos.find((x) => x.id === repoId) ?? null;
      setRepo(r);
      setBaseRepo(r);
      setAllRepos(repos);
    }).catch((e) => setError(String(e)));
    api.listPipelines(repoId).then(setPipelines).catch(() => {});
    api.listMembers(repoId).then(setMembers).catch(() => {});
  }, [repoId]);

  const updateRepo = (next: Repository) => {
    setRepo(next);
    if (saveError) setSaveError("");
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
        sandbox: repo.sandbox,
        task_targets: repo.task_targets,
        external_context: repo.external_context,
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

  const nameValid = (repo?.name ?? "").trim().length > 0;
  const attemptsValid = repo == null || repo.max_attempts == null || repo.max_attempts >= 1;
  const timeoutValid = repo == null || repo.run_timeout == null || repo.run_timeout > 0;
  const budgetValid = repo == null || repo.task_budget == null || repo.task_budget >= 0;
  const formValid = nameValid && attemptsValid && timeoutValid && budgetValid;
  const dirty = repo != null && baseRepo != null && repoDirty(repo, baseRepo);

  // Só admin global ou admin do projeto altera a configuração (auth OFF → libera).
  const isRepoAdmin =
    user != null && members.some((m) => m.role === "admin" && m.user_id === user.id);
  const canManage = !authEnabled || user == null || user.role === "admin" || isRepoAdmin;

  if (!repo) return <p className="muted">carregando…</p>;

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Configuração do projeto</h2>
        <span className="muted">
          <Link to={`/${repoId}`} className="link-btn">← dashboard</Link>
        </span>
      </div>

      {!canManage && (
        <div className="sticky-alert">
          <span>🔒 Apenas admin do projeto ou admin global pode alterar a configuração.</span>
        </div>
      )}

      <div className="card" style={{ marginTop: 12 }}>
        <div className="config-sections">
          {/* ── Geral ── */}
          <details className="config-section" open>
            <summary>Geral</summary>
            <div className="form-stack">
              <div className={`form-field ${nameValid ? "" : "form-field-invalid"}`}>
                <label className="form-label">Nome do projeto <HelpTip>Nome exibido nas listas e dashboards. Obrigatório.</HelpTip></label>
                <input type="text"
                  value={repo.name}
                  disabled={!canManage}
                  onChange={(e) => updateRepo({ ...repo, name: e.target.value })}
                />
                {!nameValid && <div className="form-error">O nome do projeto é obrigatório</div>}
              </div>
              <div className="form-field">
                <label className="form-label">Pipeline padrão <HelpTip>Pipeline usado quando uma tarefa é criada sem especificar qual pipeline utilizar.</HelpTip></label>
                <select
                  value={repo.default_pipeline_id ?? ""}
                  disabled={!canManage}
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
                    disabled={!canManage}
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
                    disabled={!canManage}
                    onChange={(e) => updateRepo({ ...repo, max_attempts: e.target.value ? Number(e.target.value) : null })}
                  />
                  {!attemptsValid && <div className="form-error">Máx. tentativas deve ser ao menos 1</div>}
                </div>
                <div className="form-field" style={{ flex: 1, minWidth: 120 }}>
                  <label className="form-label">Max decisões PM <HelpTip>Limite de vezes que o robô PM pode decidir (retry/continuar/escalar) antes de travar. Config global: AUTOIA_MAX_PM_DECISIONS.</HelpTip></label>
                  <input type="number" min={0} max={10}
                    value={repo.max_pm_decisions ?? ""}
                    placeholder="global"
                    disabled={!canManage}
                    onChange={(e) => updateRepo({ ...repo, max_pm_decisions: e.target.value ? Number(e.target.value) : null })}
                  />
                </div>
                <div className={`form-field ${timeoutValid ? "" : "form-field-invalid"}`} style={{ flex: 1, minWidth: 120 }}>
                  <label className="form-label">Timeout (seg) <HelpTip>Tempo máximo que o kimi pode rodar por fase. Se estourar, a fase falha. Config global: AUTOIA_RUN_TIMEOUT.</HelpTip></label>
                  <input type="number" min={60} step={60}
                    value={repo.run_timeout ?? ""}
                    placeholder="global"
                    disabled={!canManage}
                    onChange={(e) => updateRepo({ ...repo, run_timeout: e.target.value ? Number(e.target.value) : null })}
                  />
                  {!timeoutValid && <div className="form-error">Timeout deve ser maior que zero</div>}
                </div>
              </div>
              <div className="form-inline">
                <label className="post-merge-label">
                  <input type="checkbox"
                    checked={repo.allow_external_tasks}
                    disabled={!canManage}
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
                    disabled={!canManage}
                    onChange={(e) => updateRepo({ ...repo, task_budget: e.target.value ? Number(e.target.value) : null })}
                  />
                  {!budgetValid && <div className="form-error">Orçamento não pode ser negativo</div>}
                </div>
                <div className="form-field" style={{ flex: 1, minWidth: 120 }}>
                  <label className="form-label">Custo/interação (R$) <HelpTip>Custo estimado por chamada ao kimi (tool_call + resposta). Usado para calcular gasto acumulado. Config global: AUTOIA_COST_PER_INTERACTION.</HelpTip></label>
                  <input type="number" min={0} step={0.001}
                    value={repo.cost_per_interaction ?? ""}
                    placeholder="global"
                    disabled={!canManage}
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
                  disabled={!canManage}
                  onChange={(e) => updateRepo({ ...repo, db_rule: e.target.value || null })}
                />
              </div>
              <div className="form-field">
                <label className="form-label">Padrões de risco extras (JSON array) <HelpTip>Comandos adicionais bloqueados pelo guardrail. Ex: ["rm -rf /var", "DROP DATABASE"]. São somados aos padrões globais (AUTOIA_RISKY_PATTERNS).</HelpTip></label>
                <textarea rows={2}
                  value={repo.risky_patterns_extra ?? ""}
                  placeholder='ex: ["rm -rf /var", "DROP DATABASE"]'
                  disabled={!canManage}
                  onChange={(e) => updateRepo({ ...repo, risky_patterns_extra: e.target.value || null })}
                />
              </div>
            </div>
          </details>

          {/* ── Projetos e ambiente ── */}
          <details className="config-section">
            <summary>Projetos e ambiente</summary>
            <div className="form-stack">
              <div className="form-field">
                <label className="form-label">
                  Projetos onde os robôs podem criar tarefas <HelpTip>
                  Allowlist de repositórios para os quais este projeto pode propor tasks
                  (campo "repository" no autoia_tasks.json). Vazio = restritivo: o robô
                  só propõe tasks para o PRÓPRIO projeto. Propostas cross-repo fora desta
                  lista são recusadas.
                </HelpTip></label>
                {allRepos.filter((r) => r.id !== repoId).length === 0 ? (
                  <p className="muted small">Nenhum outro projeto cadastrado.</p>
                ) : (
                  <div className="config-checkboxes">
                    {allRepos
                      .filter((r) => r.id !== repoId)
                      .map((r) => {
                        const checked = (repo.task_targets ?? []).includes(r.name);
                        return (
                          <label className="post-merge-label" key={r.id}>
                            <input
                              type="checkbox"
                              checked={checked}
                              disabled={!canManage}
                              onChange={(e) => {
                                const next = new Set(repo.task_targets ?? []);
                                if (e.target.checked) next.add(r.name);
                                else next.delete(r.name);
                                updateRepo({ ...repo, task_targets: [...next].sort() });
                              }}
                            />
                            {r.name}
                          </label>
                        );
                      })}
                  </div>
                )}
              </div>
              <div className="form-field">
                <label className="form-label">
                  Informações úteis para os robôs (DNS de deploy, URLs, env vars) <HelpTip>
                  Texto livre injetado no contexto de TODOS os robôs (prompt + AGENTS.md).
                  Ex: "Deploy de produção: https://app.exemplo.com\nAPI staging:
                  https://api-staging.exemplo.com\nVariáveis: DB_URL, API_KEY=..."
                </HelpTip></label>
                <textarea rows={4}
                  value={repo.external_context ?? ""}
                  placeholder="ex: DNS de produção: https://app.exemplo.com&#10;staging: https://staging.exemplo.com"
                  disabled={!canManage}
                  onChange={(e) => updateRepo({ ...repo, external_context: e.target.value || null })}
                />
              </div>
            </div>
          </details>

          {/* ── Sandbox de execução ── */}
          <details className="config-section">
            <summary>Sandbox de execução</summary>
            <div className="form-stack">
              <div className="form-field">
                <label className="form-label">Isolamento <HelpTip>Roda as fases dos robôs em um contêiner isolado (nada fora do checkout/estado é gravável). "off" = spawn direto (legado). "fs" = isolamento de arquivos/privilégios com rede host. "full" = isolamento + rede bridge com egress de allowlist (proxy). Requer Docker no host. Config global: AUTOIA_SANDBOX.</HelpTip></label>
                <select
                  value={repo.sandbox ?? ""}
                  disabled={!canManage}
                  onChange={(e) => updateRepo({ ...repo, sandbox: e.target.value || null })}
                >
                  <option value="">— global (padrão) —</option>
                  <option value="off">off — sem isolamento</option>
                  <option value="fs">fs — arquivos e privilégios</option>
                  <option value="full">full — + rede allowlist</option>
                </select>
              </div>
            </div>
          </details>

          {/* ── Skills ── */}
          <details className="config-section">
            <summary>Skills</summary>
            <div className="form-stack">
              <ProjectSkills repoId={repoId} isAdmin={canManage} />
            </div>
          </details>
        </div>

        {canManage && (
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
            {dirty && (
              <span className="badge badge-warn" style={{ textTransform: "lowercase" }}>
                alterações não salvas
              </span>
            )}
            {saveError && (
              <span className="muted small" style={{ color: "var(--err)" }}>{saveError}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
