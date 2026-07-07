#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ ! -x .venv/bin/python ]; then
  bash local_tools/setup_local.sh
fi

log_dir="$HOME/Library/Logs/douyin-local-private"
mkdir -p "$log_dir" local_tools/obsidian_sync/work/logs
log_file="$log_dir/daily_health_check.log"
config_file="local_tools/obsidian_sync/creators.yaml"

{
  echo
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY HEALTH CHECK START"
  .venv/bin/python local_tools/obsidian_sync/daily_health_check.py --config "$config_file"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY HEALTH CHECK DONE"
} >> "$log_file" 2>&1
