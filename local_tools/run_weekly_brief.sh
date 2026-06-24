#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ ! -x .venv/bin/python ]; then
  bash local_tools/setup_local.sh
fi

log_dir="$HOME/Library/Logs/douyin-local-private"
mkdir -p "$log_dir" local_tools/obsidian_sync/work/logs
log_file="$log_dir/weekly_brief.log"
config_file="local_tools/obsidian_sync/creators.yaml"

{
  echo
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF START"
  .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config "$config_file"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF DONE"
} >> "$log_file" 2>&1
