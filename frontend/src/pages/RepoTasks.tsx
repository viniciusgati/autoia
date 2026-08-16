import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { TaskCardGrid } from "../components/TaskCard";
import { usePolling } from "../lib/polling";
import type { Epic, Pipeline, Project, TaskListItem } from "../types";

export default function RepoTasks() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);
  const navigate = useNavigate();

  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [pipelineId, setPipelineId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("issue");
  const [executor, setExecutor] = useState("kimi");
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [creating, setCreating] = useState(false);
  // Import de descrição a partir de arquivo (txt/md): erro inline abaixo do
  // campo de arquivo e input desabilitado com indicador durante a requisição.
  const [fileError, setFileError] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  // Associação organizacional Projeto > Épico (0..1, opcional — metadados).
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectSel, setProjectSel] = useState("");
  const [epics, setEpics] = useState<Epic[]>([]);
  const [epicsLoading, setEpicsLoading] = useState(false);
  const [epicSel, setEpicSel] = useState("");

  const load = (signal?: AbortSignal) =>
    Promise.all([api.listTasks(repoId, signal), api.listPipelines(repoId, signal)])
      .then(([t, p]) => {
        setTasks(t);
        setPipelines(p);
        setError("");
      })
      .catch((e) => {
        if (!signal?.aborted) setError(String(e));
      });

  // Poll leve (lista): atualiza a cada 15s; página é mais formulário + grid.
  usePolling(load, 15000, [repoId]);

  // Projetos do repositório (para a associação da nova tarefa).
  useEffect(() => {
    let active = true;
    setProjectsLoading(true);
    api
      .listProjects(repoId)
      .then((list) => {
        if (active) setProjects(list);
      })
      .catch(() => {
        if (active) setProjects([]);
      })
      .finally(() => {
        if (active) setProjectsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [repoId]);

  // Épicos do projeto selecionado (dependência: resetado ao trocar de projeto).
  useEffect(() => {
    setEpics([]);
    setEpicSel("");
    if (projectSel === "") {
      setEpicsLoading(false);
      return;
    }
    let active = true;
    setEpicsLoading(true);
    api
      .listEpics(Number(projectSel))
      .then((list) => {
        if (active) setEpics(list);
      })
      .catch(() => {
        if (active) setEpics([]);
      })
      .finally(() => {
        if (active) setEpicsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projectSel]);

  const onFileSelected = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ""; // permite re-selecionar o mesmo arquivo depois
    if (!file) return;
    setFileLoading(true);
    setFileError("");
    try {
      const { description: imported } = await api.importDescription(file);
      setDescription(imported); // substitui o texto atual do campo Descrição
    } catch (e) {
      // Campo Descrição permanece inalterado; erro inline abaixo do campo de arquivo.
      setFileError(String(e));
    } finally {
      setFileLoading(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setFormError("");
    setCreating(true);
    try {
      const created = await api.createTask({
        repository_id: repoId,
        pipeline_id: Number(pipelineId),
        title,
        description,
        kind,
        executor,
        project_id: projectSel === "" ? null : Number(projectSel),
        epic_id: epicSel === "" ? null : Number(epicSel),
      });
      // Sucesso: vai para o detalhe da tarefa criada, com a associação no resumo.
      navigate(`/${repoId}/tasks/${created.id}`);
    } catch (e) {
      // Erro 400/404 da API (associação inválida): mensagem inline, valores mantidos.
      setFormError(String(e));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div>
      <h2>Tarefas do projeto</h2>

      <form className="form-stack" onSubmit={submit}>
        <div className="form-inline">
          <div className="form-field" style={{flex: 1, minWidth: 160}}>
            <label className="form-label">Pipeline</label>
            <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)} required>
              <option value="">— selecione —</option>
              {pipelines.map((pipeline) => (
                <option key={pipeline.id} value={pipeline.id}>
                  {pipeline.name}
                  {pipeline.repository_id == null ? " (global)" : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label className="form-label">Tipo</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="issue">issue</option>
              <option value="bug">bug</option>
              <option value="feature">feature</option>
              <option value="chore">chore</option>
            </select>
          </div>
          <div className="form-field">
            <label className="form-label">Executor</label>
            <select value={executor} onChange={(e) => setExecutor(e.target.value)}>
              <option value="kimi">kimi code</option>
              <option value="opencode">opencode</option>
            </select>
          </div>
        </div>
        <div className="form-inline">
          <div className="form-field">
            <label className="form-label">Projeto</label>
            <select
              value={projectSel}
              onChange={(e) => setProjectSel(e.target.value)}
              disabled={projectsLoading}
            >
              {projectsLoading ? (
                <option value="">Carregando…</option>
              ) : projects.length === 0 ? (
                <option value="">Nenhum projeto cadastrado</option>
              ) : (
                <>
                  <option value="">(sem projeto)</option>
                  {projects.map((p) => (
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
              value={epicSel}
              onChange={(e) => setEpicSel(e.target.value)}
              disabled={projectsLoading || projectSel === "" || epicsLoading}
            >
              {projectSel === "" ? (
                <option value="">Selecione um projeto</option>
              ) : epicsLoading ? (
                <option value="">Carregando…</option>
              ) : epics.length === 0 ? (
                <option value="">Nenhum épico deste projeto</option>
              ) : (
                <>
                  <option value="">(sem épico)</option>
                  {epics.map((ep) => (
                    <option key={ep.id} value={ep.id}>
                      {ep.name}
                    </option>
                  ))}
                </>
              )}
            </select>
          </div>
        </div>
        <div className="form-field">
          <label className="form-label">Título da tarefa</label>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
        </div>
        <div className="form-field">
          <label className="form-label">Descrição</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
          />
        </div>
        <div className="form-field">
          <label className="form-label">Carregar arquivo (txt/md)</label>
          <div className="form-inline">
            <input
              type="file"
              accept=".txt,.md,.markdown"
              onChange={onFileSelected}
              disabled={fileLoading}
            />
            {fileLoading && <span className="muted">Carregando…</span>}
          </div>
          {fileError && <p className="form-error">{fileError}</p>}
        </div>
        {formError && <p className="form-error">{formError}</p>}
        <div className="form-actions">
          <button type="submit" disabled={creating}>
            {creating ? "criando…" : "criar tarefa"}
          </button>
        </div>
      </form>

      {error && <p className="error">{error}</p>}

      {tasks.length === 0 ? (
        <p className="muted">Nenhuma tarefa neste projeto.</p>
      ) : (
        <TaskCardGrid
          tasks={tasks}
          detailPath={`/${repoId}/tasks`}
          onChanged={load}
          onError={setError}
        />
      )}
    </div>
  );
}
