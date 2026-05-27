#!/bin/bash
cd /app
exec /opt/venv/bin/python3 -u -m server.supervisor
