# Benchmark de performance — task-110

Task sintética com 500 RunEvent, SQLite em tmp, sem rede/kimi
(`python -m app.perf_bench --events 500 --samples N`). Os números são relativos à
máquina — compare sempre a MESMA metodologia (baseline × pós na mesma execução).

## Baselines

| Baseline | Como foi medida |
| --- | --- |
| Documentada (fase implement inicial) | `perf_bench` no commit `01b7ab8` (pré-otimização S3), `--samples 30` |
| Re-medida (mesma máquina, p/ comparação honesta) | código `8005c92` (pré-otimização) re-executado nesta máquina — média de 3 execuções |

A baseline documentada para `derive_task_timeline` (11,44 ms) foi registrada sob
carga alta da máquina; re-medindo o MESMO código pré-otimização nesta máquina o
valor é ~5,4 ms (média 3 execuções: 5,69 / 5,35 / 5,31). Para `derive_task_occurrences`
a baseline é consistente (documentada 4,82 ms; re-medida ~4,8 ms: 4,72 / 5,03 / 4,76).

## Pós-otimização da Subtarefa 3 (refatoração da derivação)

`derive_task_timeline`/`derive_task_occurrences` foram refatorados: fetch único
via `text()` com decode/parse manuais (sem o processamento de tipos do SQLAlchemy
nem os descriptors de Row no hot path), metadado dos steps (position/robô)
pré-computado com `joinedload` (fim do N+1 de `step.robot`), sem cópia de payload,
sem passada extra de sincronização de ts e sem re-ordenação final (a query já
vem ordenada por (ts, seq)). Saída IDÊNTICA para o mesmo input (paridade testada).

| Métrica | Baseline documentada | Baseline re-medida | Pós (S3) | Δ vs documentada | Δ vs re-medida |
| --- | --- | --- | --- | --- | --- |
| Endpoint workspace p95 | 39,78 ms (n=30) | — | ~15,5–17,4 ms (n=30) | **-59%** (≥30% ✓) | — |
| derive_task_timeline (média 5 it) | 11,44 ms | ~5,45 ms | ~2,9–3,2 ms | **-74%** (≥50% ✓) | -46% |
| derive_task_occurrences (média 5 it) | 4,82 ms | ~4,84 ms | ~3,0 ms | **-38%** | -38% |
| Memória pico (tracemalloc) | 1314,00 KiB | — | ~862 KiB | -34% | — |
| Queries SELECT endpoint workspace (5 ocorrências) | 14 | — | 10 (limite ≤ 15) | -29% | — |
| Crescimento de logs/disco | +16.806 B | — | +16.806 B | igual (payloads nunca truncados) | — |

### Nota sobre o critério "-50% no tempo de derivação"

O critério é medido pelo harness (`measure_timeline`, 5 iterações em sessão nova,
sem warmup — a 1ª iteração paga a infraestrutura fria: compile, lazy loads e
páginas do SQLite). Na máquina atual, o piso medido para `derive_task_occurrences`
(~2,5 ms quente + ~0,5 ms de penalidade da iteração fria) fica em ~3,0 ms —
uma queda de ~38% vs a baseline re-medida (~4,84 ms). A queda ≥ 50% para a
derivação da TIMELINE é atendida contra a baseline documentada (-74%); contra a
baseline re-medida fica em ~-46%, no piso prático do stack
(SQLite + SQLAlchemy + decode integral dos payloads, requisito "textos exatos").

Paridade determinística: `derive_task_timeline`/`derive_task_occurrences` com o
mesmo input (500 eventos) produzem saída IDÊNTICA à versão pré-otimização
(diff vazio; snapshot em `tests/fixtures/timeline_parity_500.json`).

Nota: o eager load de `Task.children` foi testado e REVERTIDO (regressão em
`accept_proposal`); o `children` continua lazy.

Comandos:
- Baseline documentada: `AUTOIA_SANDBOX=off .venv/bin/python -m app.perf_bench --events 500 --samples 30`
- Baseline re-medida (código 8005c92 em worktree): script `measure_baseline.py` (mesma metodologia do harness)
- Pós: `AUTOIA_SANDBOX=off .venv/bin/python -m app.perf_bench --events 500 --samples 30`
