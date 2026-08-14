import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Robot } from "../types";

interface Props {
  repoId?: number;
}

/** Placeholders aceitos na mission, com explicação de cada um. */
const PLACEHOLDERS: { token: string; desc: string }[] = [
  { token: "{task_title}", desc: "título da tarefa em execução" },
  { token: "{task_description}", desc: "descrição completa da tarefa" },
  { token: "{step_context}", desc: "contexto da fase atual (subtarefa, instrução…)" },
  { token: "{default_branch}", desc: "branch default do repositório" },
];

/** Insere um placeholder na posição do cursor do textarea. */
function insertPlaceholder(el: HTMLTextAreaElement, token: string): void {
  const start = el.selectionStart ?? el.value.length;
  const end = el.selectionEnd ?? el.value.length;
  const next = el.value.slice(0, start) + token + el.value.slice(end);
  el.value = next;
  el.focus();
  const pos = start + token.length;
  el.setSelectionRange(pos, pos);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

export default function Robots({ repoId }: Props) {
  const [robots, setRobots] = useState<Robot[]>([]);
  const [archived, setArchived] = useState<Robot[]>([]);
  const [name, setName] = useState("");
  const [mission, setMission] = useState("");
  const [error, setError] = useState("");

  // Modal de detalhes/edição
  const [editing, setEditing] = useState<Robot | null>(null);
  const [editMission, setEditMission] = useState("");
  const [editModel, setEditModel] = useState("");
  const [editActive, setEditActive] = useState(true);
  const [saveBusy, setSaveBusy] = useState(false);

  const load = () => {
    api
      .listRobots(repoId)
      .then(setRobots)
      .catch((e) => setError(String(e)));
    api
      .listRobots(repoId, true)
      .then(setArchived)
      .catch(() => {});
  };
  useEffect(() => {
    load();
  }, [repoId]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await api.createRobot({ name, mission, repository_id: repoId ?? null });
      setName("");
      setMission("");
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const openModal = (robot: Robot) => {
    setEditing(robot);
    setEditMission(robot.mission);
    setEditModel(robot.model ?? "");
    setEditActive(robot.active);
    setError("");
  };

  const saveEdit = async () => {
    if (!editing) return;
    setSaveBusy(true);
    try {
      await api.updateRobot(editing.id, {
        mission: editMission,
        model: editModel || null,
        active: editActive,
      });
      setEditing(null);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaveBusy(false);
    }
  };

  const setArchivedFlag = async (robot: Robot, value: boolean) => {
    try {
      await api.updateRobot(robot.id, { archived: value });
      setEditing(null);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const deleteRobot = async (robot: Robot) => {
    if (!confirm(`Excluir definitivamente o robô "${robot.name}"?`)) return;
    try {
      await api.deleteRobot(robot.id);
      setEditing(null);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const projeto = robots.filter((r) => r.repository_id === repoId);
  const globais = robots.filter((r) => r.repository_id == null);
  const archivedProjeto = archived.filter((r) => r.repository_id === repoId);
  const archivedGlobais = archived.filter((r) => r.repository_id == null);

  const renderList = (list: Robot[]) =>
    list.map((robot) => (
      <button
        key={robot.id}
        className="robot-row"
        onClick={() => openModal(robot)}
        title="ver detalhes / editar"
      >
        <span className="robot-row-name">{robot.name}</span>
        <span className="robot-row-role">{robot.role}</span>
        {robot.repository_id == null && <span className="badge badge-muted">global</span>}
        {robot.active ? (
          <span className="badge badge-ok">ativo</span>
        ) : (
          <span className="badge badge-muted">inativo</span>
        )}
      </button>
    ));

  const renderArchivedList = (list: Robot[]) =>
    list.map((robot) => (
      <div key={robot.id} className="robot-row robot-row-archived">
        <span className="robot-row-name">{robot.name}</span>
        <span className="robot-row-role">{robot.role}</span>
        {robot.repository_id == null && <span className="badge badge-muted">global</span>}
        <span className="badge badge-muted">arquivado</span>
        <div style={{ flex: 1 }} />
        <button className="link-btn" onClick={() => void setArchivedFlag(robot, false)}>
          restaurar
        </button>
      </div>
    ));

  const placeholderHelp = (
    <div className="robot-placeholders">
      {PLACEHOLDERS.map((p) => (
        <button
          key={p.token}
          type="button"
          className="robot-placeholder"
          title={p.desc}
          onClick={(e) => {
            const ta = (e.currentTarget.closest(".form-field")?.querySelector("textarea")) as HTMLTextAreaElement | null;
            if (ta) insertPlaceholder(ta, p.token);
          }}
        >
          <code>{p.token}</code>
        </button>
      ))}
    </div>
  );

  return (
    <div>
      <h2>{repoId != null ? `Robôs do projeto` : "Robôs"}</h2>
      <p className="muted">
        Clique em um robô para ver os detalhes e editar a mission. Missões aceitam os
        placeholders <code>{"{task_title}"}</code>, <code>{"{task_description}"}</code> e{" "}
        <code>{"{step_context}"}</code> — clique neles no editor para inserir.
      </p>
      <form className="form-stack" onSubmit={submit}>
        <div className="form-inline" style={{ alignItems: "flex-end", gap: 8 }}>
          <div className="form-field" style={{ flex: 1 }}>
            <label className="form-label">Nome do robô</label>
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <button type="submit">
            {repoId != null ? "criar robô do projeto" : "criar robô global"}
          </button>
        </div>
        <div className="form-field">
          <label className="form-label">Missão / prompt</label>
          <textarea
            value={mission}
            onChange={(e) => setMission(e.target.value)}
            rows={4}
            required
            placeholder="Descreva a missão do robô… (use os placeholders abaixo)"
          />
          {placeholderHelp}
        </div>
      </form>
      {error && <p className="error">{error}</p>}

      {repoId != null && (
        <>
          <h3>Robôs do projeto</h3>
          {projeto.length > 0 ? (
            <div className="robot-list">{renderList(projeto)}</div>
          ) : (
            <p className="muted">Nenhum robô próprio deste projeto ainda.</p>
          )}
          <h3>Robôs globais</h3>
          <div className="robot-list">{renderList(globais)}</div>
          {archivedProjeto.length > 0 && (
            <>
              <h3>Arquivados deste projeto</h3>
              <div className="robot-list">{renderArchivedList(archivedProjeto)}</div>
            </>
          )}
          {archivedGlobais.length > 0 && (
            <>
              <h3>Arquivados globais</h3>
              <div className="robot-list">{renderArchivedList(archivedGlobais)}</div>
            </>
          )}
        </>
      )}
      {repoId == null && (
        <>
          <div className="robot-list">{renderList(robots)}</div>
          {archivedGlobais.length > 0 && (
            <>
              <h3>Arquivados</h3>
              <div className="robot-list">{renderArchivedList(archivedGlobais)}</div>
            </>
          )}
        </>
      )}

      {/* Modal de detalhes/edição */}
      {editing && (
        <div className="modal-overlay" onClick={() => setEditing(null)}>
          <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <strong>{editing.name}</strong>
              <span className="robot-row-role">{editing.role}</span>
              {editing.repository_id == null && (
                <span className="badge badge-muted">global</span>
              )}
              {editing.archived && <span className="badge badge-muted">arquivado</span>}
              <button className="panel-close" onClick={() => setEditing(null)}>
                ✕
              </button>
            </div>
            <div className="modal-body">
              <div className="form-field">
                <label className="form-label">Missão / prompt</label>
                <textarea
                  value={editMission}
                  onChange={(e) => setEditMission(e.target.value)}
                  rows={16}
                  className="robot-mission-editor"
                />
                {placeholderHelp}
              </div>
              <div className="form-inline" style={{ gap: 12, alignItems: "flex-end" }}>
                <div className="form-field" style={{ flex: 1 }}>
                  <label className="form-label">Modelo (opcional)</label>
                  <input
                    value={editModel}
                    onChange={(e) => setEditModel(e.target.value)}
                    placeholder="ex.: claude-sonnet-4"
                  />
                </div>
                <label className="post-merge-label" style={{ paddingBottom: 8 }}>
                  <input
                    type="checkbox"
                    checked={editActive}
                    onChange={(e) => setEditActive(e.target.checked)}
                  />
                  robô ativo
                </label>
              </div>
            </div>
            <div className="modal-foot">
              {editing.repository_id != null && !editing.archived && (
                <>
                  <button className="link-btn" onClick={() => void setArchivedFlag(editing, true)}>
                    arquivar
                  </button>
                  <button className="danger" onClick={() => deleteRobot(editing)}>
                    excluir
                  </button>
                </>
              )}
              {editing.archived && (
                <>
                  <button className="link-btn" onClick={() => void setArchivedFlag(editing, false)}>
                    restaurar
                  </button>
                  <button className="danger" onClick={() => deleteRobot(editing)}>
                    excluir
                  </button>
                </>
              )}
              <div style={{ flex: 1 }} />
              <button onClick={saveEdit} disabled={saveBusy}>
                {saveBusy ? "salvando…" : "salvar"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
