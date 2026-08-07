import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import type { Pipeline, Repository, Task } from "../types";

export default function Tasks() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [repositoryId, setRepositoryId] = useState("");
  const [pipelineId, setPipelineId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("issue");
  const [error, setError] = useState("");

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
      <h2>Tarefas</h2>
      <form className="form-stack" onSubmit={submit}>
        <div className="form-inline">
          <select value={repositoryId} onChange={(e) => setRepositoryId(e.target.value)} required>
            <option value="">— repositório —</option>
            {repos.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repo.name}
              </option>
            ))}
          </select>
          <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)} required>
            <option value="">— pipeline —</option>
            {pipelines.map((pipeline) => (
              <option key={pipeline.id} value={pipeline.id}>
                {pipeline.name}
              </option>
            ))}
          </select>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="issue">issue</option>
            <option value="bug">bug</option>
            <option value="feature">feature</option>
            <option value="chore">chore</option>
          </select>
        </div>
        <input placeholder="título da tarefa" value={title} onChange={(e) => setTitle(e.target.value)} required />
        <textarea placeholder="descrição…" value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />
        <button>criar tarefa</button>
      </form>
      {error && <p className="error">{error}</p>}

      {tasks.length === 0 ? (
        <p className="muted">Nenhuma tarefa.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Título</th>
              <th>Repo</th>
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
                  <Link to={`/tasks/${task.id}`}>{task.title}</Link>
                </td>
                <td>{task.repository_id}</td>
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
