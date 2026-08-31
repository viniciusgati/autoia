# Benchmark de performance — task-110

Comparação da MESMA execução (`python -m app.perf_bench --events 500 --samples N`,
task sintética com 500 RunEvent, SQLite em tmp, sem rede/kimi):

| Métrica | Baseline (pré-otimização) | Pós (S2+S3) | Variação |
| --- | --- | --- | --- |
| Endpoint workspace p95 | 39,78 ms (n=30) | 16,13 ms (n=100) | **-59%** (≥30% ✓) |
| Endpoint workspace p50 | 14,09 ms (n=30) | 11,92 ms (n=100) | -15% |
| derive_task_timeline (média 5 it) | 11,44 ms | 4,96 ms | **-57%** (≥50% ✓) |
| derive_task_occurrences (média 5 it) | 4,82 ms | 4,52 ms | -6% |
| Memória pico (tracemalloc) | 1314,00 KiB | 850,02 KiB | -35,3% |
| Queries SELECT endpoint workspace | 14 | 9 | -36% (N+1 `step_artifacts` eliminado) |
| Crescimento de logs/disco | +16.806 B | +16.806 B | igual (payloads nunca truncados) |

Paridade determinística: `derive_task_timeline`/`derive_task_occurrences` com o
mesmo input (500 eventos) produzem saída IDÊNTICA à versão pré-otimização
(diff vazio; snapshot em `tests/fixtures/timeline_parity_500.json`).

Nota: o eager load de `Task.children` foi testado e REVERTIDO — o joinedload
pré-carrega a coleção e o identity map não a re-busca após criar uma task filha
(regressão em `accept_proposal`). O `children` continua lazy (1 query extra no
workspace), dentro do limite de ≤ 3 leituras/ocorrência.

Comandos:
- Baseline: `AUTOIA_SANDBOX=off .venv/bin/python -m app.perf_bench --events 500 --samples 30`
- Pós: `AUTOIA_SANDBOX=off .venv/bin/python -m app.perf_bench --events 500 --samples 100`
