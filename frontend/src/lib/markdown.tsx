import type { ReactNode } from "react";

/** Renderizador de markdown leve e sem dependências (os textos dos robôs são markdown).
 * Seguro: o texto é escapado antes do parse — nenhum HTML cru é emitido.
 * Suporta: headings, listas (com checkboxes), blockquote, código (inline/fence),
 * negrito, links, `hr` e tabelas (separador |-|-|, alinhamento :---/:--:/---:). */

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/g;

/** Escapa e renderiza inline (code, **negrito**, [link]) de um trecho de texto
 * (usado em linhas avulsas, ex.: itens de checklist). */
export function inlineMarkdown(text: string, keyBase: string): ReactNode[] {
  return renderInline(escapeHtml(text), keyBase);
}

/** Renderiza inline (code, **negrito**, [link]) de uma linha já escapada. */
function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const token = m[0];
    const key = `${keyBase}-${i}`;
    if (token.startsWith("`")) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {link[1]}
          </a>,
        );
      } else {
        nodes.push(token);
      }
    }
    last = m.index + token.length;
    i++;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

type ListItem = { text: string; checked: boolean | null };

const CELL_RE = /^\s*\|/;
const SEPARATOR_RE = /^\s*\|?[\s:|-]+\|?\s*$/;

/** Divide uma linha de tabela em células (remove pipes externos e espaços). */
function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

/** Alinhamento da célula a partir do separador (:---, :---:, ---:). */
function alignOf(sep: string): string {
  const s = sep.trim();
  const left = s.startsWith(":");
  const right = s.endsWith(":");
  return left && right ? "center" : right ? "right" : left ? "left" : "";
}

export default function Markdown({ text }: { text: string }) {
  const lines = escapeHtml(text).split("\n");
  const blocks: ReactNode[] = [];
  let key = 0;
  let inCode = false;
  let codeBuf: string[] = [];
  let listBuf: ListItem[] = [];
  let pendingRow: string | null = null; // linha `| ... |` aguardando separador
  let table: string[][] | null = null; // linhas da tabela (primeira = header)
  let tableAlign: string[] = [];

  const flushList = () => {
    if (listBuf.length === 0) return;
    const items = listBuf.map((item, idx) => (
      <li key={idx}>
        {item.checked !== null && (
          <input
            type="checkbox"
            className="markdown-check"
            checked={item.checked}
            disabled
            readOnly
          />
        )}
        {/* Texto agrupado num span: em itens flex (checkbox), o code inline fica
            dentro do fluxo normal do span em vez de virar flex-item (que quebra
            na vertical). */}
        <span className="md-li-text">{renderInline(item.text, `li${key}-${idx}`)}</span>
      </li>
    ));
    blocks.push(<ul key={`l${key++}`}>{items}</ul>);
    listBuf = [];
  };

  const flushPending = () => {
    if (pendingRow !== null) {
      blocks.push(<p key={`p${key++}`}>{renderInline(pendingRow, `p${key}`)}</p>);
      pendingRow = null;
    }
  };

  const flushTable = () => {
    if (!table) return;
    const [header, ...rows] = table;
    blocks.push(
      <div className="markdown-table" key={`t${key++}`}>
        <table>
          <thead>
            <tr>
              {header.map((cell, i) => (
                <th key={i} className={tableAlign[i] ? `md-align-${tableAlign[i]}` : ""}>
                  {renderInline(cell, `th${key}-${i}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, i) => (
                  <td key={i} className={tableAlign[i] ? `md-align-${tableAlign[i]}` : ""}>
                    {renderInline(cell, `td${key}-${r}-${i}`)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
    table = null;
    tableAlign = [];
  };

  const flushAll = () => {
    flushList();
    flushPending();
    flushTable();
  };

  for (const rawLine of lines) {
    const line = rawLine;

    if (/^```/.test(line)) {
      flushAll();
      if (inCode) {
        blocks.push(
          <pre key={`c${key++}`}>
            <code>{codeBuf.join("\n")}</code>
          </pre>,
        );
        inCode = false;
      } else {
        inCode = true;
        codeBuf = [];
      }
      continue;
    }

    if (inCode) {
      codeBuf.push(line);
      continue;
    }

    // Linha de tabela: guarda como candidata; só vira tabela com separador na sequência
    if (table && CELL_RE.test(line)) {
      table.push(splitRow(line));
      continue;
    }
    if (pendingRow !== null && SEPARATOR_RE.test(line) && line.includes("-")) {
      // confirma a tabela: linha pendente é o header
      table = [splitRow(pendingRow)];
      tableAlign = splitRow(line).map(alignOf);
      pendingRow = null;
      continue;
    }
    if (CELL_RE.test(line)) {
      if (pendingRow === null) {
        pendingRow = line;
        continue;
      }
      // pendência anterior não virou tabela → vira parágrafo e processa esta linha
      flushPending();
      pendingRow = line;
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushAll();
      const lvl = Math.min(heading[1].length, 4) - 1;
      const Tag = (["h1", "h2", "h3", "h4"] as const)[lvl];
      blocks.push(
        <Tag key={`h${key++}`}>{renderInline(heading[2], `h${key}`)}</Tag>,
      );
      continue;
    }

    const list = line.match(/^\s*([-*]|\d+\.)\s+(.*)$/);
    if (list) {
      flushTable();
      flushPending();
      const raw = list[2];
      const cb = raw.match(/^\[([ xX])\]\s+(.*)$/);
      listBuf.push(
        cb
          ? { text: cb[2], checked: cb[1].toLowerCase() === "x" }
          : { text: raw, checked: null },
      );
      continue;
    }

    if (/^>\s?/.test(line)) {
      flushAll();
      blocks.push(
        <blockquote key={`q${key++}`}>
          {renderInline(line.replace(/^>\s?/, ""), `q${key}`)}
        </blockquote>,
      );
      continue;
    }

    if (/^ {0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushAll();
      blocks.push(<hr key={`r${key++}`} />);
      continue;
    }

    if (/^\s*$/.test(line)) {
      flushAll();
      continue;
    }

    flushAll();
    blocks.push(<p key={`p${key++}`}>{renderInline(line, `p${key}`)}</p>);
  }

  flushAll();
  if (inCode) {
    blocks.push(
      <pre key={`c${key++}`}>
        <code>{codeBuf.join("\n")}</code>
      </pre>,
    );
  }

  return <div className="markdown">{blocks}</div>;
}
