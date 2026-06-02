#!/bin/sh
set -eu
cd /app
mkdir -p /app/data
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
