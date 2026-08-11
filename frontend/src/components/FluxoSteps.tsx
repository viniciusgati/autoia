import { Link } from "react-router-dom";
import StatusBadge from "./StatusBadge";
import type { Task, TaskStep } from "../types";

/** Lista das etapas (fases) da task com ações: repetir (falha), voltar para esta
 *  fase (re-executa a partir daqui — o histórico é preservado e a nova execução
 *  aparece no fim da timeline), e link para os detalhes técnicos da fase. */
export default function FluxoSteps({
  task,
  steps,
  onRetry,
  repoId,
  taskId,
}: {
  task: Task;
  steps: TaskStep[];
  onRetry: (position: number) => void;
  repoId: number;
  taskId: number;
}) {
  return (
    <div className="fluxo-lista">
      {steps.map((s) => (
        <div key={s.id} className={`fluxo-item fluxo-${s.status}`}>
          <span className="fluxo-pos">F{s.position}</span>
          <div className="fluxo-info">
            <strong>{s.robot?.name ?? "?"}</strong>{" "}
            <span className="muted small">({s.robot?.role ?? "?"})</span>
            <StatusBadge status={s.status} />
            {s.verdict && <span className="muted small">· veredicto: {s.verdict}</span>}
            {s.post_merge && <span className="badge badge-ok">pós-merge</span>}
            {s.error && <div className="error small">{s.error}</div>}
          </div>
          <div className="fluxo-actions">
            {(s.status === "failed" || s.status === "guardrail_blocked") && (
              <button className="danger small" onClick={() => onRetry(s.position)}>
                repetir
              </button>
            )}
            {s.status === "done" && task.status !== "created" && (
              <button
                className="warn-btn small"
                onClick={() => onRetry(s.position)}
                title={`Re-executar a partir da fase ${s.position}. A nova execução aparece no fim da timeline; o histórico anterior é preservado.`}
              >
                ← voltar para esta fase
              </button>
            )}
            <Link to={`/${repoId}/tasks/${taskId}/phase/${s.id}`} className="link-btn small">
              detalhes →
            </Link>
          </div>
        </div>
      ))}
    </div>
  );
}
