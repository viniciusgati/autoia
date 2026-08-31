import { useEffect, useRef, type DependencyList } from "react";
import { nextPollInterval } from "./poll-interval";

/** Polling controlado: executa `fn` imediatamente e a cada `intervalMs`.
 *
 * - Pula o tick quando a aba está oculta (`document.hidden`) para não gastar
 *   rede/CPU em background.
 * - Pula o tick se a execução anterior ainda não terminou (evita polls
 *   sobrepostos em respostas lentas, sem abortar o fetch anterior).
 * - Aborta a requisição em voo ao desmontar (o `AbortSignal` é repassado a `fn`).
 */
export function usePolling(
  fn: (signal: AbortSignal) => void | Promise<void>,
  intervalMs: number,
  deps: DependencyList,
) {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;

    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (inFlight) return;
      inFlight = true;
      try {
        await fnRef.current(controller.signal);
      } catch {
        /* erro tratado pela própria página */
      } finally {
        inFlight = false;
      }
    };

    void tick();
    const timer = setInterval(tick, intervalMs);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Polling ADAPTATIVO (backoff): enquanto `isActive`, mantém a frequência
 * rápida (`activeIntervalMs`); quando ocioso, o intervalo dobra a cada tick até
 * `idleIntervalMs` — ex.: task `done`/`needs_review`/`open` sem fase `running`
 * polla a cada 10 s em vez de 1,5 s (≥ 50% menos requisições em 60 s ociosa).
 *
 * A transição de atividade re-inicializa o efeito (fetch imediato + backoff do
 * base), então uma task que volta a `running` volta a ser acompanhada a 1,5 s
 * na hora. O resto do contrato é igual ao `usePolling`.
 */
export function useAdaptivePolling(
  fn: (signal: AbortSignal) => void | Promise<void>,
  opts: {
    activeIntervalMs: number;
    idleIntervalMs: number;
    isActive: boolean;
    deps: DependencyList;
  },
) {
  const { activeIntervalMs, idleIntervalMs, isActive, deps } = opts;
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const isActiveRef = useRef(isActive);
  isActiveRef.current = isActive;

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let currentInterval = activeIntervalMs;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (inFlight) return;
      inFlight = true;
      try {
        await fnRef.current(controller.signal);
      } catch {
        /* erro tratado pela própria página */
      } finally {
        inFlight = false;
      }
      currentInterval = nextPollInterval(
        currentInterval,
        isActiveRef.current,
        activeIntervalMs,
        idleIntervalMs,
      );
      timer = setTimeout(() => void tick(), currentInterval);
    };

    void tick();
    return () => {
      if (timer) clearTimeout(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, isActive]);
}
