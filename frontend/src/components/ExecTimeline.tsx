import { useState } from "react";
import type { TimelineEvent } from "../types";

/** Timeline cronológica da execução do desenvolvimento (derivada dos RunEvent).
 *
 *  - Nível 1 (compacta): apenas marcos importantes (início, fases concluídas,
 *    bloqueios, intervenções, erros, fim) — responde "o que aconteceu".
 *  - Nível 2 (completa): todas as tool_calls como eventos, expansíveis.
 *  - Nível 3 (técnico): cada evento abre os dados completos (input/output/raw).
 */

const MILESTONE_TYPES = new Set([
  "development_started",
  "development_finished",
  "phase_done",
  "blocked",
  "user_intervention",
  "error",
  "task",
]);

const ICONS: Record<string, string> = {
  development_started: "▶",
  development_finished: "🏁",
  phase: "⚙",
  phase_done: "✓",
  blocked: "⛔",
  user_intervention: "👤",
  task: "🧩",
  tool_call: "⌘",
  text: "💬",
  error: "✕",
  warning: "⚠",
  system: "ℹ",
};

function fmtTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = (ms / 1000).toFixed(1);
  return `${s}s`;
}

function hasDetails(ev: TimelineEvent): boolean {
  return ev.duration_ms != null || ev.input != null || ev.output != null;
}

export default function ExecTimeline({
  events,
  level = 2,
}: {
  events: TimelineEvent[];
  level?: 1 | 2 | 3;
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());

  const visible =
    level === 1 ? events.filter((e) => MILESTONE_TYPES.has(e.type)) : events;

  const toggle = (id: number) =>
    setOpen((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (visible.length === 0) {
    return <p className="muted small">Nenhum evento registrado até agora.</p>;
  }

  return (
    <ul className="exec-timeline">
      {visible.map((ev, i) => {
        const expandable = level >= 2 && hasDetails(ev);
        const isOpen = open.has(i);
        const icon = ICONS[ev.type] ?? "·";
        const statusClass = ev.status ? ` exec-tl-${ev.status}` : "";
        return (
          <li
            key={`${ev.ts}-${ev.seq}-${i}`}
            className={`exec-tl-item${statusClass}`}
          >
            <span className="exec-tl-time">{fmtTime(ev.ts)}</span>
            <span className="exec-tl-icon">{icon}</span>
            <div className="exec-tl-body">
              <div className="exec-tl-summary">
                {ev.summary}
                {ev.status === "error" && <span className="badge badge-warn">erro</span>}
                {ev.status === "blocked" && <span className="badge badge-warn">bloqueado</span>}
              </div>
              {ev.type === "tool_call" && ev.name && (
                <div className="exec-tl-tool mono">
                  {ev.name}
                  {ev.duration_ms != null && <span className="muted"> · {fmtDuration(ev.duration_ms)}</span>}
                  {ev.status === "completed" && <span className="muted"> · ok</span>}
                </div>
              )}
              {expandable && (
                <button className="link-btn small" onClick={() => toggle(i)}>
                  {isOpen ? "recolher detalhes" : "detalhes"}
                </button>
              )}
              {expandable && isOpen && (
                <div className="exec-tl-detail">
                  <div className="exec-tl-grid">
                    {ev.duration_ms != null && (
                      <div>
                        <div className="form-label">Duração</div>
                        <span className="mono">{fmtDuration(ev.duration_ms)}</span>
                      </div>
                    )}
                    {ev.status && (
                      <div>
                        <div className="form-label">Status</div>
                        <span>{ev.status}</span>
                      </div>
                    )}
                    {ev.input != null && (
                      <div>
                        <div className="form-label">Argumentos</div>
                        <pre className="event-payload">{JSON.stringify(ev.input, null, 2)}</pre>
                      </div>
                    )}
                    {ev.output != null && (
                      <div>
                        <div className="form-label">Resultado</div>
                        <pre className="event-payload">{JSON.stringify(ev.output, null, 2)}</pre>
                      </div>
                    )}
                  </div>
                  {level === 3 && (
                    <>
                      <div className="form-label" style={{ marginTop: 8 }}>Payload bruto</div>
                      <pre className="event-payload">{JSON.stringify(ev.raw, null, 2)}</pre>
                    </>
                  )}
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
