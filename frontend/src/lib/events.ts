import type { RunEvent } from "../types";

/** Formata um tool_call do kimi como linha legível (ex.: "Bash: npm run build"). */
export function formatToolCall(event: RunEvent): string {
  const payload = event.payload as {
    tool_call?: { function?: { name?: string; arguments?: string } };
  };
  const fn = payload.tool_call?.function;
  const name = fn?.name ?? "?";
  const raw = fn?.arguments ?? "";
  try {
    const args = JSON.parse(raw) as Record<string, unknown>;
    if (name === "Bash") return `Bash: ${String(args.command ?? "")}`;
    const target =
      args.path ?? args.pattern ?? args.query ?? args.skill ?? args.url ?? args.file;
    if (target !== undefined) return `${name}: ${String(target)}`;
  } catch {
    /* argumentos não-JSON */
  }
  return `${name} ${raw}`.trim();
}

/** Resumo de uma linha para os eventos ao vivo de uma sessão. */
export function sessionEventLine(event: RunEvent): string {
  const payload = event.payload as Record<string, unknown>;
  switch (event.kind) {
    case "assistant_text":
      return String(payload.content ?? "").replace(/\s+/g, " ").trim().slice(0, 140);
    case "tool_call":
      return formatToolCall(event);
    case "tool_result": {
      const content = String(payload.content ?? "").replace(/\s+/g, " ").trim();
      return content.slice(0, 120) + (content.length > 120 ? "…" : "");
    }
    case "guardrail_blocked":
      return `⛔ ${String(payload.detail ?? payload.pattern ?? "")}`;
    case "bounce_back":
      return `↩️ voltou da fase ${String(payload.from_position ?? "?")}: ${String(
        payload.reason ?? "",
      )}`;
    case "phase_done":
      return `fase concluída → próxima ${String(payload.next ?? "?")}`;
    case "budget_hit":
      return `orçamento estourado: ${String(payload.reason ?? "")}`;
    case "subtask_start":
      return `🧩 iniciando subtarefa ${Number(payload.position ?? -1) + 1}: ${String(payload.title ?? "?")}`;
    case "subtask_implemented":
      return `✅ subtarefa ${Number(payload.position ?? -1) + 1} implementada: ${String(payload.title ?? "?")}`;
    case "subtask_verified":
      return `✔️ subtarefa ${Number(payload.position ?? -1) + 1} verificada: ${String(payload.title ?? "?")}`;
    case "subtask_failed":
      return `❌ subtarefa ${Number(payload.position ?? -1) + 1} falhou: ${String(payload.reason ?? "")}`;
    case "subtask_bounce_back":
      return `↩️ bounce-back de subtarefas: ${String(payload.positions ?? "")}`;
    case "subtask_marked_done":
      return `✅ subtarefa ${Number(payload.position ?? -1) + 1} marcada como implementada: ${String(payload.title ?? "?")}`;
    case "human_subtask_retry":
      return `👤 retry manual da subtarefa ${Number(payload.position ?? -1) + 1}: ${String(payload.title ?? "?")}`;
    case "task_blocked":
      return `⛔ desenvolvimento bloqueado aguardando instrução: ${String(payload.reason ?? "")}`;
    case "user_intervention":
      return `👤 intervenção do usuário: "${String(payload.instruction ?? "").slice(0, 160)}"`;
    case "execution_resumed":
      return `▶ execução retomada na fase ${String(payload.step ?? "?")}`;
    case "summary_generated":
      return `📄 resumo do desenvolvimento gerado (${String(payload.result ?? "?")})`;
    case "system":
      return String(payload.reason ?? JSON.stringify(payload)).slice(0, 140);
    default:
      return JSON.stringify(payload).slice(0, 140);
  }
}
