import { useState } from "react";
import type { TaskSummary } from "../types";

/** Nível 1 — resumo do desenvolvimento (gerado por LLM dedicada, persistido).
 *  Responde "o que aconteceu?" sem abrir logs. Falha na geração não aparece aqui. */

const RESULT_LABEL: Record<string, string> = {
  completed: "Concluído",
  partial: "Parcial",
  failed: "Falhou",
  pending: "Pendente",
};

export default function TaskSummaryCard({
  summary,
  onRegenerate,
  busy,
}: {
  summary: TaskSummary | null;
  onRegenerate: () => void;
  busy: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!summary) {
    return (
      <div className="card summary-card">
        <div className="card-title">
          <strong>Resumo do desenvolvimento</strong>
          <span className="muted small">gerado por LLM</span>
        </div>
        <p className="muted small">
          Ainda não há um resumo para este desenvolvimento. Ele é opcional — o
          acompanhamento funciona normalmente sem ele.
        </p>
        <button onClick={onRegenerate} disabled={busy}>
          {busy ? "gerando resumo…" : "gerar resumo"}
        </button>
      </div>
    );
  }

  return (
    <div className="card summary-card">
      <div className="card-title">
        <strong>Resumo do desenvolvimento</strong>
        <span className="muted small">gerado por LLM · {new Date(summary.created_at).toLocaleString()}</span>
      </div>

      <div className="summary-row">
        <span className="form-label">O que foi solicitado</span>
        <p className="summary-text">{summary.request || summary.summary}</p>
      </div>

      <div className="summary-row">
        <span className="form-label">O que foi implementado</span>
        <p className="summary-text">{summary.implementation || summary.summary}</p>
      </div>

      <div className="summary-row">
        <span className="form-label">Resultado</span>
        <span className={`badge summary-result summary-result-${summary.result ?? "pending"}`}>
          {RESULT_LABEL[summary.result ?? "pending"] ?? summary.result}
        </span>
        {summary.tasks_summary && <span className="muted small"> · {summary.tasks_summary}</span>}
      </div>

      {summary.changes.length > 0 && (
        <div className="summary-row">
          <span className="form-label">Principais alterações</span>
          <ul className="summary-list">
            {summary.changes.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}

      {summary.issues.length > 0 && (
        <div className="summary-row">
          <span className="form-label">Problemas / observações</span>
          <ul className="summary-list summary-list-warn">
            {summary.issues.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}

      {summary.files.length > 0 && (
        <div className="summary-row">
          <span className="form-label">Arquivos relevantes</span>
          <div className="summary-files mono">
            {summary.files.map((f, i) => <code key={i}>{f}</code>)}
          </div>
        </div>
      )}

      <div className="summary-actions">
        <button onClick={onRegenerate} disabled={busy}>
          {busy ? "regenerando…" : "regenerar resumo"}
        </button>
        <button
          className="link-btn"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? "recolher texto completo" : "ver texto completo"}
        </button>
      </div>
      {expanded && (
        <div className="summary-full">
          <div className="form-label">Resumo completo</div>
          <p className="summary-text">{summary.summary}</p>
        </div>
      )}
    </div>
  );
}
