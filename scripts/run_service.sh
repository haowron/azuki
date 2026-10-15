#!/bin/bash
# queue/runtime の一方だけを監視・再起動する supervisor。
#   bash scripts/run_service.sh queue
#   bash scripts/run_service.sh runtime
# 監視対象は process.py の restart_roles が定義する。対象外の role は停止しない。
# TERM 後は受理済み処理を待ち、RUN_SERVICE_TERM_WAIT 超過時だけ process group を強制終了する。

set -u
cd "$(dirname "$0")/.."
SERVICE_ROOT="$(pwd)"

# launchd supplies a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin): resolve uv
# wherever it lives before run_local.sh needs it, and keep the Homebrew bin
# dirs reachable — the pure-psycopg build locates libpq through `pg_config`
# (Homebrew postgresql@14), which the minimal launchd PATH cannot see.
for candidate in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  case ":$PATH:" in
    *":$candidate:"*) ;;
    *) [ -d "$candidate" ] && PATH="$candidate:$PATH" ;;
  esac
done
export PATH
command -v uv >/dev/null 2>&1 || {
  echo "$(date '+%F %T') run_service: FATAL uv not found on PATH" >&2
  exit 1
}

ROLE="${1:-}"
case "$ROLE" in
  queue|runtime) ;;
  *) echo "usage: $0 queue|runtime" >&2; exit 2 ;;
esac
SERVICE_PYTHON="${TE_SERVICE_PYTHON:-$SERVICE_ROOT/.venv/bin/python}"
if [ ! -x "$SERVICE_PYTHON" ]; then
  uv sync --frozen
fi

POLL="${RUN_SERVICE_POLL:-180}"
TERM_WAIT="${RUN_SERVICE_TERM_WAIT:-3600}"

log() { echo "$(date '+%F %T') run_service[$ROLE]: $*"; }

# Fingerprint of the Host-owned inputs a restart should pick up: path and
# modification time of every Host .py file plus the files that change the
# runtime environment. The five version-owned folders are the app's job.
host_state() {
  "$SERVICE_PYTHON" "$SERVICE_ROOT/src/trackeverything/process.py" "$ROLE" "$SERVICE_ROOT"
}

child=""

stop_group() {
  local pid="$1" i=0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt "$TERM_WAIT" ]; do
    sleep 1
    i=$((i + 1))
  done
  kill -KILL -- "-$pid" 2>/dev/null
  kill -KILL "$pid" 2>/dev/null
}

cleanup() {
  trap - INT TERM
  if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
    log "supervisor stopping; shutting down app ($child)"
    stop_group "$child"
    wait "$child" 2>/dev/null
  fi
  exit 0
}
trap cleanup INT TERM

# Job control on: each app run gets its own process group so TERM reaches the
# whole tree (uv, python, and any agent subprocesses the app spawned).
set -m

while true; do
  state="$(host_state)"
  log "starting app (code fingerprint $(printf %s "$state" | cut -c1-12))"
  bash scripts/start_$ROLE.sh &
  child=$!

  restart=0
  while kill -0 "$child" 2>/dev/null; do
    sleep "$POLL"
    kill -0 "$child" 2>/dev/null || break
    if [ "$(host_state)" != "$state" ]; then
      sleep 2
      if [ "$(host_state)" != "$state" ]; then
        log "Host-owned files changed under app pid $child; draining and restarting"
        stop_group "$child"
        restart=1
        break
      fi
    fi
  done

  wait "$child" 2>/dev/null
  rc=$?
  child=""
  if [ "$restart" -eq 1 ]; then
    sleep 1
  else
    log "app exited on its own (rc=$rc); relaunching in 5s"
    sleep 5
  fi
done
