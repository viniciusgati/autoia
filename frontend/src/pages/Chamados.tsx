import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import ModelSelect from "../components/ModelSelect";
import { usePolling } from "../lib/polling";
import type { Chamado, Epic, Project, Repository } from "../types";

export const chamadoStatusLabel: Record<string, string> = {
  aberto: "Aberto",
  em_andamento: "Em andamento",
  respondido: "Respondido",
  cancelado: "Cancelado",
  concluido: "Concluído",
  falhou: "Falhou",
};

export const chamadoStatusClass: Record<string, string> = {
  aberto: "badge",
  em_andamento: "badge badge-run",
  respondido: "badge badge-ok",
  cancelado: "badge badge-muted",
  concluido: "badge badge-ok",
  falhou: "badge badge-err",
};

/** Tela de chamados de UM projeto: lista (com filtro por projeto/épico/status) e
 *  criação de um novo chamado. O chamado é o fluxo de atendimento (Projeto > Épico
 *  > Chamado), paralelo às tasks. */
export default function Chamados() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [repos, setRepos] = useState<Repository[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [epics, setEpics] = useState<Epic[]>([]);
  const [chamados, setChamados] = useState<Chamado[]>([]);
  const [error, setError] = useState("");
  const [filterProject, setFilterProject] = useState<number | "">("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState("");
  const [form, setForm] = useState({
    title: "",
    description: "",
    project_id: "",
    epic_id: "",
    executor: "kimi",
    model: "",
  });

  const load = async (signal?: AbortSignal) => {
    try {
      const params: { repository_id: number; project_id?: number; status?: string } = { repository_id: repoId };
      if (filterProject !== "") params.project_id = Number(filterProject);
      if (filterStatus) params.status = filterStatus;
      setChamados(await api.listChamados(params, signal));
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    }
  };

  usePolling(load, 5000, [repoId, filterProject, filterStatus]);

  useEffect(() => {
    api.listRepositories().then(setRepos).catch(() => {});
    api.listProjects(repoId).then(setProjects).catch(() => {});
  }, [repoId]);

  useEffect(() => {
    setEpics([]);
    setForm((f) => ({ ...f, epic_id: "" }));
    if (form.project_id === "") return;
    api.listEpics(Number(form.project_id)).then(setEpics).catch(() => {});
  }, [form.project_id]);

  const repo = repos.find((r) => r.id === repoId);
  const projectName = (id: number | null) => projects.find((p) => p.id === id)?.name ?? "—";

  const create = async () => {
    setFormError("");
    if (!form.title.trim()) {
      setFormError("Informe o título do chamado.");
      return;
    }
    try {
      await api.createChamado({
        repository_id: repoId,
        title: form.title,
        description: form.description,
        project_id: form.project_id === "" ? null : Number(form.project_id),
        epic_id: form.epic_id === "" ? null : Number(form.epic_id),
        executor: form.executor,
        model: form.model || null,
      });
      setForm({ title: "", description: "", project_id: "", epic_id: "", executor: "kimi", model: "" });
      setCreating(false);
      void load();
    } catch (e) {
      setFormError(String(e));
    }
  };

  if (error) return <p className="error">{error}</p>;

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>{repo ? `Chamados · ${repo.name}` : "Chamados"}</h2>
        <span className="muted">{chamados.length} chamados</span>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}`} className="link-btn">
          ← dashboard
        </Link>
        <Link to={`/${repoId}/projects`} className="link-btn">
          Projetos / Épicos
        </Link>
        <button onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancelar" : "+ Novo chamado"}
        </button>
      </div>

      {creating && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="form-stack">
            <div className="form-field">
              <label className="form-label">Título</label>
              <input
                value={form.title}
                onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
                placeholder="Problema relatado pelo cliente…"
              />
            </div>
            <div className="form-field">
              <label className="form-label">Descrição</label>
              <textarea
                rows={3}
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                placeholder="Detalhes do chamado…"
              />
            </div>
            <div className="form-inline">
              <div className="form-field">
                <label className="form-label">Projeto</label>
                <select value={form.project_id} onChange={(e) => setForm((f) => ({ ...f, project_id: e.target.value }))}>
                  <option value="">(sem projeto)</option>
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-field">
                <label className="form-label">Épico</label>
                <select
                  value={form.epic_id}
                  onChange={(e) => setForm((f) => ({ ...f, epic_id: e.target.value }))}
                  disabled={form.project_id === ""}
                >
                  <option value="">(sem épico)</option>
                  {epics.map((ep) => (
                    <option key={ep.id} value={ep.id}>
                      {ep.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-field">
                <label className="form-label">Executor</label>
                <select
                  value={form.executor}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      executor: e.target.value,
                      model: e.target.value === "codex" ? f.model : "",
                    }))
                  }
                >
                  <option value="kimi">kimi</option>
                  <option value="opencode">opencode</option>
                  <option value="codex">codex</option>
                </select>
              </div>
              {form.executor === "codex" && (
                <div className="form-field">
                  <label className="form-label">Modelo</label>
                  <ModelSelect
                    value={form.model}
                    onChange={(model) => setForm((f) => ({ ...f, model }))}
                  />
                </div>
              )}
            </div>
            {formError && <p className="form-error">{formError}</p>}
            <div className="form-actions">
              <button onClick={() => void create()}>Criar chamado</button>
            </div>
          </div>
        </div>
      )}

      <div className="form-inline" style={{ marginTop: 12 }}>
        <div className="form-field">
          <label className="form-label">Filtrar projeto</label>
          <select value={filterProject} onChange={(e) => setFilterProject(e.target.value === "" ? "" : Number(e.target.value))}>
            <option value="">(todos)</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-field">
          <label className="form-label">Filtrar status</label>
          <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
            <option value="">(todos)</option>
            {Object.entries(chamadoStatusLabel).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>
      </div>

      {chamados.length === 0 && (
        <p className="muted" style={{ marginTop: 16 }}>
          Nenhum chamado neste projeto.
        </p>
      )}

      <div className="cards" style={{ marginTop: 12 }}>
        {chamados.map((c) => (
          <Link
            key={c.id}
            to={`/${repoId}/chamados/${c.id}`}
            className="card"
            style={{ textDecoration: "none" }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
              <span className="card-title">
                #{c.id} {c.title}
              </span>
              <span className={chamadoStatusClass[c.status] ?? "badge"}>{chamadoStatusLabel[c.status] ?? c.status}</span>
            </div>
            <div className="card-label" style={{ marginTop: 6 }}>
              Etapa: <b>{c.workflow_status || "—"}</b> · Projeto: {projectName(c.project_id)} ·{" "}
              Custo: R$ {c.cost_spent.toFixed(2)}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}
