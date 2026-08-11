/** Renderiza diff (unificado ou --stat) com cores por tipo de linha. */

import type { ReactNode } from "react";

interface DiffViewProps {
  code: string;
  compact?: boolean;
}

function StatSummary({ line }: { line: string }): ReactNode {
  const m = line.match(/^(\d+ files? changed)(.*)$/);
  if (!m) return <span className="diff-muted">{line}</span>;
  const rest = m[2];
  const add = rest.match(/^,?\s*(\d+ insertions?\(\+\))/);
  const afterAdd = add ? rest.slice(add[0].length) : rest;
  const del = afterAdd.match(/^,?\s*(\d+ deletions?\(-\))/);
  const afterDel = del ? afterAdd.slice(del[0].length) : afterAdd;
  return (
    <>
      <span className="diff-fname">{m[1]}</span>
      {add && <span className="diff-add-text">{add[1]}</span>}
      {del && <span className="diff-del-text">{del[1]}</span>}
      <span className="diff-muted">{afterDel}</span>
    </>
  );
}

function StatRow({ line }: { line: string }): ReactNode | null {
  // `git diff --stat`: ` path | 12 +++++-----`
  const m = line.match(/^(.+?)\|\s*(\d+)?\s*([+\-]+)\s*$/);
  if (!m) return null;
  return (
    <>
      <span className="diff-fname">{m[1]}</span>
      <span className="diff-num">{m[2] ?? ""}</span>
      <span className="diff-graph">{m[3]}</span>
    </>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "diff-hunk";
  if (/^(diff --git|index |new file|deleted file|similarity |rename from|rename to|Binary files)/.test(line))
    return "diff-filehead";
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-filehead";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-ctx";
}

export default function DiffView({ code, compact }: DiffViewProps) {
  const lines = code.split("\n");
  return (
    <div className={`diff-view${compact ? " diff-view-compact" : ""}`}>
      {lines.map((line, i) => {
        let body: ReactNode;
        const trimmed = line.length ? line : " ";
        if (line.startsWith(" ") || line === "") {
          const row = StatRow({ line });
          if (row) body = row;
          else if (/files? changed/.test(line)) body = <StatSummary line={line} />;
          else body = <span className="diff-plain">{trimmed}</span>;
        } else {
          body = <span className="diff-plain">{line}</span>;
        }
        return (
          <div key={i} className={`diff-line ${lineClass(line)}`}>
            {body}
          </div>
        );
      })}
    </div>
  );
}
