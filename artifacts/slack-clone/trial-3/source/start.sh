#!/usr/bin/env bash
# Foreground supervisor for Huddle.
set -uo pipefail

cd "$(dirname "$0")"

mkdir -p /app/data/uploads

export PYTHONUNBUFFERED=1
export PYTHONPATH=/app
export HUDDLE_DB="${HUDDLE_DB:-/app/data/huddle.db}"
export HUDDLE_UPLOADS="${HUDDLE_UPLOADS:-/app/data/uploads}"
export HUDDLE_RELAY_HOST="${HUDDLE_RELAY_HOST:-127.0.0.1}"
export HUDDLE_RELAY_PORT="${HUDDLE_RELAY_PORT:-8500}"

PY=/opt/venv/bin/python
if [[ ! -x "$PY" ]]; then PY=$(command -v python3); fi

exec "$PY" -m server.supervisor
