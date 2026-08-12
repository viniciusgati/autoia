import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import type { RepositoryMember, Task } from "../types";

/** Controle de atribuição do responsável da task (select de membros do projeto
 *  + responsável atual). Visível apenas a admin global, admin do projeto ou o
 *  próprio responsável atual. Ao salvar, `onAssigned` recebe a task atualizada
 *  (o header atualiza sem recarregar a página); erro mantém o valor anterior. */
export default function ResponsavelControl({
  task,
  repoId,
  onAssigned,
}: {
  task: Task;
  repoId: number;
  onAssigned: (task: Task) => void;
}) {
  const { user } = useAuth();
  const [members, setMembers] = useState<RepositoryMember[]>([]);
  const [loadErr, setLoadErr] = useState("");
  const [value, setValue] = useState<number | "">(task.responsible_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sincroniza o select quando o responsável muda (ex.: atribuição externa).
  useEffect(() => {
    setValue(task.responsible_id ?? "");
  }, [task.responsible_id]);

  useEffect(() => {
    let active = true;
    api
      .listMembers(repoId)
      .then((m) => {
        if (active) setMembers(m);
      })
      .catch((e) => {
        if (active) setLoadErr(String(e));
      });
    return () => {
      active = false;
    };
  }, [repoId]);

  const isRepoAdmin = useMemo(
    () => user != null && members.some((m) => m.role === "admin" && m.user_id === user.id),
    [members, user],
  );

  // Visível apenas para quem pode atuar: admin global, admin do projeto ou o
  // próprio responsável atual. Com auth OFF (user null) não há o que atribuir.
  const canAssign =
    user != null &&
    (user.role === "admin" || isRepoAdmin || user.id === task.responsible_id);
  if (!canAssign) return null;

  // Opções: membros com usuário + o responsável atual (pode não ser membro —
  // com auth ON o criador da task não vira membro automaticamente).
  const options: { id: number; label: string }[] = [];
  const seen = new Set<number>();
  for (const m of members) {
    if (m.user && !seen.has(m.user.id)) {
      seen.add(m.user.id);
      options.push({
        id: m.user.id,
        label: `${m.user.name} (${m.user.email})${m.role === "admin" ? " · admin" : ""}`,
      });
    }
  }
  const current = task.responsible;
  if (current && !seen.has(current.id)) {
    seen.add(current.id);
    options.push({ id: current.id, label: `${current.name} (${current.email})` });
  }

  const save = async () => {
    if (value === "" || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.assignResponsible(task.id, Number(value));
      onAssigned(updated);
    } catch (e) {
      // Erro (usuário inexistente, 403): mantém o valor anterior e mostra junto.
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="responsavel-control">
      <label className="form-label">Responsável</label>
      <div className="responsavel-control-row">
        <select
          value={value}
          onChange={(e) => setValue(e.target.value === "" ? "" : Number(e.target.value))}
          disabled={busy}
          title={busy ? "salvando…" : "atribuir responsável"}
        >
          {value === "" && <option value="">Não atribuída</option>}
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
        <button disabled={busy || value === ""} onClick={save}>
          {busy ? "salvando…" : "atribuir"}
        </button>
      </div>
      {error && <div className="responsavel-error">{error}</div>}
      {loadErr && !error && <div className="responsavel-error">{loadErr}</div>}
    </div>
  );
}
