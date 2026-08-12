import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Repository, RepositoryDeleteInfo } from "../types";

/** Converte o erro do backend em uma mensagem legível para o diálogo de exclusão.
 *  `api.ts` lança `new Error("403: <detalhe>")` — `String(e)` vira
 *  `"Error: 403: <detalhe>"`, então o status é casado ignorando o prefixo. */
function deleteErrorMessage(e: unknown): string {
  const msg = String(e);
  const status = msg.match(/^(?:Error:\s*)?(40[34]):/);
  if (status) {
    if (status[1] === "403") return "Você não tem permissão para excluir este projeto.";
    return "O projeto já foi removido.";
  }
  if (msg.includes("Failed to fetch") || msg.includes("NetworkError"))
    return "Falha de rede ao excluir o projeto. Verifique sua conexão e tente novamente.";
  // Qualquer outro erro HTTP (ex.: 500) chega como "Error: <status>: <detalhe>"
  // e não é legível — cai num fallback que explica o motivo e a ação.
  if (msg.includes("Error:")) return "Não foi possível excluir o projeto. Tente novamente.";
  return msg;
}

export default function Repositories() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Diálogo de confirmação de exclusão
  const [confirmRepo, setConfirmRepo] = useState<Repository | null>(null);
  const [deleteInfo, setDeleteInfo] = useState<RepositoryDeleteInfo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const load = () => api.listRepositories().then(setRepos).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);

  // "Projeto removido." some após alguns segundos
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createRepository({ name, url, default_branch: branch });
      setName("");
      setUrl("");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const openDeleteDialog = async (repo: Repository) => {
    setConfirmRepo(repo);
    setDeleteInfo(null);
    setDeleteError("");
    try {
      setDeleteInfo(await api.getRepositoryDeleteInfo(repo.id));
    } catch (e) {
      setDeleteError(deleteErrorMessage(e));
    }
  };

  const closeDeleteDialog = () => {
    if (deleting) return; // não fecha nem reconfirma durante a operação
    setConfirmRepo(null);
    setDeleteInfo(null);
    setDeleteError("");
  };

  const confirmDelete = async () => {
    if (!confirmRepo || deleting) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await api.deleteRepository(confirmRepo.id);
      // Sucesso: fecha o diálogo, recarrega a lista sem o projeto e mostra a
      // confirmação na tela.
      setConfirmRepo(null);
      setDeleteInfo(null);
      setDeleteError("");
      await load();
      setNotice("Projeto removido.");
    } catch (e) {
      // Erro: o diálogo permanece aberto com a mensagem dentro dele; a lista
      // NÃO é recarregada e o projeto continua listado.
      setDeleteError(deleteErrorMessage(e));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div>
      <h2>Repositórios</h2>
      <form className="form-inline" onSubmit={submit}>
        <div className="form-field">
          <label className="form-label">Nome</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </div>
        <div className="form-field" style={{flex: 1, minWidth: 220}}>
          <label className="form-label">URL do repositório</label>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
        </div>
        <div className="form-field">
          <label className="form-label">Branch</label>
          <input
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            className="short"
          />
        </div>
        <div className="form-actions">
          <button type="submit" disabled={busy}>{busy ? "clonando…" : "adicionar"}</button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {notice && <p className="success-notice">{notice}</p>}

      {repos.length === 0 ? (
        <p className="muted">Nenhum repositório registrado.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Nome</th>
              <th>URL</th>
              <th>Default</th>
              <th>Checkout</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {repos.map((repo) => (
              <tr key={repo.id}>
                <td>{repo.id}</td>
                <td>{repo.name}</td>
                <td className="mono">{repo.url}</td>
                <td>{repo.default_branch}</td>
                <td className="mono">{repo.local_path ?? "—"}</td>
                <td>
                  <button
                    className="danger"
                    onClick={() => openDeleteDialog(repo)}
                  >
                    remover
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmRepo && (
        <div className="modal-overlay" onClick={closeDeleteDialog}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <strong>Excluir projeto</strong>
            </div>
            <div className="modal-body">
              <p>
                Excluir o projeto irá interromper{" "}
                <strong>{deleteInfo?.active_tasks ?? "…"}</strong> task(s) em
                execução e apagar todos os dados. Esta ação é irreversível.
              </p>
              {deleteError && <p className="error">{deleteError}</p>}
            </div>
            <div className="modal-foot">
              <button
                className="link-btn"
                onClick={closeDeleteDialog}
                disabled={deleting}
              >
                Cancelar
              </button>
              <button
                className="danger"
                onClick={confirmDelete}
                disabled={deleting || deleteInfo === null}
              >
                {deleting ? (
                  <>
                    <span className="btn-spinner" aria-hidden="true" />
                    excluindo…
                  </>
                ) : (
                  "Excluir"
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
