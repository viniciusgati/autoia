/** Formatação de custos em reais (R$ X,XX — pt-BR).
 *
 * Os valores de custo vêm do executor (opencode real / kimi estimado) e são
 * tratados como reais na interface — sem conversão de moeda.
 */
export function fmtCost(value: number | null | undefined): string {
  const v = value ?? 0;
  return `R$ ${v.toFixed(2).replace(".", ",")}`;
}

/** "R$ X,XX / R$ Y,YY" (gasto / orçamento) com um único prefixo R$. */
export function fmtBudget(spent: number | null | undefined, limit: number | null | undefined): string {
  return `${fmtCost(spent)} / ${fmtCost(limit)}`;
}
