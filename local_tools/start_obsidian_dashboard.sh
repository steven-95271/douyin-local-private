#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ ! -x .venv/bin/python3 ]; then
  bash local_tools/setup_local.sh
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dashboard start: $(pwd)" >&2
exec .venv/bin/python3 -u local_tools/obsidian_sync/dashboard.py "$@"
