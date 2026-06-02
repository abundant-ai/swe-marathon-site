"""Server-Sent Events broadcasting."""
import asyncio
import json
from typing import Optional

# Subscribers keyed by (account_id_or_none, channel)
_subscribers: dict[tuple, set[asyncio.Queue]] = {}
_lock = asyncio.Lock()


async def subscribe(account_id: Optional[int], channel: str) -> asyncio.Queue:
    q = asyncio.Queue(maxsize=64)
    async with _lock:
        key = (account_id, channel)
        _subscribers.setdefault(key, set()).add(q)
    return q


async def unsubscribe(account_id: Optional[int], channel: str, q: asyncio.Queue):
    async with _lock:
        key = (account_id, channel)
        if key in _subscribers:
            _subscribers[key].discard(q)
            if not _subscribers[key]:
                del _subscribers[key]


def publish(account_id: Optional[int], channel: str, event: str, payload):
    """Synchronous publish — schedules delivery to async queues."""
    key = (account_id, channel)
    subs = _subscribers.get(key, set())
    if not subs:
        return
    msg = {"event": event, "payload": payload if isinstance(payload, str) else json.dumps(payload)}
    for q in list(subs):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass
