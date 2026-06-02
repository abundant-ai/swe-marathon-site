#!/bin/sh
set -eu
cd /app
mkdir -p /app/data /app/data/media
export CHIRP_DB_PATH="${CHIRP_DB_PATH:-/app/data/chirp.sqlite3}"
exec python3 -m uvicorn railway_app:app --host 0.0.0.0 --port "${PORT:-8000}"
