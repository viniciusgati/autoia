import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useAdaptivePolling, usePolling } from "../lib/polling";
import type { ChamadoStage, ChamadoWorkspace, ToolInfo } from "../types";
import { chamadoStatusClass, chamadoStatusLabel } from "./Chamados";

const stageStatusLabel: Record<string, string> = {
  pendente: "Pendente",
  ativa: "Ativa",
  aguardando: "Aguardando worker…",
  executando: "Executando…",
  fechada: "Fechada",
};

/** Tool label/descrição vêm da API (catálogo da etapa). */

function ToolCallView({ tool, input }: { tool: string; input: unknown }) {
  const inputStr =
    typeof input === "string" ? input : input && typeof input === "object" ? JSON.stringify(input) : String(input ?? "");
  return (
    <div className="chat-prompt" style={{ marginBottom: 4 }}>
      <span className="mono">🔧 {tool}</span>
      {inputStr && <div className="mono prewrap" style={{ marginTop: 4 }}>{inputStr}</div>}
    </div>
  );
}

function MessageRow({ m }: { m: ChamadoWorkspace["messages"][number] }) {
  if (m.kind === "user") {
    const text = String(m.payload.text ?? "");
    const tool = String(m.payload.tool ?? "");
    return (
      <div className="chat-msg">
        <div className="chat-avatar">você</div>
        <div className="chat-body">
          <div className="chat-meta">Usuário{tool ? ` · ${tool}` : ""}</div>
          <div className="chat-bubble">{text}</div>
        </div>
      </div>
    );
  }
  if (m.kind === "assistant_text") {
    const content = String(m.payload.content ?? "");
    return (
      <div className="chat-msg">
        <div className="chat-avatar chat-avatar-kimi">robô</div>
        <div className="chat-body">
          <div className="chat-meta">Assistente</div>
          <div className="chat-bubble chat-bubble-kimi">{content}</div>
        </div>
      </div>
    );
  }
  if (m.kind === "tool_call") {
    return (
      <div className="chat-msg">
        <div className="chat-avatar chat-avatar-tool">tool</div>
        <div className="chat-body">
          <ToolCallView tool={String(m.payload.tool ?? m.payload.name ?? "tool")} input={m.payload.input ?? m.payload.arguments} />
        </div>
      </div>
    );
  }
  if (m.kind === "tool_result") {
    return (
      <div className="chat-msg">
        <div className="chat-avatar chat-avatar-tool">ret</div>
        <div className="chat-body">
          <div className="chat-meta">resultado da ferramenta</div>
          <div className="prewrap mono" style={{ fontSize: 12, opacity: 0.8 }}>
            {String(m.payload.output ?? m.payload.content ?? "—").slice(0, 600)}
          </div>
        </div>
      </div>
    );
  }
  // system: eventos do worker (decisão, erro, tool_done…)
  const event = String(m.payload.event ?? "system");
  const detail = String(m.payload.error ?? m.payload.decision ?? m.payload.justificativa ?? "");
  return (
    <div className="chat-marker" style={{ marginBottom: 4 }}>
      <b>⚙ {event}</b>
      {detail && <div className="muted">{detail}</div>}
    </div>
  );
}

