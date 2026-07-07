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
  local interval_xml
  escaped_command="$(xml_escape "$command")"
  if [[ -n "$weekday" ]]; then
    interval_xml="    <key>Weekday</key>
    <integer>${weekday}</integer>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>"
  else
    interval_xml="    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>"
  fi
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
${interval_xml}
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
  if [[ -n "$weekday" ]]; then
    echo "Installed ${label}: weekday=${weekday} ${hour}:$(printf '%02d' "$minute")"
  else
    echo "Installed ${label}: daily ${hour}:$(printf '%02d' "$minute")"
  fi
}

# Remove old content-sync labels before installing the current schedule.
unload_label "local.douyin-obsidian-weekly"
unload_label "local.douyin-obsidian-content-sync"
unload_label "local.douyin-obsidian-content-sync-monday"
unload_label "local.douyin-obsidian-content-sync-wednesday"
unload_label "local.douyin-obsidian-content-sync-daily"
unload_label "local.douyin-obsidian-content-sync-stop"
unload_label "local.douyin-obsidian-daily-brief"
unload_label "local.douyin-obsidian-daily-health-check"
unload_label "local.douyin-obsidian-wechat-login-reminder-monday"
unload_label "local.douyin-obsidian-wechat-login-reminder-wednesday"
unload_label "local.douyin-obsidian-wechat-login-reminder-friday"

root="$(pwd)"
daily_limit_per_source=20
daily_recent_days=3
content_command="cd '${root}' && export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC START limit=${daily_limit_per_source} recent_days=${daily_recent_days}\"; .venv/bin/python local_tools/obsidian_sync/sync.py --config local_tools/obsidian_sync/creators.yaml --limit ${daily_limit_per_source} --recent-days ${daily_recent_days}; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC DONE\"; } >> '${launchd_log_dir}/content_sync.log' 2>&1; { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY BRIEF START\"; .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config local_tools/obsidian_sync/creators.yaml --period daily; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY BRIEF DONE\"; } >> '${launchd_log_dir}/daily_brief.log' 2>&1"
stop_command="cd '${root}' && export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && if pgrep -f 'local_tools/obsidian_sync/sync.py --config local_tools/obsidian_sync/creators.yaml' >/dev/null 2>&1; then { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC WINDOW STOP\"; launchctl stop local.douyin-obsidian-content-sync-daily || true; sleep 3; .venv/bin/python local_tools/obsidian_sync/mark_sync_stopped.py --config local_tools/obsidian_sync/creators.yaml --reason '11:00 自动暂停'; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] CONTENT SYNC WINDOW STOP DONE\"; } >> '${launchd_log_dir}/content_sync_window.log' 2>&1; { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY BRIEF START after_window_stop\"; .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config local_tools/obsidian_sync/creators.yaml --period daily; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY BRIEF DONE after_window_stop\"; } >> '${launchd_log_dir}/daily_brief.log' 2>&1; else { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] NO CONTENT SYNC RUNNING AT WINDOW STOP\"; } >> '${launchd_log_dir}/content_sync_window.log' 2>&1; fi"
health_command="cd '${root}' && export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY HEALTH CHECK START\"; .venv/bin/python local_tools/obsidian_sync/daily_health_check.py --config local_tools/obsidian_sync/creators.yaml; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] DAILY HEALTH CHECK DONE\"; } >> '${launchd_log_dir}/daily_health_check.log' 2>&1"
wechat_reminder_command="cd '${root}' && export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && bash local_tools/run_wechat_login_reminder.sh"
brief_command="cd '${root}' && export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\" && mkdir -p '${launchd_log_dir}' local_tools/obsidian_sync/work/logs && { echo; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF START\"; .venv/bin/python local_tools/obsidian_sync/weekly_brief.py --config local_tools/obsidian_sync/creators.yaml; echo \"[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] WEEKLY BRIEF DONE\"; } >> '${launchd_log_dir}/weekly_brief.log' 2>&1"

install_job \
  "local.douyin-obsidian-content-sync-daily" \
  "${content_command}" \
  "" 0 0 \
  "${launchd_log_dir}/content_sync_launchd_daily.out.log" \
  "${launchd_log_dir}/content_sync_launchd_daily.err.log"

install_job \
  "local.douyin-obsidian-content-sync-stop" \
  "${stop_command}" \
  "" 11 0 \
  "${launchd_log_dir}/content_sync_window.out.log" \
  "${launchd_log_dir}/content_sync_window.err.log"

install_job \
  "local.douyin-obsidian-daily-health-check" \
  "${health_command}" \
  "" 11 10 \
  "${launchd_log_dir}/daily_health_check.out.log" \
  "${launchd_log_dir}/daily_health_check.err.log"

install_job \
  "local.douyin-obsidian-wechat-login-reminder-monday" \
  "${wechat_reminder_command}" \
  1 18 0 \
  "${launchd_log_dir}/wechat_login_reminder_monday.out.log" \
  "${launchd_log_dir}/wechat_login_reminder_monday.err.log"

install_job \
  "local.douyin-obsidian-wechat-login-reminder-wednesday" \
  "${wechat_reminder_command}" \
  3 18 0 \
  "${launchd_log_dir}/wechat_login_reminder_wednesday.out.log" \
  "${launchd_log_dir}/wechat_login_reminder_wednesday.err.log"

install_job \
  "local.douyin-obsidian-wechat-login-reminder-friday" \
  "${wechat_reminder_command}" \
  5 18 0 \
  "${launchd_log_dir}/wechat_login_reminder_friday.out.log" \
  "${launchd_log_dir}/wechat_login_reminder_friday.err.log"

install_job \
  "local.douyin-obsidian-weekly-brief" \
  "${brief_command}" \
  1 11 0 \
  "${launchd_log_dir}/weekly_brief_launchd.out.log" \
  "${launchd_log_dir}/weekly_brief_launchd.err.log"

echo "Content sync: every day at 00:00 local time, max ${daily_limit_per_source} items per source from the last ${daily_recent_days} days"
echo "Content sync stop window: every day at 11:00 local time"
echo "Daily brief: after the daily content sync finishes"
echo "Daily health check: every day at 11:10 local time"
echo "WeChat MP login reminder: Monday, Wednesday, Friday at 18:00 local time"
echo "Weekly brief: every Monday at 11:00 local time"
echo "Content log: ${launchd_log_dir}/content_sync.log"
echo "Window stop log: ${launchd_log_dir}/content_sync_window.log"
echo "Daily brief log: ${launchd_log_dir}/daily_brief.log"
echo "Daily health check log: ${launchd_log_dir}/daily_health_check.log"
echo "WeChat MP login reminder log: ${launchd_log_dir}/wechat_login_reminder.log"
echo "Brief log: ${launchd_log_dir}/weekly_brief.log"
echo "Launchd log dir: ${launchd_log_dir}"
