import { useState } from "react";
import type { Task, TaskStep } from "../types";

/** Painel de conflito de merge (task `blocked` por conflito na integração).
 *  O merge final é feito pelo worker, mas o robô da fase (merger/developer) pode
 *  resolver o conflito NA BRANCH. Aqui o usuário escreve a instrução e re-executa
 *  a fase escolhida — a instrução entra no handoff/prompt da retomada. */

export default function MergeConflictPanel({
  task,
  steps,
  onInstructAndRetry,
  busy,
}: {
  task: Task;
  steps: TaskStep[];
  onInstructAndRetry: (instruction: string, position: number) => void;
  busy: boolean;
}) {
  const sorted = [...steps].sort((a, b) => a.position - b.position);
  const merger = sorted.find((s) => s.robot?.role === "merge") ?? null;
  const developer = sorted.find((s) => s.robot?.role === "implement" && !s.post_merge) ?? null;
  const [instruction, setInstruction] = useState(task.feedback ?? task.error ?? "");

  if (!merger) return null;

  return (
    <div className="card warn" id="conflito-merge">
      <div className="card-title">
        <strong>⚠ Conflito de merge</strong>
        <span className="badge badge-warn">integração bloqueada</span>
      </div>
      <p className="muted small">
        A integração falhou porque a branch e a branch default divergiram no mesmo
        código. O merge final é feito pelo sistema, mas o robô pode resolver o
        conflito <strong>na branch</strong> antes de integrar. Escreva a instrução
        abaixo e re-execute a fase — a instrução entra no contexto do robô.
      </p>

      {task.error && (
        <pre className="review-error" style={{ whiteSpace: "pre-wrap" }}>{task.error}</pre>
      )}

      <div className="form-field" style={{ marginTop: 10 }}>
        <label className="form-label">Instrução para o robô</label>
        <textarea
          rows={3}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="ex.: faça o merge da main na branch dando prioridade ao que já está testado; resolva os conflitos…"
        />
      </div>
      <div className="form-inline" style={{ gap: 8 }}>
        {merger && (
          <button
            disabled={busy || !instruction.trim()}
            onClick={() => onInstructAndRetry(instruction.trim(), merger.position)}
          >
            {busy ? "reenviando…" : `enviar e repetir o ${merger.robot?.name ?? "merger"}`}
          </button>
        )}
        {developer && (
          <button
            className="warn-btn"
            disabled={busy || !instruction.trim()}
            onClick={() => onInstructAndRetry(instruction.trim(), developer.position)}
          >
            retornar ao developer (resolver na branch)
          </button>
        )}
      </div>
      <p className="muted small" style={{ margin: "8px 0 0" }}>
        A instrução é salva como feedback e entra no handoff da fase re-executada. O
        histórico anterior é preservado.
      </p>
    </div>
  );
}
