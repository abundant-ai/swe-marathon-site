"""HTTP application for one node.

Exposes:
  - /api/* REST endpoints
  - /api/ws WebSocket
  - GET / serves the SPA from /app/server/web/index.html
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# allow `python -m server.http_app` from /app
sys.path.insert(0, "/app")

from aiohttp import web, WSMsgType

from server import store
from server.broadcast import BroadcastClient

WEB_DIR = Path(__file__).parent / "web"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB

# slash command syntax
_SLASH_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$", re.DOTALL)
_SHRUG = "¯\\_(ツ)_/¯"

# ---- helpers ----------------------------------------------------------------

def _bearer(req: web.Request) -> str | None:
    h = req.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    # also accept ?token= as a fallback (used by WS)
    return req.query.get("token")


def _auth_user(req: web.Request) -> dict | None:
    return store.user_for_token(_bearer(req) or "")


def _err(status: int, message: str, **extra) -> web.Response:
    body = {"error": message}
    body.update(extra)
    return web.json_response(body, status=status)


def _need_auth(req: web.Request) -> dict | web.Response:
    user = _auth_user(req)
    if not user:
        return _err(401, "unauthorized")
    return user


# ---- auth -------------------------------------------------------------------

async def register(req: web.Request) -> web.Response:
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    username = data.get("username", "")
    password = data.get("password", "")
    display_name = data.get("display_name")
    if not isinstance(username, str) or not store.valid_username(username):
        return _err(400, "invalid_username")
    if not isinstance(password, str) or len(password) < 8:
        return _err(400, "invalid_password")
    if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 64):
        return _err(400, "invalid_display_name")
    try:
        with store.Tx():
            user = store.create_user(username, password, display_name)
            token = store.issue_token(user["id"])
    except ValueError:
        return _err(409, "username_taken")
    return web.json_response({"user": user, "token": token}, status=201)


async def login(req: web.Request) -> web.Response:
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    username = data.get("username", "")
    password = data.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        return _err(400, "invalid_credentials")
    user = store.login_user(username, password)
    if not user:
        return _err(401, "invalid_credentials")
    token = store.issue_token(user["id"])
    return web.json_response({"user": user, "token": token})


async def me(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    return web.json_response({"user": u})


async def update_me(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    if not isinstance(data, dict):
        return _err(400, "invalid_body")
    try:
        with store.Tx():
            updated = store.update_user_profile(u["id"], data)
    except ValueError as e:
        return _err(400, str(e))
    return web.json_response({"user": updated})


async def get_user(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    target = store.get_user_by_id(req.match_info["id"])
    if not target:
        return _err(404, "not_found")
    return web.json_response({"user": target})


# ---- health -----------------------------------------------------------------

async def health(req: web.Request) -> web.Response:
    nid = req.app["node_id"]
    return web.json_response({"status": "ok", "node_id": nid})


# ---- workspaces -------------------------------------------------------------

async def create_workspace(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    slug = data.get("slug", "")
    name = data.get("name", "")
    if not store.valid_slug(slug):
        return _err(400, "invalid_slug")
    if not isinstance(name, str) or not (1 <= len(name) <= 80):
        return _err(400, "invalid_name")
    try:
        with store.Tx():
            ws, ch = store.create_workspace(slug, name, u["id"])
    except ValueError:
        return _err(409, "slug_taken")
    # event for general channel creation isn't needed (no subscribers yet)
    return web.json_response({"workspace": ws, "general_channel": ch}, status=201)


async def list_workspaces(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    return web.json_response({"workspaces": store.list_user_workspaces(u["id"])})


async def get_workspace(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    slug = req.match_info["slug"]
    ws = store.get_workspace_by_slug(slug)
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if not role and ws["join_mode"] != "open":
        return _err(403, "not_member")
    include_archived = req.query.get("include_archived") == "true"
    channels = store.list_workspace_channels(ws["id"], u["id"], include_archived=include_archived, include_dms=False)
    dms = []
    if role:
        dms = store.list_workspace_channels(ws["id"], u["id"], include_archived=False, include_dms=True)
        dms = [c for c in dms if c["is_dm"]]
        # DMs only the user is in
        dms = [c for c in dms if store.is_channel_member(c["id"], u["id"])]
    members = store.list_workspace_members(ws["id"])
    read_state = store.get_read_state(u["id"], ws["id"]) if role else {}
    return web.json_response({
        "workspace": ws,
        "role": role,
        "channels": channels,
        "dms": dms,
        "members": members,
        "read_state": read_state,
    })


async def patch_workspace(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    try:
        with store.Tx():
            ws2 = store.update_workspace(ws["id"], data)
    except ValueError:
        return _err(400, "invalid_field")
    return web.json_response({"workspace": ws2})


async def list_workspace_members(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if not role:
        return _err(403, "forbidden")
    return web.json_response({"members": store.list_workspace_members(ws["id"])})


async def patch_member(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    target_id = req.match_info["user_id"]
    actor_role = store.workspace_role(ws["id"], u["id"])
    if actor_role not in ("owner", "admin"):
        return _err(403, "forbidden")
    target_role = store.workspace_role(ws["id"], target_id)
    if not target_role:
        return _err(404, "not_found")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    new_role = data.get("role")
    if new_role not in ("admin", "member", "guest", "owner"):
        return _err(400, "invalid_role")
    if new_role == "owner":
        return _err(400, "use_transfer_ownership")
    if target_id == ws["owner_id"]:
        return _err(403, "cannot_modify_owner")
    if target_role == "admin" and actor_role != "owner":
        return _err(403, "cannot_modify_admin")
    with store.Tx():
        store.set_member_role(ws["id"], target_id, new_role)
    return web.json_response({"ok": True})


async def transfer_ownership(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    if ws["owner_id"] != u["id"]:
        return _err(403, "owner_only")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    new_owner_id = data.get("user_id")
    if not new_owner_id or not store.workspace_role(ws["id"], new_owner_id):
        return _err(400, "invalid_user")
    with store.Tx():
        store.transfer_ownership(ws["id"], new_owner_id)
    return web.json_response({"workspace": store.get_workspace_by_id(ws["id"])})


# ---- channels ---------------------------------------------------------------

async def create_channel(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if not role:
        return _err(403, "not_member")
    if role == "guest":
        return _err(403, "guests_cannot_create_channels")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    name = data.get("name", "")
    is_private = bool(data.get("is_private"))
    topic = data.get("topic", "")
    if not store.valid_channel_name(name):
        return _err(400, "invalid_name")
    if not isinstance(topic, str) or len(topic) > 250:
        return _err(400, "topic_too_long")
    if is_private and role not in ("owner", "admin"):
        return _err(403, "members_cannot_create_private")
    try:
        with store.Tx():
            ch = store.create_channel(ws["id"], name, is_private, topic, u["id"])
    except ValueError:
        return _err(409, "name_taken")
    return web.json_response({"channel": ch}, status=201)


async def join_channel(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    ws = store.get_workspace_by_id(ch["workspace_id"])
    actor_role = store.workspace_role(ws["id"], u["id"])
    if ch["is_private"]:
        if not actor_role:
            return _err(403, "private_channel")
        return _err(403, "private_channel")
    # public channel:
    if not actor_role:
        if ws["join_mode"] != "open":
            return _err(403, "invite_only_workspace")
        with store.Tx():
            store.add_workspace_member(ws["id"], u["id"], "member")
            store.add_channel_member(ch["id"], u["id"])
            ev = {"channel_id": ch["id"], "user_id": u["id"], "user": u}
            store.commit_event(ch["id"], "member.joined", ev)
        await req.app["dispatch"]({"type": "member.joined", **ev, "channel_id": ch["id"]})
        return web.json_response({"channel": ch})
    if actor_role == "guest":
        return _err(403, "guests_cannot_join")
    with store.Tx():
        added = store.add_channel_member(ch["id"], u["id"])
        if added:
            seq, payload = store.commit_event(ch["id"], "member.joined", {"user_id": u["id"], "user": u})
    if added:
        await req.app["dispatch"](payload)
    return web.json_response({"channel": ch})


async def leave_channel(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    with store.Tx():
        if store.is_channel_member(ch["id"], u["id"]):
            store.remove_channel_member(ch["id"], u["id"])
            seq, payload = store.commit_event(ch["id"], "member.left", {"user_id": u["id"]})
        else:
            payload = None
    if payload:
        await req.app["dispatch"](payload)
    return web.Response(status=204)


async def patch_channel(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    role = store.workspace_role(ch["workspace_id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    try:
        with store.Tx():
            ch2 = store.update_channel(ch["id"], data)
            seq, payload = store.commit_event(ch["id"], "channel.updated", {"channel": ch2})
    except ValueError as e:
        if str(e) == "conflict":
            return _err(409, "name_taken")
        if str(e) == "topic_too_long":
            return _err(400, "topic_too_long")
        return _err(400, str(e))
    await req.app["dispatch"](payload)
    return web.json_response({"channel": ch2})


# ---- messages ---------------------------------------------------------------

def _expand_slash(body: str, channel_id: str, user_id: str, app: web.Application) -> tuple[str | None, web.Response | None, str | None]:
    """Returns (final_body, error_response, side_effect).
    side_effect is one of None, "archive", "unarchive", "topic:<value>",
    "invite:<username>"."""
    if not body.startswith("/"):
        return body, None, None
    if body.startswith("//"):
        return body[1:], None, None
    m = _SLASH_RE.match(body)
    if not m:
        return None, _err(400, "unknown_command"), None
    cmd = m.group(1)
    args = (m.group(2) or "").strip()
    if cmd == "me":
        return body, None, None
    if cmd == "shrug":
        if args:
            return f"{args} {_SHRUG}", None, None
        return _SHRUG, None, None
    if cmd == "topic":
        return None, None, f"topic:{args}"
    if cmd == "archive":
        return None, None, "archive"
    if cmd == "unarchive":
        return None, None, "unarchive"
    if cmd == "invite":
        target = args.lstrip("@").split()[0] if args else ""
        if not target:
            return None, _err(400, "missing_user"), None
        return None, None, f"invite:{target}"
    return None, _err(400, "unknown_command"), None


async def post_message(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    if ch["is_archived"]:
        return _err(423, "archived")
    role = store.workspace_role(ch["workspace_id"], u["id"])
    if not role:
        return _err(403, "not_member")
    if (ch["is_private"] or ch["is_dm"]) and not store.is_channel_member(ch["id"], u["id"]):
        return _err(403, "not_member")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    body = data.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return _err(400, "empty_body")
    parent_id = data.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        return _err(400, "invalid_parent")
    if parent_id:
        parent = store.get_message(parent_id)
        if not parent or parent["channel_id"] != ch["id"]:
            return _err(400, "invalid_parent")
    file_ids = data.get("file_ids") or []
    if not isinstance(file_ids, list):
        return _err(400, "invalid_files")
    for fid in file_ids:
        f = store.get_file_row(fid)
        if not f or f["uploader_id"] != u["id"] or f["attached_to"] is not None:
            return _err(400, "invalid_file")

    final_body, err_resp, side = _expand_slash(body, ch["id"], u["id"], req.app)
    if err_resp:
        return err_resp
    if side:
        # slash commands with side effects
        kind = side.split(":", 1)[0]
        arg = side.split(":", 1)[1] if ":" in side else ""
        if kind == "topic":
            try:
                with store.Tx():
                    ch2 = store.update_channel(ch["id"], {"topic": arg})
                    seq, payload = store.commit_event(ch["id"], "channel.updated", {"channel": ch2})
            except ValueError:
                return _err(400, "invalid")
            await req.app["dispatch"](payload)
            return web.json_response({"channel": ch2}, status=201)
        if kind in ("archive", "unarchive"):
            if role not in ("owner", "admin"):
                return _err(403, "forbidden")
            with store.Tx():
                ch2 = store.update_channel(ch["id"], {"is_archived": kind == "archive"})
                seq, payload = store.commit_event(ch["id"], "channel.updated", {"channel": ch2})
            await req.app["dispatch"](payload)
            return web.json_response({"channel": ch2}, status=201)
        if kind == "invite":
            target = store.get_user_by_username(arg)
            if not target:
                return _err(400, "user_not_found")
            with store.Tx():
                store.add_workspace_member(ch["workspace_id"], target["id"], "member")
                added = store.add_channel_member(ch["id"], target["id"])
                if added:
                    seq, payload = store.commit_event(ch["id"], "member.joined",
                                                       {"user_id": target["id"], "user": target})
                else:
                    payload = None
            if payload:
                await req.app["dispatch"](payload)
            return web.json_response({"ok": True}, status=201)

    with store.Tx():
        msg, payload = store.post_message(ch["id"], u["id"], final_body, parent_id=parent_id, file_ids=file_ids)
    await req.app["dispatch"](payload)
    return web.json_response({"message": msg}, status=201)


async def list_messages(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    if (ch["is_private"] or ch["is_dm"]) and not store.is_channel_member(ch["id"], u["id"]):
        return _err(403, "not_member")
    if not ch["is_private"] and not ch["is_dm"]:
        if not store.workspace_role(ch["workspace_id"], u["id"]):
            return _err(403, "not_member")
    try:
        limit = int(req.query.get("limit", "50"))
    except ValueError:
        limit = 50
    before = req.query.get("before")
    msgs = store.list_channel_messages(ch["id"], limit=limit, before=before)
    return web.json_response({"messages": msgs})


async def edit_message_handler(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    body = data.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return _err(400, "empty_body")
    try:
        with store.Tx():
            res = store.edit_message(req.match_info["id"], body, u["id"])
    except PermissionError:
        return _err(403, "not_author")
    if not res:
        return _err(404, "not_found")
    msg, payload = res
    await req.app["dispatch"](payload)
    return web.json_response({"message": msg})


async def delete_message_handler(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        with store.Tx():
            res = store.delete_message(req.match_info["id"], u["id"])
    except PermissionError:
        return _err(403, "not_author")
    if not res:
        return _err(404, "not_found")
    channel_id, payload = res
    await req.app["dispatch"](payload)
    return web.Response(status=204)


async def thread_replies(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    parent = store.get_message(req.match_info["parent_id"])
    if not parent:
        return _err(404, "not_found")
    ch = store.get_channel(parent["channel_id"])
    if (ch["is_private"] or ch["is_dm"]) and not store.is_channel_member(ch["id"], u["id"]):
        return _err(403, "not_member")
    return web.json_response({"messages": store.list_thread_replies(parent["id"])})


async def add_reaction(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    emoji = data.get("emoji", "")
    if not isinstance(emoji, str) or not emoji or len(emoji) > 32:
        return _err(400, "invalid_emoji")
    with store.Tx():
        res = store.add_reaction(req.match_info["id"], u["id"], emoji)
    if not res:
        return _err(404, "not_found")
    channel_id, payload = res
    await req.app["dispatch"](payload)
    return web.json_response({"ok": True}, status=201)


async def remove_reaction(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    emoji = data.get("emoji", "")
    if not isinstance(emoji, str) or not emoji:
        return _err(400, "invalid_emoji")
    with store.Tx():
        res = store.remove_reaction(req.match_info["id"], u["id"], emoji)
    if not res:
        return _err(404, "not_found")
    channel_id, payload = res
    await req.app["dispatch"](payload)
    return web.json_response({"ok": True})


# ---- pins -------------------------------------------------------------------

async def list_pins(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ch = store.get_channel(req.match_info["id"])
    if not ch:
        return _err(404, "not_found")
    if ch["is_private"] and not store.is_channel_member(ch["id"], u["id"]):
        return _err(403, "not_member")
    return web.json_response({"messages": store.list_pins(ch["id"])})


async def add_pin(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    with store.Tx():
        res = store.add_pin(req.match_info["id"], u["id"])
    if not res:
        return _err(404, "not_found")
    return web.json_response({"ok": True}, status=201)


async def del_pin(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    with store.Tx():
        res = store.remove_pin(req.match_info["id"])
    if not res:
        return _err(404, "not_found")
    return web.Response(status=204)


# ---- DMs --------------------------------------------------------------------

async def create_dm(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    rid = data.get("recipient_id")
    if not rid or not store.get_user_by_id(rid):
        return _err(400, "invalid_recipient")
    if rid == u["id"]:
        return _err(400, "self_dm")
    try:
        with store.Tx():
            ch, created = store.get_or_create_dm(u["id"], rid)
    except ValueError:
        return _err(400, "no_shared_workspace")
    return web.json_response({"channel": ch}, status=201 if created else 200)


# ---- files ------------------------------------------------------------------

async def upload_file(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    reader = await req.multipart()
    filename = "upload.bin"
    content_type = "application/octet-stream"
    parts = []
    total = 0
    field = await reader.next()
    while field is not None:
        if field.name == "file":
            filename = field.filename or "upload.bin"
            content_type = field.headers.get("Content-Type", content_type)
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    return _err(413, "file_too_large")
                parts.append(chunk)
            break
        field = await reader.next()
    data = b"".join(parts)
    if not data:
        return _err(400, "no_file")
    with store.Tx():
        f = store.create_file(u["id"], filename, content_type, data)
    return web.json_response({"file": f}, status=201)


async def get_file(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    f = store.get_file(req.match_info["id"])
    if not f:
        return _err(404, "not_found")
    return web.json_response({"file": f})


async def download_file(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    row = store.get_file_row(req.match_info["id"])
    if not row:
        return _err(404, "not_found")
    path = row["path"]
    if not os.path.exists(path):
        return _err(404, "not_found")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": row["content_type"],
            "Content-Disposition": f'attachment; filename="{row["filename"]}"',
        },
    )


# ---- search -----------------------------------------------------------------

async def search(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    q = req.query.get("q", "").strip()
    if not q:
        return web.json_response({"messages": []})
    ws = req.query.get("workspace")
    return web.json_response({"messages": store.search_messages(u["id"], q, ws)})


# ---- groups -----------------------------------------------------------------

async def list_groups(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    if not store.workspace_role(ws["id"], u["id"]):
        return _err(403, "forbidden")
    return web.json_response({"groups": store.list_groups(ws["id"])})


async def create_group(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    handle = data.get("handle", "")
    name = data.get("name", "")
    members = data.get("member_user_ids") or []
    if not store.valid_group_handle(handle):
        return _err(400, "invalid_handle")
    if not isinstance(name, str) or not name:
        return _err(400, "invalid_name")
    try:
        with store.Tx():
            g = store.create_group(ws["id"], handle, name, members)
    except ValueError:
        return _err(409, "handle_taken")
    return web.json_response({"group": g}, status=201)


async def get_group(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    if not store.workspace_role(ws["id"], u["id"]):
        return _err(403, "forbidden")
    g = store.get_group(ws["id"], req.match_info["handle"])
    if not g:
        return _err(404, "not_found")
    return web.json_response({"group": g})


async def patch_group(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    with store.Tx():
        g = store.update_group(ws["id"], req.match_info["handle"], data)
    if not g:
        return _err(404, "not_found")
    return web.json_response({"group": g})


async def delete_group(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    with store.Tx():
        store.delete_group(ws["id"], req.match_info["handle"])
    return web.Response(status=204)


# ---- invitations ------------------------------------------------------------

async def create_invitation(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    try:
        data = await req.json()
    except Exception:
        data = {}
    email = data.get("email")
    invited_username = data.get("invited_username")
    expires_in = data.get("expires_in")
    max_uses = int(data.get("max_uses") or 1)
    if max_uses < 1 or max_uses > 1000:
        return _err(400, "invalid_max_uses")
    with store.Tx():
        inv = store.create_invitation(ws["id"], u["id"], email, invited_username, expires_in, max_uses)
    return web.json_response({"invitation": inv}, status=201)


async def list_invitations_handler(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    ws = store.get_workspace_by_slug(req.match_info["slug"])
    if not ws:
        return _err(404, "not_found")
    role = store.workspace_role(ws["id"], u["id"])
    if role not in ("owner", "admin"):
        return _err(403, "forbidden")
    return web.json_response({"invitations": store.list_invitations(ws["id"])})


async def accept_invitation(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    code = req.match_info["code"]
    inv = store.get_invitation(code)
    if not inv:
        return _err(404, "not_found")
    try:
        with store.Tx():
            ws_id = store.consume_invitation(code, u["id"])
    except ValueError as e:
        return _err(400, str(e))
    return web.json_response({"workspace": store.get_workspace_by_id(ws_id)})


# ---- read state -------------------------------------------------------------

async def mark_read(req: web.Request) -> web.Response:
    u = _need_auth(req)
    if isinstance(u, web.Response):
        return u
    try:
        data = await req.json()
    except Exception:
        return _err(400, "invalid_json")
    seq = int(data.get("last_read_seq") or 0)
    with store.Tx():
        store.update_read_state(u["id"], req.match_info["id"], seq)
    return web.json_response({"ok": True})


# ---- WebSocket --------------------------------------------------------------

class WSManager:
    """Tracks live WebSockets and dispatches durable events to subscribers."""

    def __init__(self, app: web.Application):
        self.app = app
        # channel_id -> set of (ws, user_id)
        self.subs: dict[str, set[tuple[web.WebSocketResponse, str]]] = {}

    def subscribe(self, ws: web.WebSocketResponse, user_id: str, channel_id: str) -> None:
        self.subs.setdefault(channel_id, set()).add((ws, user_id))

    def unsubscribe_all(self, ws: web.WebSocketResponse) -> None:
        for chid, peers in list(self.subs.items()):
            self.subs[chid] = {p for p in peers if p[0] is not ws}

    async def dispatch(self, payload: dict) -> None:
        channel_id = payload.get("channel_id")
        if not channel_id:
            return
        peers = list(self.subs.get(channel_id, ()))
        if not peers:
            return
        for ws, user_id in peers:
            if not store.event_subscribers_for_user(user_id, channel_id):
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                pass


async def ws_handler(req: web.Request) -> web.WebSocketResponse:
    user = _auth_user(req)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)
    if not user:
        await ws.send_json({"type": "error", "error": "unauthorized"})
        await ws.close()
        return ws
    mgr: WSManager = req.app["ws_manager"]
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            t = data.get("type")
            if t == "subscribe":
                ch_id = data.get("channel_id")
                if not ch_id:
                    continue
                if not store.event_subscribers_for_user(user["id"], ch_id):
                    await ws.send_json({"type": "error", "error": "forbidden", "channel_id": ch_id})
                    continue
                mgr.subscribe(ws, user["id"], ch_id)
                head = store.channel_head_seq(ch_id)
                await ws.send_json({"type": "subscribed", "channel_id": ch_id, "head_seq": head})
            elif t == "resume":
                ch_id = data.get("channel_id")
                since = int(data.get("since_seq") or 0)
                if not ch_id:
                    continue
                if not store.event_subscribers_for_user(user["id"], ch_id):
                    await ws.send_json({"type": "error", "error": "forbidden", "channel_id": ch_id})
                    continue
                earliest = store.earliest_event_seq(ch_id)
                head = store.channel_head_seq(ch_id)
                if since < (earliest - 1) and earliest > 0:
                    await ws.send_json({
                        "type": "resume.gap",
                        "channel_id": ch_id,
                        "earliest_seq": earliest,
                    })
                events = store.replay_events(ch_id, since)
                for e in events:
                    try:
                        await ws.send_json(e)
                    except Exception:
                        break
                mgr.subscribe(ws, user["id"], ch_id)
                await ws.send_json({"type": "resumed", "channel_id": ch_id, "head_seq": head})
            elif t == "unsubscribe":
                ch_id = data.get("channel_id")
                if ch_id and ch_id in mgr.subs:
                    mgr.subs[ch_id] = {p for p in mgr.subs[ch_id] if p[0] is not ws or p[1] != user["id"]}
            elif t == "ping":
                await ws.send_json({"type": "pong"})
    finally:
        mgr.unsubscribe_all(ws)
    return ws


# ---- static -----------------------------------------------------------------

async def index(req: web.Request) -> web.Response:
    path = WEB_DIR / "index.html"
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def app_js(req: web.Request) -> web.Response:
    path = WEB_DIR / "app.js"
    return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": "application/javascript"})


async def app_css(req: web.Request) -> web.Response:
    path = WEB_DIR / "app.css"
    return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": "text/css"})


# ---- app construction -------------------------------------------------------

def make_app(node_id: int) -> web.Application:
    app = web.Application(client_max_size=MAX_FILE_SIZE + 1024 * 1024)
    app["node_id"] = node_id
    store.init_db()

    ws_mgr = WSManager(app)
    app["ws_manager"] = ws_mgr
    bc = BroadcastClient(node_id=node_id)
    app["broadcast"] = bc

    async def dispatch(payload: dict) -> None:
        # local fan-out + relay publish
        try:
            await ws_mgr.dispatch(payload)
        except Exception:
            pass
        # Also push to relay so other nodes receive it. event_id is the
        # global autoincrement we assigned at commit time; we look it up.
        # Most efficient: read the just-inserted event by (channel_id, seq).
        # But we don't know the event_id here. Use the broadcast client's
        # poller to pick it up; we still try a publish using a synthetic
        # id to keep relay-fed delivery low-latency for live subscribers.
        try:
            row = store.conn().execute(
                "SELECT event_id FROM events WHERE channel_id=? AND seq=?",
                (payload.get("channel_id"), payload.get("seq")),
            ).fetchone()
            if row:
                await bc.publish(row["event_id"], payload)
        except Exception:
            pass

    app["dispatch"] = dispatch

    async def on_start(app):
        # subscribe broadcast client to local fan-out (events from other nodes)
        async def relay_in(payload: dict):
            await ws_mgr.dispatch(payload)
        bc.add_listener(relay_in)
        await bc.start()

    async def on_cleanup(app):
        await bc.stop()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)

    # Routes
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/auth/register", register)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/auth/me", me)
    app.router.add_patch("/api/users/me", update_me)
    app.router.add_get(r"/api/users/{id}", get_user)

    app.router.add_post("/api/workspaces", create_workspace)
    app.router.add_get("/api/workspaces", list_workspaces)
    app.router.add_get(r"/api/workspaces/{slug}", get_workspace)
    app.router.add_patch(r"/api/workspaces/{slug}", patch_workspace)
    app.router.add_get(r"/api/workspaces/{slug}/members", list_workspace_members)
    app.router.add_patch(r"/api/workspaces/{slug}/members/{user_id}", patch_member)
    app.router.add_post(r"/api/workspaces/{slug}/transfer_ownership", transfer_ownership)
    app.router.add_post(r"/api/workspaces/{slug}/channels", create_channel)
    app.router.add_get(r"/api/workspaces/{slug}/groups", list_groups)
    app.router.add_post(r"/api/workspaces/{slug}/groups", create_group)
    app.router.add_get(r"/api/workspaces/{slug}/groups/{handle}", get_group)
    app.router.add_patch(r"/api/workspaces/{slug}/groups/{handle}", patch_group)
    app.router.add_delete(r"/api/workspaces/{slug}/groups/{handle}", delete_group)
    app.router.add_post(r"/api/workspaces/{slug}/invitations", create_invitation)
    app.router.add_get(r"/api/workspaces/{slug}/invitations", list_invitations_handler)
    app.router.add_post(r"/api/invitations/{code}/accept", accept_invitation)

    app.router.add_post(r"/api/channels/{id}/join", join_channel)
    app.router.add_delete(r"/api/channels/{id}/members/me", leave_channel)
    app.router.add_patch(r"/api/channels/{id}", patch_channel)
    app.router.add_get(r"/api/channels/{id}/messages", list_messages)
    app.router.add_post(r"/api/channels/{id}/messages", post_message)
    app.router.add_get(r"/api/channels/{id}/pins", list_pins)
    app.router.add_post(r"/api/channels/{id}/read", mark_read)

    app.router.add_patch(r"/api/messages/{id}", edit_message_handler)
    app.router.add_delete(r"/api/messages/{id}", delete_message_handler)
    app.router.add_get(r"/api/messages/{parent_id}/replies", thread_replies)
    app.router.add_post(r"/api/messages/{id}/reactions", add_reaction)
    app.router.add_delete(r"/api/messages/{id}/reactions", remove_reaction)
    app.router.add_post(r"/api/messages/{id}/pin", add_pin)
    app.router.add_delete(r"/api/messages/{id}/pin", del_pin)

    app.router.add_post("/api/dms", create_dm)

    app.router.add_post("/api/files", upload_file)
    app.router.add_get(r"/api/files/{id}", get_file)
    app.router.add_get(r"/api/files/{id}/download", download_file)

    app.router.add_get("/api/search", search)

    app.router.add_get("/api/ws", ws_handler)

    app.router.add_get("/", index)
    app.router.add_get("/app.js", app_js)
    app.router.add_get("/app.css", app_css)
    app.router.add_get("/index.html", index)

    # CORS-ish for local dev / cross-port preflight
    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
            })
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        return resp

    app.middlewares.append(cors)
    return app


def main() -> None:
    node_id = int(os.environ.get("HUDDLE_NODE_ID", "0"))
    port = int(os.environ.get("HUDDLE_PORT", str(8000 + node_id)))
    app = make_app(node_id)
    web.run_app(app, host=os.environ.get("HUDDLE_HTTP_HOST", "0.0.0.0"), port=port, print=lambda *a, **kw: None,
                handle_signals=True, access_log=None)


if __name__ == "__main__":
    main()
