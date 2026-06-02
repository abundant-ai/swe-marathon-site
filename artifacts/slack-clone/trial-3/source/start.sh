#!/bin/bash
set -e

cd /app

echo "Starting Huddle services..."

# Start the cross-node broadcast hub
node lib/hub.js 9000 &
HUB_PID=$!
echo "Hub PID: $HUB_PID"

sleep 0.5

# Start the three HTTP nodes
node server.js 8000 &
NODE0_PID=$!
echo "Node 0 PID: $NODE0_PID"

node server.js 8001 &
NODE1_PID=$!
echo "Node 1 PID: $NODE1_PID"

node server.js 8002 &
NODE2_PID=$!
echo "Node 2 PID: $NODE2_PID"

# Start the IRC gateway
node irc.js &
IRC_PID=$!
echo "IRC PID: $IRC_PID"

# Handle termination
cleanup() {
  echo "Shutting down..."
  kill $HUB_PID 2>/dev/null || true
  kill $NODE0_PID 2>/dev/null || true
  kill $NODE1_PID 2>/dev/null || true
  kill $NODE2_PID 2>/dev/null || true
  kill $IRC_PID 2>/dev/null || true
  wait
  exit 0
}

trap cleanup SIGTERM SIGINT

# Auto-restart killed processes
while true; do
  sleep 2

  # Check and restart hub
  if ! kill -0 $HUB_PID 2>/dev/null; then
    echo "Respawning hub..."
    node lib/hub.js 9000 &
    HUB_PID=$!
  fi

  # Check and restart node 0
  if ! kill -0 $NODE0_PID 2>/dev/null; then
    echo "Respawning node 0..."
    node server.js 8000 &
    NODE0_PID=$!
  fi

  # Check and restart node 1
  if ! kill -0 $NODE1_PID 2>/dev/null; then
    echo "Respawning node 1..."
    node server.js 8001 &
    NODE1_PID=$!
  fi

  # Check and restart node 2
  if ! kill -0 $NODE2_PID 2>/dev/null; then
    echo "Respawning node 2..."
    node server.js 8002 &
    NODE2_PID=$!
  fi

  # Check and restart IRC
  if ! kill -0 $IRC_PID 2>/dev/null; then
    echo "Respawning IRC..."
    node irc.js &
    IRC_PID=$!
  fi
done
