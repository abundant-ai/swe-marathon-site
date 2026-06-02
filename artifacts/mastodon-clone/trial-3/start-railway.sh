#!/bin/sh
set -eu
cd /app
export PYTHONPATH=/app
mkdir -p /app/data /app/data/media
# Idempotent sample-data seeding (alice et al.) before serving.
python3 -m chirp.seed || true
exec python3 -m uvicorn railway_app:app --host 0.0.0.0 --port "${PORT:-8000}" --log-level warning
