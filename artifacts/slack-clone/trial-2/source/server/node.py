"""HTTP node — REST + WebSocket.

Run as: ``python -m server.node <node_id> <port>``
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web, WSMsgType
from aiohttp import hdrs

from . import store
from .bus import Bus
from .events import emit_durable, parse_slash, record_event, publish_event, FILE_LIMIT
from .static import index_html


NODE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

routes = web.RouteTableDef()


def json_response(data, status=200):
    return web.json_response(data, status=status)


def err(message, status=400, code=None):
    body = {"error": {"message": message}}
    if code:
        body["error"]["code"] = code
    return web.json_response(body, status=status)


def get_bearer(request: web.Request) -> str | None:
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    # also accept ?token=
    return request.query.get("token")


def authed(handler):
    async def wrapper(request: web.Request):
        token = get_bearer(request)
        if not token:
            return err("missing auth", status=401)
        conn = store.connect()
        try:
            user = store.auth_user(conn, token)
        finally:
            conn.close()
        if not user:
            return err("invalid token", status=401)
        request["user"] = user
        request["token"] = token
        return await handler(request)

    return wrapper


# ---------- static ----------


@routes.get("/")
async def root(request):
    return web.Response(text=index_html(), content_type="text/html")


@routes.get("/api/health")
async def health(request):
    return json_response({"status": "ok", "node_id": NODE_ID})


# ---------- auth ----------


@routes.post("/api/auth/register")
async def register(request):
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or username).strip()
    if not store.USERNAME_RE.match(username):
        return err("invalid username")
    if not isinstance(password, str) or len(password) < 8:
        return err("password too short")
    conn = store.connect()
    try:
        existing = store.find_user_by_username(conn, username)
        if existing:
            return err("username taken", status=409)
        salt = store.make_token()[:16]
        ph = store.hash_password(password, salt)
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, display_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, ph, salt, display_name or username, store.now_iso()),
        )
        uid = cur.lastrowid
        token = store.make_token()
        conn.execute(
            "INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, uid, store.now_iso()),
        )
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    return json_response({"user": store.user_to_obj(u), "token": token}, status=201)


@routes.post("/api/auth/login")
async def login(request):
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    conn = store.connect()
    try:
        u = store.find_user_by_username(conn, username)
        if not u or not store.verify_password(u["password_hash"], u["salt"], password):
            return err("invalid credentials", status=401)
        token = store.make_token()
        conn.execute(
            "INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, u["id"], store.now_iso()),
        )
    finally:
        conn.close()
    return json_response({"user": store.user_to_obj(u), "token": token})


@routes.get("/api/auth/me")
@authed
async def me(request):
    return json_response({"user": store.user_to_obj(request["user"])})


# ---------- profiles ----------


@routes.get("/api/users/{uid}")
@authed
async def get_user(request):
    try:
        uid = int(request.match_info["uid"])
    except ValueError:
        return err("bad id", status=404)
    conn = store.connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not u:
        return err("not found", status=404)
    return json_response({"user": store.user_to_obj(u)})


VALID_TIMEZONES = None


def _is_valid_tz(tz: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        return tz == "UTC"


@routes.patch("/api/users/me")
@authed
async def patch_me(request):
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    fields = []
    values = []
    if "display_name" in data:
        v = data["display_name"]
        if not isinstance(v, str) or len(v) > 80:
            return err("display_name too long")
        fields.append("display_name=?")
        values.append(v)
    if "timezone" in data:
        tz = data["timezone"] or "UTC"
        if not isinstance(tz, str) or len(tz) > 64 or not _is_valid_tz(tz):
            return err("invalid timezone")
        fields.append("timezone=?")
        values.append(tz)
    if "avatar_url" in data:
        v = data["avatar_url"] or ""
        if not isinstance(v, str) or len(v) > 1024:
            return err("avatar_url too long")
        fields.append("avatar_url=?")
        values.append(v)
    if "status_text" in data:
        v = data["status_text"] or ""
        if not isinstance(v, str) or len(v) > 200:
            return err("status_text too long")
        fields.append("status_text=?")
        values.append(v)
    if "status_emoji" in data:
        v = data["status_emoji"] or ""
        if not isinstance(v, str) or len(v) > 32:
            return err("status_emoji too long")
        fields.append("status_emoji=?")
        values.append(v)
    if not fields:
        return json_response({"user": store.user_to_obj(request["user"])})
    conn = store.connect()
    try:
        conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id=?",
            (*values, request["user"]["id"]),
        )
        u = conn.execute("SELECT * FROM users WHERE id=?", (request["user"]["id"],)).fetchone()
    finally:
        conn.close()
    return json_response({"user": store.user_to_obj(u)})


# ---------- workspaces ----------


def _create_general_channel(conn, workspace_id: int, owner_id: int):
    conn.execute(
        "INSERT INTO channels (workspace_id, name, is_private, is_dm, topic, is_archived, created_at, next_seq) "
        "VALUES (?, ?, 0, 0, '', 0, ?, 0)",
        (workspace_id, "general", store.now_iso()),
    )
    cid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
        (cid, owner_id, store.now_iso()),
    )
    return cid


@routes.post("/api/workspaces")
@authed
async def create_workspace(request):
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    slug = (data.get("slug") or "").strip()
    name = (data.get("name") or "").strip()
    if not store.SLUG_RE.match(slug):
        return err("invalid slug")
    if not name or len(name) > 80:
        return err("invalid name")
    conn = store.connect()
    try:
        existing = conn.execute("SELECT 1 FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if existing:
            return err("slug taken", status=409)
        cur = conn.execute(
            "INSERT INTO workspaces (slug, name, owner_id, join_mode, created_at) VALUES (?, ?, ?, 'open', ?)",
            (slug, name, request["user"]["id"], store.now_iso()),
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (wid, request["user"]["id"], store.now_iso()),
        )
        gid = _create_general_channel(conn, wid, request["user"]["id"])
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        gen = conn.execute("SELECT * FROM channels WHERE id=?", (gid,)).fetchone()
    finally:
        conn.close()
    return json_response(
        {
            "workspace": store.workspace_to_obj(ws),
            "general_channel": store.channel_to_obj(gen),
        },
        status=201,
    )


@routes.get("/api/workspaces")
@authed
async def list_workspaces(request):
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return json_response({"workspaces": [store.workspace_to_obj(r) for r in rows]})


@routes.get("/api/workspaces/{slug}")
@authed
async def get_workspace(request):
    slug = request.match_info["slug"]
    include_archived = request.query.get("include_archived") == "true"
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        role = store.workspace_role(conn, ws["id"], uid)
        if not role:
            return err("forbidden", status=403)
        # channels visible to caller
        if include_archived:
            chans = conn.execute(
                "SELECT * FROM channels WHERE workspace_id=? ORDER BY id",
                (ws["id"],),
            ).fetchall()
        else:
            chans = conn.execute(
                "SELECT * FROM channels WHERE workspace_id=? AND is_archived=0 ORDER BY id",
                (ws["id"],),
            ).fetchall()
        visible = []
        for c in chans:
            if c["is_private"]:
                if not store.is_channel_member(conn, c["id"], uid):
                    continue
            visible.append(c)
        members = conn.execute(
            "SELECT m.user_id, m.role, m.joined_at, u.username, u.display_name FROM workspace_members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY m.user_id",
            (ws["id"],),
        ).fetchall()
        # read_state: per-channel cursor for caller
        rs = conn.execute(
            "SELECT cm.channel_id, cm.last_read_seq, c.next_seq AS head_seq FROM channel_members cm "
            "JOIN channels c ON c.id=cm.channel_id "
            "WHERE cm.user_id=? AND c.workspace_id=?",
            (uid, ws["id"]),
        ).fetchall()
    finally:
        conn.close()
    return json_response(
        {
            "workspace": store.workspace_to_obj(ws),
            "channels": [store.channel_to_obj(c) for c in visible],
            "members": [
                {
                    "user_id": m["user_id"],
                    "role": m["role"],
                    "joined_at": m["joined_at"],
                    "username": m["username"],
                    "display_name": m["display_name"] or m["username"],
                }
                for m in members
            ],
            "read_state": [
                {"channel_id": r["channel_id"], "last_read_seq": r["last_read_seq"], "head_seq": r["head_seq"]}
                for r in rs
            ],
        }
    )


@routes.get("/api/workspaces/{slug}/members")
@authed
async def list_members(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if not store.workspace_role(conn, ws["id"], uid):
            return err("forbidden", status=403)
        rows = conn.execute(
            "SELECT m.user_id, m.role, m.joined_at, u.username, u.display_name FROM workspace_members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY m.user_id",
            (ws["id"],),
        ).fetchall()
    finally:
        conn.close()
    return json_response({"members": [
        {
            "user_id": m["user_id"],
            "role": m["role"],
            "joined_at": m["joined_at"],
            "username": m["username"],
            "display_name": m["display_name"] or m["username"],
        }
        for m in rows
    ]})


@routes.patch("/api/workspaces/{slug}")
@authed
async def patch_workspace(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        role = store.workspace_role(conn, ws["id"], uid)
        if role not in ("owner", "admin"):
            return err("forbidden", status=403)
        fields, values = [], []
        if "name" in data:
            v = data["name"]
            if not isinstance(v, str) or not (1 <= len(v) <= 80):
                return err("invalid name")
            fields.append("name=?")
            values.append(v)
        if "join_mode" in data:
            v = data["join_mode"]
            if v not in ("open", "invite_only"):
                return err("invalid join_mode")
            fields.append("join_mode=?")
            values.append(v)
        if fields:
            conn.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id=?", (*values, ws["id"]))
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    finally:
        conn.close()
    return json_response({"workspace": store.workspace_to_obj(ws)})


@routes.patch("/api/workspaces/{slug}/members/{uid}")
@authed
async def patch_member(request):
    slug = request.match_info["slug"]
    target_id = int(request.match_info["uid"])
    caller_id = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    new_role = data.get("role")
    if new_role not in ("admin", "member", "guest", "owner"):
        return err("invalid role")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        caller_role = store.workspace_role(conn, ws["id"], caller_id)
        target_role = store.workspace_role(conn, ws["id"], target_id)
        if not caller_role or not target_role:
            return err("forbidden", status=403)
        if new_role == "owner":
            return err("use transfer_ownership")
        if target_role == "owner":
            return err("forbidden", status=403)
        if caller_role == "admin" and target_role == "admin":
            return err("forbidden", status=403)
        if caller_role not in ("owner", "admin"):
            return err("forbidden", status=403)
        conn.execute(
            "UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
            (new_role, ws["id"], target_id),
        )
        row = conn.execute(
            "SELECT m.user_id, m.role, m.joined_at, u.username FROM workspace_members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.user_id=?",
            (ws["id"], target_id),
        ).fetchone()
    finally:
        conn.close()
    return json_response({"member": {
        "user_id": row["user_id"],
        "role": row["role"],
        "joined_at": row["joined_at"],
        "username": row["username"],
    }})


@routes.post("/api/workspaces/{slug}/transfer_ownership")
@authed
async def transfer_owner(request):
    slug = request.match_info["slug"]
    caller_id = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    target_id = data.get("user_id")
    if not isinstance(target_id, int):
        return err("invalid user_id")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if ws["owner_id"] != caller_id:
            return err("forbidden", status=403)
        if not store.workspace_role(conn, ws["id"], target_id):
            return err("not a member")
        conn.execute("UPDATE workspaces SET owner_id=? WHERE id=?", (target_id, ws["id"]))
        conn.execute(
            "UPDATE workspace_members SET role='owner' WHERE workspace_id=? AND user_id=?",
            (ws["id"], target_id),
        )
        conn.execute(
            "UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?",
            (ws["id"], caller_id),
        )
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    finally:
        conn.close()
    return json_response({"workspace": store.workspace_to_obj(ws)})


# ---------- channels ----------


@routes.post("/api/workspaces/{slug}/channels")
@authed
async def create_channel(request):
    slug = request.match_info["slug"]
    caller_id = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    name = (data.get("name") or "").strip()
    is_private = bool(data.get("is_private"))
    topic = data.get("topic") or ""
    if not isinstance(topic, str) or len(topic) > 250:
        return err("topic too long")
    if not store.CHANNEL_NAME_RE.match(name) or len(name) > 32:
        return err("invalid name")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        role = store.workspace_role(conn, ws["id"], caller_id)
        if not role:
            return err("forbidden", status=403)
        if role == "guest":
            return err("forbidden", status=403)
        if is_private and role not in ("owner", "admin"):
            return err("forbidden", status=403)
        existing = conn.execute(
            "SELECT 1 FROM channels WHERE workspace_id=? AND name=?",
            (ws["id"], name),
        ).fetchone()
        if existing:
            return err("name taken", status=409)
        cur = conn.execute(
            "INSERT INTO channels (workspace_id, name, is_private, is_dm, topic, is_archived, created_at, next_seq) VALUES (?, ?, ?, 0, ?, 0, ?, 0)",
            (ws["id"], name, 1 if is_private else 0, topic, store.now_iso()),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
            (cid, caller_id, store.now_iso()),
        )
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    return json_response({"channel": store.channel_to_obj(c)}, status=201)


@routes.post("/api/channels/{cid}/join")
@authed
async def join_channel(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (c["workspace_id"],)).fetchone()
        if c["is_dm"]:
            if not store.is_channel_member(conn, cid, uid):
                return err("forbidden", status=403)
            return json_response({"channel": store.channel_to_obj(c)})
        role = store.workspace_role(conn, ws["id"], uid)
        if c["is_private"]:
            if not role or not store.is_channel_member(conn, cid, uid):
                return err("forbidden", status=403)
            return json_response({"channel": store.channel_to_obj(c)})
        # public channel
        if not role:
            if ws["join_mode"] != "open":
                return err("forbidden", status=403)
            conn.execute(
                "INSERT OR IGNORE INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                (ws["id"], uid, store.now_iso()),
            )
        else:
            if role == "guest":
                return err("forbidden", status=403)
        if not store.is_channel_member(conn, cid, uid):
            conn.execute(
                "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
                (cid, uid, store.now_iso()),
            )
    finally:
        conn.close()
    # Emit join event
    bus: Bus = request.app["bus"]
    await emit_durable(bus, cid, "member.joined", {"user_id": uid})
    return json_response({"channel": store.channel_to_obj(c)})


@routes.patch("/api/channels/{cid}")
@authed
async def patch_channel(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        role = store.workspace_role(conn, c["workspace_id"], uid)
        if role not in ("owner", "admin"):
            return err("forbidden", status=403)
        fields, values = [], []
        if "topic" in data:
            v = data["topic"] or ""
            if not isinstance(v, str) or len(v) > 250:
                return err("topic too long")
            fields.append("topic=?")
            values.append(v)
        if "is_archived" in data:
            v = data["is_archived"]
            fields.append("is_archived=?")
            values.append(1 if v else 0)
        if "name" in data:
            v = (data["name"] or "").strip()
            if not store.CHANNEL_NAME_RE.match(v) or len(v) > 32:
                return err("invalid name")
            existing = conn.execute(
                "SELECT 1 FROM channels WHERE workspace_id=? AND name=? AND id<>?",
                (c["workspace_id"], v, cid),
            ).fetchone()
            if existing:
                return err("name taken", status=409)
            fields.append("name=?")
            values.append(v)
        if fields:
            conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id=?", (*values, cid))
            c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    bus: Bus = request.app["bus"]
    await emit_durable(bus, cid, "channel.updated", {"channel": store.channel_to_obj(c)})
    return json_response({"channel": store.channel_to_obj(c)})


@routes.delete("/api/channels/{cid}/members/me")
@authed
async def leave_channel(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return web.Response(status=204)
        was_member = store.is_channel_member(conn, cid, uid)
        conn.execute(
            "DELETE FROM channel_members WHERE channel_id=? AND user_id=?", (cid, uid)
        )
    finally:
        conn.close()
    if was_member:
        bus: Bus = request.app["bus"]
        await emit_durable(bus, cid, "member.left", {"user_id": uid})
    return web.Response(status=204)


# ---------- pins ----------


@routes.get("/api/channels/{cid}/pins")
@authed
async def list_pins(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if c["is_private"] and not store.is_channel_member(conn, cid, uid):
            return err("forbidden", status=403)
        rows = conn.execute(
            "SELECT m.* FROM messages m JOIN pins p ON p.message_id=m.id WHERE p.channel_id=? ORDER BY p.pinned_at DESC",
            (cid,),
        ).fetchall()
        msgs = [store.message_to_obj(conn, r) for r in rows if not r["deleted"]]
    finally:
        conn.close()
    return json_response({"messages": msgs})


@routes.post("/api/messages/{mid}/pin")
@authed
async def pin_msg(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m or m["deleted"]:
            return err("not found", status=404)
        if not store.is_channel_member(conn, m["channel_id"], uid):
            return err("forbidden", status=403)
        conn.execute(
            "INSERT OR IGNORE INTO pins (channel_id, message_id, pinned_by, pinned_at) VALUES (?, ?, ?, ?)",
            (m["channel_id"], mid, uid, store.now_iso()),
        )
    finally:
        conn.close()
    return json_response({"ok": True})


@routes.delete("/api/messages/{mid}/pin")
@authed
async def unpin_msg(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m:
            return err("not found", status=404)
        if not store.is_channel_member(conn, m["channel_id"], uid):
            return err("forbidden", status=403)
        conn.execute("DELETE FROM pins WHERE message_id=?", (mid,))
    finally:
        conn.close()
    return web.Response(status=204)


# ---------- messages ----------


async def _post_message_inner(conn, channel_row, author_id, body, parent_id, file_ids, *, ws_for_mentions=None):
    """Insert a message inside an active write transaction (caller manages tx).

    Returns (message_id, seq, event_kind, event_payload) — the caller must
    record the event row inside the same transaction (record_event) and
    publish to the bus AFTER commit.
    """
    if parent_id is not None:
        parent = conn.execute("SELECT * FROM messages WHERE id=?", (parent_id,)).fetchone()
        if not parent or parent["channel_id"] != channel_row["id"] or parent["deleted"]:
            raise ValueError("bad parent")
    seq = store.reserve_seq(conn, channel_row["id"])
    cur = conn.execute(
        "INSERT INTO messages (channel_id, author_id, body, parent_id, reply_count, created_at, seq) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (channel_row["id"], author_id, body, parent_id, store.now_iso(), seq),
    )
    mid = cur.lastrowid
    if parent_id:
        conn.execute(
            "UPDATE messages SET reply_count=reply_count+1 WHERE id=?",
            (parent_id,),
        )
    if file_ids:
        for fid in file_ids:
            f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if not f:
                raise ValueError("file not found")
            if f["uploader_id"] != author_id:
                raise ValueError("file not yours")
            if f["attached_message_id"] is not None:
                raise ValueError("file already attached")
            conn.execute("INSERT INTO message_files (message_id, file_id) VALUES (?, ?)", (mid, fid))
            conn.execute("UPDATE files SET attached_message_id=? WHERE id=?", (mid, fid))
    # mentions
    ws_id = ws_for_mentions if ws_for_mentions is not None else channel_row["workspace_id"]
    if ws_id:
        mentioned = store.parse_mentions(conn, ws_id, author_id, body)
        for u in mentioned:
            conn.execute(
                "INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?, ?)", (mid, u)
            )
    return mid, seq


@routes.post("/api/channels/{cid}/messages")
@authed
async def post_message(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    body = data.get("body")
    parent_id = data.get("parent_id")
    file_ids = data.get("file_ids") or []
    if not isinstance(body, str) or not body.strip():
        return err("empty body")
    if len(body) > 32_000:
        return err("body too long")

    # Slash command handling
    cmd, rest = parse_slash(body)
    if cmd is not None and cmd not in ("me", "shrug", "topic", "invite", "archive", "unarchive"):
        return err("unknown command")

    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if c["is_private"] or c["is_dm"]:
            if not store.is_channel_member(conn, cid, uid):
                return err("forbidden", status=403)
        else:
            role = store.workspace_role(conn, c["workspace_id"], uid)
            if not role:
                return err("forbidden", status=403)
        if c["is_archived"]:
            return err("archived", status=423)

        # Slash command: handle separately
        if cmd == "topic":
            role = store.workspace_role(conn, c["workspace_id"], uid)
            if role not in ("owner", "admin"):
                return err("forbidden", status=403)
            new_topic = (rest or "")[:250]
            conn.execute("UPDATE channels SET topic=? WHERE id=?", (new_topic, cid))
            c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            bus: Bus = request.app["bus"]
            await emit_durable(bus, cid, "channel.updated", {"channel": store.channel_to_obj(c)})
            return json_response({"channel": store.channel_to_obj(c)}, status=201)
        if cmd == "archive":
            role = store.workspace_role(conn, c["workspace_id"], uid)
            if role not in ("owner", "admin"):
                return err("forbidden", status=403)
            conn.execute("UPDATE channels SET is_archived=1 WHERE id=?", (cid,))
            c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            bus: Bus = request.app["bus"]
            await emit_durable(bus, cid, "channel.updated", {"channel": store.channel_to_obj(c)})
            return json_response({"channel": store.channel_to_obj(c)}, status=201)
        if cmd == "unarchive":
            role = store.workspace_role(conn, c["workspace_id"], uid)
            if role not in ("owner", "admin"):
                return err("forbidden", status=403)
            conn.execute("UPDATE channels SET is_archived=0 WHERE id=?", (cid,))
            c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            bus: Bus = request.app["bus"]
            await emit_durable(bus, cid, "channel.updated", {"channel": store.channel_to_obj(c)})
            return json_response({"channel": store.channel_to_obj(c)}, status=201)
        if cmd == "invite":
            m = re.match(r"^@?([A-Za-z0-9_]+)", (rest or "").strip())
            if not m:
                return err("invalid /invite")
            handle = m.group(1)
            target = store.find_user_by_username(conn, handle)
            if not target:
                return err("user not found", status=404)
            if not store.is_channel_member(conn, cid, target["id"]):
                conn.execute(
                    "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
                    (cid, target["id"], store.now_iso()),
                )
            bus: Bus = request.app["bus"]
            await emit_durable(bus, cid, "member.joined", {"user_id": target["id"]})
            return json_response({"ok": True}, status=201)

        # /me and /shrug → posted as normal messages with the documented body.
        post_body = body
        if cmd == "shrug":
            tail = (rest or "").strip()
            if tail:
                post_body = f"{tail} ¯\\_(ツ)_/¯"
            else:
                post_body = "¯\\_(ツ)_/¯"
        # /me leaves body as-is

        # Special-case literal escape "//foo" → posted as "/foo"
        if post_body.startswith("//"):
            post_body = post_body[1:]

        conn.execute("BEGIN IMMEDIATE")
        try:
            mid, seq = await _post_message_inner(
                conn, c, uid, post_body, parent_id, file_ids,
            )
            # Build the event row inside the same transaction so that the
            # cross-node poll fallback never sees a hole.
            m_row_pre = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            msg_obj_pre = store.message_to_obj(conn, m_row_pre)
            kind = "message.reply" if parent_id else "message.new"
            record_event(conn, cid, seq, kind, {"message": msg_obj_pre})
            conn.execute("COMMIT")
        except ValueError as e:
            conn.execute("ROLLBACK")
            return err(str(e))
        m_row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        msg_obj = store.message_to_obj(conn, m_row)
    finally:
        conn.close()

    bus: Bus = request.app["bus"]
    await publish_event(bus, cid, seq, kind, {"message": msg_obj})
    return json_response({"message": msg_obj}, status=201)


@routes.get("/api/channels/{cid}/messages")
@authed
async def list_messages(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    limit = min(int(request.query.get("limit", 50) or 50), 200)
    before = request.query.get("before")
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if c["is_private"] or c["is_dm"]:
            if not store.is_channel_member(conn, cid, uid):
                return err("forbidden", status=403)
        else:
            if not store.workspace_role(conn, c["workspace_id"], uid):
                return err("forbidden", status=403)
        if before:
            rows = conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?",
                (cid, int(before), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?",
                (cid, limit),
            ).fetchall()
        msgs = [store.message_to_obj(conn, r) for r in rows]
    finally:
        conn.close()
    return json_response({"messages": msgs})


@routes.patch("/api/messages/{mid}")
@authed
async def edit_message(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        return err("empty body")
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m or m["deleted"]:
            return err("not found", status=404)
        if m["author_id"] != uid:
            return err("forbidden", status=403)
        c = conn.execute("SELECT * FROM channels WHERE id=?", (m["channel_id"],)).fetchone()
        if c["is_archived"]:
            return err("archived", status=423)
        conn.execute("BEGIN IMMEDIATE")
        try:
            seq = store.reserve_seq(conn, m["channel_id"])
            conn.execute(
                "UPDATE messages SET body=?, edited_at=? WHERE id=?",
                (body, store.now_iso(), mid),
            )
            conn.execute("DELETE FROM mentions WHERE message_id=?", (mid,))
            mentioned = store.parse_mentions(conn, c["workspace_id"], uid, body)
            for u in mentioned:
                conn.execute("INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?, ?)", (mid, u))
            m_row_pre = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            msg_obj_pre = store.message_to_obj(conn, m_row_pre)
            record_event(conn, m["channel_id"], seq, "message.edited", {"message": msg_obj_pre})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        m_row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        msg_obj = store.message_to_obj(conn, m_row)
    finally:
        conn.close()
    bus: Bus = request.app["bus"]
    await publish_event(bus, m_row["channel_id"], seq, "message.edited", {"message": msg_obj})
    return json_response({"message": msg_obj})


@routes.delete("/api/messages/{mid}")
@authed
async def delete_message(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m:
            return web.Response(status=204)
        if m["deleted"]:
            return web.Response(status=204)
        c = conn.execute("SELECT * FROM channels WHERE id=?", (m["channel_id"],)).fetchone()
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (c["workspace_id"],)).fetchone()
        if not (m["author_id"] == uid or (ws and ws["owner_id"] == uid)):
            return err("forbidden", status=403)
        conn.execute("BEGIN IMMEDIATE")
        try:
            seq = store.reserve_seq(conn, m["channel_id"])
            conn.execute("UPDATE messages SET deleted=1, body='' WHERE id=?", (mid,))
            if m["parent_id"]:
                conn.execute(
                    "UPDATE messages SET reply_count=MAX(0, reply_count-1) WHERE id=?",
                    (m["parent_id"],),
                )
            record_event(conn, m["channel_id"], seq, "message.deleted", {"message_id": mid})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        cid = m["channel_id"]
    finally:
        conn.close()
    bus: Bus = request.app["bus"]
    await publish_event(bus, cid, seq, "message.deleted", {"message_id": mid})
    return web.Response(status=204)


@routes.get("/api/messages/{mid}/replies")
@authed
async def replies(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        parent = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not parent or parent["deleted"]:
            return err("not found", status=404)
        c = conn.execute("SELECT * FROM channels WHERE id=?", (parent["channel_id"],)).fetchone()
        if c["is_private"] or c["is_dm"]:
            if not store.is_channel_member(conn, c["id"], uid):
                return err("forbidden", status=403)
        else:
            if not store.workspace_role(conn, c["workspace_id"], uid):
                return err("forbidden", status=403)
        rows = conn.execute(
            "SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC",
            (mid,),
        ).fetchall()
        msgs = [store.message_to_obj(conn, r) for r in rows]
    finally:
        conn.close()
    return json_response({"messages": msgs})


# ---------- reactions ----------


@routes.post("/api/messages/{mid}/reactions")
@authed
async def add_reaction(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    emoji = (data.get("emoji") or "").strip()
    if not emoji or len(emoji) > 32:
        return err("invalid emoji")
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m or m["deleted"]:
            return err("not found", status=404)
        c = conn.execute("SELECT * FROM channels WHERE id=?", (m["channel_id"],)).fetchone()
        if c["is_private"] or c["is_dm"]:
            if not store.is_channel_member(conn, c["id"], uid):
                return err("forbidden", status=403)
        else:
            if not store.workspace_role(conn, c["workspace_id"], uid):
                return err("forbidden", status=403)
        conn.execute("BEGIN IMMEDIATE")
        try:
            existed = conn.execute(
                "SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                (mid, uid, emoji),
            ).fetchone()
            if not existed:
                conn.execute(
                    "INSERT INTO reactions (message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                    (mid, uid, emoji, store.now_iso()),
                )
                seq = store.reserve_seq(conn, m["channel_id"])
                m_row_pre = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
                msg_obj_pre = store.message_to_obj(conn, m_row_pre)
                record_event(conn, m["channel_id"], seq, "reaction.added", {
                    "message_id": mid, "emoji": emoji, "user_id": uid, "message": msg_obj_pre,
                })
            else:
                seq = None
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        m_row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        msg_obj = store.message_to_obj(conn, m_row)
    finally:
        conn.close()
    if seq is not None:
        bus: Bus = request.app["bus"]
        await publish_event(bus, m_row["channel_id"], seq, "reaction.added", {
            "message_id": mid, "emoji": emoji, "user_id": uid, "message": msg_obj,
        })
    return json_response({"message": msg_obj})


@routes.delete("/api/messages/{mid}/reactions")
@authed
async def remove_reaction(request):
    try:
        mid = int(request.match_info["mid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        # Allow body via query also
        data = {"emoji": request.query.get("emoji")}
    emoji = (data.get("emoji") or "").strip()
    if not emoji:
        return err("invalid emoji")
    conn = store.connect()
    try:
        m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not m or m["deleted"]:
            return err("not found", status=404)
        conn.execute("BEGIN IMMEDIATE")
        try:
            existed = conn.execute(
                "SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                (mid, uid, emoji),
            ).fetchone()
            if existed:
                conn.execute(
                    "DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                    (mid, uid, emoji),
                )
                seq = store.reserve_seq(conn, m["channel_id"])
                m_row_pre = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
                msg_obj_pre = store.message_to_obj(conn, m_row_pre)
                record_event(conn, m["channel_id"], seq, "reaction.removed", {
                    "message_id": mid, "emoji": emoji, "user_id": uid, "message": msg_obj_pre,
                })
            else:
                seq = None
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        m_row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        msg_obj = store.message_to_obj(conn, m_row)
    finally:
        conn.close()
    if seq is not None:
        bus: Bus = request.app["bus"]
        await publish_event(bus, m_row["channel_id"], seq, "reaction.removed", {
            "message_id": mid, "emoji": emoji, "user_id": uid, "message": msg_obj,
        })
    return json_response({"message": msg_obj})


# ---------- DMs ----------


@routes.post("/api/dms")
@authed
async def create_dm(request):
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    other = data.get("recipient_id")
    if not isinstance(other, int):
        return err("invalid recipient_id")
    if other == uid:
        return err("cannot DM yourself")
    a, b = sorted([uid, other])
    conn = store.connect()
    try:
        target = conn.execute("SELECT * FROM users WHERE id=?", (other,)).fetchone()
        if not target:
            return err("not found", status=404)
        existing = conn.execute(
            "SELECT c.* FROM dm_pairs dp JOIN channels c ON c.id=dp.channel_id WHERE dp.user1_id=? AND dp.user2_id=?",
            (a, b),
        ).fetchone()
        if existing:
            return json_response({"channel": store.channel_to_obj(existing)})
        # create a hidden DM channel; workspace_id=0 not allowed (FK).
        # Use the first workspace shared by both, otherwise create no-workspace DM
        shared = conn.execute(
            "SELECT m1.workspace_id FROM workspace_members m1 JOIN workspace_members m2 "
            "ON m1.workspace_id=m2.workspace_id WHERE m1.user_id=? AND m2.user_id=? LIMIT 1",
            (uid, other),
        ).fetchone()
        if not shared:
            return err("no shared workspace")
        cur = conn.execute(
            "INSERT INTO channels (workspace_id, name, is_private, is_dm, topic, is_archived, created_at, next_seq) VALUES (?, ?, 1, 1, '', 0, ?, 0)",
            (shared["workspace_id"], f"dm-{a}-{b}", store.now_iso()),
        )
        cid = cur.lastrowid
        for uu in (a, b):
            conn.execute(
                "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
                (cid, uu, store.now_iso()),
            )
        conn.execute(
            "INSERT INTO dm_pairs (user1_id, user2_id, channel_id) VALUES (?, ?, ?)",
            (a, b, cid),
        )
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    return json_response({"channel": store.channel_to_obj(c)}, status=201)


# ---------- files ----------


@routes.post("/api/files")
@authed
async def upload_file(request):
    uid = request["user"]["id"]
    if request.content_length and request.content_length > FILE_LIMIT + 1024:
        return err("too large", status=413)
    reader = await request.multipart()
    field = None
    while True:
        field = await reader.next()
        if field is None:
            return err("missing file")
        if field.name == "file":
            break
    filename = field.filename or "file.bin"
    content_type = field.headers.get("Content-Type", "application/octet-stream")
    size = 0
    chunks = []
    while True:
        chunk = await field.read_chunk(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > FILE_LIMIT:
            return err("too large", status=413)
        chunks.append(chunk)
    blob = b"".join(chunks)
    conn = store.connect()
    try:
        cur = conn.execute(
            "INSERT INTO files (uploader_id, filename, content_type, size, storage_path, created_at) VALUES (?, ?, ?, ?, '', ?)",
            (uid, filename, content_type, size, store.now_iso()),
        )
        fid = cur.lastrowid
        path = os.path.join(store.FILES_DIR, f"{fid}.bin")
        with open(path, "wb") as fp:
            fp.write(blob)
        conn.execute("UPDATE files SET storage_path=? WHERE id=?", (path, fid))
        f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    finally:
        conn.close()
    return json_response({"file": store.file_to_obj(f)}, status=201)


@routes.get("/api/files/{fid}")
@authed
async def get_file(request):
    try:
        fid = int(request.match_info["fid"])
    except ValueError:
        return err("not found", status=404)
    conn = store.connect()
    try:
        f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    finally:
        conn.close()
    if not f:
        return err("not found", status=404)
    return json_response({"file": store.file_to_obj(f)})


@routes.get("/api/files/{fid}/download")
@authed
async def download_file(request):
    try:
        fid = int(request.match_info["fid"])
    except ValueError:
        return err("not found", status=404)
    conn = store.connect()
    try:
        f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    finally:
        conn.close()
    if not f:
        return err("not found", status=404)
    headers = {
        "Content-Type": f["content_type"],
        "Content-Disposition": f"attachment; filename=\"{f['filename']}\"",
    }
    try:
        with open(f["storage_path"], "rb") as fp:
            data = fp.read()
    except FileNotFoundError:
        return err("not found", status=404)
    return web.Response(body=data, headers=headers)


# ---------- search ----------


@routes.get("/api/search")
@authed
async def search(request):
    uid = request["user"]["id"]
    q = (request.query.get("q") or "").strip()
    ws_slug = request.query.get("workspace")
    if not q:
        return json_response({"messages": []})
    conn = store.connect()
    try:
        if ws_slug:
            ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (ws_slug,)).fetchone()
            if not ws:
                return err("not found", status=404)
            workspace_ids = [ws["id"]]
        else:
            workspace_ids = [
                r["workspace_id"]
                for r in conn.execute(
                    "SELECT workspace_id FROM workspace_members WHERE user_id=?", (uid,)
                ).fetchall()
            ]
        if not workspace_ids:
            return json_response({"messages": []})
        # Find channels caller can read
        ph = ",".join("?" for _ in workspace_ids)
        chans = conn.execute(
            f"SELECT * FROM channels WHERE workspace_id IN ({ph})",
            workspace_ids,
        ).fetchall()
        accessible = []
        for c in chans:
            if c["is_private"] or c["is_dm"]:
                if store.is_channel_member(conn, c["id"], uid):
                    accessible.append(c["id"])
            else:
                if store.workspace_role(conn, c["workspace_id"], uid):
                    accessible.append(c["id"])
        if not accessible:
            return json_response({"messages": []})
        ph2 = ",".join("?" for _ in accessible)
        rows = conn.execute(
            f"SELECT * FROM messages WHERE channel_id IN ({ph2}) AND deleted=0 AND body LIKE ? ORDER BY id DESC LIMIT 100",
            [*accessible, f"%{q}%"],
        ).fetchall()
        msgs = [store.message_to_obj(conn, r) for r in rows]
    finally:
        conn.close()
    return json_response({"messages": msgs})


# ---------- groups ----------


@routes.get("/api/workspaces/{slug}/groups")
@authed
async def list_groups(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if not store.workspace_role(conn, ws["id"], uid):
            return err("forbidden", status=403)
        rows = conn.execute("SELECT * FROM groups_t WHERE workspace_id=? ORDER BY id", (ws["id"],)).fetchall()
        result = []
        for g in rows:
            members = [
                r["user_id"]
                for r in conn.execute(
                    "SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (g["id"],)
                ).fetchall()
            ]
            result.append({"id": g["id"], "handle": g["handle"], "name": g["name"], "member_user_ids": members})
    finally:
        conn.close()
    return json_response({"groups": result})


@routes.post("/api/workspaces/{slug}/groups")
@authed
async def create_group(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    handle = (data.get("handle") or "").strip()
    name = data.get("name") or handle
    member_ids = data.get("member_user_ids") or []
    if not store.GROUP_HANDLE_RE.match(handle):
        return err("invalid handle")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        role = store.workspace_role(conn, ws["id"], uid)
        if role not in ("owner", "admin"):
            return err("forbidden", status=403)
        existing = conn.execute(
            "SELECT 1 FROM groups_t WHERE workspace_id=? AND handle=?", (ws["id"], handle)
        ).fetchone()
        if existing:
            return err("handle taken", status=409)
        cur = conn.execute(
            "INSERT INTO groups_t (workspace_id, handle, name, created_at) VALUES (?, ?, ?, ?)",
            (ws["id"], handle, name, store.now_iso()),
        )
        gid = cur.lastrowid
        for u in member_ids:
            if isinstance(u, int):
                conn.execute(
                    "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)", (gid, u)
                )
        members = [
            r["user_id"]
            for r in conn.execute("SELECT user_id FROM group_members WHERE group_id=?", (gid,)).fetchall()
        ]
    finally:
        conn.close()
    return json_response({"group": {"id": gid, "handle": handle, "name": name, "member_user_ids": members}}, status=201)


@routes.get("/api/workspaces/{slug}/groups/{handle}")
@authed
async def get_group(request):
    slug = request.match_info["slug"]
    handle = request.match_info["handle"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if not store.workspace_role(conn, ws["id"], uid):
            return err("forbidden", status=403)
        g = conn.execute(
            "SELECT * FROM groups_t WHERE workspace_id=? AND handle=?", (ws["id"], handle)
        ).fetchone()
        if not g:
            return err("not found", status=404)
        members = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (g["id"],)
        ).fetchall()]
    finally:
        conn.close()
    return json_response({"group": {"id": g["id"], "handle": g["handle"], "name": g["name"], "member_user_ids": members}})


@routes.patch("/api/workspaces/{slug}/groups/{handle}")
@authed
async def patch_group(request):
    slug = request.match_info["slug"]
    handle = request.match_info["handle"]
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if store.workspace_role(conn, ws["id"], uid) not in ("owner", "admin"):
            return err("forbidden", status=403)
        g = conn.execute("SELECT * FROM groups_t WHERE workspace_id=? AND handle=?", (ws["id"], handle)).fetchone()
        if not g:
            return err("not found", status=404)
        if "name" in data:
            conn.execute("UPDATE groups_t SET name=? WHERE id=?", (data["name"], g["id"]))
        if "member_user_ids" in data:
            conn.execute("DELETE FROM group_members WHERE group_id=?", (g["id"],))
            for u in data["member_user_ids"]:
                if isinstance(u, int):
                    conn.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)", (g["id"], u))
        g = conn.execute("SELECT * FROM groups_t WHERE id=?", (g["id"],)).fetchone()
        members = [r["user_id"] for r in conn.execute("SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id", (g["id"],)).fetchall()]
    finally:
        conn.close()
    return json_response({"group": {"id": g["id"], "handle": g["handle"], "name": g["name"], "member_user_ids": members}})


@routes.delete("/api/workspaces/{slug}/groups/{handle}")
@authed
async def delete_group(request):
    slug = request.match_info["slug"]
    handle = request.match_info["handle"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if store.workspace_role(conn, ws["id"], uid) not in ("owner", "admin"):
            return err("forbidden", status=403)
        g = conn.execute("SELECT * FROM groups_t WHERE workspace_id=? AND handle=?", (ws["id"], handle)).fetchone()
        if not g:
            return web.Response(status=204)
        conn.execute("DELETE FROM groups_t WHERE id=?", (g["id"],))
    finally:
        conn.close()
    return web.Response(status=204)


# ---------- invitations ----------


@routes.post("/api/workspaces/{slug}/invitations")
@authed
async def create_invitation(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        data = {}
    expires_in = data.get("expires_in")
    max_uses = data.get("max_uses") or 1
    email = data.get("email")
    invited_username = data.get("invited_username")
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        role = store.workspace_role(conn, ws["id"], uid)
        if role not in ("owner", "admin"):
            return err("forbidden", status=403)
        code = store.make_token()[:16]
        from datetime import timedelta
        exp = None
        if isinstance(expires_in, int) and expires_in > 0:
            exp = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cur = conn.execute(
            "INSERT INTO invitations (code, workspace_id, email, invited_username, expires_at, max_uses, uses, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (code, ws["id"], email, invited_username, exp, max_uses, uid, store.now_iso()),
        )
        iid = cur.lastrowid
        inv = conn.execute("SELECT * FROM invitations WHERE id=?", (iid,)).fetchone()
    finally:
        conn.close()
    return json_response({"invitation": {
        "id": inv["id"],
        "code": inv["code"],
        "workspace_id": inv["workspace_id"],
        "email": inv["email"],
        "invited_username": inv["invited_username"],
        "expires_at": inv["expires_at"],
        "max_uses": inv["max_uses"],
        "uses": inv["uses"],
        "created_at": inv["created_at"],
    }}, status=201)


@routes.get("/api/workspaces/{slug}/invitations")
@authed
async def list_invitations(request):
    slug = request.match_info["slug"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if not ws:
            return err("not found", status=404)
        if store.workspace_role(conn, ws["id"], uid) not in ("owner", "admin"):
            return err("forbidden", status=403)
        rows = conn.execute("SELECT * FROM invitations WHERE workspace_id=? ORDER BY id DESC", (ws["id"],)).fetchall()
    finally:
        conn.close()
    return json_response({"invitations": [
        {
            "id": r["id"],
            "code": r["code"],
            "workspace_id": r["workspace_id"],
            "email": r["email"],
            "invited_username": r["invited_username"],
            "expires_at": r["expires_at"],
            "max_uses": r["max_uses"],
            "uses": r["uses"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]})


@routes.post("/api/invitations/{code}/accept")
@authed
async def accept_invitation(request):
    code = request.match_info["code"]
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        inv = conn.execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
        if not inv:
            return err("not found", status=404)
        if inv["uses"] >= inv["max_uses"]:
            return err("invitation exhausted")
        if inv["expires_at"]:
            now = datetime.now(timezone.utc)
            try:
                # parse iso z
                exp = datetime.fromisoformat(inv["expires_at"].replace("Z", "+00:00"))
                if now >= exp:
                    return err("invitation expired")
            except Exception:
                pass
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (inv["workspace_id"],)).fetchone()
        if not store.workspace_role(conn, ws["id"], uid):
            conn.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                (ws["id"], uid, store.now_iso()),
            )
        conn.execute("UPDATE invitations SET uses=uses+1 WHERE id=?", (inv["id"],))
    finally:
        conn.close()
    return json_response({"workspace": store.workspace_to_obj(ws)})


# ---------- read state ----------


@routes.post("/api/channels/{cid}/read")
@authed
async def update_read(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    last = data.get("last_read_seq")
    if not isinstance(last, int) or last < 0:
        return err("invalid last_read_seq")
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if not store.is_channel_member(conn, cid, uid):
            return err("forbidden", status=403)
        # never move backwards
        conn.execute(
            "UPDATE channel_members SET last_read_seq=MAX(last_read_seq, ?) WHERE channel_id=? AND user_id=?",
            (last, cid, uid),
        )
        row = conn.execute(
            "SELECT last_read_seq FROM channel_members WHERE channel_id=? AND user_id=?",
            (cid, uid),
        ).fetchone()
    finally:
        conn.close()
    return json_response({"last_read_seq": row["last_read_seq"]})


# ---------- channel add member ----------


@routes.post("/api/channels/{cid}/members")
@authed
async def add_channel_member(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    try:
        data = await request.json()
    except Exception:
        return err("bad json")
    target = data.get("user_id")
    target_username = data.get("username")
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if not store.is_channel_member(conn, cid, uid):
            role = store.workspace_role(conn, c["workspace_id"], uid)
            if role not in ("owner", "admin"):
                return err("forbidden", status=403)
        if target_username and not target:
            u = store.find_user_by_username(conn, target_username)
            if not u:
                return err("user not found", status=404)
            target = u["id"]
        if not isinstance(target, int):
            return err("missing user_id")
        if not store.workspace_role(conn, c["workspace_id"], target):
            return err("user not in workspace", status=400)
        if not store.is_channel_member(conn, cid, target):
            conn.execute(
                "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
                (cid, target, store.now_iso()),
            )
    finally:
        conn.close()
    bus: Bus = request.app["bus"]
    await emit_durable(bus, cid, "member.joined", {"user_id": target})
    return json_response({"ok": True}, status=201)


@routes.get("/api/channels/{cid}/members")
@authed
async def list_channel_members(request):
    try:
        cid = int(request.match_info["cid"])
    except ValueError:
        return err("not found", status=404)
    uid = request["user"]["id"]
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return err("not found", status=404)
        if c["is_private"] and not store.is_channel_member(conn, cid, uid):
            return err("forbidden", status=403)
        rows = conn.execute(
            "SELECT cm.user_id, cm.joined_at, u.username, u.display_name FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=? ORDER BY cm.user_id",
            (cid,),
        ).fetchall()
    finally:
        conn.close()
    return json_response({"members": [
        {
            "user_id": r["user_id"],
            "joined_at": r["joined_at"],
            "username": r["username"],
            "display_name": r["display_name"] or r["username"],
        }
        for r in rows
    ]})


# ---------- WebSocket ----------


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    token = request.query.get("token")
    if not token:
        h = request.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            token = h[7:].strip()
    conn = store.connect()
    try:
        user = store.auth_user(conn, token) if token else None
    finally:
        conn.close()
    if not user:
        await ws.send_json({"type": "error", "message": "unauthorized"})
        await ws.close()
        return ws

    subs = set()  # subscribed channel ids
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(frame: dict):
        cid = frame.get("channel_id")
        if cid in subs:
            try:
                queue.put_nowait(frame)
            except Exception:
                pass

    bus: Bus = request.app["bus"]
    bus.add_listener(on_event)

    async def deliver():
        while True:
            frame = await queue.get()
            try:
                await ws.send_json(frame)
            except Exception:
                return

    deliver_task = asyncio.create_task(deliver())

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")
                if t == "subscribe":
                    cid = data.get("channel_id")
                    if not isinstance(cid, int):
                        continue
                    if not _can_read_channel(user["id"], cid):
                        await ws.send_json({"type": "error", "channel_id": cid, "message": "forbidden"})
                        continue
                    subs.add(cid)
                    head = _channel_head_seq(cid)
                    await ws.send_json({"type": "subscribed", "channel_id": cid, "head_seq": head})
                elif t == "resume":
                    cid = data.get("channel_id")
                    since = data.get("since_seq")
                    if not isinstance(cid, int) or not isinstance(since, int):
                        continue
                    if not _can_read_channel(user["id"], cid):
                        await ws.send_json({"type": "error", "channel_id": cid, "message": "forbidden"})
                        continue
                    subs.add(cid)
                    # send all events with seq > since (full retention)
                    conn = store.connect()
                    try:
                        rows = conn.execute(
                            "SELECT * FROM events WHERE channel_id=? AND seq>? ORDER BY seq",
                            (cid, since),
                        ).fetchall()
                        head = conn.execute(
                            "SELECT next_seq FROM channels WHERE id=?", (cid,)
                        ).fetchone()["next_seq"]
                    finally:
                        conn.close()
                    for r in rows:
                        await ws.send_json(store.event_to_frame(r))
                    await ws.send_json({"type": "resumed", "channel_id": cid, "head_seq": head})
                elif t == "unsubscribe":
                    cid = data.get("channel_id")
                    subs.discard(cid)
                elif t == "ping":
                    await ws.send_json({"type": "pong"})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        bus.remove_listener(on_event)
        deliver_task.cancel()
        try:
            await deliver_task
        except Exception:
            pass
    return ws


def _can_read_channel(uid: int, cid: int) -> bool:
    conn = store.connect()
    try:
        c = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not c:
            return False
        if c["is_private"] or c["is_dm"]:
            return store.is_channel_member(conn, cid, uid)
        return store.workspace_role(conn, c["workspace_id"], uid) is not None
    finally:
        conn.close()


def _channel_head_seq(cid: int) -> int:
    conn = store.connect()
    try:
        r = conn.execute("SELECT next_seq FROM channels WHERE id=?", (cid,)).fetchone()
        return r["next_seq"] if r else 0
    finally:
        conn.close()


# ---------- app setup ----------


async def make_app():
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.add_routes(routes)
    app.router.add_get("/api/ws", ws_handler)

    bus = Bus(NODE_ID)
    await bus.start()
    app["bus"] = bus
    app["node_id"] = NODE_ID
    return app


def main():
    async def run():
        app = await make_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, os.environ.get("HUDDLE_HTTP_HOST", "0.0.0.0"), PORT)
        await site.start()
        print(f"[node{NODE_ID}] listening on :{PORT}", flush=True)
        # serve forever
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
