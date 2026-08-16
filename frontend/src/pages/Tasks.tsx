import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { TaskCardGrid } from "../components/TaskCard";
import type { Pipeline, Repository, TaskListItem } from "../types";

export default function Tasks() {
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [repositoryId, setRepositoryId] = useState("");
  const [pipelineId, setPipelineId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("issue");
  const [executor, setExecutor] = useState("kimi");
  const [error, setError] = useState("");
  // Import de descrição a partir de arquivo (txt/md): erro inline abaixo do
  // campo de arquivo e input desabilitado com indicador durante a requisição.
  const [fileError, setFileError] = useState("");
  const [fileLoading, setFileLoading] = useState(false);

  const load = () =>
    Promise.all([api.listTasks(), api.listRepositories(), api.listPipelines()])
      .then(([t, r, p]) => {
        setTasks(t);
        setRepos(r);
        setPipelines(p);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createTask({
        repository_id: Number(repositoryId),
        pipeline_id: Number(pipelineId),
        title,
        description,
        kind,
        executor,
      });
      setTitle("");
      setDescription("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

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

  return (
    <div>
      <h2>Tarefas</h2>
      <form className="form-stack" onSubmit={submit}>
        <div className="form-inline">
          <div className="form-field" style={{flex: 1, minWidth: 160}}>
            <label className="form-label">Repositório</label>
            <select value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)} required>
              <option value="">— selecione —</option>
              {repos.map((repo) => (
                <option key={repo.id} value={repo.id}>
                  {repo.name}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field" style={{flex: 1, minWidth: 160}}>
            <label className="form-label">Pipeline</label>
            <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)} required>
              <option value="">— selecione —</option>
              {pipelines.map((pipeline) => (
                <option key={pipeline.id} value={pipeline.id}>
                  {pipeline.name}
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
        <div className="form-field">
          <label className="form-label">Título da tarefa</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} required />
        </div>
        <div className="form-field">
          <label className="form-label">Descrição</label>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />
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
          {fileError && <p className="error">{fileError}</p>}
        </div>
        <div className="form-actions">
          <button type="submit">criar tarefa</button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}

      {tasks.length === 0 ? (
        <p className="muted">Nenhuma tarefa.</p>
      ) : (
        <TaskCardGrid
          tasks={tasks}
          detailPath="/tasks"
          repoNames={Object.fromEntries(repos.map((r) => [r.id, r.name]))}
          onChanged={load}
          onError={setError}
        />
      )}
    </div>
  );
}
