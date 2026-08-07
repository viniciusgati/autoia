import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Pipeline, Robot } from "../types";

export default function Pipelines() {
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [robots, setRobots] = useState<Robot[]>([]);
  const [name, setName] = useState("");
  const [rows, setRows] = useState<{ position: number; robot_id: string; post_merge: boolean }[]>([
    { position: 0, robot_id: "", post_merge: false },
    { position: 1, robot_id: "", post_merge: false },
    { position: 2, robot_id: "", post_merge: false },
  ]);
  const [error, setError] = useState("");

  const load = () =>
    Promise.all([api.listPipelines(), api.listRobots()])
      .then(([p, r]) => {
        setPipelines(p);
        setRobots(r);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const steps = rows
      .filter((row) => row.robot_id !== "")
      .map((row) => ({
        position: row.position,
        robot_id: Number(row.robot_id),
        post_merge: row.post_merge,
      }));
    if (steps.length === 0) {
      setError("adicione pelo menos uma fase");
      return;
    }
    try {
      await api.createPipeline({ name, steps });
      setName("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h2>Pipelines</h2>
      <form className="form-stack" onSubmit={submit}>
        <input placeholder="nome do pipeline" value={name} onChange={(e) => setName(e.target.value)} required />
        {rows.map((row, index) => (
          <div className="form-inline" key={index}>
            <input
              type="number"
              className="short"
              value={row.position}
              onChange={(e) =>
                setRows((rows) => rows.map((r, i) => (i === index ? { ...r, position: Number(e.target.value) } : r)))
              }
            />
            <select
              value={row.robot_id}
              onChange={(e) => setRows((rows) => rows.map((r, i) => (i === index ? { ...r, robot_id: e.target.value } : r)))}
            >
              <option value="">— robô —</option>
              {robots.map((robot) => (
                <option key={robot.id} value={robot.id}>
                  {robot.name}
                </option>
              ))}
            </select>
            <label className="post-merge-label">
              <input
                type="checkbox"
                checked={row.post_merge}
                onChange={(e) =>
                  setRows((rows) => rows.map((r, i) => (i === index ? { ...r, post_merge: e.target.checked } : r)))
                }
              />
              pós-merge
            </label>
            <button
              type="button"
              className="danger"
              onClick={() => setRows((rows) => rows.filter((_, i) => i !== index))}
            >
              x
            </button>
          </div>
        ))}
        <div className="form-inline">
          <button
            type="button"
            onClick={() =>
              setRows((rows) => [
                ...rows,
                { position: rows.length, robot_id: "", post_merge: false },
              ])
            }
          >
            + fase
          </button>
          <button type="submit">criar pipeline</button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}

      {pipelines.map((pipeline) => (
        <div className="card" key={pipeline.id}>
          <div className="card-title">
            <strong>{pipeline.name}</strong>
            <span className="muted">fases: {pipeline.steps.length}</span>
          </div>
          <ol>
            {pipeline.steps.map((step) => (
              <li key={step.id}>
                {step.position} → {step.robot?.name ?? `robô ${step.robot_id}`}
                {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}
