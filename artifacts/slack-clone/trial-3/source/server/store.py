"""SQLite-based storage layer with dense per-channel sequence allocation.

All write operations that affect a channel allocate `seq` atomically within
the same transaction so the (channel_id, seq) sequence stays dense across
HTTP nodes and the IRC gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_PATH = os.environ.get("HUDDLE_DB", "/app/data/huddle.db")
UPLOAD_DIR = Path(os.environ.get("HUDDLE_UPLOADS", "/app/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(8)
    return f"v1${salt}${hashlib.sha256((salt + pw).encode()).hexdigest()}"


def verify_password(pw: str, h: str) -> bool:
    try:
        v, salt, digest = h.split("$", 2)
        return hashlib.sha256((salt + pw).encode()).hexdigest() == digest
    except Exception:
        return False


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_SLUG_RE = re.compile(r"^[a-z0-9-]{2,32}$")
_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_GROUP_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TZ_DB = {
    "UTC", "GMT", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Europe/Amsterdam", "Europe/Madrid", "Europe/Rome", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Vancouver", "America/Mexico_City", "America/Sao_Paulo",
    "America/Buenos_Aires", "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong",
    "Asia/Singapore", "Asia/Seoul", "Asia/Kolkata", "Asia/Dubai",
    "Australia/Sydney", "Australia/Melbourne", "Pacific/Auckland", "Africa/Johannesburg",
    "Africa/Cairo", "Africa/Lagos",
}


def valid_username(s: str) -> bool:
    return isinstance(s, str) and bool(_USERNAME_RE.match(s))


def valid_slug(s: str) -> bool:
    return isinstance(s, str) and bool(_SLUG_RE.match(s))


def valid_channel_name(s: str) -> bool:
    return isinstance(s, str) and 1 <= len(s) <= 32 and bool(_CHANNEL_NAME_RE.match(s))


def valid_group_handle(s: str) -> bool:
    return isinstance(s, str) and bool(_GROUP_HANDLE_RE.match(s))


def valid_timezone(s: str) -> bool:
    return isinstance(s, str) and (s in TZ_DB or "/" in s and len(s) <= 64)


_dbs: dict[int, sqlite3.Connection] = {}


def conn() -> sqlite3.Connection:
    pid = os.getpid()
    c = _dbs.get(pid)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA foreign_keys=ON")
        _dbs[pid] = c
    return c


def init_db() -> None:
    c = conn()
    with open(SCHEMA_PATH) as f:
        c.executescript(f.read())


def begin() -> None:
    conn().execute("BEGIN IMMEDIATE")


def commit() -> None:
    conn().execute("COMMIT")


def rollback() -> None:
    try:
        conn().execute("ROLLBACK")
    except sqlite3.Error:
        pass


class Tx:
    def __enter__(self):
        begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            commit()
        else:
            rollback()
        return False


# ---------- users ----------

def serialize_user(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "timezone": row["timezone"],
        "avatar_url": row["avatar_url"],
        "status_text": row["status_text"],
        "status_emoji": row["status_emoji"],
    }


def get_user_by_id(user_id: str) -> dict | None:
    row = conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return serialize_user(row)


def get_user_by_username(username: str) -> dict | None:
    row = conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return serialize_user(row)


def get_user_row(user_id: str) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def create_user(username: str, password: str, display_name: str | None = None) -> dict:
    user_id = new_id("u_")
    h = hash_password(password)
    dn = display_name or username
    try:
        conn().execute(
            "INSERT INTO users (id, username, password_hash, display_name, timezone, avatar_url, status_text, status_emoji, created_at) "
            "VALUES (?, ?, ?, ?, 'UTC', '', '', '', ?)",
            (user_id, username, h, dn, now_iso()),
        )
    except sqlite3.IntegrityError:
        raise ValueError("conflict")
    return get_user_by_id(user_id)


def login_user(username: str, password: str) -> dict | None:
    row = conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return serialize_user(row)


def update_user_profile(user_id: str, fields: dict) -> dict:
    allowed = {"display_name", "timezone", "avatar_url", "status_text", "status_emoji"}
    sets = []
    args: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if not isinstance(v, str):
            raise ValueError("invalid_field")
        if k == "display_name" and len(v) > 64:
            raise ValueError("too_long")
        if k == "timezone" and not valid_timezone(v):
            raise ValueError("invalid_timezone")
        if k == "status_text" and len(v) > 100:
            raise ValueError("too_long")
        if k == "status_emoji" and len(v) > 16:
            raise ValueError("too_long")
        if k == "avatar_url" and len(v) > 512:
            raise ValueError("too_long")
        sets.append(f"{k}=?")
        args.append(v)
    if sets:
        args.append(user_id)
        conn().execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
    return get_user_by_id(user_id)


def issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(24)
    conn().execute("INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, now_iso()))
    return token


def user_for_token(token: str) -> dict | None:
    if not token:
        return None
    row = conn().execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token=?",
        (token,),
    ).fetchone()
    return serialize_user(row)


def revoke_token(token: str) -> None:
    conn().execute("DELETE FROM tokens WHERE token=?", (token,))


# ---------- workspaces / channels ----------

def serialize_workspace(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "join_mode": row["join_mode"],
        "created_at": row["created_at"],
    }


def serialize_channel(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "is_private": bool(row["is_private"]),
        "is_dm": bool(row["is_dm"]),
        "topic": row["topic"],
        "is_archived": bool(row["is_archived"]),
        "created_at": row["created_at"],
    }


def get_workspace_by_slug(slug: str) -> dict | None:
    row = conn().execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
    return serialize_workspace(row) if row else None


def get_workspace_by_id(wsid: str) -> dict | None:
    row = conn().execute("SELECT * FROM workspaces WHERE id=?", (wsid,)).fetchone()
    return serialize_workspace(row) if row else None


def workspace_role(workspace_id: str, user_id: str) -> str | None:
    row = conn().execute(
        "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    ).fetchone()
    return row["role"] if row else None


def list_user_workspaces(user_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id "
        "WHERE m.user_id=? ORDER BY w.created_at",
        (user_id,),
    ).fetchall()
    return [serialize_workspace(r) for r in rows]


def list_workspace_channels(workspace_id: str, user_id: str | None = None, include_archived: bool = False, include_dms: bool = False) -> list[dict]:
    q = "SELECT * FROM channels WHERE workspace_id=?"
    args: list[Any] = [workspace_id]
    if not include_archived:
        q += " AND is_archived=0"
    if not include_dms:
        q += " AND is_dm=0"
    q += " ORDER BY is_dm, created_at"
    rows = conn().execute(q, args).fetchall()
    out = []
    for r in rows:
        ch = serialize_channel(r)
        if ch["is_private"] and user_id is not None:
            mem = conn().execute(
                "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
                (ch["id"], user_id),
            ).fetchone()
            if not mem:
                continue
        out.append(ch)
    return out


def list_workspace_members(workspace_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT m.user_id, m.role, m.joined_at, u.username, u.display_name "
        "FROM workspace_members m JOIN users u ON u.id=m.user_id "
        "WHERE m.workspace_id=? ORDER BY m.joined_at",
        (workspace_id,),
    ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "role": r["role"],
            "joined_at": r["joined_at"],
            "username": r["username"],
            "display_name": r["display_name"] or r["username"],
        }
        for r in rows
    ]


def create_workspace(slug: str, name: str, owner_id: str) -> tuple[dict, dict]:
    wsid = new_id("w_")
    ts = now_iso()
    try:
        conn().execute(
            "INSERT INTO workspaces (id, slug, name, owner_id, join_mode, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
            (wsid, slug, name, owner_id, ts),
        )
    except sqlite3.IntegrityError:
        raise ValueError("conflict")
    conn().execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (wsid, owner_id, ts),
    )
    chid = new_id("c_")
    conn().execute(
        "INSERT INTO channels (id, workspace_id, name, is_private, is_dm, topic, is_archived, created_at) "
        "VALUES (?, ?, 'general', 0, 0, '', 0, ?)",
        (chid, wsid, ts),
    )
    conn().execute(
        "INSERT INTO channel_members (channel_id, user_id, joined_at) VALUES (?, ?, ?)",
        (chid, owner_id, ts),
    )
    conn().execute("INSERT INTO channel_seq (channel_id, next_seq) VALUES (?, 1)", (chid,))
    ws = get_workspace_by_slug(slug)
    ch = get_channel(chid)
    return ws, ch


def add_workspace_member(workspace_id: str, user_id: str, role: str = "member") -> None:
    if workspace_role(workspace_id, user_id):
        return
    conn().execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
        (workspace_id, user_id, role, now_iso()),
    )


def update_workspace(workspace_id: str, fields: dict) -> dict:
    sets = []
    args: list[Any] = []
    for k, v in fields.items():
        if k == "name" and isinstance(v, str) and 1 <= len(v) <= 80:
            sets.append("name=?")
            args.append(v)
        elif k == "join_mode" and v in ("open", "invite_only"):
            sets.append("join_mode=?")
            args.append(v)
        else:
            raise ValueError("invalid_field")
    if sets:
        args.append(workspace_id)
        conn().execute(f"UPDATE workspaces SET {', '.join(sets)} WHERE id=?", args)
    return get_workspace_by_id(workspace_id)


def set_member_role(workspace_id: str, user_id: str, role: str) -> None:
    conn().execute(
        "UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
        (role, workspace_id, user_id),
    )


def transfer_ownership(workspace_id: str, new_owner_id: str) -> None:
    cur = conn()
    old = cur.execute("SELECT owner_id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    if not old:
        return
    cur.execute("UPDATE workspaces SET owner_id=? WHERE id=?", (new_owner_id, workspace_id))
    cur.execute(
        "UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?",
        (workspace_id, old["owner_id"]),
    )
    cur.execute(
        "UPDATE workspace_members SET role='owner' WHERE workspace_id=? AND user_id=?",
        (workspace_id, new_owner_id),
    )


# ---------- channels ----------

def get_channel(channel_id: str) -> dict | None:
    row = conn().execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    return serialize_channel(row) if row else None


def get_channel_row(channel_id: str) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


def get_channel_by_name(workspace_id: str, name: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM channels WHERE workspace_id=? AND name=?",
        (workspace_id, name),
    ).fetchone()
    return serialize_channel(row) if row else None


def create_channel(workspace_id: str, name: str, is_private: bool, topic: str, creator_id: str) -> dict:
    chid = new_id("c_")
    ts = now_iso()
    try:
        conn().execute(
            "INSERT INTO channels (id, workspace_id, name, is_private, is_dm, topic, is_archived, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, 0, ?)",
            (chid, workspace_id, name, 1 if is_private else 0, topic, ts),
        )
    except sqlite3.IntegrityError:
        raise ValueError("conflict")
    conn().execute(
        "INSERT INTO channel_members (channel_id, user_id, joined_at) VALUES (?, ?, ?)",
        (chid, creator_id, ts),
    )
    conn().execute("INSERT INTO channel_seq (channel_id, next_seq) VALUES (?, 1)", (chid,))
    return get_channel(chid)


def add_channel_member(channel_id: str, user_id: str) -> bool:
    cur = conn()
    exists = cur.execute(
        "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
        (channel_id, user_id),
    ).fetchone()
    if exists:
        return False
    cur.execute(
        "INSERT INTO channel_members (channel_id, user_id, joined_at) VALUES (?, ?, ?)",
        (channel_id, user_id, now_iso()),
    )
    return True


def remove_channel_member(channel_id: str, user_id: str) -> None:
    conn().execute(
        "DELETE FROM channel_members WHERE channel_id=? AND user_id=?",
        (channel_id, user_id),
    )


def is_channel_member(channel_id: str, user_id: str) -> bool:
    return bool(conn().execute(
        "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
        (channel_id, user_id),
    ).fetchone())


def list_channel_members(channel_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT u.id, u.username, u.display_name FROM channel_members m "
        "JOIN users u ON u.id=m.user_id WHERE m.channel_id=? ORDER BY m.joined_at",
        (channel_id,),
    ).fetchall()
    return [
        {"user_id": r["id"], "username": r["username"], "display_name": r["display_name"] or r["username"]}
        for r in rows
    ]


def update_channel(channel_id: str, fields: dict) -> dict:
    sets = []
    args: list[Any] = []
    for k, v in fields.items():
        if k == "topic":
            if not isinstance(v, str) or len(v) > 250:
                raise ValueError("topic_too_long")
            sets.append("topic=?")
            args.append(v)
        elif k == "is_archived":
            sets.append("is_archived=?")
            args.append(1 if v else 0)
        elif k == "name":
            if not valid_channel_name(v):
                raise ValueError("invalid_name")
            sets.append("name=?")
            args.append(v)
        else:
            raise ValueError("invalid_field")
    if sets:
        args.append(channel_id)
        try:
            conn().execute(f"UPDATE channels SET {', '.join(sets)} WHERE id=?", args)
        except sqlite3.IntegrityError:
            raise ValueError("conflict")
    return get_channel(channel_id)


# ---------- DMs ----------

def get_or_create_dm(user_a: str, user_b: str) -> tuple[dict, bool]:
    """Returns (channel, created)."""
    pair = tuple(sorted([user_a, user_b]))
    dm_key = f"dm:{pair[0]}:{pair[1]}"
    cur = conn()
    row = cur.execute("SELECT * FROM channels WHERE dm_key=?", (dm_key,)).fetchone()
    if row:
        return serialize_channel(row), False
    # find a workspace they share
    row = cur.execute(
        "SELECT a.workspace_id FROM workspace_members a JOIN workspace_members b "
        "ON a.workspace_id=b.workspace_id WHERE a.user_id=? AND b.user_id=? LIMIT 1",
        (user_a, user_b),
    ).fetchone()
    if not row:
        raise ValueError("no_shared_workspace")
    workspace_id = row["workspace_id"]
    chid = new_id("d_")
    ts = now_iso()
    cur.execute(
        "INSERT INTO channels (id, workspace_id, name, is_private, is_dm, topic, is_archived, created_at, dm_key) "
        "VALUES (?, ?, ?, 1, 1, '', 0, ?, ?)",
        (chid, workspace_id, f"dm-{chid[:8]}", ts, dm_key),
    )
    cur.execute(
        "INSERT INTO channel_members (channel_id, user_id, joined_at) VALUES (?, ?, ?), (?, ?, ?)",
        (chid, user_a, ts, chid, user_b, ts),
    )
    cur.execute("INSERT INTO channel_seq (channel_id, next_seq) VALUES (?, 1)", (chid,))
    return get_channel(chid), True


# ---------- messages, reactions, threads ----------

def alloc_seq(channel_id: str) -> int:
    c = conn()
    cur = c.execute("UPDATE channel_seq SET next_seq = next_seq + 1 WHERE channel_id=?", (channel_id,))
    if cur.rowcount == 0:
        c.execute("INSERT INTO channel_seq (channel_id, next_seq) VALUES (?, 2)", (channel_id,))
        return 1
    row = c.execute("SELECT next_seq FROM channel_seq WHERE channel_id=?", (channel_id,)).fetchone()
    return row["next_seq"] - 1


def channel_head_seq(channel_id: str) -> int:
    row = conn().execute("SELECT next_seq FROM channel_seq WHERE channel_id=?", (channel_id,)).fetchone()
    if not row:
        return 0
    return row["next_seq"] - 1


def commit_event(channel_id: str, kind: str, payload: dict, seq: int | None = None) -> tuple[int, dict]:
    """Allocate seq and persist event. Returns (seq, payload-with-seq)."""
    if seq is None:
        seq = alloc_seq(channel_id)
    payload["seq"] = seq
    payload["channel_id"] = channel_id
    payload["type"] = kind
    conn().execute(
        "INSERT INTO events (channel_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (channel_id, seq, kind, json.dumps(payload), now_iso()),
    )
    return seq, payload


def post_message(channel_id: str, author_id: str, body: str, parent_id: str | None = None,
                 file_ids: list[str] | None = None) -> tuple[dict, dict]:
    msg_id = new_id("m_")
    ts = now_iso()
    seq = alloc_seq(channel_id)
    conn().execute(
        "INSERT INTO messages (id, channel_id, author_id, body, parent_id, seq, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, channel_id, author_id, body, parent_id, seq, ts),
    )
    if file_ids:
        for fid in file_ids:
            conn().execute("INSERT INTO message_files (message_id, file_id) VALUES (?, ?)", (msg_id, fid))
            conn().execute("UPDATE files SET attached_to=? WHERE id=?", (msg_id, fid))
    # mentions
    for uid in resolve_mentions(channel_id, body):
        conn().execute("INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?, ?)", (msg_id, uid))

    msg = build_message(msg_id)
    if parent_id:
        kind = "message.reply"
    else:
        kind = "message.new"
    payload = {"message": msg}
    seq2, payload = commit_event(channel_id, kind, payload, seq=seq)
    return msg, payload


def edit_message(message_id: str, new_body: str, editor_id: str) -> tuple[dict, dict] | None:
    cur = conn()
    row = cur.execute("SELECT * FROM messages WHERE id=? AND deleted_at IS NULL", (message_id,)).fetchone()
    if not row:
        return None
    if row["author_id"] != editor_id:
        raise PermissionError("not_author")
    ts = now_iso()
    cur.execute("UPDATE messages SET body=?, edited_at=? WHERE id=?", (new_body, ts, message_id))
    cur.execute("DELETE FROM mentions WHERE message_id=?", (message_id,))
    for uid in resolve_mentions(row["channel_id"], new_body):
        cur.execute("INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?, ?)", (message_id, uid))
    msg = build_message(message_id)
    seq, payload = commit_event(row["channel_id"], "message.edited", {"message": msg})
    return msg, payload


def delete_message(message_id: str, deleter_id: str) -> tuple[str, dict] | None:
    cur = conn()
    row = cur.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    if row["deleted_at"]:
        return row["channel_id"], {"type": "message.deleted", "message_id": message_id, "channel_id": row["channel_id"], "seq": row["seq"]}
    # author or workspace owner
    ws_row = cur.execute(
        "SELECT w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?",
        (row["channel_id"],),
    ).fetchone()
    is_owner = ws_row and ws_row["owner_id"] == deleter_id
    if row["author_id"] != deleter_id and not is_owner:
        raise PermissionError("not_author")
    ts = now_iso()
    cur.execute("UPDATE messages SET deleted_at=? WHERE id=?", (ts, message_id))
    cur.execute("DELETE FROM reactions WHERE message_id=?", (message_id,))
    seq, payload = commit_event(row["channel_id"], "message.deleted", {"message_id": message_id})
    return row["channel_id"], payload


def get_message(message_id: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM messages WHERE id=? AND deleted_at IS NULL", (message_id,)
    ).fetchone()
    if not row:
        return None
    return build_message(message_id)


def build_message(message_id: str) -> dict | None:
    cur = conn()
    row = cur.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    if row["deleted_at"]:
        return None
    author_row = cur.execute("SELECT * FROM users WHERE id=?", (row["author_id"],)).fetchone()
    author = serialize_user(author_row) if author_row else None
    files = [
        serialize_file(f)
        for f in cur.execute(
            "SELECT f.* FROM message_files mf JOIN files f ON f.id=mf.file_id WHERE mf.message_id=?",
            (message_id,),
        ).fetchall()
    ]
    reaction_rows = cur.execute(
        "SELECT emoji, user_id FROM reactions WHERE message_id=? ORDER BY created_at",
        (message_id,),
    ).fetchall()
    reactions: dict[str, dict] = {}
    for rr in reaction_rows:
        e = rr["emoji"]
        if e not in reactions:
            reactions[e] = {"emoji": e, "count": 0, "user_ids": []}
        reactions[e]["count"] += 1
        reactions[e]["user_ids"].append(rr["user_id"])
    mention_rows = cur.execute("SELECT user_id FROM mentions WHERE message_id=?", (message_id,)).fetchall()
    reply_count = cur.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE parent_id=? AND deleted_at IS NULL",
        (message_id,),
    ).fetchone()["c"]
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "author": author,
        "body": row["body"],
        "parent_id": row["parent_id"],
        "reply_count": reply_count,
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "files": files,
        "reactions": list(reactions.values()),
        "mentions": [r["user_id"] for r in mention_rows],
        "seq": row["seq"],
    }


def list_channel_messages(channel_id: str, limit: int = 50, before: str | None = None,
                          parent_only: bool = True) -> list[dict]:
    q = ("SELECT id FROM messages WHERE channel_id=? AND deleted_at IS NULL"
         + (" AND parent_id IS NULL" if parent_only else "")
         + " ORDER BY seq DESC LIMIT ?")
    args: list[Any] = [channel_id, max(1, min(limit, 200))]
    rows = conn().execute(q, args).fetchall()
    out = [build_message(r["id"]) for r in rows]
    out = [m for m in out if m is not None]
    if before:
        before_row = conn().execute("SELECT seq FROM messages WHERE id=?", (before,)).fetchone()
        if before_row:
            cutoff = before_row["seq"]
            out = [m for m in out if m["seq"] < cutoff]
    return out


def list_thread_replies(parent_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT id FROM messages WHERE parent_id=? AND deleted_at IS NULL ORDER BY seq ASC",
        (parent_id,),
    ).fetchall()
    out = [build_message(r["id"]) for r in rows]
    return [m for m in out if m is not None]


def add_reaction(message_id: str, user_id: str, emoji: str) -> tuple[str, dict] | None:
    cur = conn()
    row = cur.execute("SELECT channel_id FROM messages WHERE id=? AND deleted_at IS NULL", (message_id,)).fetchone()
    if not row:
        return None
    cur.execute(
        "INSERT OR IGNORE INTO reactions (message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)",
        (message_id, user_id, emoji, now_iso()),
    )
    msg = build_message(message_id)
    seq, payload = commit_event(row["channel_id"], "reaction.added",
                                 {"message_id": message_id, "emoji": emoji, "user_id": user_id, "message": msg})
    return row["channel_id"], payload


def remove_reaction(message_id: str, user_id: str, emoji: str) -> tuple[str, dict] | None:
    cur = conn()
    row = cur.execute("SELECT channel_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    cur.execute(
        "DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
        (message_id, user_id, emoji),
    )
    msg = build_message(message_id)
    seq, payload = commit_event(row["channel_id"], "reaction.removed",
                                 {"message_id": message_id, "emoji": emoji, "user_id": user_id, "message": msg})
    return row["channel_id"], payload


# ---------- mentions ----------

_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)")


def resolve_mentions(channel_id: str, body: str) -> set[str]:
    """Return user ids mentioned in body. Excludes self-mentions and non-workspace members."""
    cur = conn()
    ch = cur.execute("SELECT workspace_id FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not ch:
        return set()
    ws_id = ch["workspace_id"]
    out: set[str] = set()
    for m in _MENTION_RE.finditer(body):
        handle = m.group(1)
        # email-shaped: skip if followed by domain "."
        # the regex prevents the @ being mid-word; we also skip if preceded by something that looks like email user
        start = m.start()
        if start > 0 and body[start - 1].isalnum():
            continue
        # check user
        u = cur.execute(
            "SELECT u.id FROM users u JOIN workspace_members wm ON wm.user_id=u.id "
            "WHERE u.username=? AND wm.workspace_id=?",
            (handle, ws_id),
        ).fetchone()
        if u:
            out.add(u["id"])
            continue
        # group?
        g_members = cur.execute(
            "SELECT user_id FROM group_members WHERE workspace_id=? AND handle=?",
            (ws_id, handle),
        ).fetchall()
        if g_members:
            for gm in g_members:
                # only include users still in workspace
                wm = cur.execute(
                    "SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?",
                    (ws_id, gm["user_id"]),
                ).fetchone()
                if wm:
                    out.add(gm["user_id"])
    return out


# ---------- files ----------

def serialize_file(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "uploader_id": row["uploader_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "created_at": row["created_at"],
    }


def create_file(uploader_id: str, filename: str, content_type: str, data: bytes) -> dict:
    fid = new_id("f_")
    path = UPLOAD_DIR / fid
    with open(path, "wb") as f:
        f.write(data)
    conn().execute(
        "INSERT INTO files (id, uploader_id, filename, content_type, size, path, created_at, attached_to) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (fid, uploader_id, filename, content_type, len(data), str(path), now_iso()),
    )
    return get_file(fid)


def get_file(file_id: str) -> dict | None:
    row = conn().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    return serialize_file(row) if row else None


def get_file_row(file_id: str) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()


# ---------- pins ----------

def add_pin(message_id: str, user_id: str) -> tuple[str, dict] | None:
    cur = conn()
    row = cur.execute("SELECT channel_id FROM messages WHERE id=? AND deleted_at IS NULL", (message_id,)).fetchone()
    if not row:
        return None
    cur.execute(
        "INSERT OR IGNORE INTO pins (message_id, channel_id, pinned_by, pinned_at) VALUES (?, ?, ?, ?)",
        (message_id, row["channel_id"], user_id, now_iso()),
    )
    return row["channel_id"], {"message_id": message_id}


def remove_pin(message_id: str) -> str | None:
    cur = conn()
    row = cur.execute("SELECT channel_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return None
    cur.execute("DELETE FROM pins WHERE message_id=?", (message_id,))
    return row["channel_id"]


def list_pins(channel_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT message_id FROM pins WHERE channel_id=? ORDER BY pinned_at DESC",
        (channel_id,),
    ).fetchall()
    out = []
    for r in rows:
        m = build_message(r["message_id"])
        if m:
            out.append(m)
    return out


# ---------- groups ----------

def list_groups(workspace_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM groups WHERE workspace_id=? ORDER BY handle", (workspace_id,)
    ).fetchall()
    out = []
    for r in rows:
        members = conn().execute(
            "SELECT user_id FROM group_members WHERE workspace_id=? AND handle=?",
            (workspace_id, r["handle"]),
        ).fetchall()
        out.append({
            "handle": r["handle"],
            "name": r["name"],
            "member_user_ids": [m["user_id"] for m in members],
        })
    return out


def get_group(workspace_id: str, handle: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM groups WHERE workspace_id=? AND handle=?", (workspace_id, handle)
    ).fetchone()
    if not row:
        return None
    members = conn().execute(
        "SELECT user_id FROM group_members WHERE workspace_id=? AND handle=?",
        (workspace_id, handle),
    ).fetchall()
    return {
        "handle": row["handle"],
        "name": row["name"],
        "member_user_ids": [m["user_id"] for m in members],
    }


def create_group(workspace_id: str, handle: str, name: str, member_user_ids: list[str]) -> dict:
    try:
        conn().execute(
            "INSERT INTO groups (workspace_id, handle, name, created_at) VALUES (?, ?, ?, ?)",
            (workspace_id, handle, name, now_iso()),
        )
    except sqlite3.IntegrityError:
        raise ValueError("conflict")
    for uid in member_user_ids:
        conn().execute(
            "INSERT OR IGNORE INTO group_members (workspace_id, handle, user_id) VALUES (?, ?, ?)",
            (workspace_id, handle, uid),
        )
    return get_group(workspace_id, handle)


def update_group(workspace_id: str, handle: str, fields: dict) -> dict | None:
    cur = conn()
    if not get_group(workspace_id, handle):
        return None
    if "name" in fields:
        cur.execute(
            "UPDATE groups SET name=? WHERE workspace_id=? AND handle=?",
            (fields["name"], workspace_id, handle),
        )
    if "member_user_ids" in fields:
        cur.execute("DELETE FROM group_members WHERE workspace_id=? AND handle=?", (workspace_id, handle))
        for uid in fields["member_user_ids"]:
            cur.execute(
                "INSERT OR IGNORE INTO group_members (workspace_id, handle, user_id) VALUES (?, ?, ?)",
                (workspace_id, handle, uid),
            )
    return get_group(workspace_id, handle)


def delete_group(workspace_id: str, handle: str) -> None:
    conn().execute("DELETE FROM groups WHERE workspace_id=? AND handle=?", (workspace_id, handle))
    conn().execute("DELETE FROM group_members WHERE workspace_id=? AND handle=?", (workspace_id, handle))


# ---------- invitations ----------

def create_invitation(workspace_id: str, invited_by: str, email: str | None,
                      invited_username: str | None, expires_in: int | None,
                      max_uses: int) -> dict:
    code = secrets.token_urlsafe(12)
    ts = now_iso()
    expires_at = None
    if expires_in:
        expires_at = datetime.fromtimestamp(time.time() + int(expires_in), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
    conn().execute(
        "INSERT INTO invitations (code, workspace_id, invited_by, email, invited_username, "
        "expires_at, max_uses, used_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (code, workspace_id, invited_by, email, invited_username, expires_at, max_uses, ts),
    )
    return {
        "code": code,
        "workspace_id": workspace_id,
        "email": email,
        "invited_username": invited_username,
        "expires_at": expires_at,
        "max_uses": max_uses,
        "used_count": 0,
        "created_at": ts,
    }


def get_invitation(code: str) -> dict | None:
    row = conn().execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    return {
        "code": row["code"],
        "workspace_id": row["workspace_id"],
        "email": row["email"],
        "invited_username": row["invited_username"],
        "expires_at": row["expires_at"],
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "created_at": row["created_at"],
    }


def list_invitations(workspace_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM invitations WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "code": r["code"],
            "workspace_id": r["workspace_id"],
            "email": r["email"],
            "invited_username": r["invited_username"],
            "expires_at": r["expires_at"],
            "max_uses": r["max_uses"],
            "used_count": r["used_count"],
            "created_at": r["created_at"],
        })
    return out


def consume_invitation(code: str, user_id: str) -> str | None:
    cur = conn()
    row = cur.execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    if row["expires_at"]:
        try:
            exp = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            exp = None
        if exp and exp.timestamp() < time.time():
            raise ValueError("expired")
    if row["used_count"] >= row["max_uses"]:
        raise ValueError("exhausted")
    cur.execute("UPDATE invitations SET used_count = used_count + 1 WHERE code=?", (code,))
    add_workspace_member(row["workspace_id"], user_id, "member")
    return row["workspace_id"]


# ---------- read state ----------

def get_read_state(user_id: str, workspace_id: str) -> dict[str, int]:
    rows = conn().execute(
        "SELECT r.channel_id, r.last_read_seq FROM read_state r "
        "JOIN channels c ON c.id=r.channel_id "
        "WHERE r.user_id=? AND c.workspace_id=?",
        (user_id, workspace_id),
    ).fetchall()
    return {r["channel_id"]: r["last_read_seq"] for r in rows}


def update_read_state(user_id: str, channel_id: str, seq: int) -> None:
    cur = conn()
    row = cur.execute(
        "SELECT last_read_seq FROM read_state WHERE user_id=? AND channel_id=?",
        (user_id, channel_id),
    ).fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO read_state (user_id, channel_id, last_read_seq) VALUES (?, ?, ?)",
            (user_id, channel_id, max(0, seq)),
        )
    else:
        new_seq = max(row["last_read_seq"], seq)
        cur.execute(
            "UPDATE read_state SET last_read_seq=? WHERE user_id=? AND channel_id=?",
            (new_seq, user_id, channel_id),
        )


# ---------- search ----------

def search_messages(user_id: str, query: str, workspace_slug: str | None = None) -> list[dict]:
    cur = conn()
    args: list[Any] = []
    q = (
        "SELECT m.id FROM messages m "
        "JOIN channels c ON c.id=m.channel_id "
        "JOIN workspaces w ON w.id=c.workspace_id "
        "WHERE m.deleted_at IS NULL AND m.body LIKE ? "
        "AND ("
        "  c.is_private=0 OR EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=c.id AND cm.user_id=?)"
        ") "
    )
    args.append(f"%{query}%")
    args.append(user_id)
    if workspace_slug:
        q += " AND w.slug=?"
        args.append(workspace_slug)
    q += " ORDER BY m.created_at DESC LIMIT 50"
    rows = cur.execute(q, args).fetchall()
    return [m for m in (build_message(r["id"]) for r in rows) if m is not None]


# ---------- events / replay ----------

def replay_events(channel_id: str, since_seq: int, limit: int = 500) -> list[dict]:
    rows = conn().execute(
        "SELECT payload FROM events WHERE channel_id=? AND seq > ? ORDER BY seq ASC LIMIT ?",
        (channel_id, since_seq, limit),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def earliest_event_seq(channel_id: str) -> int:
    row = conn().execute(
        "SELECT MIN(seq) AS s FROM events WHERE channel_id=?", (channel_id,)
    ).fetchone()
    return (row["s"] if row and row["s"] is not None else 0)


def latest_global_event_id() -> int:
    row = conn().execute("SELECT MAX(event_id) AS e FROM events").fetchone()
    return row["e"] or 0


def events_after_global(after_event_id: int, limit: int = 200) -> list[tuple[int, dict]]:
    rows = conn().execute(
        "SELECT event_id, payload FROM events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
        (after_event_id, limit),
    ).fetchall()
    return [(r["event_id"], json.loads(r["payload"])) for r in rows]


def event_subscribers_for_user(user_id: str, channel_id: str) -> bool:
    """Whether user can receive events for this channel."""
    cur = conn()
    ch = cur.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not ch:
        return False
    if ch["is_private"] or ch["is_dm"]:
        return bool(cur.execute(
            "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
            (channel_id, user_id),
        ).fetchone())
    # public channel: user must be in workspace
    return bool(cur.execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (ch["workspace_id"], user_id),
    ).fetchone())
