#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f local_tools/urls.txt ]; then
  cp local_tools/urls.example.txt local_tools/urls.txt
fi

if [ ! -f local_tools/douyin_cookie.txt ]; then
  cp local_tools/douyin_cookie.example.txt local_tools/douyin_cookie.txt
fi

echo "Ready. Activate with: source .venv/bin/activate"
