#!/bin/bash
# Foreground launcher for the Huddle chat cluster.
set -e
cd "$(dirname "$0")"
export HUDDLE_DATA_DIR="${HUDDLE_DATA_DIR:-/app/data}"
mkdir -p "$HUDDLE_DATA_DIR/files"
exec /opt/venv/bin/python3 -u -m app.supervisor
