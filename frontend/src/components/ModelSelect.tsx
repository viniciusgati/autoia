import { useEffect, useState } from "react";
import { getCodexModels, getOpenCodeModels } from "../api";

interface Props {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  /** Executor que define a origem do catálogo (codex | opencode). */
  source?: "codex" | "opencode";
}

/** Seletor de modelo do executor. Popula os modelos do CLI correspondente
 *  (`codex debug models` ou `opencode models`); vazio = modelo padrão do executor. */
export default function ModelSelect({ value, onChange, disabled, source = "codex" }: Props) {
  const [models, setModels] = useState<string[] | null>(null);

  useEffect(() => {
    let alive = true;
    (source === "opencode" ? getOpenCodeModels() : getCodexModels()).then((list) => {
      if (alive) setModels(list);
    });
    return () => {
      alive = false;
    };
  }, [source]);

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || models === null}
      title={models === null ? `carregando modelos do ${source}…` : undefined}
    >
      <option value="">padrão do {source}</option>
      {(models ?? []).map((model) => (
        <option key={model} value={model}>
          {model}
        </option>
      ))}
    </select>
  );
}
