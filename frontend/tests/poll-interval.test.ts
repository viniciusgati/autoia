/**
 * Teste de contagem instrumentada do polling adaptativo (roda com Node, sem
 * framework de testes JS): valida a lógica pura de backoff do `poll-interval`.
 *
 * Execução:
 *   node --experimental-strip-types --test frontend/tests/poll-interval.test.ts
 *
 * Critérios da história (Subtarefa 5):
 *   - task ociosa (sem fase running) → ≥ 50% menos requisições em 60 s;
 *   - task running → frequência de 1,5 s mantida.
 */
import assert from "node:assert/strict";
import test from "node:test";
import { countPollTicks, nextPollInterval } from "../src/lib/poll-interval.ts";

const ACTIVE_MS = 1500; // frequência rápida (task rodando)
const IDLE_MAX_MS = 10000; // teto do backoff (task ociosa)
const WINDOW_MS = 60_000;

test("backoff ocioso reduz >=50% das requisicoes em 60s", () => {
  const idleTicks = countPollTicks(false, WINDOW_MS, ACTIVE_MS, IDLE_MAX_MS);
  const fixedTicks = countPollTicks(true, WINDOW_MS, ACTIVE_MS, IDLE_MAX_MS);
  assert.ok(
    fixedTicks >= 2,
    `baseline de 1,5s deveria gerar ~40 ticks, veio ${fixedTicks}`,
  );
  assert.ok(
    idleTicks <= fixedTicks * 0.5,
    `ocioso ${idleTicks} ticks vs fixo ${fixedTicks} — não reduziu >=50%`,
  );
});

test("task running mantem frequencia de 1,5s", () => {
  const runningTicks = countPollTicks(true, WINDOW_MS, ACTIVE_MS, IDLE_MAX_MS);
  // 60_000 / 1_500 = 40 intervalos completos + o tick inicial.
  assert.ok(runningTicks >= 39 && runningTicks <= 42, `got ${runningTicks}`);
});

test("intervalo ocioso dobra ate o teto e ativo volta ao base", () => {
  let interval = ACTIVE_MS;
  const seen: number[] = [];
  for (let i = 0; i < 6; i += 1) {
    seen.push(interval);
    interval = nextPollInterval(interval, false, ACTIVE_MS, IDLE_MAX_MS);
  }
  assert.deepEqual(seen, [1500, 3000, 6000, 10000, 10000, 10000]);
  // Ativo reseta para o base (retomada da fase running).
  assert.equal(nextPollInterval(interval, true, ACTIVE_MS, IDLE_MAX_MS), ACTIVE_MS);
});
