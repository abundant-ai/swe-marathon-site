"""Helpers for emitting durable channel events.

Two-step emission so the events row is committed in the same SQLite
transaction as whatever mutation triggered it (message insert, channel
update, etc.):

1. ``record_event(conn, channel_id, seq, kind, payload)`` — INSERT into
   ``events`` inside the caller's open transaction.
2. ``publish_event(bus, channel_id, seq, kind, payload)`` — fan out the
   frame to local WebSocket subscribers and other nodes via the broker.

For one-shot events that don't need to be transactional with anything
else (member.joined, channel.updated), use :func:`emit_durable` which
opens its own ``BEGIN IMMEDIATE``, allocates a fresh seq, records the
event, and publishes.
"""
import json
import re
from . import store

FILE_LIMIT = 10 * 1024 * 1024


def parse_slash(body: str):
    if not body or not body.startswith("/"):
        return None, body
    if body.startswith("//"):
        return None, body
    m = re.match(r"^/([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", body, re.S)
    if not m:
        return None, body
    return m.group(1).lower(), (m.group(2) or "").strip()


def record_event(conn, channel_id, seq, kind, payload):
    store.append_event(conn, channel_id, seq, kind, payload)


async def publish_event(bus, channel_id, seq, kind, payload):
    frame = dict(payload)
    frame["type"] = kind
    frame["seq"] = seq
    frame["channel_id"] = channel_id
    await bus.publish(frame)


async def emit_durable(bus, channel_id, kind, payload, *, seq=None):
    """Allocate a seq, persist the event row, and publish.

    Use this for events that don't piggyback on another transaction
    (member.joined, channel.updated, etc.). Pass ``seq`` if the caller
    already allocated one (must already be persisted as an events row).
    """
    if seq is None:
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq = store.reserve_seq(conn, channel_id)
                store.append_event(conn, channel_id, seq, kind, payload)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    await publish_event(bus, channel_id, seq, kind, payload)
    return seq
