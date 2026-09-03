import { useEffect, useState } from "react";
import { getCodexModels } from "../api";

interface Props {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

/** Seletor de modelo do executor codex. Popula os modelos disponíveis do CLI
 *  (`codex debug models`); a opção vazia = modelo padrão do codex (config). */
export default function ModelSelect({ value, onChange, disabled }: Props) {
  const [models, setModels] = useState<string[] | null>(null);

  useEffect(() => {
    let alive = true;
    getCodexModels().then((list) => {
      if (alive) setModels(list);
    });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || models === null}
      title={models === null ? "carregando modelos do codex…" : undefined}
    >
      <option value="">padrão do codex</option>
      {(models ?? []).map((model) => (
        <option key={model} value={model}>
          {model}
        </option>
      ))}
    </select>
  );
}
