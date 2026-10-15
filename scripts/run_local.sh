#!/bin/bash
# Launcher: run a command in the TrackEverything host environment on this
# machine. The agents work on host state directly — running a repo's test
# suite, reading/editing real checkouts, driving host CLIs.
#
# Setup: PostgreSQL (`db`) and Redis (`redis`) run in Docker
# (docker-compose.yml). Start them first:
#
#   docker compose up -d db redis
#   bash scripts/start_queue.sh          # queue process:   python -m trackeverything.api
#   bash scripts/start_runtime.sh        # runtime process: python -m trackeverything.core.builtin
#   bash scripts/run_local.sh python -   # store maintenance (AGENTS.md runbook):
#                                         # stdin is a Python snippet, run with
#                                         # the same env as the app
#
# Without a command it prints usage and exits non-zero.
# Stop a process with Ctrl-C (SIGINT triggers the clean shutdown path).
#
# Run at most one queue process and one runtime process against the same
# database; nothing enforces it. A second queue process would connect the
# same Discord bot token; a second runtime
# process's startup recovery would close the first one's running work as
# interrupted.
#
# Environment: .env supplies credentials; a variable
# already present in the calling shell wins — including one set to empty,
# which disables that credential for this run (e.g.
# `DISCORD_BOT_TOKEN= GITHUB_TOKEN= bash scripts/run_local.sh` for a quiet
# smoke test).
#
# Host-side side effects:
# - GITHUB_TOKEN set  → env_setup writes an authenticated GitHub insteadOf
#   rule into your real ~/.gitconfig (token visible in that file).
# - ZAI_API_KEY set   → env_setup (re)writes ~/.pi/agent/models.json.
#
# config.py defaults point at this checkout's data/ and config/ directories;
# pre-set DOCS_DIR, REPOS_DIR, REPORTS_DIR, ... to override them.

set -eu

cd "$(dirname "$0")/.."

# Import .env keys that the caller has not set (empty counts as set).
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case $key in ''|'#'*) continue ;; esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < .env
fi

# --- investigation CLI environment ---
# claude talks to the GLM Coding Plan through Z.AI's Anthropic-compatible
# endpoint — same key as the framework LLM, no extra credential. The
# per-request timeout stays well under the review's total deadline
# (REVIEW_TIMEOUT=1800s).
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.z.ai/api/anthropic}"
if [ -n "${ZAI_API_KEY:-}" ]; then
  export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$ZAI_API_KEY}"
fi
export API_TIMEOUT_MS="${API_TIMEOUT_MS:-600000}"

# pi 0.84 requires Node 20+ while this machine's nvm default stays on 18:
# prepend the newest nvm-managed Node 22 (where pi is installed). The nvm
# default alias is deliberately untouched.
_node22_bin=$(ls -d "$HOME"/.nvm/versions/node/v22*/bin 2>/dev/null | sort -V | tail -1)
if [ -n "$_node22_bin" ]; then
  PATH="$_node22_bin:$PATH"
fi
# claude lives in ~/.local/bin (native install) — make sure the app's
# subprocesses resolve it even under a minimal PATH.
if [ -d "$HOME/.local/bin" ]; then
  PATH="$HOME/.local/bin:$PATH"
fi
export PATH
unset _node22_bin

# --- webhook port: the SharedGateway chain owns 8080 ---
# gateway (a7d381c) fronts localhost:8080 (nginx) -> 18081 (auth) and
# forwards to the app at 18080. Native mode must be that 18080 upstream,
# never 8080 — binding 8080 would shadow the gateway front.
export WEBHOOK_PORT="${WEBHOOK_PORT:-18080}"

# --- persistence routing: the Docker db service over loopback ---
: "${TE_DB_PORT:=5432}"
: "${POSTGRES_PASSWORD:=trackeverything}"
export DATABASE_URL="${TE_DATABASE_URL:-postgresql://trackeverything:${POSTGRES_PASSWORD}@127.0.0.1:${TE_DB_PORT}/trackeverything}"
export QUEUE_REDIS_URL="${QUEUE_REDIS_URL:-redis://127.0.0.1:${TE_REDIS_PORT:-6379}/0}"
export QUEUE_NAMESPACE="${QUEUE_NAMESPACE:-trackeverything}"

export PYTHONUNBUFFERED=1

if [ "$#" -gt 0 ]; then
  exec uv run "$@"
fi
echo "usage: $0 COMMAND [ARGS...]  (start the processes with scripts/start_queue.sh / scripts/start_runtime.sh)" >&2
exit 2
