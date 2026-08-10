import type { Task } from "../types";

interface Props {
  task: Task;
  muted?: boolean;
  /** Mostra o nome do robô abaixo de cada bolinha (dashboard do projeto). */
  showLabels?: boolean;
}

/** Trilha das fases da task: ✓ concluída · ● atual · ○ pendente · ✕ falhou · ↺ re-execução.
 *  O tooltip de cada bolinha traz fase, robô, status e tentativa.
 *  Com `showLabels`, exibe o nome do robô truncado abaixo. */
export default function PhaseStepper({ task, muted, showLabels }: Props) {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    <div className={`stepper${muted ? " stepper-muted" : ""}${showLabels ? " stepper-labels" : ""}`}>
      {steps.map((step) => {
        const state =
          step.status === "done"
            ? "done"
            : step.status === "running"
              ? "active"
              : step.status === "failed" || step.status === "guardrail_blocked"
                ? "failed"
                : "pending";
        const isRerun = step.status === "pending" && step.attempt > 1;
        const displayState = isRerun ? "rerun" : state;
        const tooltip = `Fase ${step.position}/${steps.length} · ${
          step.robot?.name ?? "?"
        }${step.post_merge ? " · pós-merge" : ""} · ${step.status}${
          isRerun ? " (re-execução)" : ""
        } (tentativa ${step.attempt})`;
        const name = step.robot?.name ?? `F${step.position}`;
        return (
          <span className="stepper-item" key={step.id}>
            {step.post_merge && <span className="stepper-post" title="fase pós-merge" />}
            <span className="stepper-dot-group">
              <span className={`stepper-dot stepper-${displayState}`} title={tooltip}>
                {state === "done" ? "✓" : state === "failed" ? "✕" : isRerun ? "↺" : step.position}
              </span>
              {showLabels && (
                <span className="stepper-label" title={name}>
                  {name}
                </span>
              )}
            </span>
          </span>
        );
      })}
    </div>
  );
}
