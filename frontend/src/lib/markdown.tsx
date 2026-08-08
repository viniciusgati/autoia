import type { ReactNode } from "react";

/** Renderizador de markdown leve e sem dependências (os textos dos robôs são markdown).
 * Seguro: o texto é escapado antes do parse — nenhum HTML cru é emitido. */

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

export default function Markdown({ text }: { text: string }) {
  const lines = escapeHtml(text).split("\n");
  const blocks: ReactNode[] = [];
  let key = 0;
  let inCode = false;
  let codeBuf: string[] = [];
  let listBuf: string[] = [];

  const flushList = () => {
    if (listBuf.length === 0) return;
    const items = listBuf.map((item, idx) => (
      <li key={idx}>{renderInline(item, `li${key}-${idx}`)}</li>
    ));
    blocks.push(<ul key={`l${key++}`}>{items}</ul>);
    listBuf = [];
  };

  for (const rawLine of lines) {
    const line = rawLine;

    if (/^```/.test(line)) {
      flushList();
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

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushList();
      const lvl = Math.min(heading[1].length, 4) - 1;
      const Tag = (["h1", "h2", "h3", "h4"] as const)[lvl];
      blocks.push(
        <Tag key={`h${key++}`}>{renderInline(heading[2], `h${key}`)}</Tag>,
      );
      continue;
    }

    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      listBuf.push(line.replace(/^\s*([-*]|\d+\.)\s+/, ""));
      continue;
    }

    if (/^>\s?/.test(line)) {
      flushList();
      blocks.push(
        <blockquote key={`q${key++}`}>
          {renderInline(line.replace(/^>\s?/, ""), `q${key}`)}
        </blockquote>,
      );
      continue;
    }

    if (/^\s*$/.test(line)) {
      flushList();
      continue;
    }

    flushList();
    blocks.push(<p key={`p${key++}`}>{renderInline(line, `p${key}`)}</p>);
  }

  flushList();
  if (inCode) {
    blocks.push(
      <pre key={`c${key++}`}>
        <code>{codeBuf.join("\n")}</code>
      </pre>,
    );
  }

  return <div className="markdown">{blocks}</div>;
}
