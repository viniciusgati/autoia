#!/usr/bin/env bash
# Sobe o projeto autoia (API + worker + frontend).
# Uso:
#   ./start.sh                 # API + worker (3 threads) + frontend dev (:5173)
#   AUTOIA_WORKERS=5 ./start.sh  # número de workers paralelos (default 3)
#   ./start.sh api             # só a API
#   ./start.sh worker          # só o worker
#   ./start.sh frontend        # frontend dev (:5173)
#   ./start.sh stop            # derruba tudo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv/bin/activate"
LOGS="$ROOT/data/logs"
PIDS="$ROOT/data/.start.pids"
PID_API="$PIDS/api.pid"
PID_WORKER="$PIDS/worker.pid"
PID_FRONT="$PIDS/frontend.pid"
WORKERS="${AUTOIA_WORKERS:-3}"

if [[ ! -f "$VENV" ]]; then
  echo "venv não encontrado. Rode: python3 -m venv .venv && . .venv/bin/activate && pip install -e \".[dev]\""
  exit 1
fi

mkdir -p "$LOGS" "$PIDS"

source "$VENV"

# PATH sane: garante `docker`, `kimi`, `opencode` e toolchains mesmo quando o
# processo pai subiu numa sessão com PATH corrompido (ex.: display-manager).
HOME_DIR="${HOME:-$HOME}"
NVM_BIN="$(compgen -G "$HOME_DIR/.nvm/versions/node/*/bin" 2>/dev/null | sort -V | tail -n 1 || true)"
export PATH="$HOME_DIR/.kimi-code/bin${NVM_BIN:+:$NVM_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

start_api() {
  if [[ -f "$PID_API" ]] && kill -0 "$(cat "$PID_API")" 2>/dev/null; then
    echo "API já está rodando (PID $(cat "$PID_API"))"
  else
    echo "Iniciando API (:9000)..."
    nohup autoia-api > "$LOGS/api.log" 2>&1 &
    echo $! > "$PID_API"
  fi
}

start_worker() {
  if [[ -f "$PID_WORKER" ]] && kill -0 "$(cat "$PID_WORKER")" 2>/dev/null; then
    echo "Worker já está rodando (PID $(cat "$PID_WORKER"))"
  else
    echo "Iniciando worker ($WORKERS processo(s))..."
    nohup autoia-worker --workers "$WORKERS" > "$LOGS/worker.log" 2>&1 &
    echo $! > "$PID_WORKER"
  fi
}

start_frontend() {
  if [[ -f "$PID_FRONT" ]] && kill -0 "$(cat "$PID_FRONT")" 2>/dev/null; then
    echo "Frontend já está rodando (PID $(cat "$PID_FRONT"))"
  else
    echo "Iniciando frontend dev (:5173)..."
    nohup npm --prefix "$ROOT/frontend" run dev > "$LOGS/frontend.log" 2>&1 &
    echo $! > "$PID_FRONT"
  fi
}

stop() {
  for f in "$PID_API" "$PID_WORKER" "$PID_FRONT"; do
    if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
      echo "Parando $(basename "$f") (PID $(cat "$f"))"
      kill "$(cat "$f")"
    fi
    rm -f "$f"
  done
  echo "Tudo parado."
}

MODE="${1:-all}"

case "$MODE" in
  all)
    start_api
    start_worker
    start_frontend
    echo "Frontend dev em http://localhost:5173 | API em http://localhost:9000"
    ;;
  api)
    start_api
    ;;
  worker)
    start_worker
    ;;
  frontend)
    start_frontend
    ;;
  stop)
    stop
    ;;
  *)
    echo "Modo desconhecido: $MODE"
    echo "Uso: $0 [all|api|worker|frontend|stop]"
    exit 1
    ;;
esac
