#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

launchd_log_dir="$HOME/Library/Logs/douyin-local-private"
mkdir -p "$HOME/Library/LaunchAgents" "$launchd_log_dir" local_tools/obsidian_sync/work/logs

unload_label() {
  local label="$1"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
}

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  printf '%s' "$value"
}

install_job() {
  local label="$1"
  local command="$2"
  local weekday="$3"
  local hour="$4"
  local minute="$5"
  local out_log="$6"
  local err_log="$7"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"

  local escaped_command
  escaped_command="$(xml_escape "$command")"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>${escaped_command}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>${weekday}</integer>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${out_log}</string>
  <key>StandardErrorPath</key>
  <string>${err_log}</string>
</dict>
</plist>
PLIST

  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  launchctl enable "gui/$(id -u)/${label}"
  echo "Installed ${label}: weekday=${weekday} ${hour}:$(printf '%02d' "$minute")"
}

# Remove the old combined job if it was installed before the split schedule.
unload_label "local.douyin-obsidian-weekly"
unload_label "local.douyin-obsidian-content-sync"

root="$(pwd)"
content_command="cd '${root}' && export PATH=\"\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC START\"; .venv/bin/python local_tools/obsidian_sync/sync.py --config local_tools/obsidian_sync/creators.yaml; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC DONE\"; } >> '${launchd_log_dir}/weekly_content_sync.log' 2>&1"
brief_command="cd '${root}' && export PATH=\"\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF START\"; .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config local_tools/obsidian_sync/creators.yaml; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF DONE\"; } >> '${launchd_log_dir}/weekly_brief.log' 2>&1"

install_job \
  "local.douyin-obsidian-content-sync-monday" \
  "${content_command}" \
  1 6 0 \
  "${launchd_log_dir}/weekly_content_launchd_monday.out.log" \
  "${launchd_log_dir}/weekly_content_launchd_monday.err.log"

install_job \
  "local.douyin-obsidian-content-sync-wednesday" \
  "${content_command}" \
  3 6 0 \
  "${launchd_log_dir}/weekly_content_launchd_wednesday.out.log" \
  "${launchd_log_dir}/weekly_content_launchd_wednesday.err.log"

install_job \
  "local.douyin-obsidian-weekly-brief" \
  "${brief_command}" \
  1 11 0 \
  "${launchd_log_dir}/weekly_brief_launchd.out.log" \
  "${launchd_log_dir}/weekly_brief_launchd.err.log"

echo "Content sync: every Monday and Wednesday at 06:00 local time"
echo "Weekly brief: every Monday at 11:00 local time"
echo "Content log: ${launchd_log_dir}/weekly_content_sync.log"
echo "Brief log: ${launchd_log_dir}/weekly_brief.log"
echo "Launchd log dir: ${launchd_log_dir}"
