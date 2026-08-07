const LABELS: Record<string, { label: string; cls: string }> = {
  created: { label: "criada", cls: "badge-muted" },
  queued: { label: "na fila", cls: "badge-muted" },
  in_progress: { label: "em andamento", cls: "badge-run" },
  done: { label: "concluída", cls: "badge-ok" },
  failed: { label: "falhou", cls: "badge-err" },
  blocked: { label: "bloqueada", cls: "badge-err" },
  needs_review: { label: "revisão humana", cls: "badge-warn" },
  pending: { label: "pendente", cls: "badge-muted" },
  running: { label: "rodando", cls: "badge-run" },
  guardrail_blocked: { label: "guardrail", cls: "badge-err" },
};

export default function StatusBadge({ status }: { status: string }) {
  const info = LABELS[status] ?? { label: status, cls: "badge-muted" };
  return <span className={`badge ${info.cls}`}>{info.label}</span>;
}
