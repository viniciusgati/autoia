import { AlertIcon, CheckIcon, EyeIcon, PauseIcon, XIcon } from "./Icons";

/** Ícone animado de status de task (substitui o label textual nos cards).
 *
 *  - executando/fila: spinner girando (tudo "rodando")
 *  - pausada: pause
 *  - cancelada/falhou: X
 *  - concluída: check
 *  - bloqueada/guardrail: alerta
 *  - revisão/aprovação humana: olho
 *  - criada/pendente: ponto
 *  O label continua como `title` (tooltip) para acesso/contexto.
 */

const STATUS_META: Record<string, { title: string; cls: string }> = {
  created: { title: "criada", cls: "status-dot" },
  queued: { title: "na fila", cls: "status-queue" },
  in_progress: { title: "em andamento", cls: "status-spin" },
  done: { title: "concluída", cls: "status-ok" },
  failed: { title: "falhou", cls: "status-err" },
  blocked: { title: "bloqueada", cls: "status-err" },
  needs_review: { title: "revisão humana", cls: "status-warn" },
  waiting_approval: { title: "aprovação humana", cls: "status-warn" },
  pending: { title: "pendente", cls: "status-dot" },
  running: { title: "rodando", cls: "status-spin" },
  guardrail_blocked: { title: "guardrail bloqueou", cls: "status-err" },
  paused: { title: "pausada", cls: "status-warn" },
  cancelled: { title: "cancelada", cls: "status-err" },
};

export default function StatusIcon({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? { title: status, cls: "status-dot" };

  let icon: React.ReactNode;
  switch (status) {
    case "queued":
      // na fila: spinner AZUL (aguardando o worker pegar)
      icon = <span className="status-spinner status-spinner-queue" aria-hidden="true" />;
      break;
    case "in_progress":
    case "running":
      // rodando de verdade: spinner VERDE (execução ativa)
      icon = <span className="status-spinner status-spinner-run" aria-hidden="true" />;
      break;
    case "paused":
      icon = <PauseIcon size={15} />;
      break;
    case "cancelled":
    case "failed":
      icon = <XIcon size={15} />;
      break;
    case "done":
      icon = <CheckIcon size={15} />;
      break;
    case "blocked":
    case "guardrail_blocked":
      icon = <AlertIcon size={15} />;
      break;
    case "needs_review":
    case "waiting_approval":
      icon = <EyeIcon size={15} />;
      break;
    default:
      icon = <span className="status-dot-core" aria-hidden="true" />;
  }

  return (
    <span className={`status-icon ${meta.cls}`} title={meta.title} aria-label={meta.title}>
      {icon}
    </span>
  );
}
