#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  bash local_tools/setup_local.sh
fi

.venv/bin/python -m pip install -r requirements-obsidian.txt

echo "Obsidian sync dependencies are ready."
