import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import type { Pipeline, Task } from "../types";

export default function RepoTasks() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [tasks, setTasks] = useState<Task[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [pipelineId, setPipelineId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("issue");
  const [error, setError] = useState("");

  const load = () =>
    Promise.all([api.listTasks(repoId), api.listPipelines()])
      .then(([t, p]) => {
        setTasks(t);
        setPipelines(p);
        setError("");
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createTask({
        repository_id: repoId,
        pipeline_id: Number(pipelineId),
        title,
        description,
        kind,
      });
      setTitle("");
      setDescription("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const start = async (id: number) => {
    try {
      await api.startTask(id);
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
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Título</th>
              <th>Status</th>
              <th>Custo (US$)</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>{task.id}</td>
                <td>
                  <Link to={`/${repoId}/tasks/${task.id}`}>{task.title}</Link>
                </td>
                <td>
                  <StatusBadge status={task.status} />
                </td>
                <td>{task.cost_spent.toFixed(2)}</td>
                <td>
                  {task.status === "created" && (
                    <button onClick={() => start(task.id)}>iniciar</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
