import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { MSG_SEM_PERMISSAO } from "../lib/tasks";
import type { TaskProposal } from "../types";

/** Card de proposta de task filha: título, badge kind/repo, descrição e os botões
 *  aceitar/rejeitar (aprovação humana). Quando aceita, vira link para a task criada.
 */
export default function ProposalCard({
  proposal,
  repoNames,
  parentRepoName,
  parentDetailPath,
  onChanged,
  onError,
  canAct = true,
}: {
  proposal: TaskProposal;
  /** Nome dos repositórios por id (para exibir o alvo cross-repo). */
  repoNames?: Record<number, string>;
  /** Nome do repositório da task pai (importante na visão "todos os projetos"). */
  parentRepoName?: string;
  /** Caminho base do detalhe da task pai (ex.: `/1/tasks`). */
  parentDetailPath?: string;
  onChanged: () => void;
  onError: (message: string) => void;
  /** Permissão de atuação (responsável/admin) — desabilita aceitar/rejeitar. */
  canAct?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const actTitle = canAct ? undefined : MSG_SEM_PERMISSAO;

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      onChanged();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const targetName =
    proposal.target_repository_id != null
      ? repoNames?.[proposal.target_repository_id]
      : parentDetailPath
        ? null // mesmo repo da task pai
        : null;

  const parentProject =
    proposal.repository_id != null
      ? repoNames?.[proposal.repository_id]
      : parentRepoName;

  return (
    <div className="proposal-card">
      <div className="proposal-head">
        <span className="proposal-icon">🧩</span>
        <span className="proposal-title">{proposal.title}</span>
        <span className="badge badge-warn">
          {proposal.status === "accepted"
            ? "aceita"
            : proposal.status === "rejected"
              ? "rejeitada"
              : "proposta"}
        </span>
      </div>
      <div className="proposal-meta">
        <span className="badge">{proposal.kind}</span>
        {targetName && <span className="muted small">→ repo {targetName}</span>}
        {parentProject && (
          <span className="muted small">
            {targetName ? "de " : ""}{parentProject}
          </span>
        )}
        <span className="muted small">
          proposta de #{proposal.task_id}
        </span>
      </div>
      {proposal.description && (
        <p className="proposal-desc">{proposal.description}</p>
      )}

      {proposal.status === "pending" ? (
        <div className="proposal-actions">
          <button
            onClick={() => run(() => api.acceptProposal(proposal.task_id, proposal.id))}
            disabled={busy || !canAct}
            title={actTitle}
          >
            {busy ? "…" : "aceitar"}
          </button>
          <button
            className="danger"
            onClick={() => run(() => api.rejectProposal(proposal.task_id, proposal.id))}
            disabled={busy || !canAct}
            title={actTitle}
          >
            rejeitar
          </button>
        </div>
      ) : (
        proposal.status === "accepted" &&
        proposal.accepted_task_id != null && (
          <div className="proposal-actions">
            <Link
              to={
                parentDetailPath
                  ? `${parentDetailPath}/${proposal.accepted_task_id}`
                  : `/tasks/${proposal.accepted_task_id}`
              }
              className="link-btn"
            >
              ver task criada #{proposal.accepted_task_id} →
            </Link>
          </div>
        )
      )}
    </div>
  );
}
