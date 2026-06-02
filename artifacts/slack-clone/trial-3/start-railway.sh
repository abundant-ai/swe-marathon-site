#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d "$SCRIPT_DIR/source" ]; then
  cd "$SCRIPT_DIR/source"
else
  cd "$SCRIPT_DIR"
fi
# DeepSeek V4 Pro (Terminus 2) submission: Node/Express cluster. The verifier ran
# three nodes plus a broadcast hub; for the Railway single-service demo we run the
# hub locally and a single HTTP node bound on 0.0.0.0 so the public domain reaches it.
export HUDDLE_HTTP_HOST="${HUDDLE_HTTP_HOST:-0.0.0.0}"
mkdir -p data
node lib/hub.js 9000 &
exec node server.js 8000
