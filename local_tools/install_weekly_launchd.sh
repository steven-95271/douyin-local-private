#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p "$HOME/Library/LaunchAgents" local_tools/obsidian_sync/work/logs

unload_label() {
  local label="$1"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
}

install_job() {
  local label="$1"
  local script="$2"
  local weekday="$3"
  local hour="$4"
  local minute="$5"
  local out_log="$6"
  local err_log="$7"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"

  chmod +x "$script"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${script}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(pwd)</string>
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

root="$(pwd)"
install_job \
  "local.douyin-obsidian-content-sync" \
  "${root}/local_tools/run_weekly_content_sync.sh" \
  0 22 0 \
  "${root}/local_tools/obsidian_sync/work/logs/weekly_content_launchd.out.log" \
  "${root}/local_tools/obsidian_sync/work/logs/weekly_content_launchd.err.log"

install_job \
  "local.douyin-obsidian-weekly-brief" \
  "${root}/local_tools/run_weekly_brief.sh" \
  1 11 0 \
  "${root}/local_tools/obsidian_sync/work/logs/weekly_brief_launchd.out.log" \
  "${root}/local_tools/obsidian_sync/work/logs/weekly_brief_launchd.err.log"

echo "Content sync: every Sunday at 22:00 local time"
echo "Weekly brief: every Monday at 11:00 local time"
echo "Content log: ${root}/local_tools/obsidian_sync/work/logs/weekly_content_sync.log"
echo "Brief log: ${root}/local_tools/obsidian_sync/work/logs/weekly_brief.log"
