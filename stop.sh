#!/usr/bin/env bash
# Derruba TUDO do autoia — o espelho do `start.sh` (que só sobe).
#
# O que ele encerra, nesta ordem:
#   1. Serviços: autoia-api, autoia-worker (pai + filhos), autoia-chamado-worker,
#      autoia-chat-worker — via `autoia-stop` (SIGTERM -> SIGKILL, com o handler
#      de shutdown limpo do worker: mata os executores e NÃO grava falha fake).
#   2. Frontend dev (npm run dev / vite / esbuild) iniciado pelo start.sh.
#   3. Órfãos dos robôs: kimi/opencode/codex com cwd no workspace, emuladores
#      Android, gradle daemons, `next-server`/servidores que os robôs deixaram
#      rodando dentro de `data/workspaces/<repo>/task_*`.
#   4. Contêineres do sandbox (cidfiles) + marcadores `.stop-*` e heartbeats
#      (feito pelo `autoia-stop`; repetido aqui como fallback sem venv).
#   5. Arquivos de PID do `start.sh`.
#
# Uso:
#   ./stop.sh          # derruba tudo e confere o que sobrou
#   ./stop.sh --quiet  # só o resumo final
#
# Nunca mata processos do usuário fora do autoia: executores LLM (kimi/opencode/
# codex) só contam como órfão quando o cwd está DENTRO de `data/workspaces`, e o
# frontend é casado pelo path absoluto do repo.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv/bin/activate"
PIDS="$ROOT/data/.start.pids"
WORKSPACES="$ROOT/data/workspaces"
QUIET=0
[[ "${1:-}" == "--quiet" || "${1:-}" == "-q" ]] && QUIET=1

say() { [[ $QUIET -eq 1 ]] || echo "$@"; }

# ---------------------------------------------------------------- serviços ---
stop_services() {
  if [[ -x "$ROOT/.venv/bin/autoia-stop" ]]; then
    say "parando serviços + órfãos + sandbox (autoia-stop)..."
    "$ROOT/.venv/bin/autoia-stop" 2>&1 | sed 's/^/  /'
  else
    say "autoia-stop não encontrado no venv; matando serviços por cmdline..."
    pkill -TERM -f '/autoia-(api|worker|chamado-worker|chat-worker)( |$)' 2>/dev/null || true
    sleep 5
    pkill -KILL -f '/autoia-(api|worker|chamado-worker|chat-worker)( |$)' 2>/dev/null || true
  fi
}

# --------------------------------------------------------------- frontend ----
# O `nohup npm run dev &` do start.sh nasce no MESMO process group do start.sh
# (não dá pra killar o grupo), então casamos pelo PID gravado + cmdline com o
# path absoluto do repo, e varremos os filhos (vite/esbuild) por path também.
stop_frontend() {
  local pidfile="$PIDS/frontend.pid"
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    say "parando frontend dev (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null || true
  fi

  # Qualquer resto que aponte pro frontend deste repo (vite, esbuild, npm run dev).
  local pat="(^|[ /])$ROOT/frontend/"
  local p
  for p in $(pgrep -f 'npm run dev|node_modules/.bin/vite|@esbuild/|esbuild --service' 2>/dev/null); do
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    if tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" | grep -qE "$pat"; then
      kill -TERM "$p" 2>/dev/null || true
    fi
  done
  sleep 2
  for p in $(pgrep -f 'npm run dev|node_modules/.bin/vite|@esbuild/|esbuild --service' 2>/dev/null); do
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    if tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" | grep -qE "$pat"; then
      kill -KILL "$p" 2>/dev/null || true
    fi
  done
}

# ------------------------------------------------- órfãos com cwd no ws ------
# Rede de proteção (vale mesmo sem venv): processos que os robôs lançaram e que
# sobrevivem ao worker — `next-server`, servidores fake, emulador, gradle.
workspace_orphans() {
  local p cwd
  for p in /proc/[0-9]*; do
    p="${p#/proc/}"
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    cwd="$(readlink "/proc/$p/cwd" 2>/dev/null || true)"
    case "$cwd" in
      "$WORKSPACES"|"$WORKSPACES"/*)
        if kill -0 "$p" 2>/dev/null; then
          say "  órfão (cwd em workspace) PID $p: $(tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" | cut -c1-80)"
          kill -TERM "$p" 2>/dev/null || true
        fi
        ;;
    esac
  done
  sleep 3
  for p in /proc/[0-9]*; do
    p="${p#/proc/}"
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    cwd="$(readlink "/proc/$p/cwd" 2>/dev/null || true)"
    case "$cwd" in
      "$WORKSPACES"|"$WORKSPACES"/*) kill -KILL "$p" 2>/dev/null || true ;;
    esac
  done
}

# -------------------------------------------------------- confirmação final --
remaining() {
  local out="" p cwd cmd
  for p in /proc/[0-9]*; do
    p="${p#/proc/}"
    [[ "$p" == "$$" || "$p" == "$PPID" ]] && continue
    # Redireciona o stderr ANTES do arquivo: se o processo morrer entre o glob
    # e a leitura, o bash não imprime "Arquivo ou diretório inexistente".
    cmd="$(tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" || true)" || continue
    [[ -z "$cmd" ]] && continue
    cwd="$(readlink "/proc/$p/cwd" 2>/dev/null || true)" || continue
    case "$cmd" in
      *autoia-api*|*autoia-worker*|*autoia-chamado-worker*|*autoia-chat-worker*)
        out+="$p  serviço: ${cmd:0:90}"$'\n' ;;
      *"$ROOT/frontend/"*|*"$ROOT/frontend "*)
        out+="$p  frontend: ${cmd:0:90}"$'\n' ;;
    esac
    case "$cwd" in
      "$WORKSPACES"|"$WORKSPACES"/*)
        out+="$p  workspace: ${cmd:0:90}"$'\n' ;;
    esac
  done
  printf '%s' "$out"
}

# ----------------------------------------------------------------- execução --
stop_services
stop_frontend
workspace_orphans

# PID files do start.sh: sem isso o próximo `./start.sh` acha serviço morto.
if [[ -d "$PIDS" ]]; then
  rm -f "$PIDS"/*.pid
  say "PID files removidos ($PIDS)."
fi

leftover="$(remaining)"
if [[ -z "$leftover" ]]; then
  echo "autoia parado — nada sobrou rodando (serviços, frontend, robôs e workspaces)."
else
  echo "ATENÇÃO — ainda há processos do autoia rodando:"
  printf '%s' "$leftover"
  exit 1
fi
