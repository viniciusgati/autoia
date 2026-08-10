import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import HelpTip from "../components/HelpTip";
import type { Pipeline, Robot } from "../types";

interface Props {
  repoId?: number;
}

export default function Pipelines({ repoId }: Props) {
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [robots, setRobots] = useState<Robot[]>([]);
  const [name, setName] = useState("");
  const [rows, setRows] = useState<{ position: number; robot_id: string; post_merge: boolean; pause_before: boolean }[]>([
    { position: 0, robot_id: "", post_merge: false, pause_before: false },
    { position: 1, robot_id: "", post_merge: false, pause_before: false },
    { position: 2, robot_id: "", post_merge: false, pause_before: false },
  ]);
  const [error, setError] = useState("");

  const load = () =>
    Promise.all([api.listPipelines(repoId), api.listRobots(repoId)])
      .then(([p, r]) => {
        setPipelines(p);
        setRobots(r);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, [repoId]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const steps = rows
      .filter((row) => row.robot_id !== "")
      .map((row) => ({
        position: row.position,
        robot_id: Number(row.robot_id),
        post_merge: row.post_merge,
        pause_before: row.pause_before,
      }));
    if (steps.length === 0) {
      setError("adicione pelo menos uma fase");
      return;
    }
    try {
      await api.createPipeline({ name, steps, repository_id: repoId ?? null });
      setName("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const projeto = pipelines.filter((p) => p.repository_id === repoId);
  const globais = pipelines.filter((p) => p.repository_id == null);

  const renderList = (list: Pipeline[], showGlobal: boolean) =>
    list.map((pipeline) => (
      <div className="card" key={pipeline.id}>
        <div className="card-title">
          <strong>{pipeline.name}</strong>
          {showGlobal && <span className="badge badge-muted">global</span>}
          <span className="muted">fases: {pipeline.steps.length}</span>
        </div>
        <ol>
          {pipeline.steps.map((step) => (
            <li key={step.id}>
              {step.position} → {step.robot?.name ?? `robô ${step.robot_id}`}
              {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
              {step.pause_before && <span className="badge badge-warn">aprovação humana</span>}
            </li>
          ))}
        </ol>
      </div>
    ));

  return (
    <div>
      <h2>{repoId != null ? `Pipelines do projeto` : "Pipelines"}</h2>
      <form className="form-stack" onSubmit={submit}>
        <div className="form-field">
          <label className="form-label">Nome do pipeline</label>
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </div>
        {rows.map((row, index) => (
          <div className="form-inline" key={index}>
            <div className="form-field">
              <label className="form-label">Posição</label>
              <input
                type="number"
                className="short"
                value={row.position}
                onChange={(e) =>
                  setRows((rows) => rows.map((r, i) => (i === index ? { ...r, position: Number(e.target.value) } : r)))
                }
              />
            </div>
            <div className="form-field" style={{flex: 1, minWidth: 160}}>
              <label className="form-label">Robô</label>
              <select
                value={row.robot_id}
                onChange={(e) => setRows((rows) => rows.map((r, i) => (i === index ? { ...r, robot_id: e.target.value } : r)))}
              >
                <option value="">— selecione —</option>
                {robots.map((robot) => (
                  <option key={robot.id} value={robot.id}>
                    {robot.name}
                    {robot.repository_id == null ? " (global)" : ""}
                  </option>
                ))}
              </select>
            </div>
            <label className="post-merge-label" style={{alignSelf: "flex-end", marginBottom: 2}}>
              <input
                type="checkbox"
                checked={row.post_merge}
                onChange={(e) =>
                  setRows((rows) => rows.map((r, i) => (i === index ? { ...r, post_merge: e.target.checked } : r)))
                }
              />
              pós-merge
            </label>
            <label className="post-merge-label" style={{alignSelf: "flex-end", marginBottom: 2}}>
              <input
                type="checkbox"
                checked={row.pause_before}
                onChange={(e) =>
                  setRows((rows) => rows.map((r, i) => (i === index ? { ...r, pause_before: e.target.checked } : r)))
                }
              />
              pausar antes
              <HelpTip>
                Antes de executar esta fase, o pipeline para e um humano precisa
                revisar o trabalho das fases anteriores e aprovar (ou editar a
                história e voltar fases). Útil antes do desenvolvimento, do merge
                ou do teste pós-deploy.
              </HelpTip>
            </label>
            <button
              type="button"
              className="danger"
              style={{alignSelf: "flex-end", marginBottom: 2}}
              onClick={() => setRows((rows) => rows.filter((_, i) => i !== index))}
            >
              remover
            </button>
          </div>
        ))}
        <div className="form-actions">
          <button
            type="button"
            onClick={() =>
              setRows((rows) => [
                ...rows,
                { position: rows.length, robot_id: "", post_merge: false, pause_before: false },
              ])
            }
          >
            + fase
          </button>
          <button type="submit">
            {repoId != null ? "criar pipeline do projeto" : "criar pipeline global"}
          </button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}

      {repoId != null && (
        <>
          <h3>Pipelines do projeto</h3>
          {projeto.length > 0 ? (
            renderList(projeto, false)
          ) : (
            <p className="muted">Nenhum pipeline próprio deste projeto ainda.</p>
          )}
          <h3>Pipelines globais</h3>
          {renderList(globais, true)}
        </>
      )}
      {repoId == null && renderList(pipelines, false)}
    </div>
  );
}
