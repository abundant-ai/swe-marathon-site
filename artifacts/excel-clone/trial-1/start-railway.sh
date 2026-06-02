#!/bin/sh
set -eu
cd /app
mkdir -p /app/data
# server/__main__.py binds 0.0.0.0 on $PORT (Railway-injected, defaults to 8000).
exec python3 -u -m server
