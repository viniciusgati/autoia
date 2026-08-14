import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import type { Epic, Project, Repository } from "../types";

/** Tela de Projetos / Épicos de UM repositório: CRUD dos dois níveis de
 *  organização dos chamados, com recursos LLM (resumo do projeto, escopo/resumo do
 *  épico) gerados em background. */
export default function Projects() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [epicsByProject, setEpicsByProject] = useState<Record<number, Epic[]>>({});
  const [error, setError] = useState("");
  const [newProject, setNewProject] = useState({ name: "", description: "" });
  const [newEpic, setNewEpic] = useState<Record<number, { name: string; description: string }>>({});

  const load = async () => {
    try {
      const [all, projs] = await Promise.all([
        api.listRepositories(),
        api.listProjects(repoId),
      ]);
      setRepo(all.find((r) => r.id === repoId) ?? null);
      setProjects(projs);
      const epics: Record<number, Epic[]> = {};
      await Promise.all(
        projs.map(async (p) => {
          epics[p.id] = await api.listEpics(p.id);
        }),
      );
      setEpicsByProject(epics);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    void load();
  }, [repoId]);

  const createProject = async () => {
    if (!newProject.name.trim()) return;
    try {
      await api.createProject({ repository_id: repoId, name: newProject.name, description: newProject.description });
      setNewProject({ name: "", description: "" });
      void load();
    } catch (e) {
      setError(String(e));
    }
  };

  const createEpic = async (projectId: number) => {
    const data = newEpic[projectId];
    if (!data?.name.trim()) return;
    try {
      await api.createEpic({ project_id: projectId, name: data.name, description: data.description });
      setNewEpic((m) => ({ ...m, [projectId]: { name: "", description: "" } }));
      void load();
    } catch (e) {
      setError(String(e));
    }
  };

  if (error) return <p className="error">{error}</p>;

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>{repo ? `Projetos · ${repo.name}` : "Projetos"}</h2>
        <span className="muted">{projects.length} projetos</span>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}`} className="link-btn">
          ← dashboard
        </Link>
        <Link to={`/${repoId}/chamados`} className="link-btn">
          Chamados
        </Link>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <div className="form-stack">
          <div className="form-field">
            <label className="form-label">Novo projeto</label>
            <input
              value={newProject.name}
              onChange={(e) => setNewProject((p) => ({ ...p, name: e.target.value }))}
              placeholder="Nome do projeto (ex.: Portal de clientes)…"
            />
          </div>
          <div className="form-field">
            <textarea
              rows={2}
              value={newProject.description}
              onChange={(e) => setNewProject((p) => ({ ...p, description: e.target.value }))}
              placeholder="Objetivo do projeto…"
            />
          </div>
          <div className="form-actions">
            <button onClick={() => void createProject()}>Criar projeto</button>
          </div>
        </div>
      </div>

      {projects.length === 0 && (
        <p className="muted" style={{ marginTop: 16 }}>
          Nenhum projeto cadastrado neste repositório.
        </p>
      )}

      {projects.map((p) => (
        <div key={p.id} className="card" style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
            <b>{p.name}</b>
            <span className="badge badge-muted">{p.status}</span>
          </div>
          {p.description && <div className="muted" style={{ marginTop: 4 }}>{p.description}</div>}
          {p.summary && (
            <div className="config-summary" style={{ marginTop: 8 }}>
              <div className="card-label">Resumo (LLM)</div>
              <div className="prewrap">{p.summary}</div>
            </div>
          )}
          <div className="form-actions" style={{ marginTop: 8 }}>
            <button onClick={() => api.regenerateProjectSummary(p.id).then(() => setTimeout(() => void load(), 1200))}>
              {p.summary ? "Regenerar resumo" : "Gerar resumo"}
            </button>
            <button
              className="danger-link"
              onClick={() => {
                if (window.confirm(`Excluir projeto "${p.name}"? Os chamados são desvinculados (não apagados).`)) {
                  api.deleteProject(p.id).then(() => void load()).catch((e) => setError(String(e)));
                }
              }}
            >
              excluir
            </button>
          </div>

          <div className="config-section" style={{ marginTop: 8 }}>
            <div className="card-label">Épicos</div>
            {(epicsByProject[p.id] ?? []).length === 0 && (
              <p className="muted">Nenhum épico.</p>
            )}
            {(epicsByProject[p.id] ?? []).map((ep) => (
              <div key={ep.id} style={{ borderLeft: "2px solid var(--border, #ddd)", paddingLeft: 8, marginTop: 6 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <b>{ep.name}</b>
                  <span className="badge badge-muted">{ep.status}</span>
                </div>
                {ep.description && <div className="muted">{ep.description}</div>}
                {ep.scope && (
                  <div className="prewrap" style={{ marginTop: 4, fontSize: 13 }}>{ep.scope}</div>
                )}
                {ep.summary && (
                  <div className="prewrap" style={{ marginTop: 4, fontSize: 13, opacity: 0.8 }}>{ep.summary}</div>
                )}
                <div className="form-inline" style={{ marginTop: 4 }}>
                  <button
                    className="link-btn"
                    onClick={() => api.regenerateEpicScope(ep.id).then(() => setTimeout(() => void load(), 1200))}
                  >
                    {ep.scope ? "regenerar escopo" : "gerar escopo"}
                  </button>
                  <button
                    className="link-btn"
                    onClick={() => api.regenerateEpicSummary(ep.id).then(() => setTimeout(() => void load(), 1200))}
                  >
                    {ep.summary ? "regenerar resumo" : "gerar resumo"}
                  </button>
                  <button
                    className="danger-link"
                    onClick={() => {
                      if (window.confirm(`Excluir épico "${ep.name}"?`)) {
                        api.deleteEpic(ep.id).then(() => void load()).catch((e) => setError(String(e)));
                      }
                    }}
                  >
                    excluir
                  </button>
                </div>
              </div>
            ))}
            <div className="form-inline" style={{ marginTop: 6 }}>
              <input
                style={{ flex: 1 }}
                value={newEpic[p.id]?.name ?? ""}
                onChange={(e) => setNewEpic((m) => ({ ...m, [p.id]: { name: e.target.value, description: m[p.id]?.description ?? "" } }))}
                placeholder="Nome do épico…"
              />
              <button onClick={() => void createEpic(p.id)}>+ Épico</button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
