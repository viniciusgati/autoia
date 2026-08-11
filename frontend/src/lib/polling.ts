import { useEffect, useRef, type DependencyList } from "react";

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
