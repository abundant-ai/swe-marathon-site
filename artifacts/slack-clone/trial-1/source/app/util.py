"""Small helpers shared by the HTTP node and the IRC gateway."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from . import db


SLUG_RE = re.compile(r"^[a-z0-9-]{2,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
GROUP_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MENTION_RE = re.compile(r"(?:(?<=^)|(?<=[\s,.!?:;()\[\]{}]))@([A-Za-z0-9_-]{2,32})")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Roles ordered weakest -> strongest.
ROLE_RANK = {"guest": 0, "member": 1, "admin": 2, "owner": 3}


def public_user(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "timezone": row["timezone"] or "UTC",
        "avatar_url": row["avatar_url"] or "",
        "status_text": row["status_text"] or "",
        "status_emoji": row["status_emoji"] or "",
    }


def public_workspace(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "join_mode": row["join_mode"],
        "created_at": db.iso(row["created_at"]),
    }


def public_channel(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "is_private": bool(row["is_private"]),
        "is_dm": bool(row["is_dm"]),
        "topic": row["topic"] or "",
        "is_archived": bool(row["is_archived"]),
        "head_seq": row["head_seq"],
        "created_at": db.iso(row["created_at"]),
    }


def public_file(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "uploader_id": row["uploader_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "created_at": db.iso(row["created_at"]),
    }


def public_invitation(row: sqlite3.Row | dict) -> dict:
    return {
        "code": row["code"],
        "workspace_id": row["workspace_id"],
        "inviter_id": row["inviter_id"],
        "email": row["email"],
        "invited_username": row["invited_username"],
        "expires_at": db.iso(row["expires_at"]) if row["expires_at"] else None,
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "created_at": db.iso(row["created_at"]),
    }


def public_group(workspace_id: str, row: sqlite3.Row | dict) -> dict:
    members = [r["user_id"] for r in db.conn().execute(
        "SELECT user_id FROM user_group_members WHERE workspace_id = ? AND handle = ? ORDER BY user_id",
        (workspace_id, row["handle"]),
    ).fetchall()]
    return {
        "handle": row["handle"],
        "name": row["name"],
        "member_user_ids": members,
    }


def get_user(user_id: str) -> Optional[dict]:
    row = db.conn().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return public_user(row) if row else None


def get_user_by_name(username: str) -> Optional[dict]:
    row = db.conn().execute(
        "SELECT * FROM users WHERE lower(username) = lower(?)", (username,)
    ).fetchone()
    return public_user(row) if row else None


def workspace_role(workspace_id: str, user_id: str) -> Optional[str]:
    row = db.conn().execute(
        "SELECT role FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    return row["role"] if row else None


def is_admin_or_owner(role: Optional[str]) -> bool:
    return role in ("admin", "owner")


def channel_is_member(channel_id: str, user_id: str) -> bool:
    row = db.conn().execute(
        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
        (channel_id, user_id),
    ).fetchone()
    return row is not None


def message_files(message_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT f.* FROM message_files mf JOIN files f ON f.id = mf.file_id "
        "WHERE mf.message_id = ?",
        (message_id,),
    ).fetchall()
    return [public_file(r) for r in rows]


def message_reactions(message_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT emoji, user_id FROM reactions WHERE message_id = ? ORDER BY emoji, user_id",
        (message_id,),
    ).fetchall()
    bucket: dict[str, list[str]] = {}
    for r in rows:
        bucket.setdefault(r["emoji"], []).append(r["user_id"])
    return [{"emoji": e, "count": len(uids), "user_ids": uids} for e, uids in bucket.items()]


def message_mentions(message_id: str) -> list[str]:
    rows = db.conn().execute(
        "SELECT user_id FROM message_mentions WHERE message_id = ?",
        (message_id,),
    ).fetchall()
    return [r["user_id"] for r in rows]


def public_message(row: sqlite3.Row | dict) -> dict:
    author = get_user(row["author_id"]) or {
        "id": row["author_id"],
        "username": "(deleted)",
        "display_name": "(deleted)",
        "timezone": "UTC",
        "avatar_url": "",
        "status_text": "",
        "status_emoji": "",
    }
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "author": author,
        "body": row["body"],
        "parent_id": row["parent_id"],
        "reply_count": row["reply_count"],
        "created_at": db.iso(row["created_at"]),
        "edited_at": db.iso(row["edited_at"]) if row["edited_at"] else None,
        "deleted": bool(row["deleted"]),
        "files": message_files(row["id"]),
        "reactions": message_reactions(row["id"]),
        "mentions": message_mentions(row["id"]),
        "seq": row["seq"],
    }


def resolve_mentions(workspace_id: str, channel_id: str, body: str, author_id: str) -> list[str]:
    """Return a deduped list of user ids referenced by `@username` and `@group-handle`."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in MENTION_RE.finditer(body):
        handle = m.group(1)
        # Skip email-shaped mentions: if "@" is preceded by alnum, the regex
        # already excludes via lookbehind, but be paranoid.
        u = db.conn().execute(
            "SELECT id FROM users WHERE lower(username) = lower(?)", (handle,)
        ).fetchone()
        if u:
            uid = u["id"]
            if uid == author_id:
                continue
            wm = db.conn().execute(
                "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, uid),
            ).fetchone()
            if wm and uid not in seen_set:
                seen.append(uid)
                seen_set.add(uid)
            continue
        g = db.conn().execute(
            "SELECT 1 FROM user_groups WHERE workspace_id = ? AND handle = ?",
            (workspace_id, handle.lower()),
        ).fetchone()
        if g:
            for r in db.conn().execute(
                "SELECT user_id FROM user_group_members WHERE workspace_id = ? AND handle = ?",
                (workspace_id, handle.lower()),
            ).fetchall():
                uid = r["user_id"]
                if uid == author_id:
                    continue
                if uid in seen_set:
                    continue
                seen.append(uid)
                seen_set.add(uid)
    return seen


def channel_workspace_id(channel_id: str) -> Optional[str]:
    row = db.conn().execute(
        "SELECT workspace_id FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    return row["workspace_id"] if row else None
