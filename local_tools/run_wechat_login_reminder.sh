#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log_dir="$HOME/Library/Logs/douyin-local-private"
mkdir -p "$log_dir" local_tools/obsidian_sync/work/logs
log_file="$log_dir/wechat_login_reminder.log"

dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=true
fi

message="公众号后台登录态刷新提醒

请在今天 18:00 左右打开 mp.weixin.qq.com，确认公众号后台处于登录状态。

登录后打开本地面板或 Chrome 插件，同步公众号后台 Cookie/token。

这样今晚 00:00 的公众号增量抓取会更稳；如果忘了刷新，系统仍会继续跑其他平台，并在 11:10 自检里提示公众号待处理。"

prompt="请通过 Telegram 机器人 @Steven_Secretary_bot 把下面这条提醒发送给我。只发送正文，不需要解释。

${message}"

{
  echo
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WECHAT LOGIN REMINDER START"
  if [[ "$dry_run" == "true" ]]; then
    echo "DRY_RUN"
    printf '%s\n' "$prompt"
  elif command -v hermes >/dev/null 2>&1; then
    if hermes --profile secretary -z "$prompt"; then
      echo "HERMES sent"
    else
      echo "HERMES failed"
    fi
  else
    echo "HERMES missing"
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WECHAT LOGIN REMINDER DONE"
} >> "$log_file" 2>&1
