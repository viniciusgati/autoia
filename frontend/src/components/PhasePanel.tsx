import { useState } from "react";
import { Link } from "react-router-dom";
import ArtifactGallery from "./ArtifactGallery";
import HelpTip from "./HelpTip";
import StatusBadge from "./StatusBadge";
import Markdown from "../lib/markdown";
import type { TaskStep } from "../types";

interface PhasePanelProps {
  step: TaskStep | null;
  repoId: number;
  taskId: number;
  taskStatus: string;
  onClose: () => void;
  onRetry: (position: number) => void;
}

export default function PhasePanel({ step, repoId, taskId, taskStatus, onClose, onRetry }: PhasePanelProps) {
  const [expanded, setExpanded] = useState(false);
  const open = step != null;

  if (!step) return null;

  const canRetry = step.status === "failed" || step.status === "guardrail_blocked";
  const canBounceBack = taskStatus !== "created" && step.status === "done";

  return (
    <>
      {/* Overlay */}
      <div
        className={`panel-overlay${open ? " open" : ""}`}
        onClick={onClose}
      />

      {/* Panel */}
      <div className={`panel${open ? " open" : ""}`}>
        {/* Header */}
        <div className="panel-header">
          <div>
            <div className="panel-title">
              Fase {step.position} · {step.robot?.name ?? `robô ${step.robot?.id ?? "?"}`}
            </div>
            <div className="panel-subtitle">
              <StatusBadge status={step.status} />
              <span>tentativa {step.attempt}</span>
              {step.attempt > 1 && (
                <HelpTip>
                  Esta fase foi re-executada {step.attempt - 1} vez(es).
                  O limite é definido em <strong>Configurações do projeto → Max tentativas</strong>.
                </HelpTip>
              )}
              {step.verdict && <span>· veredicto: {step.verdict}</span>}
              {step.verdict && (
                <HelpTip>
                  {step.verdict === "READY" && "QA aprovou a história — está clara e implementável."}
                  {step.verdict === "PASS" && "Tester/avaliador aprovou — código atende os critérios de aceite."}
                  {step.verdict === "FAIL" && "Fase reprovada — o trabalho volta para a fase anterior para correção."}
                  {step.verdict === "NEEDS_WORK" && "Precisa de ajustes — a fase anterior será re-executada com as correções pedidas."}
                  {!["READY", "PASS", "FAIL", "NEEDS_WORK"].includes(step.verdict) && `Veredicto emitido pelo robô: ${step.verdict}`}
                </HelpTip>
              )}
              {step.post_merge && <span className="badge badge-ok">pós-merge</span>}
            </div>
          </div>
          <button className="panel-close" onClick={onClose} aria-label="Fechar">
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="panel-body">
          {/* Diff stat */}
          {step.diff_stat && (
            <div className="panel-section">
              <div className="panel-section-label">Alterações</div>
              <pre className="panel-diff">{step.diff_stat}</pre>
            </div>
          )}

          {/* Error */}
          {step.error && (
            <div className="panel-section">
              <div className="panel-section-label">Erro</div>
              <div className="error">{step.error}</div>
            </div>
          )}

          {/* Summary */}
          <div className="panel-section">
            <div className="panel-section-label">Resumo do robô</div>
            {step.summary ? (
              <>
                <div className={`card panel-summary-clamped${expanded ? " panel-summary-expanded" : ""}`}>
                  <Markdown text={step.summary} />
                </div>
                {!expanded && (
                  <button className="panel-more-btn" onClick={() => setExpanded(true)}>
                    ler tudo ↓
                  </button>
                )}
                {expanded && (
                  <button className="panel-more-btn" onClick={() => setExpanded(false)}>
                    recolher ↑
                  </button>
                )}
              </>
            ) : (
              <p className="muted small">
                {step.status === "running"
                  ? "Robô está executando — o relatório será gerado ao final da fase."
                  : step.status === "pending"
                    ? "Fase aguardando execução."
                    : "Nenhum relatório gerado."}
              </p>
            )}
          </div>

          {/* Artifacts (screenshots) */}
          <ArtifactGallery stepId={step.id} />

          {/* Meta info */}
          <div className="panel-section">
            <div className="panel-section-label">Informações</div>
            <div className="muted small" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {step.started_at && <span>Início: {new Date(step.started_at).toLocaleString()}</span>}
              {step.finished_at && <span>Fim: {new Date(step.finished_at).toLocaleString()}</span>}
              {!step.started_at && <span>Não iniciada</span>}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="panel-footer">
          <Link
            to={`/${repoId}/tasks/${taskId}/phase/${step.id}`}
            className="button"
            style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "var(--accent)", color: "#fff", border: "0", borderRadius: "var(--radius-sm)", padding: "8px 16px", fontSize: 13, fontWeight: 500, textDecoration: "none" }}
          >
            detalhes completos →
          </Link>

          <div style={{ flex: 1 }} />

          {canRetry && (
            <button className="danger" onClick={() => onRetry(step.position)}>
              repetir fase
            </button>
          )}
          {canBounceBack && (
            <button className="warn-btn" onClick={() => onRetry(step.position)}>
              ← voltar para esta fase
            </button>
          )}
        </div>
      </div>
    </>
  );
}
