#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo ".venv not found. Running setup first..."
  bash local_tools/setup_local.sh
fi

exec .venv/bin/python local_tools/extension_server.py --douyin-cookie-file local_tools/douyin_cookie.txt "$@"
