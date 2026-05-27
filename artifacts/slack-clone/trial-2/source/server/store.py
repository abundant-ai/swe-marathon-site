"""SQLite-backed storage shared by all three HTTP nodes and the IRC gateway.

Per-channel ``seq`` is allocated atomically inside a write transaction
(BEGIN IMMEDIATE) so concurrent writers from any node observe a dense,
gap-free, per-channel sequence.
"""
import json
import os
import re
import secrets
import sqlite3
import time
import hashlib
import hmac
from datetime import datetime, timezone

DATA_DIR = os.environ.get("HUDDLE_DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")
FILES_DIR = os.path.join(DATA_DIR, "files")

os.makedirs(FILES_DIR, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    avatar_url TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    status_emoji TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    join_mode TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_dm INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    next_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS dm_pairs (
    user1_id INTEGER NOT NULL,
    user2_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    PRIMARY KEY (user1_id, user2_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    parent_id INTEGER REFERENCES messages(id),
    reply_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE TABLE IF NOT EXISTS pins (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    pinned_by INTEGER NOT NULL,
    pinned_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attached_message_id INTEGER REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS message_files (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, file_id)
);
CREATE TABLE IF NOT EXISTS mentions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS groups_t (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, handle)
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups_t(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT,
    invited_username TEXT,
    expires_at TEXT,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_chan_seq ON events(channel_id, seq);
CREATE TABLE IF NOT EXISTS irc_nicks (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    nick TEXT NOT NULL UNIQUE
);
"""


def init_schema():
    conn = connect()
    try:
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s)
    finally:
        conn.close()


# ------------ password helpers ------------

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 50_000
    ).hex()


def make_token() -> str:
    return secrets.token_hex(24)


def verify_password(stored_hash: str, salt: str, given: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_password(given, salt))


# ------------ row helpers ------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
SLUG_RE = re.compile(r"^[a-z0-9-]{2,32}$")
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
GROUP_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def user_to_obj(row) -> dict:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "timezone": row["timezone"] or "UTC",
        "avatar_url": row["avatar_url"] or "",
        "status_text": row["status_text"] or "",
        "status_emoji": row["status_emoji"] or "",
        "created_at": row["created_at"],
    }


def workspace_to_obj(row) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "join_mode": row["join_mode"],
        "created_at": row["created_at"],
    }


def channel_to_obj(row) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "is_private": bool(row["is_private"]),
        "is_dm": bool(row["is_dm"]),
        "topic": row["topic"] or "",
        "is_archived": bool(row["is_archived"]),
        "created_at": row["created_at"],
    }


def file_to_obj(row) -> dict:
    return {
        "id": row["id"],
        "uploader_id": row["uploader_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "created_at": row["created_at"],
    }


def reactions_for(conn, message_id):
    rows = conn.execute(
        "SELECT emoji, user_id FROM reactions WHERE message_id=? ORDER BY created_at",
        (message_id,),
    ).fetchall()
    by_emoji = {}
    for r in rows:
        e = r["emoji"]
        if e not in by_emoji:
            by_emoji[e] = []
        by_emoji[e].append(r["user_id"])
    return [
        {"emoji": e, "count": len(uids), "user_ids": uids}
        for e, uids in by_emoji.items()
    ]


def files_for(conn, message_id):
    rows = conn.execute(
        "SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id "
        "WHERE mf.message_id=? ORDER BY f.id",
        (message_id,),
    ).fetchall()
    return [file_to_obj(r) for r in rows]


def mentions_for(conn, message_id):
    rows = conn.execute(
        "SELECT user_id FROM mentions WHERE message_id=?", (message_id,)
    ).fetchall()
    return [r["user_id"] for r in rows]


def message_to_obj(conn, row) -> dict:
    if row is None:
        return None
    user_row = conn.execute(
        "SELECT * FROM users WHERE id=?", (row["author_id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "author": user_to_obj(user_row),
        "body": row["body"] if not row["deleted"] else "",
        "parent_id": row["parent_id"],
        "reply_count": row["reply_count"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "deleted": bool(row["deleted"]),
        "files": files_for(conn, row["id"]) if not row["deleted"] else [],
        "reactions": reactions_for(conn, row["id"]) if not row["deleted"] else [],
        "mentions": mentions_for(conn, row["id"]),
        "seq": row["seq"],
    }


def reserve_seq(conn, channel_id: int) -> int:
    """Allocate the next dense seq for this channel.

    Caller must already be inside a transaction (BEGIN IMMEDIATE)."""
    cur = conn.execute(
        "UPDATE channels SET next_seq = next_seq + 1 WHERE id=? RETURNING next_seq",
        (channel_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("channel not found")
    return row[0]


def append_event(conn, channel_id: int, seq: int, kind: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO events (channel_id, seq, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel_id, seq, kind, json.dumps(payload), now_iso()),
    )
    return cur.lastrowid


def event_to_frame(row) -> dict:
    payload = json.loads(row["payload"])
    payload["type"] = row["kind"]
    payload["seq"] = row["seq"]
    payload["channel_id"] = row["channel_id"]
    return payload


def workspace_role(conn, workspace_id: int, user_id: int):
    row = conn.execute(
        "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    ).fetchone()
    return row["role"] if row else None


def is_channel_member(conn, channel_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
        (channel_id, user_id),
    ).fetchone() is not None


def channel_member_ids(conn, channel_id: int):
    return [
        r["user_id"]
        for r in conn.execute(
            "SELECT user_id FROM channel_members WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
    ]


def workspace_user_ids(conn, workspace_id: int):
    return [
        r["user_id"]
        for r in conn.execute(
            "SELECT user_id FROM workspace_members WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
    ]


def find_user_by_username(conn, username: str):
    return conn.execute(
        "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
    ).fetchone()


def auth_user(conn, token: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",
        (token,),
    ).fetchone()
    return row


def parse_mentions(conn, workspace_id: int, author_id: int, body: str):
    """Return list of resolved user ids for ``@username`` in ``body``.

    - No mid-word mentions (must follow start-of-string or whitespace/punct,
      and the next char must not be ``[A-Za-z0-9_]``).
    - Skip self-mentions.
    - Email-shaped (``foo@bar.tld``) does not match since `@` must follow a
      non-word character or be at the start.
    - Group handles (``@team``) expand to the union of members.
    - Members must belong to the workspace.
    """
    found = set()
    pattern = re.compile(r"(?:^|[^A-Za-z0-9_])@([A-Za-z0-9_][A-Za-z0-9_\-]{0,32})(?![A-Za-z0-9_])")
    ws_members = set(workspace_user_ids(conn, workspace_id))
    for m in pattern.finditer(body):
        handle = m.group(1)
        u = find_user_by_username(conn, handle)
        if u and u["id"] != author_id and u["id"] in ws_members:
            found.add(u["id"])
            continue
        # group?
        g = conn.execute(
            "SELECT id FROM groups_t WHERE workspace_id=? AND handle=? COLLATE NOCASE",
            (workspace_id, handle),
        ).fetchone()
        if g:
            members = conn.execute(
                "SELECT user_id FROM group_members WHERE group_id=?", (g["id"],)
            ).fetchall()
            for r in members:
                if r["user_id"] != author_id and r["user_id"] in ws_members:
                    found.add(r["user_id"])
    return sorted(found)
