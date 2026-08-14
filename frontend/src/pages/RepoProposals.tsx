import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import ProposalCard from "../components/ProposalCard";
import { usePolling } from "../lib/polling";
import type { Repository, TaskProposal } from "../types";

/** Tela de propostas de tasks filhas de UM projeto: lista as que aguardam decisão
 *  humana (pendentes) e as aceitas (com link para a task criada). Rejeitadas saem.
 *  Cada proposta abre o modal completo para explorar e editar antes de aceitar.
 */
export default function RepoProposals() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [proposals, setProposals] = useState<TaskProposal[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const load = async (signal?: AbortSignal) => {
    try {
      const list = await api.listRepoProposals(repoId, signal);
      setProposals(list);
      setUpdatedAt(new Date());
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    }
  };

  // Poll: decisões humanas (aceitar/rejeitar/editar) mudam a lista.
  usePolling(load, 5000, [repoId]);

  useEffect(() => {
    api.listRepositories().then((all) => {
      setRepos(all);
      setRepo(all.find((r) => r.id === repoId) ?? null);
    }).catch(() => {});
  }, [repoId]);

  if (error) return <p className="error">{error}</p>;

  const repoNames = Object.fromEntries(repos.map((r) => [r.id, r.name]));
  const pending = proposals.filter((p) => p.status === "pending");
  const accepted = proposals.filter((p) => p.status === "accepted");

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>{repo ? `Propostas · ${repo.name}` : "Propostas"}</h2>
        <span className="muted">
          {updatedAt ? `atualizado ${updatedAt.toLocaleTimeString()}` : "carregando…"} ·{" "}
          {pending.length} aguardando decisão
        </span>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}`} className="link-btn">
          ← dashboard
        </Link>
        <Link to={`/${repoId}/tasks`} className="link-btn">
          + Nova tarefa
        </Link>
      </div>

      {proposals.length === 0 && (
        <p className="muted" style={{ marginTop: 16 }}>
          Nenhuma proposta de tarefa neste projeto. Propostas aparecem aqui quando um
          robô (ex.: pipeline de brainstorm) gera tasks filhas para decisão humana.
        </p>
      )}

      {pending.length > 0 && (
        <>
          <h3 className="resumo-section">Aguardando decisão</h3>
          <div className="proposal-list">
            {pending.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                repoNames={repoNames}
                parentRepoName={repo?.name}
                parentDetailPath={`/${repoId}/tasks`}
                onChanged={() => void load()}
                onError={setError}
              />
            ))}
          </div>
        </>
      )}

      {accepted.length > 0 && (
        <>
          <h3 className="resumo-section">Aceitas (tasks criadas)</h3>
          <div className="proposal-list">
            {accepted.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                repoNames={repoNames}
                parentRepoName={repo?.name}
                parentDetailPath={`/${repoId}/tasks`}
                onChanged={() => void load()}
                onError={setError}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
