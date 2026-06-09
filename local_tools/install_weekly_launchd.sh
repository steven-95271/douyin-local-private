#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

label="local.douyin-obsidian-weekly"
plist="$HOME/Library/LaunchAgents/${label}.plist"
script="$(pwd)/local_tools/run_weekly_obsidian_sync.sh"
out_log="$(pwd)/local_tools/obsidian_sync/work/logs/weekly_launchd.out.log"
err_log="$(pwd)/local_tools/obsidian_sync/work/logs/weekly_launchd.err.log"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$out_log")"
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
    <integer>1</integer>
    <key>Hour</key>
    <integer>11</integer>
    <key>Minute</key>
    <integer>0</integer>
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

echo "Installed ${label}"
echo "Schedule: every Monday at 11:00 local time"
echo "Plist: ${plist}"
echo "Log: $(pwd)/local_tools/obsidian_sync/work/logs/weekly_sync.log"
