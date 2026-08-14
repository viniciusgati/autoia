import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import HelpTip from "./HelpTip";
import { MSG_SEM_PERMISSAO } from "../lib/tasks";
import Markdown from "../lib/markdown";
import type { Pipeline, TaskProposal } from "../types";

/** Modal para EXPLORAR uma proposta de task filha por inteiro (título, kind,
 *  descrição completa, projeto alvo, task de origem) e decidir: aceitar ou
 *  rejeitar. Antes de aceitar, permite EDITAR título/kind/descrição — a task
 *  criada nasce com os valores editados. Quando aceita, mostra o link para a
 *  task criada.
 */
export default function ProposalModal({
  proposal,
  repoNames,
  parentRepoName,
  parentDetailPath,
  onClose,
  onChanged,
  onError,
  canAct = true,
}: {
  proposal: TaskProposal;
  repoNames?: Record<number, string>;
  parentRepoName?: string;
  parentDetailPath?: string;
  onClose: () => void;
  onChanged: () => void;
  onError: (message: string) => void;
  canAct?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(proposal.title);
  const [kind, setKind] = useState(proposal.kind);
  const [description, setDescription] = useState(proposal.description);
  const [pipelineId, setPipelineId] = useState<number | null>(proposal.pipeline_id);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;

  // Pipelines disponíveis para a task filha: do repo alvo + globais.
  useEffect(() => {
    if (proposal.status !== "pending") return;
    api
      .listPipelines(proposal.target_repository_id ?? proposal.repository_id ?? undefined)
      .then((list) => {
        setPipelines(list);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposal.id]);

  const run = async (action: () => Promise<unknown>, after?: () => void) => {
    setBusy(true);
    try {
      await action();
      onChanged();
      after?.();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    await run(() =>
      api.updateProposal(proposal.task_id, proposal.id, {
        title: title.trim() || proposal.title,
        description,
        kind,
        pipeline_id: pipelineId ?? null,
      }),
    );
    setEditing(false);
  };

  const targetName =
    proposal.target_repository_id != null
      ? repoNames?.[proposal.target_repository_id]
      : parentRepoName;

  const statusLabel =
    proposal.status === "accepted"
      ? "aceita"
      : proposal.status === "rejected"
        ? "rejeitada"
        : "aguardando sua decisão";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal ws-proposal-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="proposal-icon">🧩</span>
          <strong>{proposal.title}</strong>
          <button className="link-btn" onClick={onClose}>fechar</button>
        </div>

        <div className="modal-body">
          <div className="proposal-meta">
            <span className="badge">{kind}</span>
            <span className={`ws-proposal-status ${proposal.status === "accepted" ? "ws-ok" : proposal.status === "rejected" ? "ws-err" : ""}`}>
              {statusLabel}
            </span>
            {targetName && <span className="muted small">→ repo {targetName}</span>}
            {parentRepoName && (
              <span className="muted small">de {parentRepoName}</span>
            )}
            <span className="muted small">proposta de #{proposal.task_id}</span>
          </div>

          {proposal.status === "pending" && editing ? (
            <div className="form-stack">
              <div className="form-field">
                <label className="form-label">Título</label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </div>
              <div className="form-field">
                <label className="form-label">Tipo</label>
                <select value={kind} onChange={(e) => setKind(e.target.value)}>
                  <option value="feature">feature</option>
                  <option value="bug">bug</option>
                  <option value="issue">issue</option>
                  <option value="chore">chore</option>
                </select>
              </div>
              <div className="form-field">
                <label className="form-label">
                  Pipeline da task <HelpTip>
                  Define as fases da task filha quando aceita. Se não escolher, usa o
                  pipeline padrão do projeto (ou o da task de origem).
                </HelpTip>
                </label>
                <select
                  value={pipelineId ?? ""}
                  onChange={(e) => setPipelineId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">— padrão do projeto —</option>
                  {pipelines.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                      {p.repository_id == null ? " (global)" : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-field">
                <label className="form-label">Descrição</label>
                <textarea
                  rows={8}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
            </div>
          ) : proposal.description ? (
            <div className="proposal-full">
              <Markdown text={proposal.description} />
            </div>
          ) : (
            <p className="muted small">Sem descrição detalhada nesta proposta.</p>
          )}

          {proposal.status === "accepted" && proposal.accepted_task_id != null && (
            <p className="muted small">
              Esta proposta foi aceita e virou a{" "}
              <Link
                to={
                  parentDetailPath
                    ? `${parentDetailPath}/${proposal.accepted_task_id}`
                    : `/tasks/${proposal.accepted_task_id}`
                }
                className="link-btn"
              >
                task #{proposal.accepted_task_id} →
              </Link>
            </p>
          )}
        </div>

        {proposal.status === "pending" && (
          <div className="modal-foot">
            {editing ? (
              <>
                <button
                  onClick={() => void saveEdits()}
                  disabled={busy || !canAct || !title.trim()}
                  title={actTitle}
                >
                  {busy ? "…" : "salvar alterações"}
                </button>
                <button
                  className="link-btn"
                  onClick={() => {
                    setTitle(proposal.title);
                    setKind(proposal.kind);
                    setDescription(proposal.description);
                    setPipelineId(proposal.pipeline_id ?? null);
                    setEditing(false);
                  }}
                  disabled={busy}
                >
                  cancelar
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={() => setEditing(true)}
                  disabled={busy || !canAct}
                  title={actTitle}
                >
                  editar
                </button>
                <button
                  onClick={() => run(() => api.acceptProposal(proposal.task_id, proposal.id))}
                  disabled={busy || !canAct}
                  title={actTitle}
                >
                  {busy ? "…" : "Aceitar e criar task"}
                </button>
                <button
                  className="danger"
                  onClick={() => run(() => api.rejectProposal(proposal.task_id, proposal.id))}
                  disabled={busy || !canAct}
                  title={actTitle}
                >
                  Rejeitar
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
