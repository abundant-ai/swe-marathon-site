#!/bin/sh
set -eu
cd /app
mkdir -p /app/data
export PYTHONPATH=/app
# halyard/server.py reads $PORT and binds 0.0.0.0 (waitress, werkzeug fallback).
exec python3 -m halyard.server
