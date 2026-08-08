import type { Task } from "../types";

/** Trilha das fases da task: ✓ concluída · ● atual · ○ pendente · ✕ falhou.
 * O tooltip de cada bolinha traz fase, robô, status e tentativa. */
export default function PhaseStepper({ task, muted }: { task: Task; muted?: boolean }) {
  const steps = [...task.steps].sort((a, b) => a.position - b.position);
  return (
    <div className={`stepper${muted ? " stepper-muted" : ""}`}>
      {steps.map((step) => {
        const state =
          step.status === "done"
            ? "done"
            : step.status === "running"
              ? "active"
              : step.status === "failed" || step.status === "guardrail_blocked"
                ? "failed"
                : "pending";
        const label = `Fase ${step.position}/${steps.length} · ${
          step.robot?.name ?? "?"
        }${step.post_merge ? " · pós-merge" : ""} · ${step.status} (tentativa ${step.attempt})`;
        return (
          <span className="stepper-item" key={step.id}>
            {step.post_merge && <span className="stepper-post" title="fase pós-merge" />}
            <span className={`stepper-dot stepper-${state}`} title={label}>
              {state === "done" ? "✓" : state === "failed" ? "✕" : step.position}
            </span>
          </span>
        );
      })}
    </div>
  );
}
