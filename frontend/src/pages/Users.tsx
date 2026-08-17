import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import type { User } from "../types";

/** Remove o prefixo "NNN: " que o `request` adiciona ao status HTTP. */
function apiErrorMsg(e: unknown): string {
  return String(e).replace(/^\d+: /, "");
}

/** Tela de gestão de usuários — restrita a admin global (auth OFF = libera). */
export default function Users() {
  const { user, authEnabled } = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  // form de criação
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"member" | "admin">("member");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [created, setCreated] = useState(false);

  const [actionError, setActionError] = useState("");

  const canManage = !authEnabled || user?.role === "admin";

  const load = () => {
    setLoading(true);
    setLoadError("");
    api
      .listUsers()
      .then((list) => setUsers(list))
      .catch((e) => setLoadError(apiErrorMsg(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canManage || creating) return;
    setCreating(true);
    setCreateError("");
    setCreated(false);
    try {
      await api.createUser({ name: name.trim(), email: email.trim(), password, role });
      setName("");
      setEmail("");
      setPassword("");
      setRole("member");
      setCreated(true);
      load();
    } catch (err) {
      setCreateError(apiErrorMsg(err));
    } finally {
      setCreating(false);
    }
  };

  const update = async (id: number, data: { name?: string; email?: string; password?: string; role?: string; active?: boolean }) => {
    if (!canManage) return;
    setActionError("");
    try {
      await api.updateUser(id, data);
      load();
    } catch (err) {
      setActionError(apiErrorMsg(err));
    }
  };

  const changeRole = (u: User, nextRole: string) => {
    if (nextRole === u.role) return;
    void update(u.id, { role: nextRole });
  };

  const toggleActive = (u: User) => {
    void update(u.id, { active: !u.active });
  };

  const resetPassword = (u: User) => {
    const nova = window.prompt(`Nova senha para ${u.email} (mín. 6 caracteres):`);
    if (nova == null || nova.trim() === "") return;
    if (nova.trim().length < 6) {
      setActionError("A senha deve ter ao menos 6 caracteres.");
      return;
    }
    void update(u.id, { password: nova });
  };

  const fmtDate = (iso: string) => new Date(iso).toLocaleString("pt-BR");

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>Usuários</h2>
        <span className="muted">gestão de contas (admin global)</span>
      </div>

      {!canManage && (
        <div className="sticky-alert">
          <span>🔒 Apenas admin global pode gerenciar usuários.</span>
        </div>
      )}

      {canManage && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="card-title">Criar usuário</div>
          <form className="form-stack" onSubmit={submit}>
            {created && <div className="form-error" style={{ color: "var(--ok)" }}>Usuário criado.</div>}
            {createError && <div className="form-error">{createError}</div>}
            <div className="form-inline">
              <div className="form-field" style={{ flex: 1, minWidth: 160 }}>
                <label className="form-label">Nome</label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  minLength={1}
                  maxLength={100}
                />
              </div>
              <div className="form-field" style={{ flex: 2, minWidth: 200 }}>
                <label className="form-label">E-mail</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  minLength={3}
                  maxLength={255}
                />
              </div>
            </div>
            <div className="form-inline">
              <div className="form-field" style={{ flex: 1, minWidth: 160 }}>
                <label className="form-label">Senha (mín. 6)</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={6}
                  maxLength={255}
                  autoComplete="new-password"
                />
              </div>
              <div className="form-field" style={{ flex: 1, minWidth: 160 }}>
                <label className="form-label">Papel</label>
                <select value={role} onChange={(e) => setRole(e.target.value as "member" | "admin")}>
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                </select>
              </div>
            </div>
            <div className="form-actions">
              <button type="submit" disabled={creating}>
                {creating ? "criando…" : "criar usuário"}
              </button>
            </div>
          </form>
        </div>
      )}

      {actionError && <div className="form-error">{actionError}</div>}

      {loading && <p className="muted">carregando…</p>}
      {loadError && <p className="error">{loadError}</p>}

      {!loading && !loadError && users.length === 0 && (
        <p className="muted">Nenhum usuário cadastrado.</p>
      )}

      {!loading && users.length > 0 && (
        <div className="card" style={{ marginTop: 12 }}>
          <table>
            <thead>
              <tr>
                <th>Nome</th>
                <th>E-mail</th>
                <th>Papel</th>
                <th>Status</th>
                <th>Criado em</th>
                <th style={{ textAlign: "right" }}>Ações</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>{u.name}</td>
                  <td>{u.email}</td>
                  <td>
                    {canManage ? (
                      <select
                        value={u.role}
                        onChange={(e) => changeRole(u, e.target.value)}
                      >
                        <option value="member">member</option>
                        <option value="admin">admin</option>
                      </select>
                    ) : (
                      u.role
                    )}
                  </td>
                  <td>{u.active ? "ativo" : "inativo"}</td>
                  <td className="muted">{fmtDate(u.created_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    {canManage && (
                      <>
                        <button className="warn-btn" onClick={() => resetPassword(u)} title="Redefinir senha">
                          senha
                        </button>{" "}
                        {u.active ? (
                          <button className="danger" onClick={() => toggleActive(u)}>
                            inativar
                          </button>
                        ) : (
                          <button onClick={() => toggleActive(u)}>reativar</button>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
