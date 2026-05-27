"""HTTP node — REST + WebSocket server for one Huddle node."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Optional

from aiohttp import WSMsgType, web

from . import db, util
from .broker import BrokerClient


NODE_ID = int(os.environ.get("HUDDLE_NODE_ID", "0"))
HTTP_PORT = int(os.environ.get("HUDDLE_HTTP_PORT", str(8000 + NODE_ID)))
HTTP_HOST = os.environ.get("HUDDLE_HTTP_HOST", "127.0.0.1")
MAX_FILE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Subscriptions: channel_id -> set of (queue, user_id)
# ---------------------------------------------------------------------------

class HubState:
    def __init__(self):
        self.subs: dict[str, set[asyncio.Queue]] = {}
        self.last_event_id: int = 0
        self.broker_alive: bool = False

    def subscribe(self, channel_id: str, q: asyncio.Queue):
        self.subs.setdefault(channel_id, set()).add(q)

    def unsubscribe(self, channel_id: str, q: asyncio.Queue):
        s = self.subs.get(channel_id)
        if s:
            s.discard(q)
            if not s:
                self.subs.pop(channel_id, None)

    def fanout(self, channel_id: str, frame: dict):
        for q in list(self.subs.get(channel_id, ())):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass


# ---------------------------------------------------------------------------
# Helpers for request handling
# ---------------------------------------------------------------------------

def json_response(obj: Any, status: int = 200) -> web.Response:
    return web.json_response(obj, status=status)


def err(status: int, msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=status)


async def get_body(req: web.Request) -> dict:
    if req.content_type and "application/json" not in req.content_type:
        return {}
    try:
        data = await req.json()
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def auth_user(req: web.Request) -> Optional[dict]:
    auth = req.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
    if not token:
        token = req.query.get("token", "")
    if not token:
        return None
    row = db.conn().execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token = ?",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def require_auth(req: web.Request) -> dict:
    u = auth_user(req)
    if not u:
        raise web.HTTPUnauthorized(reason="auth required",
                                   text=json.dumps({"error": "unauthenticated"}),
                                   content_type="application/json")
    return u


# ---------------------------------------------------------------------------
# Event commit: write event to DB + push to local subs + broker notify
# ---------------------------------------------------------------------------

async def commit_and_broadcast(app, channel_id: str, kind: str, payload: dict) -> tuple[int, int]:
    """Commit a new event row inside the active transaction and queue an
    after-commit broadcast. Caller must already be inside a transaction.
    """
    eid, seq = db.commit_event(channel_id, kind, payload)
    pending = app["pending_broadcasts"]
    pending.append((eid, channel_id, seq, kind, payload))
    return eid, seq


async def flush_broadcasts(app):
    pending = app["pending_broadcasts"]
    if not pending:
        return
    hub: HubState = app["hub"]
    bc: BrokerClient = app["broker"]
    for eid, channel_id, seq, kind, payload in pending:
        frame = {"type": kind, "seq": seq, "channel_id": channel_id, **payload}
        hub.fanout(channel_id, frame)
        if eid > hub.last_event_id:
            hub.last_event_id = eid
        await bc.publish({"id": eid, "channel_id": channel_id, "seq": seq, "kind": kind})
    pending.clear()


# A small context manager for a SQLite IMMEDIATE transaction.
class Tx:
    def __init__(self, app):
        self.app = app
    def __enter__(self):
        self.c = db.tx_begin()
        # reset pending list per-tx
        self.app["pending_broadcasts"] = []
        return self.c
    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            db.tx_commit()
        else:
            db.tx_rollback()
            self.app["pending_broadcasts"] = []


def in_tx(handler):
    """Decorator: run handler inside a SQLite IMMEDIATE transaction.

    On success, flush any pending broadcasts after commit. Convert
    web.HTTPException raised inside cleanly.
    """
    async def wrapper(req: web.Request):
        app = req.app
        try:
            with Tx(app):
                resp = await handler(req)
            await flush_broadcasts(app)
            return resp
        except web.HTTPException:
            raise
    return wrapper


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

async def health(req):
    return json_response({"status": "ok", "node_id": NODE_ID})


@in_tx
async def register(req):
    body = await get_body(req)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    display_name = (body.get("display_name") or username).strip()
    if not username or not util.USERNAME_RE.match(username):
        return err(400, "invalid username")
    if not isinstance(password, str) or len(password) < 8:
        return err(400, "password too short")
    existing = db.conn().execute(
        "SELECT 1 FROM users WHERE lower(username) = lower(?)", (username,)
    ).fetchone()
    if existing:
        return err(409, "username taken")
    uid = db.gen_id()
    db.conn().execute(
        "INSERT INTO users(id, username, password_hash, display_name, timezone, avatar_url, status_text, status_emoji, created_at) "
        "VALUES (?, ?, ?, ?, 'UTC', '', '', '', ?)",
        (uid, username, db.hash_password(password), display_name or username, db.now_ms()),
    )
    token = db.gen_token()
    db.conn().execute("INSERT INTO tokens(token, user_id, created_at) VALUES (?, ?, ?)",
                      (token, uid, db.now_ms()))
    user = util.get_user(uid)
    return json_response({"user": user, "token": token}, status=201)


@in_tx
async def login(req):
    body = await get_body(req)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not isinstance(password, str):
        return err(400, "missing fields")
    row = db.conn().execute(
        "SELECT * FROM users WHERE lower(username) = lower(?)", (username,)
    ).fetchone()
    if not row or not db.verify_password(password, row["password_hash"]):
        return err(401, "invalid credentials")
    token = db.gen_token()
    db.conn().execute("INSERT INTO tokens(token, user_id, created_at) VALUES (?, ?, ?)",
                      (token, row["id"], db.now_ms()))
    return json_response({"user": util.public_user(row), "token": token})


async def me(req):
    u = require_auth(req)
    return json_response({"user": util.public_user(u)})


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

async def get_user_route(req):
    u = require_auth(req)
    target = db.conn().execute(
        "SELECT * FROM users WHERE id = ?", (req.match_info["id"],)
    ).fetchone()
    if not target:
        return err(404, "no such user")
    return json_response({"user": util.public_user(target)})


@in_tx
async def patch_me(req):
    u = require_auth(req)
    body = await get_body(req)
    fields = {}
    if "display_name" in body:
        v = body["display_name"]
        if not isinstance(v, str) or not (1 <= len(v) <= 64):
            return err(400, "display_name length")
        fields["display_name"] = v
    if "timezone" in body:
        v = body["timezone"]
        if not isinstance(v, str) or not (1 <= len(v) <= 64):
            return err(400, "timezone")
        fields["timezone"] = v
    if "avatar_url" in body:
        v = body["avatar_url"]
        if not isinstance(v, str) or len(v) > 1024:
            return err(400, "avatar_url")
        fields["avatar_url"] = v
    if "status_text" in body:
        v = body["status_text"]
        if not isinstance(v, str) or len(v) > 200:
            return err(400, "status_text")
        fields["status_text"] = v
    if "status_emoji" in body:
        v = body["status_emoji"]
        if not isinstance(v, str) or len(v) > 32:
            return err(400, "status_emoji")
        fields["status_emoji"] = v
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [u["id"]]
        db.conn().execute(f"UPDATE users SET {cols} WHERE id = ?", params)
    fresh = db.conn().execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone()
    user = util.public_user(fresh)
    # Broadcast to the user's channels for any subscribers tracking presence.
    for r in db.conn().execute(
        "SELECT channel_id FROM channel_members WHERE user_id = ?", (u["id"],)
    ).fetchall():
        await commit_and_broadcast(req.app, r["channel_id"], "user.updated", {"user": user})
    return json_response({"user": user})


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

@in_tx
async def create_workspace(req):
    u = require_auth(req)
    body = await get_body(req)
    slug = (body.get("slug") or "").strip().lower()
    name = (body.get("name") or "").strip()
    if not util.SLUG_RE.match(slug):
        return err(400, "invalid slug")
    if not name or len(name) > 64:
        return err(400, "invalid name")
    existing = db.conn().execute("SELECT 1 FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if existing:
        return err(409, "workspace slug taken")
    wid = db.gen_id()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO workspaces(id, slug, name, owner_id, join_mode, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (wid, slug, name, u["id"], now),
    )
    db.conn().execute(
        "INSERT INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (wid, u["id"], now),
    )
    cid = db.gen_id()
    db.conn().execute(
        "INSERT INTO channels(id, workspace_id, name, is_private, is_dm, topic, is_archived, head_seq, created_at) "
        "VALUES (?, ?, 'general', 0, 0, '', 0, 0, ?)",
        (cid, wid, now),
    )
    db.conn().execute(
        "INSERT INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, u["id"], now),
    )
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (wid,)).fetchone()
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    return json_response({
        "workspace": util.public_workspace(ws),
        "general_channel": util.public_channel(ch),
    }, status=201)


async def list_workspaces(req):
    u = require_auth(req)
    rows = db.conn().execute(
        "SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id = w.id "
        "WHERE m.user_id = ? ORDER BY w.created_at ASC",
        (u["id"],),
    ).fetchall()
    return json_response({"workspaces": [util.public_workspace(w) for w in rows]})


def _ws_detail(workspace_id: str, user_id: str, include_archived: bool = False) -> dict:
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    member_role = util.workspace_role(workspace_id, user_id)
    is_member = member_role is not None
    chan_q = "SELECT * FROM channels WHERE workspace_id = ?"
    chan_args = [workspace_id]
    if not include_archived:
        chan_q += " AND is_archived = 0"
    chan_q += " ORDER BY is_dm ASC, name ASC"
    channels = []
    read_state = {}
    for c in db.conn().execute(chan_q, chan_args).fetchall():
        # Hide private/DM channels the user is not a member of.
        if c["is_private"] or c["is_dm"]:
            if not db.conn().execute(
                "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
                (c["id"], user_id),
            ).fetchone():
                continue
        channels.append(util.public_channel(c))
        cm = db.conn().execute(
            "SELECT last_read_seq FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (c["id"], user_id),
        ).fetchone()
        if cm:
            read_state[c["id"]] = cm["last_read_seq"]
    members = []
    for m in db.conn().execute(
        "SELECT m.user_id, m.role, m.joined_at, u.* FROM workspace_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? ORDER BY m.joined_at ASC",
        (workspace_id,),
    ).fetchall():
        members.append({
            "user_id": m["user_id"],
            "role": m["role"],
            "joined_at": db.iso(m["joined_at"]),
            "user": util.public_user(m),
        })
    return {
        "workspace": util.public_workspace(ws),
        "channels": channels,
        "members": members,
        "read_state": read_state,
        "viewer_role": member_role,
    }


async def get_workspace(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    include_archived = req.query.get("include_archived") in ("1", "true", "True")
    detail = _ws_detail(ws["id"], u["id"], include_archived=include_archived)
    return json_response(detail)


async def workspace_members(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    members = []
    for m in db.conn().execute(
        "SELECT m.user_id, m.role, m.joined_at, u.* FROM workspace_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? ORDER BY m.joined_at ASC",
        (ws["id"],),
    ).fetchall():
        members.append({
            "user_id": m["user_id"],
            "role": m["role"],
            "joined_at": db.iso(m["joined_at"]),
            "user": util.public_user(m),
        })
    return json_response({"members": members})


@in_tx
async def patch_workspace(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    role = util.workspace_role(ws["id"], u["id"])
    if not util.is_admin_or_owner(role):
        return err(403, "forbidden")
    body = await get_body(req)
    fields = {}
    if "name" in body:
        v = body["name"]
        if not isinstance(v, str) or not (1 <= len(v) <= 64):
            return err(400, "invalid name")
        fields["name"] = v
    if "join_mode" in body:
        v = body["join_mode"]
        if v not in ("open", "invite_only"):
            return err(400, "invalid join_mode")
        fields["join_mode"] = v
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [ws["id"]]
        db.conn().execute(f"UPDATE workspaces SET {cols} WHERE id = ?", params)
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (ws["id"],)).fetchone()
    return json_response({"workspace": util.public_workspace(ws)})


@in_tx
async def patch_workspace_member(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    target_id = req.match_info["user_id"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    actor_role = util.workspace_role(ws["id"], u["id"])
    if not util.is_admin_or_owner(actor_role):
        return err(403, "forbidden")
    target_row = db.conn().execute(
        "SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (ws["id"], target_id),
    ).fetchone()
    if not target_row:
        return err(404, "no such member")
    body = await get_body(req)
    new_role = body.get("role")
    if new_role not in ("admin", "member", "guest", "owner"):
        return err(400, "invalid role")
    if new_role == "owner":
        return err(400, "use transfer_ownership")
    if target_row["role"] == "owner":
        return err(403, "cannot demote owner")
    if target_row["role"] == "admin" and actor_role != "owner":
        return err(403, "admins cannot modify another admin")
    db.conn().execute(
        "UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?",
        (new_role, ws["id"], target_id),
    )
    return json_response({"member": {"user_id": target_id, "role": new_role}})


@in_tx
async def transfer_ownership(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    if ws["owner_id"] != u["id"]:
        return err(403, "owner only")
    body = await get_body(req)
    target_id = body.get("user_id")
    if not target_id:
        return err(400, "user_id required")
    target = db.conn().execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (ws["id"], target_id),
    ).fetchone()
    if not target:
        return err(404, "target not a member")
    db.conn().execute("UPDATE workspaces SET owner_id = ? WHERE id = ?", (target_id, ws["id"]))
    db.conn().execute(
        "UPDATE workspace_members SET role = 'admin' WHERE workspace_id = ? AND user_id = ?",
        (ws["id"], u["id"]),
    )
    db.conn().execute(
        "UPDATE workspace_members SET role = 'owner' WHERE workspace_id = ? AND user_id = ?",
        (ws["id"], target_id),
    )
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (ws["id"],)).fetchone()
    return json_response({"workspace": util.public_workspace(ws)})


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@in_tx
async def create_channel(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    role = util.workspace_role(ws["id"], u["id"])
    if not role:
        return err(403, "not a workspace member")
    body = await get_body(req)
    name = (body.get("name") or "").strip().lower()
    is_private = bool(body.get("is_private"))
    topic = (body.get("topic") or "").strip()
    if not util.CHANNEL_NAME_RE.match(name):
        return err(400, "invalid channel name")
    if len(topic) > 250:
        return err(400, "topic too long")
    if role == "guest":
        return err(403, "guests cannot create channels")
    if is_private and not util.is_admin_or_owner(role):
        return err(403, "only admins create private channels")
    existing = db.conn().execute(
        "SELECT 1 FROM channels WHERE workspace_id = ? AND name = ?",
        (ws["id"], name),
    ).fetchone()
    if existing:
        return err(409, "channel name taken")
    cid = db.gen_id()
    now = db.now_ms()
    db.conn().execute(
        "INSERT INTO channels(id, workspace_id, name, is_private, is_dm, topic, is_archived, head_seq, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?, 0, 0, ?)",
        (cid, ws["id"], name, 1 if is_private else 0, topic, now),
    )
    db.conn().execute(
        "INSERT INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, u["id"], now),
    )
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    return json_response({"channel": util.public_channel(ch)}, status=201)


@in_tx
async def patch_channel(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    role = util.workspace_role(ch["workspace_id"], u["id"])
    if not util.is_admin_or_owner(role):
        return err(403, "forbidden")
    body = await get_body(req)
    fields = {}
    if "topic" in body:
        v = body["topic"]
        if not isinstance(v, str) or len(v) > 250:
            return err(400, "topic too long")
        fields["topic"] = v
    if "name" in body:
        v = (body["name"] or "").strip().lower()
        if not util.CHANNEL_NAME_RE.match(v):
            return err(400, "invalid channel name")
        if v != ch["name"]:
            existing = db.conn().execute(
                "SELECT 1 FROM channels WHERE workspace_id = ? AND name = ? AND id != ?",
                (ch["workspace_id"], v, cid),
            ).fetchone()
            if existing:
                return err(409, "channel name taken")
        fields["name"] = v
    if "is_archived" in body:
        v = bool(body["is_archived"])
        fields["is_archived"] = 1 if v else 0
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [cid]
        db.conn().execute(f"UPDATE channels SET {cols} WHERE id = ?", params)
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    payload = {"channel": util.public_channel(ch)}
    await commit_and_broadcast(req.app, cid, "channel.updated", payload)
    return json_response(payload)


@in_tx
async def join_channel(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    role = util.workspace_role(ch["workspace_id"], u["id"])
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (ch["workspace_id"],)).fetchone()
    if not role:
        if ch["is_private"] or ws["join_mode"] != "open":
            return err(403, "not a workspace member")
        # Auto-onboard.
        db.conn().execute(
            "INSERT OR IGNORE INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
            (ws["id"], u["id"], db.now_ms()),
        )
        role = "member"
    if role == "guest" and not ch["is_private"]:
        return err(403, "guests cannot join channels")
    if ch["is_private"]:
        already = db.conn().execute(
            "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
            (cid, u["id"]),
        ).fetchone()
        if not already:
            return err(403, "private channel; need invite")
    db.conn().execute(
        "INSERT OR IGNORE INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, u["id"], db.now_ms()),
    )
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    payload = {"user": util.public_user(u)}
    await commit_and_broadcast(req.app, cid, "member.joined", payload)
    return json_response({"channel": util.public_channel(ch)})


@in_tx
async def leave_channel(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    db.conn().execute(
        "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
        (cid, u["id"]),
    )
    payload = {"user_id": u["id"]}
    await commit_and_broadcast(req.app, cid, "member.left", payload)
    return web.Response(status=204)


@in_tx
async def add_channel_member(req):
    """Helper for the SPA to add an existing workspace member to a channel."""
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    role = util.workspace_role(ch["workspace_id"], u["id"])
    if not role:
        return err(403, "forbidden")
    if not util.channel_is_member(cid, u["id"]) and not util.is_admin_or_owner(role):
        return err(403, "forbidden")
    body = await get_body(req)
    target_username = (body.get("username") or "").strip()
    target_id = body.get("user_id")
    if target_username and not target_id:
        ur = db.conn().execute(
            "SELECT id FROM users WHERE lower(username) = lower(?)", (target_username,)
        ).fetchone()
        if ur:
            target_id = ur["id"]
    if not target_id:
        return err(400, "user not found")
    target = db.conn().execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
        (ch["workspace_id"], target_id),
    ).fetchone()
    if not target:
        return err(400, "target not a workspace member")
    db.conn().execute(
        "INSERT OR IGNORE INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, target_id, db.now_ms()),
    )
    user = util.get_user(target_id)
    await commit_and_broadcast(req.app, cid, "member.joined", {"user": user})
    return json_response({"member": {"user_id": target_id, "user": user}})


async def list_channel_members(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
        return err(403, "private channel")
    members = []
    for m in db.conn().execute(
        "SELECT u.*, cm.joined_at, cm.last_read_seq FROM channel_members cm "
        "JOIN users u ON u.id = cm.user_id WHERE cm.channel_id = ? ORDER BY cm.joined_at ASC",
        (cid,),
    ).fetchall():
        ws_role = util.workspace_role(ch["workspace_id"], m["id"])
        members.append({
            "user_id": m["id"],
            "user": util.public_user(m),
            "joined_at": db.iso(m["joined_at"]),
            "last_read_seq": m["last_read_seq"],
            "role": ws_role or "guest",
        })
    return json_response({"members": members})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def _slash_command(body: str) -> tuple[str, str]:
    """Return (effective_body, command_action)."""
    return body, ""


@in_tx
async def post_message(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    body = await get_body(req)
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        return err(400, "empty body")
    parent_id = body.get("parent_id")
    file_ids = body.get("file_ids") or []
    if not isinstance(file_ids, list):
        return err(400, "file_ids must be list")
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    if ch["is_archived"]:
        return err(423, "channel archived")
    role = util.workspace_role(ch["workspace_id"], u["id"])
    if not role:
        return err(403, "not a workspace member")
    if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
        return err(403, "not a channel member")
    # Slash commands
    raw = text
    posted_body = text
    pre_action = None
    if posted_body.startswith("/") and not posted_body.startswith("//"):
        # parse
        rest = posted_body[1:]
        cmd, _, args = rest.partition(" ")
        cmd = cmd.lower()
        args = args.strip()
        if cmd == "me":
            posted_body = "/me " + args  # canonical
            posted_body = "/me " + args
            posted_body = raw  # keep original verbatim per spec
        elif cmd == "shrug":
            posted_body = (args + " " if args else "") + "¯\\_(ツ)_/¯"
        elif cmd == "topic":
            if not util.is_admin_or_owner(role):
                return err(403, "only admins set topic")
            new_topic = args[:250]
            db.conn().execute("UPDATE channels SET topic = ? WHERE id = ?", (new_topic, cid))
            updated = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
            await commit_and_broadcast(req.app, cid, "channel.updated",
                                       {"channel": util.public_channel(updated)})
            return json_response({"channel": util.public_channel(updated)})
        elif cmd == "invite":
            mention_handle = args.lstrip("@")
            target = db.conn().execute(
                "SELECT id FROM users WHERE lower(username) = lower(?)", (mention_handle,)
            ).fetchone()
            if not target:
                return err(400, "no such user")
            wm = db.conn().execute(
                "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
                (ch["workspace_id"], target["id"]),
            ).fetchone()
            if not wm:
                return err(400, "not a workspace member")
            db.conn().execute(
                "INSERT OR IGNORE INTO channel_members(channel_id, user_id, joined_at, last_read_seq) "
                "VALUES (?, ?, ?, 0)",
                (cid, target["id"], db.now_ms()),
            )
            await commit_and_broadcast(req.app, cid, "member.joined",
                                       {"user": util.get_user(target["id"])})
            return json_response({"ok": True})
        elif cmd == "archive":
            if not util.is_admin_or_owner(role):
                return err(403, "only admins archive")
            db.conn().execute("UPDATE channels SET is_archived = 1 WHERE id = ?", (cid,))
            updated = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
            await commit_and_broadcast(req.app, cid, "channel.updated",
                                       {"channel": util.public_channel(updated)})
            return json_response({"channel": util.public_channel(updated)})
        elif cmd == "unarchive":
            if not util.is_admin_or_owner(role):
                return err(403, "only admins unarchive")
            db.conn().execute("UPDATE channels SET is_archived = 0 WHERE id = ?", (cid,))
            updated = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
            await commit_and_broadcast(req.app, cid, "channel.updated",
                                       {"channel": util.public_channel(updated)})
            return json_response({"channel": util.public_channel(updated)})
        else:
            return err(400, "unknown command")
    elif posted_body.startswith("//"):
        posted_body = posted_body[1:]
    if parent_id:
        prow = db.conn().execute(
            "SELECT id, channel_id FROM messages WHERE id = ? AND deleted = 0",
            (parent_id,),
        ).fetchone()
        if not prow or prow["channel_id"] != cid:
            return err(400, "invalid parent")
    mid = db.gen_id()
    now = db.now_ms()
    eid, seq = db.commit_event(cid, "__placeholder__", {})
    # Replace placeholder with real event row. Easier: insert message first
    # using a fresh seq we just got, then patch the event. Simpler still:
    # just insert message with seq we already computed and rewrite event.
    db.conn().execute(
        "INSERT INTO messages(id, channel_id, author_id, body, parent_id, created_at, edited_at, deleted, seq, reply_count) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, 0)",
        (mid, cid, u["id"], posted_body, parent_id, now, seq),
    )
    # Validate file_ids belong to caller and aren't already attached.
    for fid in file_ids:
        f = db.conn().execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
        if not f or f["uploader_id"] != u["id"] or f["message_id"]:
            return err(400, "invalid file_ids")
        db.conn().execute(
            "INSERT INTO message_files(message_id, file_id) VALUES (?, ?)", (mid, fid),
        )
        db.conn().execute("UPDATE files SET message_id = ? WHERE id = ?", (mid, fid))
    # Mentions
    mentions = util.resolve_mentions(ch["workspace_id"], cid, posted_body, u["id"])
    for uid in mentions:
        db.conn().execute(
            "INSERT OR IGNORE INTO message_mentions(message_id, user_id) VALUES (?, ?)",
            (mid, uid),
        )
    # Reply count
    if parent_id:
        db.conn().execute(
            "UPDATE messages SET reply_count = reply_count + 1 WHERE id = ?", (parent_id,)
        )
    # Patch the placeholder event with the real payload now that the message
    # row exists. We use the same event id and seq; update kind & payload.
    msg_row = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    msg_obj = util.public_message(msg_row)
    kind = "message.reply" if parent_id else "message.new"
    db.conn().execute(
        "UPDATE events SET kind = ?, payload = ? WHERE id = ?",
        (kind, json.dumps({"message": msg_obj}), eid),
    )
    req.app["pending_broadcasts"].append((eid, cid, seq, kind, {"message": msg_obj}))
    if parent_id:
        # Bump parent reply_count for any clients listening on this channel.
        parent_row = db.conn().execute("SELECT * FROM messages WHERE id = ?", (parent_id,)).fetchone()
        if parent_row:
            parent_obj = util.public_message(parent_row)
            await commit_and_broadcast(req.app, cid, "message.edited", {"message": parent_obj})
    return json_response({"message": msg_obj}, status=201)


async def list_messages(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
        return err(403, "private channel")
    try:
        limit = int(req.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    before = req.query.get("before")
    q = "SELECT * FROM messages WHERE channel_id = ? AND deleted = 0 AND parent_id IS NULL"
    args = [cid]
    if before:
        b = db.conn().execute("SELECT seq FROM messages WHERE id = ?", (before,)).fetchone()
        if b:
            q += " AND seq < ?"
            args.append(b["seq"])
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    rows = db.conn().execute(q, args).fetchall()
    return json_response({"messages": [util.public_message(r) for r in rows]})


@in_tx
async def edit_message(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    body = await get_body(req)
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        return err(400, "empty body")
    row = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    if not row or row["deleted"]:
        return err(404, "no such message")
    if row["author_id"] != u["id"]:
        return err(403, "not author")
    db.conn().execute(
        "UPDATE messages SET body = ?, edited_at = ? WHERE id = ?",
        (text, db.now_ms(), mid),
    )
    # Re-resolve mentions
    db.conn().execute("DELETE FROM message_mentions WHERE message_id = ?", (mid,))
    ws_id = util.channel_workspace_id(row["channel_id"])
    for uid in util.resolve_mentions(ws_id, row["channel_id"], text, u["id"]):
        db.conn().execute(
            "INSERT OR IGNORE INTO message_mentions(message_id, user_id) VALUES (?, ?)",
            (mid, uid),
        )
    fresh = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    obj = util.public_message(fresh)
    await commit_and_broadcast(req.app, row["channel_id"], "message.edited", {"message": obj})
    return json_response({"message": obj})


@in_tx
async def delete_message(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    row = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    if not row:
        return web.Response(status=204)
    if row["deleted"]:
        return web.Response(status=204)
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (row["channel_id"],)).fetchone()
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (ch["workspace_id"],)).fetchone()
    if row["author_id"] != u["id"] and ws["owner_id"] != u["id"]:
        return err(403, "forbidden")
    db.conn().execute(
        "UPDATE messages SET deleted = 1, body = '' WHERE id = ?", (mid,),
    )
    if row["parent_id"]:
        db.conn().execute(
            "UPDATE messages SET reply_count = MAX(reply_count - 1, 0) WHERE id = ?",
            (row["parent_id"],),
        )
    await commit_and_broadcast(req.app, row["channel_id"], "message.deleted",
                               {"message_id": mid})
    return web.Response(status=204)


async def list_replies(req):
    u = require_auth(req)
    pid = req.match_info["parent_id"]
    parent = db.conn().execute("SELECT * FROM messages WHERE id = ?", (pid,)).fetchone()
    if not parent:
        return err(404, "no such message")
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (parent["channel_id"],)).fetchone()
    if ch["is_private"] and not util.channel_is_member(ch["id"], u["id"]):
        return err(403, "private channel")
    rows = db.conn().execute(
        "SELECT * FROM messages WHERE parent_id = ? AND deleted = 0 ORDER BY seq ASC",
        (pid,),
    ).fetchall()
    return json_response({"messages": [util.public_message(r) for r in rows]})


@in_tx
async def add_reaction(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    body = await get_body(req)
    emoji = (body.get("emoji") or "").strip()
    if not emoji or len(emoji) > 64:
        return err(400, "invalid emoji")
    row = db.conn().execute("SELECT * FROM messages WHERE id = ? AND deleted = 0", (mid,)).fetchone()
    if not row:
        return err(404, "no such message")
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (row["channel_id"],)).fetchone()
    if ch["is_private"] and not util.channel_is_member(ch["id"], u["id"]):
        return err(403, "private channel")
    db.conn().execute(
        "INSERT OR IGNORE INTO reactions(message_id, user_id, emoji) VALUES (?, ?, ?)",
        (mid, u["id"], emoji),
    )
    fresh = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    obj = util.public_message(fresh)
    await commit_and_broadcast(req.app, row["channel_id"], "reaction.added",
                               {"message_id": mid, "user_id": u["id"], "emoji": emoji,
                                "message": obj})
    return json_response({"message": obj})


@in_tx
async def remove_reaction(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    body = await get_body(req)
    emoji = (body.get("emoji") or "").strip()
    if not emoji:
        return err(400, "invalid emoji")
    row = db.conn().execute("SELECT * FROM messages WHERE id = ? AND deleted = 0", (mid,)).fetchone()
    if not row:
        return err(404, "no such message")
    db.conn().execute(
        "DELETE FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
        (mid, u["id"], emoji),
    )
    fresh = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    obj = util.public_message(fresh)
    await commit_and_broadcast(req.app, row["channel_id"], "reaction.removed",
                               {"message_id": mid, "user_id": u["id"], "emoji": emoji,
                                "message": obj})
    return json_response({"message": obj})


# ---------------------------------------------------------------------------
# DMs
# ---------------------------------------------------------------------------

@in_tx
async def create_dm(req):
    u = require_auth(req)
    body = await get_body(req)
    other = body.get("recipient_id")
    workspace_slug = body.get("workspace") or req.query.get("workspace")
    if not other:
        return err(400, "recipient_id required")
    if other == u["id"]:
        return err(400, "no DM with self")
    other_user = db.conn().execute("SELECT * FROM users WHERE id = ?", (other,)).fetchone()
    if not other_user:
        return err(404, "no such user")
    # Pick the first shared workspace.
    if workspace_slug:
        ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (workspace_slug,)).fetchone()
        if not ws:
            return err(404, "no such workspace")
    else:
        ws = db.conn().execute(
            "SELECT w.* FROM workspaces w JOIN workspace_members a ON a.workspace_id = w.id AND a.user_id = ? "
            "JOIN workspace_members b ON b.workspace_id = w.id AND b.user_id = ? LIMIT 1",
            (u["id"], other),
        ).fetchone()
        if not ws:
            return err(400, "no shared workspace")
    a, b = sorted([u["id"], other])
    existing = db.conn().execute(
        "SELECT channel_id FROM dms WHERE workspace_id = ? AND user_a = ? AND user_b = ?",
        (ws["id"], a, b),
    ).fetchone()
    if existing:
        ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (existing["channel_id"],)).fetchone()
        return json_response({"channel": util.public_channel(ch)}, status=200)
    cid = db.gen_id()
    now = db.now_ms()
    name = f"dm-{a[:6]}-{b[:6]}"
    db.conn().execute(
        "INSERT INTO channels(id, workspace_id, name, is_private, is_dm, topic, is_archived, head_seq, created_at) "
        "VALUES (?, ?, ?, 1, 1, '', 0, 0, ?)",
        (cid, ws["id"], name, now),
    )
    db.conn().execute(
        "INSERT INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, a, now),
    )
    db.conn().execute(
        "INSERT INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, b, now),
    )
    db.conn().execute(
        "INSERT INTO dms(workspace_id, user_a, user_b, channel_id) VALUES (?, ?, ?, ?)",
        (ws["id"], a, b, cid),
    )
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    return json_response({"channel": util.public_channel(ch)}, status=201)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

async def upload_file(req):
    u = require_auth(req)
    reader = await req.multipart()
    field = await reader.next()
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None:
        return err(400, "no file part")
    filename = field.filename or "upload.bin"
    content_type = field.headers.get("Content-Type", "application/octet-stream")
    fid = db.gen_id()
    storage_path = os.path.join(db.FILES_DIR, fid)
    size = 0
    with open(storage_path, "wb") as out:
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                out.close()
                try:
                    os.remove(storage_path)
                except OSError:
                    pass
                return err(413, "file too large")
            out.write(chunk)
    db.conn().execute(
        "INSERT INTO files(id, uploader_id, filename, content_type, size, storage_path, message_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        (fid, u["id"], filename, content_type, size, storage_path, db.now_ms()),
    )
    row = db.conn().execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    return json_response({"file": util.public_file(row)}, status=201)


async def get_file(req):
    require_auth(req)
    fid = req.match_info["id"]
    row = db.conn().execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    if not row:
        return err(404, "no such file")
    return json_response({"file": util.public_file(row)})


async def download_file(req):
    require_auth(req)
    fid = req.match_info["id"]
    row = db.conn().execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    if not row:
        return err(404, "no such file")
    return web.FileResponse(
        path=row["storage_path"],
        headers={
            "Content-Type": row["content_type"],
            "Content-Disposition": f'attachment; filename="{row["filename"]}"',
        },
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search(req):
    u = require_auth(req)
    q = (req.query.get("q") or "").strip()
    workspace_slug = req.query.get("workspace")
    if not q:
        return json_response({"messages": []})
    args = [u["id"]]
    sql = (
        "SELECT m.* FROM messages m JOIN channels c ON c.id = m.channel_id "
        "JOIN workspace_members wm ON wm.workspace_id = c.workspace_id AND wm.user_id = ? "
        "WHERE m.deleted = 0"
    )
    if workspace_slug:
        sql += " AND c.workspace_id = (SELECT id FROM workspaces WHERE slug = ?)"
        args.append(workspace_slug)
    sql += (" AND (c.is_private = 0 OR EXISTS (SELECT 1 FROM channel_members cm "
            "WHERE cm.channel_id = c.id AND cm.user_id = ?))")
    args.append(u["id"])
    sql += " AND m.body LIKE ? ORDER BY m.created_at DESC LIMIT 50"
    args.append(f"%{q}%")
    rows = db.conn().execute(sql, args).fetchall()
    return json_response({"messages": [util.public_message(r) for r in rows]})


# ---------------------------------------------------------------------------
# Read state
# ---------------------------------------------------------------------------

@in_tx
async def channel_read(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    body = await get_body(req)
    last = body.get("last_read_seq")
    if not isinstance(last, int) or last < 0:
        return err(400, "invalid last_read_seq")
    if not util.channel_is_member(cid, u["id"]):
        return err(403, "not a channel member")
    db.conn().execute(
        "UPDATE channel_members SET last_read_seq = MAX(last_read_seq, ?) "
        "WHERE channel_id = ? AND user_id = ?",
        (last, cid, u["id"]),
    )
    return json_response({"ok": True})


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

@in_tx
async def pin_message(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    row = db.conn().execute("SELECT * FROM messages WHERE id = ? AND deleted = 0", (mid,)).fetchone()
    if not row:
        return err(404, "no such message")
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (row["channel_id"],)).fetchone()
    if ch["is_private"] and not util.channel_is_member(ch["id"], u["id"]):
        return err(403, "forbidden")
    db.conn().execute(
        "INSERT OR IGNORE INTO pins(channel_id, message_id, user_id, pinned_at) VALUES (?, ?, ?, ?)",
        (ch["id"], mid, u["id"], db.now_ms()),
    )
    return json_response({"ok": True})


@in_tx
async def unpin_message(req):
    u = require_auth(req)
    mid = req.match_info["id"]
    db.conn().execute("DELETE FROM pins WHERE message_id = ?", (mid,))
    return web.Response(status=204)


async def list_pins(req):
    u = require_auth(req)
    cid = req.match_info["id"]
    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not ch:
        return err(404, "no such channel")
    if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
        return err(403, "private channel")
    rows = db.conn().execute(
        "SELECT m.* FROM pins p JOIN messages m ON m.id = p.message_id "
        "WHERE p.channel_id = ? AND m.deleted = 0 ORDER BY p.pinned_at DESC",
        (cid,),
    ).fetchall()
    return json_response({"messages": [util.public_message(r) for r in rows]})


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------

async def list_groups(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    if not util.workspace_role(ws["id"], u["id"]):
        return err(403, "not a member")
    rows = db.conn().execute(
        "SELECT * FROM user_groups WHERE workspace_id = ? ORDER BY handle ASC",
        (ws["id"],),
    ).fetchall()
    return json_response({"groups": [util.public_group(ws["id"], r) for r in rows]})


@in_tx
async def create_group(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    role = util.workspace_role(ws["id"], u["id"])
    if not util.is_admin_or_owner(role):
        return err(403, "forbidden")
    body = await get_body(req)
    handle = (body.get("handle") or "").strip().lower()
    name = (body.get("name") or "").strip()
    members = body.get("member_user_ids") or []
    if not util.GROUP_HANDLE_RE.match(handle) or not name:
        return err(400, "invalid group")
    existing = db.conn().execute(
        "SELECT 1 FROM user_groups WHERE workspace_id = ? AND handle = ?",
        (ws["id"], handle),
    ).fetchone()
    if existing:
        return err(409, "handle taken")
    db.conn().execute(
        "INSERT INTO user_groups(workspace_id, handle, name) VALUES (?, ?, ?)",
        (ws["id"], handle, name),
    )
    for uid in members:
        db.conn().execute(
            "INSERT OR IGNORE INTO user_group_members(workspace_id, handle, user_id) VALUES (?, ?, ?)",
            (ws["id"], handle, uid),
        )
    row = db.conn().execute(
        "SELECT * FROM user_groups WHERE workspace_id = ? AND handle = ?",
        (ws["id"], handle),
    ).fetchone()
    return json_response({"group": util.public_group(ws["id"], row)}, status=201)


async def get_group(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    handle = req.match_info["handle"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    row = db.conn().execute(
        "SELECT * FROM user_groups WHERE workspace_id = ? AND handle = ?",
        (ws["id"], handle),
    ).fetchone()
    if not row:
        return err(404, "no such group")
    return json_response({"group": util.public_group(ws["id"], row)})


@in_tx
async def patch_group(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    handle = req.match_info["handle"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    if not util.is_admin_or_owner(util.workspace_role(ws["id"], u["id"])):
        return err(403, "forbidden")
    row = db.conn().execute(
        "SELECT * FROM user_groups WHERE workspace_id = ? AND handle = ?",
        (ws["id"], handle),
    ).fetchone()
    if not row:
        return err(404, "no such group")
    body = await get_body(req)
    if "name" in body:
        v = body["name"]
        if not isinstance(v, str) or not (1 <= len(v) <= 64):
            return err(400, "invalid name")
        db.conn().execute(
            "UPDATE user_groups SET name = ? WHERE workspace_id = ? AND handle = ?",
            (v, ws["id"], handle),
        )
    if "member_user_ids" in body:
        new_members = body["member_user_ids"] or []
        if not isinstance(new_members, list):
            return err(400, "invalid members")
        db.conn().execute(
            "DELETE FROM user_group_members WHERE workspace_id = ? AND handle = ?",
            (ws["id"], handle),
        )
        for uid in new_members:
            db.conn().execute(
                "INSERT OR IGNORE INTO user_group_members(workspace_id, handle, user_id) VALUES (?, ?, ?)",
                (ws["id"], handle, uid),
            )
    row = db.conn().execute(
        "SELECT * FROM user_groups WHERE workspace_id = ? AND handle = ?",
        (ws["id"], handle),
    ).fetchone()
    return json_response({"group": util.public_group(ws["id"], row)})


@in_tx
async def delete_group(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    handle = req.match_info["handle"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    if not util.is_admin_or_owner(util.workspace_role(ws["id"], u["id"])):
        return err(403, "forbidden")
    db.conn().execute("DELETE FROM user_groups WHERE workspace_id = ? AND handle = ?",
                      (ws["id"], handle))
    db.conn().execute("DELETE FROM user_group_members WHERE workspace_id = ? AND handle = ?",
                      (ws["id"], handle))
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

@in_tx
async def create_invitation(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    role = util.workspace_role(ws["id"], u["id"])
    if not util.is_admin_or_owner(role):
        return err(403, "forbidden")
    body = await get_body(req)
    email = body.get("email")
    invited_username = body.get("invited_username")
    expires_in = body.get("expires_in")
    max_uses = body.get("max_uses") or 1
    expires_at = None
    if isinstance(expires_in, int) and expires_in > 0:
        expires_at = db.now_ms() + expires_in * 1000
    if not isinstance(max_uses, int) or max_uses < 1:
        return err(400, "invalid max_uses")
    if email is not None:
        if not isinstance(email, str) or not util.EMAIL_RE.match(email):
            return err(400, "invalid email")
    if invited_username is not None:
        if not isinstance(invited_username, str):
            return err(400, "invalid username")
    code = db.gen_token()
    db.conn().execute(
        "INSERT INTO invitations(code, workspace_id, inviter_id, email, invited_username, expires_at, max_uses, used_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (code, ws["id"], u["id"], email, invited_username, expires_at, max_uses, db.now_ms()),
    )
    row = db.conn().execute("SELECT * FROM invitations WHERE code = ?", (code,)).fetchone()
    return json_response({"invitation": util.public_invitation(row)}, status=201)


async def list_invitations(req):
    u = require_auth(req)
    slug = req.match_info["slug"]
    ws = db.conn().execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if not ws:
        return err(404, "no such workspace")
    if not util.is_admin_or_owner(util.workspace_role(ws["id"], u["id"])):
        return err(403, "forbidden")
    rows = db.conn().execute(
        "SELECT * FROM invitations WHERE workspace_id = ? ORDER BY created_at DESC",
        (ws["id"],),
    ).fetchall()
    return json_response({"invitations": [util.public_invitation(r) for r in rows]})


@in_tx
async def accept_invitation(req):
    u = require_auth(req)
    code = req.match_info["code"]
    inv = db.conn().execute("SELECT * FROM invitations WHERE code = ?", (code,)).fetchone()
    if not inv:
        return err(404, "no such invitation")
    if inv["expires_at"] and inv["expires_at"] < db.now_ms():
        return err(400, "expired")
    if inv["used_count"] >= inv["max_uses"]:
        return err(400, "exhausted")
    db.conn().execute(
        "INSERT OR IGNORE INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
        (inv["workspace_id"], u["id"], db.now_ms()),
    )
    db.conn().execute(
        "UPDATE invitations SET used_count = used_count + 1 WHERE code = ?",
        (code,),
    )
    # Auto-add to general
    gen = db.conn().execute(
        "SELECT id FROM channels WHERE workspace_id = ? AND name = 'general'",
        (inv["workspace_id"],),
    ).fetchone()
    if gen:
        db.conn().execute(
            "INSERT OR IGNORE INTO channel_members(channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
            (gen["id"], u["id"], db.now_ms()),
        )
    ws = db.conn().execute("SELECT * FROM workspaces WHERE id = ?", (inv["workspace_id"],)).fetchone()
    return json_response({"workspace": util.public_workspace(ws)})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def ws_handler(req):
    u = auth_user(req)
    if not u:
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    hub: HubState = req.app["hub"]
    queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
    subscribed: set[str] = set()

    async def writer_task():
        while not ws.closed:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                continue
            try:
                await ws.send_json(frame)
            except Exception:
                break

    async def deliver_catch_up(channel_id: str, since_seq: int):
        evts = db.fetch_events_since(channel_id, since_seq, limit=2000)
        if since_seq > 0:
            head_now = db.channel_head_seq(channel_id)
            # if since_seq is older than the oldest event we have, signal a gap
            min_kept_row = db.conn().execute(
                "SELECT MIN(seq) AS m FROM events WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            min_kept = (min_kept_row["m"] or 0) if min_kept_row else 0
            if since_seq + 1 < min_kept:
                await queue.put({"type": "resume.gap", "channel_id": channel_id,
                                 "earliest_seq": min_kept})
        for evt in evts:
            await queue.put({
                "type": evt["kind"],
                "seq": evt["seq"],
                "channel_id": channel_id,
                **evt["payload"],
            })

    writer = asyncio.create_task(writer_task())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            kind = data.get("type")
            cid = data.get("channel_id")
            if kind == "subscribe" and cid:
                ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                if not ch:
                    continue
                if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
                    continue
                if cid not in subscribed:
                    hub.subscribe(cid, queue)
                    subscribed.add(cid)
                head = db.channel_head_seq(cid)
                await queue.put({"type": "subscribed", "channel_id": cid, "head_seq": head})
            elif kind == "resume" and cid:
                ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                if not ch:
                    continue
                if ch["is_private"] and not util.channel_is_member(cid, u["id"]):
                    continue
                since = int(data.get("since_seq") or 0)
                if cid not in subscribed:
                    hub.subscribe(cid, queue)
                    subscribed.add(cid)
                await deliver_catch_up(cid, since)
                head = db.channel_head_seq(cid)
                await queue.put({"type": "resumed", "channel_id": cid, "head_seq": head})
            elif kind == "unsubscribe" and cid:
                if cid in subscribed:
                    hub.unsubscribe(cid, queue)
                    subscribed.discard(cid)
            elif kind == "ping":
                await queue.put({"type": "pong"})
    finally:
        for cid in list(subscribed):
            hub.unsubscribe(cid, queue)
        writer.cancel()
        try:
            await writer
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
    return ws


# ---------------------------------------------------------------------------
# DB tail loop — guarantees fan-out continues even if broker is dead.
# ---------------------------------------------------------------------------

async def db_tail_loop(app):
    hub: HubState = app["hub"]
    while True:
        try:
            await asyncio.sleep(0.25)
            evts = db.fetch_events_after_id(hub.last_event_id, limit=500)
            for e in evts:
                if e["id"] <= hub.last_event_id:
                    continue
                hub.last_event_id = e["id"]
                frame = {"type": e["kind"], "seq": e["seq"],
                         "channel_id": e["channel_id"], **e["payload"]}
                hub.fanout(e["channel_id"], frame)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)


async def broker_notice_handler(app):
    """Receive notices from the broker and trigger an immediate poll."""
    hub: HubState = app["hub"]

    async def on_notice(notice):
        # We don't ship payloads through the broker — just kick the tail.
        # The DB tail loop will pull the new rows next tick. To minimise
        # delivery latency, do a single fetch right now too.
        evts = db.fetch_events_after_id(hub.last_event_id, limit=500)
        for e in evts:
            if e["id"] <= hub.last_event_id:
                continue
            hub.last_event_id = e["id"]
            frame = {"type": e["kind"], "seq": e["seq"],
                     "channel_id": e["channel_id"], **e["payload"]}
            hub.fanout(e["channel_id"], frame)

    return on_notice


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")


async def index(req):
    return web.FileResponse(INDEX_HTML_PATH)


async def static_file(req):
    name = req.match_info["name"]
    if "/" in name or name.startswith("."):
        return web.Response(status=404)
    path = os.path.join(os.path.dirname(__file__), "static", name)
    if not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path)


# ---------------------------------------------------------------------------
# Build app
# ---------------------------------------------------------------------------

def make_app() -> web.Application:
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app["hub"] = HubState()
    app["pending_broadcasts"] = []

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/static/{name}", static_file)

    app.router.add_get("/api/health", health)
    app.router.add_post("/api/auth/register", register)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/auth/me", me)

    app.router.add_get("/api/users/{id}", get_user_route)
    app.router.add_patch("/api/users/me", patch_me)

    app.router.add_post("/api/workspaces", create_workspace)
    app.router.add_get("/api/workspaces", list_workspaces)
    app.router.add_get("/api/workspaces/{slug}", get_workspace)
    app.router.add_get("/api/workspaces/{slug}/members", workspace_members)
    app.router.add_patch("/api/workspaces/{slug}", patch_workspace)
    app.router.add_patch("/api/workspaces/{slug}/members/{user_id}", patch_workspace_member)
    app.router.add_post("/api/workspaces/{slug}/transfer_ownership", transfer_ownership)
    app.router.add_post("/api/workspaces/{slug}/channels", create_channel)
    app.router.add_get("/api/workspaces/{slug}/groups", list_groups)
    app.router.add_post("/api/workspaces/{slug}/groups", create_group)
    app.router.add_get("/api/workspaces/{slug}/groups/{handle}", get_group)
    app.router.add_patch("/api/workspaces/{slug}/groups/{handle}", patch_group)
    app.router.add_delete("/api/workspaces/{slug}/groups/{handle}", delete_group)
    app.router.add_post("/api/workspaces/{slug}/invitations", create_invitation)
    app.router.add_get("/api/workspaces/{slug}/invitations", list_invitations)
    app.router.add_post("/api/invitations/{code}/accept", accept_invitation)

    app.router.add_patch("/api/channels/{id}", patch_channel)
    app.router.add_post("/api/channels/{id}/join", join_channel)
    app.router.add_delete("/api/channels/{id}/members/me", leave_channel)
    app.router.add_post("/api/channels/{id}/members", add_channel_member)
    app.router.add_get("/api/channels/{id}/members", list_channel_members)
    app.router.add_post("/api/channels/{id}/messages", post_message)
    app.router.add_get("/api/channels/{id}/messages", list_messages)
    app.router.add_post("/api/channels/{id}/read", channel_read)
    app.router.add_get("/api/channels/{id}/pins", list_pins)

    app.router.add_patch("/api/messages/{id}", edit_message)
    app.router.add_delete("/api/messages/{id}", delete_message)
    app.router.add_get("/api/messages/{parent_id}/replies", list_replies)
    app.router.add_post("/api/messages/{id}/reactions", add_reaction)
    app.router.add_delete("/api/messages/{id}/reactions", remove_reaction)
    app.router.add_post("/api/messages/{id}/pin", pin_message)
    app.router.add_delete("/api/messages/{id}/pin", unpin_message)

    app.router.add_post("/api/dms", create_dm)
    app.router.add_post("/api/files", upload_file)
    app.router.add_get("/api/files/{id}", get_file)
    app.router.add_get("/api/files/{id}/download", download_file)
    app.router.add_get("/api/search", search)

    app.router.add_get("/api/ws", ws_handler)

    async def on_start(app):
        db.init_db()
        hub: HubState = app["hub"]
        hub.last_event_id = db.latest_event_id()

        async def on_notice(notice):
            # Triggered by the broker; pull all new events from DB.
            evts = db.fetch_events_after_id(hub.last_event_id, limit=500)
            for e in evts:
                if e["id"] <= hub.last_event_id:
                    continue
                hub.last_event_id = e["id"]
                frame = {"type": e["kind"], "seq": e["seq"],
                         "channel_id": e["channel_id"], **e["payload"]}
                hub.fanout(e["channel_id"], frame)

        bc = BrokerClient(on_notice=on_notice)
        await bc.start()
        app["broker"] = bc
        app["db_tail_task"] = asyncio.create_task(db_tail_loop(app))

    async def on_cleanup(app):
        try:
            await app["broker"].stop()
        except Exception:
            pass
        t = app.get("db_tail_task")
        if t:
            t.cancel()
            try:
                await t
            except Exception:
                pass

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    app = make_app()
    web.run_app(app, host=HTTP_HOST, port=HTTP_PORT, print=lambda *a, **k: None,
                handle_signals=True, access_log=None)


if __name__ == "__main__":
    main()
