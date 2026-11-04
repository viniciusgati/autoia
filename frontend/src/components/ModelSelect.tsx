import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { getCodexModels, getOpenCodeModels } from "../api";

interface Props {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  /** Executor que define a origem do catálogo (codex | opencode). */
  source?: "codex" | "opencode";
  /** Exige escolher um modelo do catálogo. Padrão: opencode — o modelo
   *  default dele não funciona bem, então a task/chamado sempre recebe um
   *  modelo real vindo do CLI. */
  required?: boolean;
}

/** Posição do menu (portal no body): `fixed` com coordenadas do input. */
interface MenuPos {
  top?: number;
  bottom?: number;
  left: number;
  minWidth: number;
  maxWidth: number;
  maxHeight: number;
}

const MENU_GAP = 4;
const MENU_MARGIN = 8;
const MENU_MAX_H = 260;

/** Combobox BUSCÁVEL do modelo do executor.
 *
 *  O catálogo vem SEMPRE do CLI (`opencode models` / `codex debug models`) —
 *  enquanto não chega, a UI mostra o estado de carregamento em vez de servir
 *  uma lista fixa de modelos default. CLI fora → estado "não respondeu" com
 *  botão de tentar de novo.
 *
 *  O menu é renderizado em PORTAL no `<body>` com `position: fixed` ancorado no
 *  input: dentro dos cards/headers ele ficava preso ao stacking context e ao
 *  `overflow` dos ancestrais (`backdrop-filter`) — aparecia por baixo do layout
 *  e era cortado nas bordas. */
