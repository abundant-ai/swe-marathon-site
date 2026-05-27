"""Per-process bus that delivers durable channel events to local subscribers.

Each node has one :class:`Bus`. It:
- Keeps a TCP connection to the broker (``127.0.0.1:9100``); when the
  connection is up, every node publishes events to the broker which fans
  them out to all other nodes.
- Falls back to polling the ``events`` table in SQLite when the broker is
  unreachable, so cross-node fan-out continues even while the broker is
  being restarted.
- Locally, dispatches frames to any registered async listeners (the
  WebSocket subscribers and the IRC gateway).
"""
import asyncio
import json
import os
import time

from . import store

BROKER_HOST = os.environ.get("HUDDLE_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HUDDLE_BROKER_PORT", "9100"))


class Bus:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self._listeners = []  # list[callable(frame: dict)]
        self._broker_writer: asyncio.StreamWriter | None = None
        self._delivered = set()  # set[(channel_id, seq)] dedup window
        self._delivered_q: list[tuple[int, int]] = []
        # Track highest event_id we've already locally delivered, for the
        # polling fallback.
        self._last_event_id = 0
        self._init_last_event_id()
        # A monotonic guard to avoid duplicating broker frames against poll
        # frames: if both arrive, the second one is dropped.

    def _init_last_event_id(self):
        conn = store.connect()
        try:
            row = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
            if row and row["m"] is not None:
                self._last_event_id = row["m"]
        finally:
            conn.close()

    def add_listener(self, cb):
        self._listeners.append(cb)

    def remove_listener(self, cb):
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    async def start(self):
        asyncio.create_task(self._broker_loop())
        asyncio.create_task(self._poll_loop())

    async def _broker_loop(self):
        while True:
            try:
                reader, writer = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
                self._broker_writer = writer
                # Send a hello so the broker can identify us (not strictly used).
                writer.write((json.dumps({"hello": self.node_id}) + "\n").encode("utf-8"))
                await writer.drain()
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        frame = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    self._dispatch(frame)
            except Exception:
                pass
            self._broker_writer = None
            await asyncio.sleep(0.5)

    async def _poll_loop(self):
        """Tail the events table so fan-out keeps working while the broker is
        down. Operates at ~500ms which keeps fan-out under the 5-second SLA.
        """
        while True:
            try:
                conn = store.connect()
                try:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 500",
                        (self._last_event_id,),
                    ).fetchall()
                finally:
                    conn.close()
                for r in rows:
                    self._last_event_id = max(self._last_event_id, r["id"])
                    frame = store.event_to_frame(r)
                    self._dispatch(frame)
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def _dispatch(self, frame: dict):
        key = (frame.get("channel_id"), frame.get("seq"))
        if key in self._delivered:
            return
        self._delivered.add(key)
        self._delivered_q.append(key)
        if len(self._delivered_q) > 4096:
            old = self._delivered_q.pop(0)
            self._delivered.discard(old)
        for cb in list(self._listeners):
            try:
                res = cb(frame)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                pass

    async def publish(self, frame: dict):
        """Deliver a durable event (already persisted to events table) to
        local listeners and to other nodes via the broker."""
        # Local delivery first so this node's WS subscribers see it
        # immediately without waiting on a network round trip.
        self._dispatch(frame)
        w = self._broker_writer
        if w is not None:
            try:
                w.write((json.dumps(frame) + "\n").encode("utf-8"))
                await w.drain()
            except Exception:
                # Broker dropped — the poll loop on every node will pick the
                # event up from the events table within ~500ms.
                pass
