import StatusBadge from "./StatusBadge";
import type { TaskStep } from "../types";

function bulletState(step: TaskStep): string {
  if (step.status === "done") return "done";
  if (step.status === "running") return "running";
  if (step.status === "failed" || step.status === "guardrail_blocked") return "failed";
  // Pending com attempt > 1 = fase resetada para re-execução
  if (step.status === "pending" && step.attempt > 1) return "rerun";
  return "pending";
}

interface TimelineProps {
  steps: TaskStep[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  onRetry?: (position: number) => void;
}

export default function Timeline({ steps, selectedId, onSelect, onRetry }: TimelineProps) {
  const sorted = [...steps].sort((a, b) => a.position - b.position);

  return (
    <div className="timeline">
      {sorted.map((step, i) => {
        const state = bulletState(step);
        const selected = selectedId === step.id;
        const isRerun = step.status === "pending" && step.attempt > 1;

        return (
          <div
            key={step.id}
            className="timeline-item"
            style={{ animationDelay: `${i * 50}ms` }}
          >
            <div className={`timeline-bullet timeline-bullet-${state}`} />
            <button
              className={`timeline-card${selected ? " timeline-card-selected" : ""}`}
              onClick={() => onSelect(step.id)}
            >
              <span className="timeline-pos">{step.position}</span>
              <div className="timeline-info">
                <span className="timeline-robot">{step.robot?.name ?? `robô ${step.robot?.id ?? "?"}`}</span>
                <StatusBadge status={step.status} />
              </div>
              <div className="timeline-meta">
                {step.verdict && <span>{step.verdict}</span>}
                <span className={step.attempt > 1 ? "timeline-attempt-retry" : ""}>
                  tent. {step.attempt}
                </span>
                {isRerun && <span className="badge badge-warn">re-execução</span>}
                {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
              </div>
              <span className="timeline-chevron">→</span>
              {(step.status === "failed" || step.status === "guardrail_blocked") && onRetry && (
                <button
                  className="danger timeline-retry-btn"
                  onClick={(e) => { e.stopPropagation(); onRetry(step.position); }}
                  title="Repetir esta fase"
                >
                  repetir
                </button>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
