#!/bin/bash
# 引数なしは開発 checkout の main、--remote は GitHub の main を配備する。
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run --no-sync --project "$REPO_DIR" python "$REPO_DIR/scripts/deploy.py" "$@"
