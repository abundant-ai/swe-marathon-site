"""Cross-node broadcast broker.

Each HTTP node opens a TCP connection to the broker and writes a one-line JSON
notice ({"id": <events.id>, ...}) whenever it commits an event. The broker
re-broadcasts every notice to every connected node. Notices are advisory only:
nodes always read the canonical event from SQLite by id, and a separate poll
loop in the node also tails the events table so a broker outage does not stop
fan-out.

This file holds both the broker server (run as its own process) and a small
client used inside each node.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket


BROKER_HOST = os.environ.get("HUDDLE_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HUDDLE_BROKER_PORT", "9100"))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

async def _server_handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peers: set):
    peers.add(writer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            for w in list(peers):
                if w is writer:
                    continue
                try:
                    w.write(line)
                except Exception:
                    pass
            # Drain only our own writer; peer drains run independently.
    except Exception:
        pass
    finally:
        peers.discard(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_broker():
    peers: set = set()

    async def handler(r, w):
        await _server_handle(r, w, peers)

    server = await asyncio.start_server(handler, BROKER_HOST, BROKER_PORT, reuse_address=True)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Client (used inside nodes)
# ---------------------------------------------------------------------------

class BrokerClient:
    """Persistent line-based client. Reconnects with backoff."""

    def __init__(self, on_notice):
        self.on_notice = on_notice
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = False

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _run(self):
        backoff = 0.2
        while not self._stop:
            try:
                reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
                async with self._lock:
                    self._writer = writer
                backoff = 0.2
                while not self._stop:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        notice = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    try:
                        await self.on_notice(notice)
                    except Exception:
                        pass
            except Exception:
                pass
            async with self._lock:
                self._writer = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)

    async def publish(self, notice: dict):
        async with self._lock:
            w = self._writer
        if w is None:
            return False
        try:
            w.write((json.dumps(notice) + "\n").encode("utf-8"))
            return True
        except Exception:
            return False
