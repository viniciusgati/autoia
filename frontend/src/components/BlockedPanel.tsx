import { useState } from "react";
import type { Task } from "../types";

/** Painel de bloqueio aguardando instrução (Nível 2).
 *  O agente declarou que não consegue continuar sozinho; o usuário fornece uma
 *  instrução e o desenvolvimento retoma exatamente de onde parou. */

export default function BlockedPanel({
  task,
  onContinue,
  busy,
}: {
  task: Task;
  onContinue: (instruction: string) => void;
  busy: boolean;
}) {
  const [instruction, setInstruction] = useState(task.resume_instruction ?? "");

  return (
    <div className="card warn" id="bloqueio-instrucao">
      <div className="card-title">
        <strong>⛔ Desenvolvimento bloqueado — aguardando instrução</strong>
        <span className="badge badge-warn">não é uma falha</span>
      </div>
      <p className="muted small">
        O agente não consegue continuar sozinho neste momento. Forneça uma instrução
        sobre <strong>como deseja que a execução continue</strong> — a execução
        retomará do ponto exato em que parou, preservando contexto, histórico e
        alterações já feitas.
      </p>

      {task.block_reason_type && (
        <div className="blocked-reason">
          <div className="form-label">Motivo do bloqueio</div>
          <div className="muted small" style={{ marginBottom: 4 }}>
            tipo: <code>{task.block_reason_type}</code>
          </div>
          <p className="summary-text">{task.block_reason || "—"}</p>
          {task.block_question && (
            <>
              <div className="form-label" style={{ marginTop: 8 }}>Pergunta do agente</div>
              <p className="summary-text">{task.block_question}</p>
            </>
          )}
        </div>
      )}

      <div className="form-field" style={{ marginTop: 10 }}>
        <label className="form-label">Como deseja continuar?</label>
        <textarea
          rows={3}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="ex.: utilize a abordagem B e não altere a estrutura atual do serviço…"
        />
      </div>
      <div className="form-actions">
        <button disabled={busy || !instruction.trim()} onClick={() => onContinue(instruction.trim())}>
          {busy ? "retomando…" : "▶ continuar execução"}
        </button>
        <span className="muted small">
          A instrução entra na timeline como intervenção do usuário e no contexto da retomada.
        </span>
      </div>
    </div>
  );
}
