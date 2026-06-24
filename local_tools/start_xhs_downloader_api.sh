#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXTERNAL_DIR="$ROOT_DIR/local_tools/external"
APP_DIR="$EXTERNAL_DIR/XHS-Downloader-src"
ZIP_APP_DIR="$EXTERNAL_DIR/XHS-Downloader-zip/XHS-Downloader-master"
REPO_URL="https://github.com/JoeanAmier/XHS-Downloader.git"
UV_BIN="${UV_BIN:-}"
if [[ -z "$UV_BIN" ]]; then
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    UV_BIN="$(command -v uv)"
  fi
fi
echo "Using uv: $UV_BIN"

mkdir -p "$EXTERNAL_DIR"

if [[ -f "$ZIP_APP_DIR/main.py" ]]; then
  APP_DIR="$ZIP_APP_DIR"
elif [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

cd "$APP_DIR"
echo "Starting XHS-Downloader API in: $APP_DIR"
if [[ -x ".venv/bin/python" ]]; then
  exec ".venv/bin/python" main.py api
fi
exec "$UV_BIN" run --python 3.12 python main.py api
