"""Tabula HTTP / WebSocket server."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
from typing import Any

from aiohttp import web

from .api import build_app


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), access_log=None)


if __name__ == "__main__":
    main()
