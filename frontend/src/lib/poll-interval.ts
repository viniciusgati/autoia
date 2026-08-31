/** Lógica pura do polling adaptativo (sem React — testável em Node).

 * A função de intervalo é a única regra de negócio do backoff: enquanto a
 * entidade está ATIVA (ex.: task com fase `running`), o polling mantém a
 * frequência rápida (`activeIntervalMs`); quando OCIOSA (ex.: task `done`/
 * `needs_review`/`open` sem fase rodando), o intervalo dobra a cada tick até
 * `idleIntervalMs` — menos requisições, mesma capacidade de reação.
 */
export function nextPollInterval(
  currentIntervalMs: number,
  isActive: boolean,
  activeIntervalMs: number,
  idleIntervalMs: number,
): number {
  if (isActive) return activeIntervalMs;
  return Math.min(Math.max(currentIntervalMs * 2, activeIntervalMs), idleIntervalMs);
}

/** Simula `durationMs` de polling e devolve o nº de ticks (requisições).
 * Usado pelo teste de contagem instrumentada (60 s ociosa vs ativa). */
export function countPollTicks(
  isActive: boolean,
  durationMs: number,
  activeIntervalMs: number = 1500,
  idleIntervalMs: number = 10000,
): number {
  let ticks = 0;
  let elapsed = 0;
  let interval = activeIntervalMs;
  while (elapsed < durationMs) {
    ticks += 1;
    elapsed += interval;
    interval = nextPollInterval(interval, isActive, activeIntervalMs, idleIntervalMs);
  }
  return ticks;
}
