#!/bin/sh
set -eu
cd /app
mkdir -p /app/data /app/data/objects /app/data/parts /app/data/tmp
export PYTHONPATH=/app
exec gunicorn \
  --workers 1 \
  --worker-class gthread \
  --threads 64 \
  --bind "0.0.0.0:${PORT:-8000}" \
  --timeout 300 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --access-logfile - \
  --error-logfile - \
  --log-level warning \
  halyard.app:app