export default function ChamadoDetail() {
  const { repoId: repoIdStr, chamadoId: chamadoIdStr } = useParams<{ repoId: string; chamadoId: string }>();
  const repoId = Number(repoIdStr);
  const chamadoId = Number(chamadoIdStr);

  const [ws, setWs] = useState<ChamadoWorkspace | null>(null);
  const [repoName, setRepoName] = useState("");
  const [workerAlive, setWorkerAlive] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [input, setInput] = useState("");

  const load = async (signal?: AbortSignal) => {
    try {
      setWs(await api.getChamadoWorkspace(chamadoId, signal));
    } catch (e) {
      if (!signal?.aborted) setError(String(e));
    }
  };

  // Polling adaptativo: etapa do chamado em andamento (aguardando/executando)
  // mantém 2 s; ociosa reduz a frequência (backoff até 10 s).
  const stageBusy =
    ws?.current_stage != null &&
    (ws.current_stage.status === "aguardando" || ws.current_stage.status === "executando");

  useAdaptivePolling(load, {
    activeIntervalMs: 2000,
    idleIntervalMs: 10000,
    isActive: stageBusy,
    deps: [chamadoId],
  });
  usePolling(
    (signal) => {
      api
        .getChamadoWorkerStatus(signal)
        .then((s) => setWorkerAlive(s.alive))
        .catch(() => setWorkerAlive(false));
    },
    5000,
    [],
  );

  useEffect(() => {
    api.listRepositories().then((all) => setRepoName(all.find((r) => r.id === repoId)?.name ?? "")).catch(() => {});
  }, [repoId]);

  if (error) return <p className="error">{error}</p>;
  if (!ws) return <p className="muted">Carregando chamado…</p>;

  const chamado = ws.chamado;
  const stage = ws.current_stage;
  const busyNow = stage != null && (stage.status === "aguardando" || stage.status === "executando");

  const runTool = async (tool: ToolInfo) => {
    if (!input.trim() || busyNow) return;
    setError("");
    try {
      await api.runChamadoTool(chamado.id, tool.key, input);
      setInput("");
      void load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : String(e)));
    } finally {
    }
  };

  const closeStage = async () => {
    if (busyNow) return;
    setError("");
    try {
      await api.closeChamadoStage(chamado.id);
      void load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : String(e)));
    } finally {
    }
  };

  const stageIsActive = (s: ChamadoStage) =>
    s.status === "ativa" || s.status === "aguardando" || s.status === "executando";

  return (
    <div className="resumo">
      <div className="resumo-header">
        <h2>
          Chamado #{chamado.id} {chamado.title}
        </h2>
        <span className="muted">
          {repoName} · <b>{chamado.workflow_status || "—"}</b> ·{" "}
          <span className={chamadoStatusClass[chamado.status] ?? "badge"}>
            {chamadoStatusLabel[chamado.status] ?? chamado.status}
          </span>
          {" · "}
          custo R$ {chamado.cost_spent.toFixed(2)} / {chamado.budget_limit.toFixed(2)} ·{" "}
          <span className={`worker-dot${workerAlive === null ? "" : workerAlive ? " worker-dot-on" : " worker-dot-off"}`} />
          {workerAlive === null ? " verificando worker…" : workerAlive ? " worker ativo" : " worker offline"}
        </span>
      </div>

      <div className="resumo-actions">
        <Link to={`/${repoId}/chamados`} className="link-btn">
          ← chamados
        </Link>
        <Link to={`/${repoId}`} className="link-btn">
          dashboard
        </Link>
      </div>

      {chamado.error && <p className="error">{chamado.error}</p>}

      {/* Histórico de etapas */}
      <h3 className="resumo-section">Etapas</h3>
      <div className="fluxo-lista">
        {ws.stages.map((s) => (
          <div key={s.id} className="fluxo-item">
            <span className="fluxo-pos">{s.position + 1}</span>
            <div className="fluxo-info" style={{ flex: 1 }}>
              <b>{s.stage_type_name ?? "etapa"}</b>
              {s.decision && (
                <div className="muted">decisão: {s.decision}</div>
              )}
              {s.result && <div className="prewrap" style={{ fontSize: 13, marginTop: 4 }}>{s.result}</div>}
              {s.error && <div className="error" style={{ marginTop: 4 }}>{s.error}</div>}
            </div>
            <span
              className={`badge${stageIsActive(s) ? " badge-run" : s.status === "fechada" ? " badge-ok" : " badge-muted"}`}
            >
              {stageStatusLabel[s.status] ?? s.status}
            </span>
          </div>
        ))}
      </div>

      {/* Ferramentas da etapa atual */}
      {stage && (
        <div className="config-section" style={{ marginTop: 16 }}>
          <div className="config-sections">
            <div className="card-label">Ferramentas da etapa “{stage.stage_type_name}”</div>
            <div className="form-inline" style={{ marginTop: 8 }}>
              {ws.tools.map((t) => (
                <button key={t.key} disabled={busyNow || !input.trim()} title={t.description} onClick={() => void runTool(t)}>
                  {t.label}
                </button>
              ))}
              <button className="warn-btn" disabled={busyNow} onClick={() => void closeStage()} title="Fechar avalia a etapa e decide a próxima (resposta/cancelar/avançar)">
                Fechar avaliação
              </button>
            </div>
            {busyNow && <p className="muted" style={{ marginTop: 6 }}>Processando…</p>}
          </div>
        </div>
      )}

      {/* Transcrição */}
      <h3 className="resumo-section">Conversa da etapa</h3>
      <div className="chat" style={{ marginTop: 8 }}>
        {ws.messages.length === 0 && (
          <p className="muted">Nenhuma interação ainda. Use uma ferramenta abaixo para começar.</p>
        )}
        {ws.messages.map((m) => (
          <MessageRow key={m.id} m={m} />
        ))}
      </div>

      {/* Campo de interação */}
      <div className="form-inline" style={{ marginTop: 12 }}>
        <input
          className="ws-send"
          style={{ flex: 1 }}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && ws.tools[0] && !busyNow) void runTool(ws.tools[0]);
          }}
          placeholder="Escreva o pedido e clique na ferramenta (ex.: avalie o erro usando o arquivo src/app.py como base)…"
          disabled={busyNow}
        />
      </div>
    </div>
  );
}
