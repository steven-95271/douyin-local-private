#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  bash local_tools/setup_local.sh
fi

exec .venv/bin/python local_tools/obsidian_sync/dashboard.py "$@"
