#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/source" ]; then
  cd "$SCRIPT_DIR/source"
else
  cd "$SCRIPT_DIR"
fi
export HUDDLE_DATA_DIR="${HUDDLE_DATA_DIR:-/app/data}"
export HUDDLE_HTTP_HOST="${HUDDLE_HTTP_HOST:-0.0.0.0}"
export HUDDLE_HTTP_BASE_PORT="${PORT:-8000}"
export HUDDLE_BROKER_HOST="${HUDDLE_BROKER_HOST:-127.0.0.1}"
export HUDDLE_BROKER_PORT="${HUDDLE_BROKER_PORT:-9100}"
export HUDDLE_IRC_HOST="${HUDDLE_IRC_HOST:-0.0.0.0}"
export HUDDLE_IRC_PORT="${HUDDLE_IRC_PORT:-6667}"
mkdir -p "$HUDDLE_DATA_DIR/files"
PYTHON_BIN="${PYTHON_BIN:-python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python3
fi
exec "$PYTHON_BIN" -u -m server.supervisor
