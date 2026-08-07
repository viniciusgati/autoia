import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Robot } from "../types";

export default function Robots() {
  const [robots, setRobots] = useState<Robot[]>([]);
  const [name, setName] = useState("");
  const [mission, setMission] = useState("");
  const [error, setError] = useState("");

  const load = () => api.listRobots().then(setRobots).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createRobot({ name, mission });
      setName("");
      setMission("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h2>Robôs</h2>
      <p className="muted">
        Missões aceitam os placeholders <code>{"{task_title}"}</code>,{" "}
        <code>{"{task_description}"}</code> e <code>{"{step_context}"}</code>.
      </p>
      <form className="form-stack" onSubmit={submit}>
        <input placeholder="nome (developer, qa, merger…)" value={name} onChange={(e) => setName(e.target.value)} required />
        <textarea
          placeholder="missão / prompt do robô…"
          value={mission}
          onChange={(e) => setMission(e.target.value)}
          rows={4}
          required
        />
        <button>criar robô</button>
      </form>
      {error && <p className="error">{error}</p>}

      {robots.map((robot) => (
        <div className="card" key={robot.id}>
          <div className="card-title">
            <strong>{robot.name}</strong>
            {robot.active ? <span className="badge badge-ok">ativo</span> : <span className="badge badge-muted">inativo</span>}
          </div>
          <pre className="mission">{robot.mission}</pre>
        </div>
      ))}
    </div>
  );
}
