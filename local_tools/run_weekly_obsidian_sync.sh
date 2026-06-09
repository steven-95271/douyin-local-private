#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ ! -x .venv/bin/python ]; then
  bash local_tools/setup_local.sh
fi

mkdir -p local_tools/obsidian_sync/work/logs
log_file="local_tools/obsidian_sync/work/logs/weekly_sync.log"
config_file="local_tools/obsidian_sync/creators.yaml"

{
  echo
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY START"
  .venv/bin/python local_tools/obsidian_sync/sync.py --config "$config_file"
  .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config "$config_file"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY DONE"
} >> "$log_file" 2>&1
