#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/source" ]; then
  cd "$SCRIPT_DIR/source"
else
  cd "$SCRIPT_DIR"
fi
# Codex / GPT-5.5 submission: single-file stdlib HTTP server. The verifier ran
# three nodes on 127.0.0.1:8000-8002; for the Railway single-service demo we run
# only node 0 and bind it on 0.0.0.0 so the public domain can reach it.
export HUDDLE_HTTP_HOST="${HUDDLE_HTTP_HOST:-0.0.0.0}"
mkdir -p /app/data
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi
exec "$PYTHON_BIN" -u server.py http 0
