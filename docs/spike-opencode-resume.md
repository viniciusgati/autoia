# Spike — Resume de sessão no `opencode run` (reuso de contexto/cache)

Investigação da Subtarefa 1 da task #78: confirmar a flag de continuação de
sessão do CLI `opencode run` e o evento JSONL (`--format json`) que expõe o id
da sessão. Ambiente: `opencode 1.18.18` (bun), binário real do host executado
com `HOME`/`XDG_*` apontando para diretório gravável (o `~/.local/state`
default era read-only no sandbox).

## Conclusão

1. **A flag existe**: `opencode run -s, --session <id>` ("session id to
   continue") retoma uma sessão pelo id. Há também `-c, --continue` (última
   sessão) e `--fork` (fork de sessão — exige `--session`/`--continue`). O
   padrão adotado é **`--session <id>`** (o id vem da própria sessão anterior,
   então `--continue` não serve). Sintaxe com valor separado (dois tokens),
   não `--session=<id>`.
2. **O id da sessão aparece no JSONL**: todo evento (`step_start`, `text`,
   `step_finish`, `error`, …) carrega `sessionID` no **topo do objeto** — não
   é preciso um evento especial de sessão (`session_start`/`session_finish`
   continuam fora do parse). Capturar `obj["sessionID"]` na primeira linha
   (o `step_start` é sempre o primeiro evento) já entrega o id.
3. **A retomada funciona e preserva o contexto**: re-executando o MESMO prompt
   curto com `--session <id>`, o `sessionID` permanece o mesmo e o `step_finish`
   reportou `cache.read: 7936` (contra `write: 0` na primeira execução) —
   evidência direta de que o contexto da sessão anterior foi reutilizado (o
   `input` caiu de 6946 para 68 tokens no run de continuação).

## Evidência real

### `opencode run --help` (saída real, versão 1.18.18)

```text
opencode run [message..]

run opencode with a message

Positionals:
  message  message to send                                                     [array] [default: []]

Options:
  -h, --help         show help                                                             [boolean]
  ...
  -c, --continue     continue the last session                                             [boolean]
  -s, --session      session id to continue                                                 [string]
      --fork         fork the session before continuing (requires --continue or --session) [boolean]
  ...
  -m, --model        model to use in the format of provider/model                           [string]
  ...
      --format       format: default (formatted) or json (raw JSON events)
                                          [string] [choices: "default", "json"] [default: "default"]
  ...
      --dir          directory to run in, path on remote server if attaching                [string]
```

Trecho real (recortado; a lista completa inclui `--print-logs`, `--log-level`,
`--pure`, `--command`, `--share`, `--agent`, `-f/--file`, `--title`,
`--attach`, `-p/--password`, `-u/--username`, `--port`, `--variant`,
`--thinking`, `-i/--interactive`, `--auto`).

### JSONL real — onde o id da sessão aparece

Execução: `opencode run "diga apenas: ok" --format json --dir <tmp>`
(3 linhas de saída; `sessionID` no topo de TODAS):

```json
{"type":"step_start","timestamp":1786961360436,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f32762c001y2SdY4N0rhT78L","messageID":"msg_00f326c63001zzZhG1lGnDaE5M","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"step-start"}}
{"type":"text","timestamp":1786961360761,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f327749001mLh6Jip2Kc2HTq","messageID":"msg_00f326c63001zzZhG1lGnDaE5M","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"text","text":"ok","time":{"start":1786961360713,"end":1786961360743}}}
{"type":"step_finish","timestamp":1786961360761,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f32776c001a2kP1F0o9Es6th","reason":"stop","messageID":"msg_00f326c63001zzZhG1lGnDaE5M","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"step-finish","tokens":{"total":7996,"input":6946,"output":4,"reasoning":22,"cache":{"write":0,"read":1024}},"cost":0}}
```

### Retomada real com `--session <id>` (mesma sessão + cache read)

Execução: `opencode run "continue" --session ses_ff0cd9591ffevljv9g1qsD7siL --format json --dir <tmp>`:

```json
{"type":"step_start","timestamp":1786961376125,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f32b378001y1SRxDmyn6jP7m","messageID":"msg_00f329d1d0018nqra0VOcnWQ2V","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"step-start"}}
{"type":"text","timestamp":1786961378704,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f32bccf001w7th8rpKUD73LS","messageID":"msg_00f329d1d0018nqra0VOcnWQ2V","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"text","text":"What would you like me to help with?","time":{"start":1786961378511,"end":1786961378687}}}
{"type":"step_finish","timestamp":1786961378704,"sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","part":{"id":"prt_00f32bd84001y7NzCw6vZrulZg","reason":"stop","messageID":"msg_00f329d1d0018nqra0VOcnWQ2V","sessionID":"ses_ff0cd9591ffevljv9g1qsD7siL","type":"step-finish","tokens":{"total":8126,"input":68,"output":12,"reasoning":110,"cache":{"write":0,"read":7936}},"cost":0}}
```

Observações:

- `sessionID` é **idêntico** ao da sessão original → a continuação retomou a
  MESMA sessão (não criou uma nova).
- `tokens.input` caiu de 6946 → 68 e `tokens.cache.read` subiu para 7936: o
  contexto anterior foi lido do cache (reuso efetivo de contexto).
- No JSONL não há evento `session_start`/`session_finish` neste formato — o id
  vem no topo de cada evento, começando pelo `step_start`.

## Decisão de implementação

- Flag: `--session <id>` (dois tokens), aplicada ao comando
  `opencode run <prompt> --format json --dir <cwd> [--session <id>] [-m <model>]`
  somente quando `resume_session_id` for fornecido (sem ele, comando idêntico
  ao atual).
- Captura: `outcome.session_id = obj["sessionID"]` no topo de cada linha
  JSONL parseada — o campo está em todos os eventos; o `step_start` (primeiro)
  já o entrega. Os tipos `session_*` continuam fora da timeline/guardrails
  (`_JSONL_SKIP_TYPES` inalterado).
- Onde a operadora verifica o resultado: custo da retentativa no Workspace
  (custo real do `step_finish` por ocorrência), o prompt "CONTINUAÇÃO" no chat
  da ocorrência re-executada e o `TaskStep.session_id` na visão técnica (já
  exposto pela timeline/API).
