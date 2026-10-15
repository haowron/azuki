#!/bin/bash
# queue プロセス(HTTP・受信接続)を前面で起動する。
# 環境変数と .env の扱いは run_local.sh に従う。
set -eu
cd "$(dirname "$0")/.."
exec bash scripts/run_local.sh python -m trackeverything.api
