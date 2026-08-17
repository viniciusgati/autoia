import { useEffect, useState } from "react";
import { api } from "../api";
import { usePolling } from "../lib/polling";
import type { CleanTarget, CleanTargetResult, StorageReport } from "../types";

/** Tela global "Configuração geral do sistema" (`/config`): mede o espaço
 *  ocupado pelos dados gerados pelo autoia (5 categorias) e permite limpar com
 *  segurança os arquivos órfãos — logs antigos e lixo de teste dentro de
 *  workspaces de tasks não ativas. O banco de dados e os workspaces inteiros
 *  nunca são removidos. */

/** Nomes PT-BR dos alvos de limpeza (confirm e detalhe por alvo). */
const TARGET_LABELS: Record<CleanTarget, string> = {
  logs: "Logs antigos",
  pytest_tmp: "Lixo do pytest (.pytest-tmp)",
  smoke: "Dados de smoke test (data/smoke)",
  chrome_profiles: "Perfis de Chrome (chrome-profile)",
};

/** Alvos enviados ao clicar em "Limpar dados órfãos". */
const ALL_CLEAN_TARGETS: CleanTarget[] = ["logs", "pytest_tmp", "smoke", "chrome_profiles"];

/** Formata bytes em PT-BR (B / KB / MB / GB). */
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) {
    return `${(n / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} KB`;
  }
  if (n < 1024 * 1024 * 1024) {
    return `${(n / (1024 * 1024)).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} MB`;
  }
  return `${(n / (1024 * 1024 * 1024)).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} GB`;
}

/** Mensagem de erro em PT-BR: traduz os casos comuns (401/rede) e mantém o
 *  detalhe do backend (PT-BR) nos demais (400/403/500). */
function errorMessage(e: unknown): string {
  const s = String(e);
  if (s.startsWith("401")) return "Sessão expirada — faça login novamente. (401)";
  if (s.includes("Failed to fetch") || s.includes("NetworkError") || s.startsWith("TypeError")) {
    return "Falha de rede — verifique a conexão e tente novamente.";
  }
  return s;
}

export default function SystemConfig() {
  const [report, setReport] = useState<StorageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [cleaning, setCleaning] = useState(false);
  const [cleanError, setCleanError] = useState("");
  const [cleanResult, setCleanResult] = useState<CleanTargetResult[] | null>(null);
  const [cleanTotal, setCleanTotal] = useState(0);

  /** `silent = true` (polling) atualiza sem resetar o estado de carregando. */
  const load = async (signal?: AbortSignal, silent = false) => {
    if (!silent) {
      setLoading(true);
      setError("");
    }
    try {
      const data = await api.getStorageReport(signal);
      setReport(data);
      setError("");
    } catch (e) {
      if (signal?.aborted) return;
      setError(errorMessage(e));
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // Polling de 30 s: atualiza silenciosamente (sem reset de estado).
  usePolling((signal) => load(signal, true), 30000, []);

  // Vazio = resposta sem categorias ou todas com 0 bytes / 0 itens.
  const isEmpty =
    report == null ||
    report.categories.length === 0 ||
    report.categories.every((c) => c.size_bytes === 0 && c.item_count === 0);

  const handleClean = async () => {
    const alvos = ALL_CLEAN_TARGETS.map((t) => `• ${TARGET_LABELS[t]}`).join("\n");
    const confirmado = window.confirm(
      `Limpar dados órfãos?\n\n${alvos}\n\nO banco de dados e os workspaces inteiros são preservados.`,
    );
    if (!confirmado) return; // cancelar não dispara chamada
    setCleaning(true);
    setCleanError("");
    setCleanResult(null);
    try {
      const result = await api.cleanStorage(ALL_CLEAN_TARGETS);
      setCleanResult(result.targets);
      setCleanTotal(result.total_bytes_freed);
      setReport(result.report);
    } catch (e) {
      setCleanError(errorMessage(e));
    } finally {
      setCleaning(false);
    }
  };

  return (
    <div>
      <h2>Configuração geral do sistema</h2>
      <p className="muted">
        Medição do espaço ocupado pelos dados gerados pelo autoia e limpeza
        segura de arquivos órfãos. O banco de dados e os workspaces de tarefas
        nunca são removidos.
      </p>

      <div className="form-inline" style={{ marginBottom: 14 }}>
        <button type="button" onClick={() => void load()} disabled={loading}>
          Atualizar
        </button>
        <button
          type="button"
          className="danger"
          onClick={() => void handleClean()}
          disabled={loading || cleaning || error !== "" || isEmpty}
        >
          {cleaning ? (
            <>
              <span className="btn-spinner" />
              Limpando…
            </>
          ) : (
            "Limpar dados órfãos"
          )}
        </button>
      </div>

      {cleanError && <p className="error">{cleanError}</p>}
      {cleanResult && (
        <div className="success-notice">
          <div>
            <strong>{fmtBytes(cleanTotal)} liberados</strong>
          </div>
          {cleanResult.map((r) => (
            <div key={r.target}>
              • {TARGET_LABELS[r.target as CleanTarget] ?? r.target}: {r.item_count} item
              {r.item_count === 1 ? "" : "s"} removido{r.item_count === 1 ? "" : "s"} (
              {fmtBytes(r.bytes_freed)})
            </div>
          ))}
        </div>
      )}

      {loading ? (
        <p className="muted">Carregando relatório de armazenamento…</p>
      ) : error ? (
        <div className="section-error">
          <span>{error}</span>
          <button onClick={() => void load()}>Tentar novamente</button>
        </div>
      ) : isEmpty ? (
        <p className="muted">Nenhum dado de armazenamento encontrado</p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>Categoria</th>
                <th>Tamanho</th>
                <th>Itens</th>
                <th>Limpeza</th>
              </tr>
            </thead>
            <tbody>
              {report.categories.map((c) => (
                <tr key={c.id}>
                  <td>{c.label}</td>
                  <td className="mono">{fmtBytes(c.size_bytes)}</td>
                  <td>{c.item_count}</td>
                  <td>
                    {c.cleanable ? (
                      <span className="badge badge-warn">limpável</span>
                    ) : (
                      <span className="badge badge-muted">seguro</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="storage-total">
            Total: <strong>{fmtBytes(report.total_bytes)}</strong>
          </p>
        </>
      )}
    </div>
  );
}
