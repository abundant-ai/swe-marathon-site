"""Synchronous SQLite-backed storage for the Huddle cluster.

Every node opens the same database in WAL mode. A single `events` table holds
every durable channel-scoped event with a per-channel dense `seq` so any node
can replay history. Cross-node fan-out happens via the broker (TCP) when it's
up and via a DB-tail poll loop when it isn't, so killing the broker doesn't
stop fan-out.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable, Optional


DATA_DIR = os.environ.get("HUDDLE_DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "huddle.db")
FILES_DIR = os.path.join(DATA_DIR, "files")

# Connections are per-thread; aiohttp runs on a single event loop, so each
# process really only ever uses one connection.
_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def gen_id() -> str:
    """A short, URL-safe, time-sortable id."""
    return uuid.uuid4().hex[:24]


def gen_token() -> str:
    return secrets.token_urlsafe(24)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    secs, frac = divmod(ms, 1000)
    t = time.gmtime(secs)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + f".{frac:03d}Z"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    timezone TEXT DEFAULT 'UTC',
    avatar_url TEXT DEFAULT '',
    status_text TEXT DEFAULT '',
    status_emoji TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    join_mode TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_dm INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    is_archived INTEGER NOT NULL DEFAULT 0,
    head_seq INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(workspace_id, name)
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    parent_id TEXT,
    created_at INTEGER NOT NULL,
    edited_at INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL,
    reply_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages(channel_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS pins (
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    pinned_at INTEGER NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    uploader_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    message_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS message_files (
    message_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY (message_id, file_id)
);
CREATE TABLE IF NOT EXISTS message_mentions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS dms (
    workspace_id TEXT NOT NULL,
    user_a TEXT NOT NULL,
    user_b TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_a, user_b)
);
CREATE TABLE IF NOT EXISTS user_groups (
    workspace_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (workspace_id, handle)
);
CREATE TABLE IF NOT EXISTS user_group_members (
    workspace_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (workspace_id, handle, user_id)
);
CREATE TABLE IF NOT EXISTS invitations (
    code TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    email TEXT,
    invited_username TEXT,
    expires_at INTEGER,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(channel_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(id);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def init_db() -> None:
    os.makedirs(FILES_DIR, exist_ok=True)
    c = conn()
    c.executescript(SCHEMA)
    # Bootstrap counters that gate global ordering not used today but reserved.
    c.execute(
        "INSERT OR IGNORE INTO counters(name, value) VALUES (?, ?)",
        ("event_seq", 0),
    )


def tx_begin() -> sqlite3.Connection:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    return c


def tx_commit() -> None:
    conn().execute("COMMIT")


def tx_rollback() -> None:
    try:
        conn().execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# -----------------------------------------------------------------------------
# Event commit
# -----------------------------------------------------------------------------

def commit_event(channel_id: str, kind: str, payload: dict) -> tuple[int, int]:
    """Inside an open transaction, advance head_seq and append an event.

    Returns (event_id, seq).
    """
    c = conn()
    cur = c.execute(
        "UPDATE channels SET head_seq = head_seq + 1 WHERE id = ? RETURNING head_seq",
        (channel_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown channel {channel_id}")
    seq = row["head_seq"]
    cur = c.execute(
        "INSERT INTO events(channel_id, seq, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (channel_id, seq, kind, json.dumps(payload), now_ms()),
    )
    eid = cur.fetchone()["id"]
    return int(eid), int(seq)


def fetch_events_since(channel_id: str, since_seq: int, limit: int = 1000) -> list[dict]:
    rows = conn().execute(
        "SELECT seq, kind, payload, created_at FROM events "
        "WHERE channel_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
        (channel_id, since_seq, limit),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "seq": r["seq"],
            "kind": r["kind"],
            "payload": json.loads(r["payload"]),
            "created_at": r["created_at"],
        })
    return out


def fetch_events_after_id(after_id: int, limit: int = 5000) -> list[dict]:
    rows = conn().execute(
        "SELECT id, channel_id, seq, kind, payload FROM events "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "channel_id": r["channel_id"],
            "seq": r["seq"],
            "kind": r["kind"],
            "payload": json.loads(r["payload"]),
        })
    return out


def latest_event_id() -> int:
    row = conn().execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def channel_head_seq(channel_id: str) -> int:
    row = conn().execute(
        "SELECT head_seq FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    return int(row["head_seq"]) if row else 0


# -----------------------------------------------------------------------------
# Hashing — keep dependency-free; PBKDF2 is plenty for this exercise.
# -----------------------------------------------------------------------------

import hashlib

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 50_000)
    return "pbkdf2$" + salt.hex() + "$" + h.hex()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, salt_hex, h_hex = encoded.split("$")
        if algo != "pbkdf2":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(h_hex)
    except Exception:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 50_000)
    return secrets.compare_digest(h, expected)