export default function ModelSelect({
  value,
  onChange,
  disabled,
  source = "codex",
  required = source === "opencode",
}: Props) {
  const [models, setModels] = useState<string[] | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [menuPos, setMenuPos] = useState<MenuPos | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);

  const close = () => {
    setOpen(false);
    setQuery("");
    setActive(0);
  };

  useEffect(() => {
    let alive = true;
    setModels(null);
    (source === "opencode" ? getOpenCodeModels() : getCodexModels())
      .then((list) => {
        if (alive) setModels(list);
      })
      .catch(() => {
        if (alive) setModels([]);
      });
    return () => {
      alive = false;
    };
  }, [source, attempt]);

  // Posiciona o menu ancorado no input (portal + fixed): recalcula ao abrir, no
  // resize e em qualquer scroll (capture pega scroll de containers internos).
  useEffect(() => {
    if (!open) {
      setMenuPos(null);
      return;
    }
    const recompute = () => {
      const anchor = rootRef.current;
      if (!anchor) return;
      const rect = anchor.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const cap = Math.min(420, vw - MENU_MARGIN * 2);
      const left = Math.max(MENU_MARGIN, Math.min(rect.left, vw - MENU_MARGIN - cap));
      const maxWidth = Math.min(cap, vw - MENU_MARGIN - left);
      const minWidth = Math.min(Math.max(rect.width, 170), maxWidth);
      // Sem espaço confortável abaixo, abre para cima (com o teto de altura que
      // couber naquele lado).
      const below = vh - rect.bottom - MENU_GAP - MENU_MARGIN;
      const above = rect.top - MENU_GAP - MENU_MARGIN;
      const openUp = below < 200 && above > below;
      const maxHeight = Math.max(120, Math.min(MENU_MAX_H, openUp ? above : below));
      const next: MenuPos = openUp
        ? {
            bottom: vh - rect.top + MENU_GAP,
            left,
            minWidth,
            maxWidth,
            maxHeight,
          }
        : {
            top: rect.bottom + MENU_GAP,
            left,
            minWidth,
            maxWidth,
            maxHeight,
          };
      setMenuPos((prev) =>
        prev &&
        prev.top === next.top &&
        prev.bottom === next.bottom &&
        prev.left === next.left &&
        prev.minWidth === next.minWidth &&
        prev.maxWidth === next.maxWidth &&
        prev.maxHeight === next.maxHeight
          ? prev
          : next,
      );
    };
    recompute();
    window.addEventListener("resize", recompute);
    window.addEventListener("scroll", recompute, true);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [open]);

  // Fecha o menu ao clicar fora do combobox. O menu vive num PORTAL (fora do
  // `rootRef`), então ele conta como "dentro" — sem isso o clique no item
  // fechava o menu antes do `click` selecionar.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const options = useMemo(() => {
    const all = models ?? [];
    const term = query.trim().toLowerCase();
    const filtered = term ? all.filter((m) => m.toLowerCase().includes(term)) : all;
    // Modelo já escolhido e fora do catálogo (ex.: acabou de sair da lista)
    // continua visível — não some sozinho debaixo do usuário.
    if (value && !filtered.includes(value) && (!term || value.toLowerCase().includes(term))) {
      return [value, ...filtered];
    }
    return filtered;
  }, [models, query, value]);

  const select = (model: string) => {
    onChange(model);
    close();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      if (open) {
        event.preventDefault();
        close();
      }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (options.length === 0) return;
      setActive((i) =>
        event.key === "ArrowDown"
          ? Math.min(i + 1, options.length - 1)
          : Math.max(i - 1, 0),
      );
      return;
    }
    if (event.key === "Enter" && open) {
      if (options.length > 0) {
        event.preventDefault();
        select(options[Math.min(active, options.length - 1)]);
      }
      return;
    }
    if (event.key === "Tab") close();
  };

  const loading = models === null;
  const cliDown = !loading && models !== null && models.length === 0;
  const noMatch = !loading && options.length === 0;
  const missing = required && !value && !loading;

  const placeholder = loading
    ? `carregando catálogo do ${source}…`
    : cliDown
      ? `catálogo do ${source} indisponível`
      : missing
        ? `buscar modelo do ${source}…`
        : "buscar modelo…";

  return (
    <div
      className={`model-combo${missing ? " model-combo-missing" : ""}`}
      ref={rootRef}
    >
      <input
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-haspopup="listbox"
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        value={open ? query : value}
        placeholder={placeholder}
        title={
          loading
            ? `esperando o catálogo do ${source} (CLI)…`
            : cliDown
              ? `o ${source} não respondeu — tente de novo`
              : required
                ? `modelo do ${source} — catálogo do CLI (obrigatório)`
                : `modelo do ${source} (vazio = padrão do ${source})`
        }
        onChange={(e) => {
          if (!open) setOpen(true);
          setQuery(e.target.value);
          setActive(0);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {open &&
        menuPos &&
        createPortal(
          <ul
            className="model-combo-menu"
            role="listbox"
            ref={menuRef}
            style={{
              top: menuPos.top,
              bottom: menuPos.bottom,
              left: menuPos.left,
              minWidth: menuPos.minWidth,
              maxWidth: menuPos.maxWidth,
              maxHeight: menuPos.maxHeight,
            }}
          >
            {loading && (
              <li className="model-combo-note">esperando o catálogo do {source}…</li>
            )}
            {!loading && !required && (
              <li
                role="option"
                aria-selected={!value}
                className={`model-combo-item${!value ? " model-combo-selected" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => select("")}
              >
                padrão do {source}
              </li>
            )}
            {options.map((model, i) => (
              <li
                key={model}
                role="option"
                aria-selected={model === value}
                className={`model-combo-item${
                  model === value ? " model-combo-selected" : ""
                }${i === active ? " model-combo-active" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => select(model)}
                onMouseEnter={() => setActive(i)}
              >
                {model}
              </li>
            ))}
            {noMatch && !loading && (
              <li className="model-combo-note">
                {cliDown
                  ? `o ${source} não respondeu com o catálogo de modelos`
                  : `nenhum modelo para "${query.trim()}"`}
              </li>
            )}
            {cliDown && (
              <li
                className="model-combo-retry"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  setModels(null);
                  setAttempt((a) => a + 1);
                }}
              >
                tentar buscar o catálogo de novo
              </li>
            )}
          </ul>,
          document.body,
        )}
    </div>
  );
}
