import { FormEvent, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { TaskCardGrid } from "../components/TaskCard";
import { usePolling } from "../lib/polling";
import type { Pipeline, TaskListItem } from "../types";

export default function RepoTasks() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [pipelineId, setPipelineId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("issue");
  const [executor, setExecutor] = useState("kimi");
  const [error, setError] = useState("");

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

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createTask({
        repository_id: repoId,
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
        <div className="form-actions">
          <button type="submit">criar tarefa</button>
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
