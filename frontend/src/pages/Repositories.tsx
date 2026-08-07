import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Repository } from "../types";

export default function Repositories() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => api.listRepositories().then(setRepos).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
  }, []);

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

  return (
    <div>
      <h2>Repositórios</h2>
      <form className="form-inline" onSubmit={submit}>
        <input
          placeholder="nome"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          placeholder="git@host:user/repo.git (SSH) ou caminho local"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
          className="wide"
        />
        <input
          placeholder="branch default"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          className="short"
        />
        <button disabled={busy}>{busy ? "clonando…" : "adicionar"}</button>
      </form>
      {error && <p className="error">{error}</p>}

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
                    onClick={() =>
                      api.deleteRepository(repo.id).then(load).catch((e) => setError(String(e)))
                    }
                  >
                    remover
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
