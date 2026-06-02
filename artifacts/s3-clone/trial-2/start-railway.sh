#!/bin/sh
set -eu
cd /app
mkdir -p /app/data
# halyard.py binds 0.0.0.0 on $PORT (Railway-injected, defaults to 8000).
exec python3 -u halyard.py
