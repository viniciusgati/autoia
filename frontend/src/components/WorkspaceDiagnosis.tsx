import { useEffect, useRef } from "react";
import Markdown from "../lib/markdown";
import { currentFailureOccurrence, suggestedCorrectionStep, type SubtaskDiagnosis } from "../lib/workspaceDiagnosis";
import type { Task, TimelineEvent, Workspace, WorkspaceOccurrence } from "../types";

const SUBTASK_LABELS: Record<string, string> = {
  pending: "Pendente", implementing: "Implementando", implemented: "Aguardando validação",
  verifying: "Validando", done: "Concluída", failed: "Falhou",
};

/** Primeiro parágrafo do relatório como prévia; o modal preserva o texto integral. */
function reportExcerpt(report: string): string {
  const paragraphs = report.replace(/\r\n/g, "\n").split(/\n\s*\n/);
  const paragraph = paragraphs.map((part) => part.split("\n")
    .filter((line) => !/^\s*(#{1,6}\s|```)/.test(line))
    .join("\n").replace(/^\s*(FAIL|PASS|NEEDS_WORK|AUSENTE)\b\s*[:—-]?\s*/i, "").trim())
    .find(Boolean) ?? "";
  return paragraph.length > 500 ? `${paragraph.slice(0, 500)}…` : paragraph;
}

export function WorkspaceDiagnosis({ ws, diagnoses, canAct, onSubtask, onPrepare, onDetails, onFocus }: {
  ws: Workspace;
  diagnoses: SubtaskDiagnosis[];
  canAct: boolean;
  onSubtask: (position: number) => void;
  onPrepare: (diagnosis: SubtaskDiagnosis) => void;
  onDetails: (occurrence: WorkspaceOccurrence) => void;
  onFocus: () => void;
}) {
  const task = ws.task;
  const issues = diagnoses.filter((item) => item.needsAttention);
  const recovering = diagnoses.filter((item) => item.recovering);
  const active = ["in_progress", "queued"].includes(task.status);
  const halted = ["blocked", "failed", "needs_review", "waiting_approval", "paused"].includes(task.status);
  const failure = currentFailureOccurrence(ws);
  const done = task.status === "done";
  const blocked = task.status === "blocked";
  const phase = failure ?? ws.occurrences.find((occurrence) => occurrence.status === "running");
  const title = done ? "Entrega concluída"
    : blocked ? "O agente precisa da sua orientação"
    : halted && (issues.length > 0 || failure) ? "Entenda o que interrompeu a tarefa"
    : task.status === "paused" ? "Tarefa pausada"
    : task.status === "waiting_approval" ? "Etapa aguardando sua aprovação"
    : halted ? "Sua revisão é o próximo passo"
    : active && (issues.length > 0 || recovering.length > 0) ? "Acompanhe a correção e a nova validação"
    : active ? "Trabalho em andamento"
    : task.status === "cancelled" ? "Tarefa cancelada"
    : task.status === "open" ? "Aguardando sua orientação" : "Tudo pronto para começar";
  const reason = halted
    ? task.block_reason || task.error || failure?.stop?.reason
    : null;
  const next = blocked
    ? task.block_question || "Leia o motivo e envie a orientação necessária no campo de mensagem."
    : halted && issues.length > 0
      ? "Abra o relatório da subtarefa, confira a evidência e prepare uma orientação para a etapa responsável."
      : halted && task.status === "waiting_approval"
        ? "Revise a entrega desta etapa antes de autorizar a continuação."
        : halted && task.status === "paused"
          ? "Confira o andamento abaixo. Use Continuar quando estiver pronto para retomar."
          : halted
            ? "Confira o motivo da parada e os detalhes da execução antes de orientar a próxima tentativa."
            : active && (issues.length > 0 || recovering.length > 0)
              ? "A tarefa continua na fila ou em execução. Os relatórios anteriores ficam disponíveis; a próxima validação confirmará o resultado."
              : done ? "Acompanhe as entregas e consulte o histórico completo das etapas abaixo."
                : active ? "As etapas e os resultados são atualizados automaticamente."
                  : task.status === "open" ? "Envie uma mensagem para definir o próximo trabalho do agente."
                    : "As etapas, entregas e validações aparecerão aqui conforme o trabalho avançar.";
  const finishedSteps = task.steps.filter((step) => step.status === "done").length;

  return (
    <section id="diagnostico" className={`ws-diagnosis ws-diagnosis-${halted ? "attention" : done ? "ok" : "active"}`} aria-labelledby="ws-diagnosis-title">
      <div className="ws-diagnosis-heading">
        <div>
          <span className="ws-diagnosis-eyebrow">VISÃO DA TAREFA</span>
          <h3 id="ws-diagnosis-title" className="ws-diagnosis-title">{title}</h3>
        </div>
        {phase && <span className={`badge ${halted ? "badge-warn" : "badge-run"}`}>Fase {phase.position + 1} · {phase.robot?.name ?? "agente"}</span>}
      </div>
      {reason && <div className="ws-diagnosis-reason"><Markdown text={reason} /></div>}
      <p className="ws-diagnosis-copy">{next}</p>
      <div className="ws-diagnosis-stats">
        <div className="ws-diagnosis-stat"><strong>{finishedSteps}<small> / {task.steps.length}</small></strong><span>etapas concluídas</span></div>
        <div className="ws-diagnosis-stat"><strong>{task.subtasks.filter((subtask) => subtask.status === "done").length}<small> / {task.subtasks.length}</small></strong><span>subtarefas validadas</span></div>
        <div className="ws-diagnosis-stat"><strong>{issues.length}</strong><span>{active ? "subtarefas a resolver" : "subtarefas com pendência"}</span></div>
      </div>
      {issues.length > 0 && (
        <ul className="ws-diagnosis-list">
          {issues.map((diagnosis) => (
            <li className="ws-diagnosis-item" key={diagnosis.subtask.id}>
              <div className="ws-diagnosis-item-copy">
                <span className={`badge ${diagnosis.inconclusive ? "badge-warn" : "badge-err"}`}>{diagnosis.inconclusive ? "Validação inconclusiva" : diagnosis.failure?.event.raw.payload.phase === "verify" ? "Validação reprovada" : "Pendência"}</span>
                <strong>#{diagnosis.subtask.position + 1} · {diagnosis.subtask.title}</strong>
                {diagnosis.reason && <p>{diagnosis.reason}</p>}
                {diagnosis.report && <div className="ws-diagnosis-excerpt"><Markdown text={reportExcerpt(diagnosis.report)} /></div>}
                {diagnosis.failure && <span className="ws-subtask-source">Fase {diagnosis.failure.occurrence.position + 1} · {diagnosis.failure.occurrence.robot?.name} · execução {diagnosis.failure.occurrence.run}</span>}
              </div>
              <div className="ws-diagnosis-actions">
                <button onClick={() => onSubtask(diagnosis.subtask.position)}>Ver relatório completo</button>
                {halted && <button className="link-btn" disabled={!canAct} onClick={() => onPrepare(diagnosis)}>Preparar orientação</button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {halted && issues.length === 0 && (
        <div className="ws-diagnosis-actions">
          {failure && <button onClick={() => onDetails(failure)}>Ver detalhes da execução</button>}
          <button className="link-btn" disabled={!canAct} onClick={onFocus}>Escrever orientação</button>
        </div>
      )}
    </section>
  );
}

export function SubtaskCard({ diagnosis, onOpen }: { diagnosis: SubtaskDiagnosis; onOpen: () => void }) {
  const { subtask, needsAttention, recovering, inconclusive } = diagnosis;
  const status = needsAttention ? inconclusive ? "Inconclusiva" : "Requer atenção"
    : SUBTASK_LABELS[subtask.status] ?? subtask.status;
  const badge = needsAttention ? "badge-err" : subtask.status === "done" ? "badge-ok"
    : ["implementing", "verifying"].includes(subtask.status) ? "badge-run" : "badge-muted";
  return (
    <button className={`ws-subtask-card${needsAttention ? " ws-subtask-card-attention" : ""}`} onClick={onOpen} aria-label={`Subtarefa ${subtask.position + 1}: ${subtask.title}. ${status}. Ver detalhes e relatório.`}>
      <span className="ws-subtask-card-top"><span className="ws-sub-pos">{subtask.position + 1}</span><span className={`badge ${badge}`}>{status}</span></span>
      <strong className="ws-subtask-card-title">{subtask.title}</strong>
      {needsAttention && diagnosis.reason && <span className="ws-subtask-card-reason">{diagnosis.reason}</span>}
      {recovering && <span className="ws-subtask-card-meta">Nova execução após uma falha anterior</span>}
      <span className="ws-subtask-card-link">{needsAttention ? "Ler relatório e próximo passo" : "Ver detalhes e histórico"} <span aria-hidden="true">↗</span></span>
    </button>
  );
}

export function WorkspaceEventDetails({ events }: { events: TimelineEvent[] }) {
  return (
    <div className="ws-diagnosis-events">
      {events.map((event) => (
        <details key={event.seq}>
          <summary><span className={`badge ${event.status === "error" ? "badge-err" : "badge-muted"}`}>{new Date(event.ts).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}</span> {event.name} — {event.summary}</summary>
          {event.raw.kind === "assistant_text" && typeof event.raw.payload.content === "string" && <Markdown text={event.raw.payload.content} />}
          {event.input && <><h5>Entrada completa</h5><pre>{JSON.stringify(event.input, null, 2)}</pre></>}
          {event.output && <><h5>Resultado completo</h5><pre>{JSON.stringify(event.output, null, 2)}</pre></>}
          <h5>Registro original completo</h5><pre>{JSON.stringify(event.raw, null, 2)}</pre>
        </details>
      ))}
    </div>
  );
}

export function SubtaskDetailsModal({ task, diagnosis, canAct, onClose, onPrepare, onDetails }: {
  task: Task;
  diagnosis: SubtaskDiagnosis;
  canAct: boolean;
  onClose: () => void;
  onPrepare: (diagnosis: SubtaskDiagnosis) => void;
  onDetails: (occurrence: WorkspaceOccurrence) => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previous?.focus();
    };
  }, []);
  const { subtask, failure, needsAttention, inconclusive, report, agentResponse } = diagnosis;
  const target = suggestedCorrectionStep(task, diagnosis);
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div ref={dialogRef} tabIndex={-1} className="modal ws-diff-modal ws-subtask-modal" role="dialog" aria-modal="true" aria-labelledby="ws-subtask-modal-title" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
        if (event.key === "Tab") {
          const elements = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], summary, [tabindex="0"]');
          if (!elements?.length) return;
          const first = elements[0];
          const last = elements[elements.length - 1];
          if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }}>
        <div className="modal-head"><strong id="ws-subtask-modal-title">Subtarefa {subtask.position + 1} · {subtask.title}</strong><button className="link-btn" onClick={onClose}>Fechar</button></div>
        <div className="modal-body">
          <section className="ws-subtask-detail-section">
            <span className={`badge ${needsAttention ? "badge-err" : subtask.status === "done" ? "badge-ok" : "badge-muted"}`}>{needsAttention ? inconclusive ? "Validação inconclusiva" : "Pendência atual" : SUBTASK_LABELS[subtask.status] ?? subtask.status}</span>
            {failure && <p className="ws-subtask-source">{needsAttention ? "Falha registrada" : "Última falha no histórico"} · fase {failure.occurrence.position + 1} · {failure.occurrence.robot?.name} · execução {failure.occurrence.run} · {new Date(failure.event.ts).toLocaleString("pt-BR")}</p>}
            {diagnosis.reason && <div className="ws-subtask-report"><Markdown text={diagnosis.reason} /></div>}
            {!needsAttention && failure && <p className="muted small">Esta falha pertence ao histórico. O estado atual da subtarefa é “{SUBTASK_LABELS[subtask.status] ?? subtask.status}”.</p>}
          </section>
          {(needsAttention || failure) && <section className="ws-subtask-detail-section">
            <h4>{needsAttention ? "Relatório completo da falha" : "Relatório da falha anterior"}</h4>
            {report ? <><p className="ws-subtask-source">{diagnosis.reportSource}</p><div className="ws-subtask-report"><Markdown text={report} /></div></>
              : <p className="muted">Não há um relatório de validação disponível para esta falha nos dados recebidos. Consulte a resposta do agente e os registros da execução abaixo.</p>}
            {agentResponse && agentResponse !== report && <details className="ws-subtask-detail-section"><summary>Resposta completa do agente nesta execução</summary><div className="ws-subtask-report"><Markdown text={agentResponse} /></div></details>}
            {failure && <button className="link-btn" onClick={() => onDetails(failure.occurrence)}>Abrir todos os registros desta execução ↗</button>}
          </section>}
          {needsAttention && <section className="ws-subtask-detail-section">
            <h4>O que fazer agora</h4>
            <p>{inconclusive ? "A validação não chegou a uma conclusão. Confira se houve interrupção, falta de acesso ou tempo esgotado antes de pedir uma nova validação." : "Use os problemas e critérios descritos no relatório para orientar a correção. Depois, a subtarefa precisa ser validada novamente."}</p>
            {target && <p className="ws-subtask-source">Etapa sugerida: fase {target.position + 1} · {target.robot?.name}.</p>}
            <button disabled={!canAct} onClick={() => onPrepare(diagnosis)}>Preparar orientação</button>
            <p className="muted small">Preenche a mensagem para você revisar. A execução só é solicitada quando você envia.</p>
          </section>}
          {subtask.description && <section className="ws-subtask-detail-section"><h4>Escopo da subtarefa</h4><Markdown text={subtask.description} /></section>}
          {subtask.acceptance_criteria && <section className="ws-subtask-detail-section"><h4>Critérios de aceite</h4><Markdown text={subtask.acceptance_criteria} /></section>}
          {!needsAttention && subtask.summary && subtask.summary !== report && <section className="ws-subtask-detail-section"><h4>Último relato salvo na subtarefa</h4><div className="ws-subtask-report"><Markdown text={subtask.summary} /></div></section>}
        </div>
      </div>
    </div>
  );
}
