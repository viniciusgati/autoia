import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import ProposalCard from "../components/ProposalCard";
import { usePolling } from "../lib/polling";
import type { Repository, TaskProposal } from "../types";

/** Tela de propostas de tasks filhas de UM projeto: lista as que aguardam decisão
 *  humana (pendentes) agrupadas pela task de origem, em ordem de posição. O botão
 *  "aceitar todas em ordem" cria as filhas encadeadas (`depends_on`) e já enfileira —
 *  o worker executa uma por vez respeitando a cadeia. Cada proposta abre o modal
 *  completo para explorar e editar antes de aceitar.
 */
export default function RepoProposals() {
  const { repoId: repoIdStr } = useParams<{ repoId: string }>();
  const repoId = Number(repoIdStr);

  const [proposals, setProposals] = useState<TaskProposal[]>([]);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [error, setError] = useState("");
  const [busyTask, setBusyTask] = useState<number | null>(null);
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

  // Poll: decisões humanas (aceitar/rejeitar/editar) e novas propostas mudam a lista.
  usePolling(load, 5000, [repoId]);

  useEffect(() => {
    api.listRepositories().then((all) => {
      setRepos(all);
      setRepo(all.find((r) => r.id === repoId) ?? null);
    }).catch(() => {});
  }, [repoId]);

  const acceptAll = async (taskId: number, count: number) => {
    if (
      !window.confirm(
        `Aceitar ${count} proposta(s) em ordem?\n\n` +
          "As tasks serão criadas encadeadas e já enfileiradas — o worker executa " +
          "uma por vez, respeitando as dependências.",
      )
    )
      return;
    setBusyTask(taskId);
    try {
      await api.acceptAllProposals(taskId);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTask(null);
    }
  };

  if (error) return <p className="error">{error}</p>;

  const repoNames = Object.fromEntries(repos.map((r) => [r.id, r.name]));
  const pending = proposals
    .filter((p) => p.status === "pending")
    .sort((a, b) => a.task_id - b.task_id || a.position - b.position);

  // Agrupa por task de origem: posições/dependências são relativas ao arquivo de
  // propostas daquela task (a cadeia só faz sentido dentro do grupo).
  const groups: { taskId: number; items: TaskProposal[] }[] = [];
  for (const p of pending) {
    const last = groups[groups.length - 1];
    if (!last || last.taskId !== p.task_id) {
      groups.push({ taskId: p.task_id, items: [p] });
    } else {
      last.items.push(p);
    }
  }

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

      {pending.length === 0 && (
        <p className="muted" style={{ marginTop: 16 }}>
          Nenhuma proposta de tarefa aguardando decisão. Propostas aparecem aqui quando um
          robô (ex.: pipeline de brainstorm) gera tasks filhas para decisão humana.
        </p>
      )}

      {groups.map(({ taskId, items }) => {
        const titlesByPosition = new Map(
          proposals
            .filter((p) => p.task_id === taskId)
            .map((p) => [p.position, `#${p.position + 1} ${p.title}`]),
        );
        return (
          <div key={taskId} className="proposal-group">
            <h3 className="resumo-section proposal-group-head">
              <span>Propostas da task #{taskId}</span>
              <button
                className="link-btn"
                disabled={busyTask === taskId}
                onClick={() => void acceptAll(taskId, items.length)}
                title="Cria todas as tasks filhas encadeadas e já enfileira na ordem"
              >
                {busyTask === taskId
                  ? "aceitando…"
                  : `⚡ aceitar todas em ordem (${items.length})`}
              </button>
            </h3>
            <div className="proposal-list">
              {items.map((p) => (
                <ProposalCard
                  key={p.id}
                  proposal={p}
                  repoNames={repoNames}
                  parentRepoName={repo?.name}
                  parentDetailPath={`/${repoId}/tasks`}
                  dependsOnLabels={p.depends_on.map(
                    (dep) => titlesByPosition.get(dep) ?? `#${dep + 1}`,
                  )}
                  onChanged={() => void load()}
                  onError={setError}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
