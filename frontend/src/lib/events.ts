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
