#!/usr/bin/env python3
import base64
import warnings

warnings.filterwarnings("ignore", message="'cgi' is deprecated.*", category=DeprecationWarning)
import cgi
import email.utils
import hashlib
import hmac
import html
import io
import json
import os
import queue
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_DIR = "/app"
DATA_DIR = os.path.join(APP_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "huddle.sqlite3")
FILE_LIMIT = 10 * 1024 * 1024
PORTS = [8000, 8001, 8002]
IRC_PORT = 6667

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
WORKSPACE_RE = re.compile(r"^[a-z0-9-]{2,32}$")
CHANNEL_RE = re.compile(r"^[a-z0-9-]{1,32}$")
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@.])@([A-Za-z0-9_-]{1,32})(?![A-Za-z0-9_])")


def utcnow():
    return datetime.now(timezone.utc)


def ts(dt=None):
    dt = dt or utcnow()
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:20]


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            avatar_url TEXT NOT NULL DEFAULT '',
            status_text TEXT NOT NULL DEFAULT '',
            status_emoji TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES users(id),
            join_mode TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            is_private INTEGER NOT NULL DEFAULT 0,
            is_dm INTEGER NOT NULL DEFAULT 0,
            topic TEXT NOT NULL DEFAULT '',
            is_archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            head_seq INTEGER NOT NULL DEFAULT 0,
            dm_key TEXT UNIQUE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_workspace_name
            ON channels(workspace_id, name) WHERE is_dm = 0;
        CREATE TABLE IF NOT EXISTS channel_members (
            channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            PRIMARY KEY(channel_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            author_id TEXT NOT NULL REFERENCES users(id),
            body TEXT NOT NULL,
            parent_id TEXT REFERENCES messages(id),
            reply_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            edited_at TEXT,
            deleted_at TEXT,
            seq INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages(channel_id, seq);
        CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
        CREATE TABLE IF NOT EXISTS reactions (
            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            emoji TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(message_id, user_id, emoji)
        );
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            uploader_id TEXT NOT NULL REFERENCES users(id),
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            content BLOB NOT NULL,
            attached_message_id TEXT REFERENCES messages(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pins (
            message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS read_state (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            last_read_seq INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS invitations (
            code TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            email TEXT,
            invited_username TEXT,
            expires_at TEXT,
            max_uses INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            handle TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, handle)
        );
        CREATE TABLE IF NOT EXISTS group_members (
            workspace_id TEXT NOT NULL,
            handle TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(workspace_id, handle, user_id),
            FOREIGN KEY(workspace_id, handle) REFERENCES groups(workspace_id, handle) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(channel_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_events_channel_seq ON events(channel_id, seq);
        """
    )
    conn.close()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password, stored):
    try:
        kind, salt, digest = stored.split("$", 2)
        if kind != "pbkdf2":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
        return hmac.compare_digest(test, digest)
    except Exception:
        return False


def row_to_user(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "timezone": row["timezone"],
        "avatar_url": row["avatar_url"],
        "status_text": row["status_text"],
        "status_emoji": row["status_emoji"],
    }


def row_to_workspace(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "join_mode": row["join_mode"],
    }


def row_to_channel(row):
    if not row:
        return None
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


def file_obj(row):
    return {
        "id": row["id"],
        "uploader_id": row["uploader_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "created_at": row["created_at"],
    }


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_channel(conn, channel_id):
    return conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


def get_workspace_by_slug(conn, slug):
    return conn.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()


def auth_user_from_token(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?",
        (token,),
    ).fetchone()
    return row


def member_role(conn, workspace_id, user_id):
    row = conn.execute(
        "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    ).fetchone()
    return row["role"] if row else None


def is_channel_member(conn, channel_id, user_id):
    return bool(
        conn.execute(
            "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",
            (channel_id, user_id),
        ).fetchone()
    )


def can_access_channel(conn, channel, user_id):
    if not channel:
        return False
    if channel["is_dm"] or channel["is_private"]:
        return is_channel_member(conn, channel["id"], user_id)
    if channel["workspace_id"]:
        return member_role(conn, channel["workspace_id"], user_id) is not None
    return is_channel_member(conn, channel["id"], user_id)


def require_adminish(conn, workspace_id, user_id):
    return member_role(conn, workspace_id, user_id) in ("owner", "admin")


def commit_event(conn, channel_id, kind, payload):
    row = conn.execute("SELECT head_seq FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not row:
        raise ValueError("channel not found")
    seq = int(row["head_seq"]) + 1
    conn.execute("UPDATE channels SET head_seq=? WHERE id=?", (seq, channel_id))
    conn.execute(
        "INSERT INTO events(channel_id, seq, type, payload, created_at) VALUES(?,?,?,?,?)",
        (channel_id, seq, kind, json.dumps(payload, separators=(",", ":")), ts()),
    )
    return seq


def event_frame(row):
    payload = json.loads(row["payload"])
    frame = {"type": row["type"], "seq": row["seq"], "channel_id": row["channel_id"]}
    frame.update(payload)
    return frame


def reaction_list(conn, message_id):
    rows = conn.execute(
        "SELECT emoji, user_id FROM reactions WHERE message_id=? ORDER BY emoji, user_id",
        (message_id,),
    ).fetchall()
    by_emoji = {}
    for row in rows:
        by_emoji.setdefault(row["emoji"], []).append(row["user_id"])
    return [
        {"emoji": emoji, "count": len(user_ids), "user_ids": user_ids}
        for emoji, user_ids in by_emoji.items()
    ]


def resolve_mentions(conn, workspace_id, body, author_id):
    if not workspace_id:
        return []
    found = []
    for m in MENTION_RE.finditer(body or ""):
        handle = m.group(1)
        u = conn.execute(
            """
            SELECT u.id FROM users u
            JOIN workspace_members wm ON wm.user_id=u.id
            WHERE wm.workspace_id=? AND u.username=?
            """,
            (workspace_id, handle),
        ).fetchone()
        if u and u["id"] != author_id:
            found.append(u["id"])
            continue
        g_rows = conn.execute(
            "SELECT user_id FROM group_members WHERE workspace_id=? AND handle=?",
            (workspace_id, handle),
        ).fetchall()
        for gr in g_rows:
            if gr["user_id"] != author_id:
                found.append(gr["user_id"])
    deduped = []
    for item in found:
        if item not in deduped:
            deduped.append(item)
    return deduped


def message_obj(conn, row):
    if not row:
        return None
    author = get_user(conn, row["author_id"])
    files = conn.execute(
        "SELECT * FROM files WHERE attached_message_id=? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    ch = get_channel(conn, row["channel_id"])
    mentions = resolve_mentions(conn, ch["workspace_id"] if ch else None, row["body"], row["author_id"])
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "author": row_to_user(author),
        "body": row["body"],
        "parent_id": row["parent_id"],
        "reply_count": row["reply_count"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "files": [file_obj(f) for f in files],
        "reactions": reaction_list(conn, row["id"]),
        "mentions": mentions,
        "seq": row["seq"],
    }


def latest_head(conn, channel_id):
    row = conn.execute("SELECT head_seq FROM channels WHERE id=?", (channel_id,)).fetchone()
    return int(row["head_seq"]) if row else 0


def json_dumps(data):
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def validate_json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "invalid JSON")


def parse_auth(handler, conn):
    auth = handler.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    user = auth_user_from_token(conn, token)
    if not user:
        raise ApiError(401, "missing or invalid bearer token")
    return user, token


def valid_timezone(name):
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def attach_own_files(conn, user_id, message_id, file_ids):
    for fid in file_ids or []:
        f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not f or f["uploader_id"] != user_id or f["attached_message_id"]:
            raise ApiError(400, "file cannot be attached")
    for fid in file_ids or []:
        conn.execute("UPDATE files SET attached_message_id=? WHERE id=?", (message_id, fid))


def create_message(conn, user, channel_id, body, parent_id=None, file_ids=None, blocks=None):
    body = body if body is not None else ""
    if not isinstance(body, str) or body.strip() == "":
        raise ApiError(400, "message body cannot be empty")
    channel = get_channel(conn, channel_id)
    if not channel:
        raise ApiError(404, "channel not found")
    if not can_access_channel(conn, channel, user["id"]):
        raise ApiError(403, "forbidden")
    if channel["is_archived"] and not (isinstance(body, str) and body.startswith("/unarchive")):
        raise ApiError(423, "channel is archived")
    if parent_id:
        parent = conn.execute(
            "SELECT * FROM messages WHERE id=? AND channel_id=? AND deleted_at IS NULL",
            (parent_id, channel_id),
        ).fetchone()
        if not parent:
            raise ApiError(404, "parent message not found")

    if body.startswith("/") and not body.startswith("//"):
        parts = body.split(" ", 1)
        cmd = parts[0][1:]
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "me":
            pass
        elif cmd == "shrug":
            body = (arg + " " if arg else "") + "¯\\_(ツ)_/¯"
        elif cmd == "topic":
            if len(arg) > 250:
                raise ApiError(400, "topic is too long")
            if not require_adminish(conn, channel["workspace_id"], user["id"]):
                raise ApiError(403, "forbidden")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE channels SET topic=? WHERE id=?", (arg, channel_id))
            updated = get_channel(conn, channel_id)
            commit_event(conn, channel_id, "channel.updated", {"channel": row_to_channel(updated)})
            conn.execute("COMMIT")
            return {"channel": row_to_channel(updated)}, 200
        elif cmd == "archive" or cmd == "unarchive":
            if not require_adminish(conn, channel["workspace_id"], user["id"]):
                raise ApiError(403, "forbidden")
            archived = 1 if cmd == "archive" else 0
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE channels SET is_archived=? WHERE id=?", (archived, channel_id))
            updated = get_channel(conn, channel_id)
            commit_event(conn, channel_id, "channel.updated", {"channel": row_to_channel(updated)})
            conn.execute("COMMIT")
            return {"channel": row_to_channel(updated)}, 200
        elif cmd == "invite":
            if not channel["workspace_id"]:
                raise ApiError(400, "cannot invite to a DM")
            target = arg.strip()
            if target.startswith("@"):
                target = target[1:]
            u = conn.execute("SELECT * FROM users WHERE username=?", (target,)).fetchone()
            if not u or not member_role(conn, channel["workspace_id"], u["id"]):
                raise ApiError(404, "user not found")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)",
                (channel_id, u["id"], ts()),
            )
            commit_event(conn, channel_id, "member.joined", {"user": row_to_user(u)})
            conn.execute("COMMIT")
            return {"channel": row_to_channel(channel)}, 200
        else:
            raise ApiError(400, "unknown slash command")

    msg_id = rid("m")
    now = ts()
    conn.execute("BEGIN IMMEDIATE")
    seq = commit_event(conn, channel_id, "message.reply" if parent_id else "message.new", {})
    conn.execute(
        """
        INSERT INTO messages(id,channel_id,author_id,body,parent_id,reply_count,created_at,edited_at,deleted_at,seq)
        VALUES(?,?,?,?,?,0,?,?,NULL,?)
        """,
        (msg_id, channel_id, user["id"], body, parent_id, now, None, seq),
    )
    attach_own_files(conn, user["id"], msg_id, file_ids or [])
    if parent_id:
        conn.execute(
            "UPDATE messages SET reply_count=reply_count+1 WHERE id=? AND deleted_at IS NULL",
            (parent_id,),
        )
    msg = message_obj(conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())
    conn.execute(
        "UPDATE events SET payload=? WHERE channel_id=? AND seq=?",
        (json.dumps({"message": msg}, separators=(",", ":")), channel_id, seq),
    )
    conn.execute("COMMIT")
    return {"message": msg}, 201


class HuddleHandler(BaseHTTPRequestHandler):
    server_version = "Huddle/1.0"

    def log_message(self, fmt, *args):
        return

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        super().end_headers()

    def send_json(self, status, data):
        payload = json_dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_empty(self, status=204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_empty(204)

    def do_GET(self):
        if self.path.startswith("/api/ws"):
            self.handle_ws()
            return
        try:
            self.route("GET")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": "internal server error"})

    def do_POST(self):
        try:
            self.route("POST")
        except ApiError as e:
            try:
                self.send_json(e.status, {"error": e.message})
            except BrokenPipeError:
                pass
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal server error"})

    def do_PATCH(self):
        try:
            self.route("PATCH")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal server error"})

    def do_DELETE(self):
        try:
            self.route("DELETE")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal server error"})

    def route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]
        conn = db()
        try:
            if method == "GET" and path == "/":
                payload = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if method == "GET" and path == "/api/health":
                self.send_json(200, {"status": "ok", "node_id": self.server.node_id})
                return
            if len(parts) >= 2 and parts[0] == "api" and parts[1] == "auth":
                self.handle_auth(conn, method, parts)
                return

            user, token = parse_auth(self, conn)
            if parts[:2] == ["api", "users"]:
                self.handle_users(conn, method, parts, user)
                return
            if parts[:2] == ["api", "workspaces"]:
                self.handle_workspaces(conn, method, parts, user, query)
                return
            if parts[:2] == ["api", "channels"]:
                self.handle_channels(conn, method, parts, user, query)
                return
            if parts[:2] == ["api", "messages"]:
                self.handle_messages(conn, method, parts, user)
                return
            if parts[:2] == ["api", "dms"]:
                self.handle_dms(conn, method, parts, user)
                return
            if parts[:2] == ["api", "files"]:
                self.handle_files(conn, method, parts, user)
                return
            if parts[:2] == ["api", "search"]:
                self.handle_search(conn, method, query, user)
                return
            if parts[:2] == ["api", "invitations"]:
                self.handle_invitation_accept(conn, method, parts, user)
                return
            raise ApiError(404, "not found")
        finally:
            conn.close()

    def handle_auth(self, conn, method, parts):
        if method == "POST" and parts == ["api", "auth", "register"]:
            body = validate_json_body(self)
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))
            display = str(body.get("display_name") or username)
            if not USERNAME_RE.match(username):
                raise ApiError(400, "username must be URL-safe")
            if len(password) < 8:
                raise ApiError(400, "password must be at least 8 characters")
            if len(display) > 80:
                raise ApiError(400, "display name is too long")
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ApiError(409, "username already exists")
            user_id = rid("u")
            token = secrets.token_urlsafe(32)
            now = ts()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO users(id,username,password_hash,display_name,timezone,avatar_url,status_text,status_emoji,created_at)
                VALUES(?,?,?,?,?,?,?, ?,?)
                """,
                (user_id, username, hash_password(password), display, "UTC", "", "", "", now),
            )
            conn.execute("INSERT INTO tokens(token,user_id,created_at) VALUES(?,?,?)", (token, user_id, now))
            conn.execute("COMMIT")
            user = row_to_user(get_user(conn, user_id))
            self.send_json(201, {"user": user, "token": token})
            return
        if method == "POST" and parts == ["api", "auth", "login"]:
            body = validate_json_body(self)
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                raise ApiError(401, "wrong username or password")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO tokens(token,user_id,created_at) VALUES(?,?,?)", (token, row["id"], ts()))
            self.send_json(200, {"user": row_to_user(row), "token": token})
            return
        if method == "GET" and parts == ["api", "auth", "me"]:
            user, _ = parse_auth(self, conn)
            self.send_json(200, {"user": row_to_user(user)})
            return
        raise ApiError(404, "not found")

    def handle_users(self, conn, method, parts, user):
        if method == "GET" and len(parts) == 3:
            row = get_user(conn, parts[2])
            if not row:
                raise ApiError(404, "user not found")
            self.send_json(200, {"user": row_to_user(row)})
            return
        if method == "PATCH" and parts == ["api", "users", "me"]:
            body = validate_json_body(self)
            allowed = {"display_name": 80, "timezone": 80, "avatar_url": 500, "status_text": 140, "status_emoji": 32}
            updates = {}
            for k, lim in allowed.items():
                if k in body:
                    val = str(body[k] or "")
                    if len(val) > lim:
                        raise ApiError(400, f"{k} is too long")
                    if k == "timezone" and not valid_timezone(val):
                        raise ApiError(400, "invalid timezone")
                    updates[k] = val
            if updates:
                fields = ",".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE users SET {fields} WHERE id=?", (*updates.values(), user["id"]))
                for ch in conn.execute("SELECT channel_id FROM channel_members WHERE user_id=?", (user["id"],)):
                    conn.execute("BEGIN IMMEDIATE")
                    commit_event(conn, ch["channel_id"], "user.updated", {"user": row_to_user(get_user(conn, user["id"]))})
                    conn.execute("COMMIT")
            self.send_json(200, {"user": row_to_user(get_user(conn, user["id"]))})
            return
        raise ApiError(404, "not found")

    def handle_workspaces(self, conn, method, parts, user, query):
        if method == "POST" and parts == ["api", "workspaces"]:
            body = validate_json_body(self)
            slug = str(body.get("slug", ""))
            name = str(body.get("name", "")).strip()
            if not WORKSPACE_RE.match(slug):
                raise ApiError(400, "invalid workspace slug")
            if not name or len(name) > 80:
                raise ApiError(400, "invalid workspace name")
            if conn.execute("SELECT 1 FROM workspaces WHERE slug=?", (slug,)).fetchone():
                raise ApiError(409, "workspace slug already exists")
            wid = rid("w")
            cid = rid("c")
            now = ts()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO workspaces(id,slug,name,owner_id,join_mode,created_at) VALUES(?,?,?,?,?,?)",
                         (wid, slug, name, user["id"], "open", now))
            conn.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES(?,?,?,?)",
                         (wid, user["id"], "owner", now))
            conn.execute(
                "INSERT INTO channels(id,workspace_id,name,is_private,is_dm,topic,is_archived,created_at,head_seq,dm_key) VALUES(?,?,?,?,?,?,?,?,0,NULL)",
                (cid, wid, "general", 0, 0, "", 0, now),
            )
            conn.execute("INSERT INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)",
                         (cid, user["id"], now))
            conn.execute("COMMIT")
            self.send_json(201, {"workspace": row_to_workspace(get_workspace_by_slug(conn, slug)),
                                 "general_channel": row_to_channel(get_channel(conn, cid))})
            return
        if method == "GET" and parts == ["api", "workspaces"]:
            rows = conn.execute(
                """
                SELECT w.* FROM workspaces w
                JOIN workspace_members wm ON wm.workspace_id=w.id
                WHERE wm.user_id=?
                ORDER BY w.created_at
                """,
                (user["id"],),
            ).fetchall()
            self.send_json(200, {"workspaces": [row_to_workspace(r) for r in rows]})
            return
        if len(parts) >= 3:
            ws = get_workspace_by_slug(conn, parts[2])
            if not ws:
                raise ApiError(404, "workspace not found")
            role = member_role(conn, ws["id"], user["id"])
            if method == "GET" and len(parts) == 3:
                if not role:
                    raise ApiError(403, "forbidden")
                include_archived = query.get("include_archived", ["false"])[0].lower() == "true"
                rows = conn.execute(
                    """
                    SELECT c.* FROM channels c
                    LEFT JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=?
                    WHERE c.workspace_id=? AND c.is_dm=0
                      AND (? OR c.is_archived=0)
                      AND (c.is_private=0 OR cm.user_id IS NOT NULL)
                    ORDER BY c.name
                    """,
                    (user["id"], ws["id"], 1 if include_archived else 0),
                ).fetchall()
                members = conn.execute(
                    "SELECT * FROM workspace_members WHERE workspace_id=? ORDER BY joined_at",
                    (ws["id"],),
                ).fetchall()
                reads = conn.execute(
                    "SELECT channel_id,last_read_seq FROM read_state WHERE user_id=?",
                    (user["id"],),
                ).fetchall()
                self.send_json(200, {
                    "workspace": row_to_workspace(ws),
                    "channels": [row_to_channel(r) for r in rows],
                    "members": [dict(m) for m in members],
                    "read_state": {r["channel_id"]: r["last_read_seq"] for r in reads},
                })
                return
            if method == "PATCH" and len(parts) == 3:
                if role not in ("owner", "admin"):
                    raise ApiError(403, "forbidden")
                body = validate_json_body(self)
                updates = {}
                if "name" in body:
                    name = str(body["name"] or "").strip()
                    if not name or len(name) > 80:
                        raise ApiError(400, "invalid name")
                    updates["name"] = name
                if "join_mode" in body:
                    jm = str(body["join_mode"])
                    if jm not in ("open", "invite_only"):
                        raise ApiError(400, "invalid join_mode")
                    updates["join_mode"] = jm
                if updates:
                    fields = ",".join(f"{k}=?" for k in updates)
                    conn.execute(f"UPDATE workspaces SET {fields} WHERE id=?", (*updates.values(), ws["id"]))
                self.send_json(200, {"workspace": row_to_workspace(get_workspace_by_slug(conn, ws["slug"]))})
                return
            if len(parts) >= 4 and parts[3] == "members":
                self.workspace_members(conn, method, parts, user, ws, role)
                return
            if len(parts) >= 4 and parts[3] == "channels":
                self.workspace_channels(conn, method, parts, user, ws, role)
                return
            if len(parts) >= 4 and parts[3] == "groups":
                self.workspace_groups(conn, method, parts, user, ws, role)
                return
            if len(parts) >= 4 and parts[3] == "invitations":
                self.workspace_invitations(conn, method, parts, user, ws, role)
                return
            if method == "POST" and len(parts) == 4 and parts[3] == "transfer_ownership":
                if role != "owner":
                    raise ApiError(403, "forbidden")
                body = validate_json_body(self)
                target = str(body.get("user_id", ""))
                if not member_role(conn, ws["id"], target):
                    raise ApiError(404, "member not found")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE workspaces SET owner_id=? WHERE id=?", (target, ws["id"]))
                conn.execute("UPDATE workspace_members SET role='owner' WHERE workspace_id=? AND user_id=?", (ws["id"], target))
                conn.execute("UPDATE workspace_members SET role='admin' WHERE workspace_id=? AND user_id=?", (ws["id"], user["id"]))
                conn.execute("COMMIT")
                self.send_json(200, {"workspace": row_to_workspace(get_workspace_by_slug(conn, ws["slug"]))})
                return
        raise ApiError(404, "not found")

    def workspace_members(self, conn, method, parts, user, ws, role):
        if method == "GET" and len(parts) == 4:
            if not role:
                raise ApiError(403, "forbidden")
            rows = conn.execute(
                "SELECT * FROM workspace_members WHERE workspace_id=? ORDER BY joined_at",
                (ws["id"],),
            ).fetchall()
            self.send_json(200, {"members": [dict(r) for r in rows]})
            return
        if method == "PATCH" and len(parts) == 5:
            if role not in ("owner", "admin"):
                raise ApiError(403, "forbidden")
            target_id = parts[4]
            body = validate_json_body(self)
            new_role = str(body.get("role", ""))
            if new_role == "owner":
                raise ApiError(400, "use transfer_ownership")
            if new_role not in ("admin", "member", "guest"):
                raise ApiError(400, "invalid role")
            target_role = member_role(conn, ws["id"], target_id)
            if not target_role:
                raise ApiError(404, "member not found")
            if target_id == ws["owner_id"] or target_role == "owner":
                raise ApiError(403, "cannot change owner")
            if role == "admin" and target_role == "admin":
                raise ApiError(403, "admins cannot modify admins")
            conn.execute(
                "UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
                (new_role, ws["id"], target_id),
            )
            self.send_json(200, {"member": {"workspace_id": ws["id"], "user_id": target_id, "role": new_role}})
            return
        raise ApiError(404, "not found")

    def workspace_channels(self, conn, method, parts, user, ws, role):
        if method == "POST" and len(parts) == 4:
            if not role:
                raise ApiError(403, "forbidden")
            if role == "guest":
                raise ApiError(403, "guests cannot create channels")
            body = validate_json_body(self)
            name = str(body.get("name", ""))
            is_private = bool(body.get("is_private", False))
            topic = str(body.get("topic", "") or "")
            if not CHANNEL_RE.match(name):
                raise ApiError(400, "invalid channel name")
            if len(topic) > 250:
                raise ApiError(400, "topic is too long")
            if is_private and role not in ("owner", "admin"):
                raise ApiError(403, "members cannot create private channels")
            if conn.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=? AND is_dm=0", (ws["id"], name)).fetchone():
                raise ApiError(409, "channel already exists")
            cid = rid("c")
            now = ts()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO channels(id,workspace_id,name,is_private,is_dm,topic,is_archived,created_at,head_seq,dm_key) VALUES(?,?,?,?,?,?,?,?,0,NULL)",
                (cid, ws["id"], name, 1 if is_private else 0, 0, topic, 0, now),
            )
            conn.execute("INSERT INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)", (cid, user["id"], now))
            conn.execute("COMMIT")
            self.send_json(201, {"channel": row_to_channel(get_channel(conn, cid))})
            return
        raise ApiError(404, "not found")

    def workspace_groups(self, conn, method, parts, user, ws, role):
        if not role:
            raise ApiError(403, "forbidden")
        if method == "GET" and len(parts) == 4:
            rows = conn.execute("SELECT * FROM groups WHERE workspace_id=? ORDER BY handle", (ws["id"],)).fetchall()
            self.send_json(200, {"groups": [group_obj(conn, ws["id"], r["handle"]) for r in rows]})
            return
        if method == "POST" and len(parts) == 4:
            if role not in ("owner", "admin"):
                raise ApiError(403, "forbidden")
            body = validate_json_body(self)
            handle = str(body.get("handle", "")).lstrip("@")
            name = str(body.get("name", handle))
            members = body.get("member_user_ids", [])
            if not WORKSPACE_RE.match(handle):
                raise ApiError(400, "invalid group handle")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO groups(workspace_id,handle,name,created_at) VALUES(?,?,?,?)",
                         (ws["id"], handle, name, ts()))
            for uid in members:
                if member_role(conn, ws["id"], uid):
                    conn.execute("INSERT OR IGNORE INTO group_members(workspace_id,handle,user_id) VALUES(?,?,?)",
                                 (ws["id"], handle, uid))
            conn.execute("COMMIT")
            self.send_json(201, {"group": group_obj(conn, ws["id"], handle)})
            return
        if len(parts) == 5:
            handle = parts[4]
            if method == "GET":
                obj = group_obj(conn, ws["id"], handle)
                if not obj:
                    raise ApiError(404, "group not found")
                self.send_json(200, {"group": obj})
                return
            if role not in ("owner", "admin"):
                raise ApiError(403, "forbidden")
            if method == "PATCH":
                body = validate_json_body(self)
                if not group_obj(conn, ws["id"], handle):
                    raise ApiError(404, "group not found")
                if "name" in body:
                    conn.execute("UPDATE groups SET name=? WHERE workspace_id=? AND handle=?",
                                 (str(body["name"]), ws["id"], handle))
                if "member_user_ids" in body:
                    conn.execute("DELETE FROM group_members WHERE workspace_id=? AND handle=?", (ws["id"], handle))
                    for uid in body["member_user_ids"] or []:
                        if member_role(conn, ws["id"], uid):
                            conn.execute("INSERT OR IGNORE INTO group_members(workspace_id,handle,user_id) VALUES(?,?,?)",
                                         (ws["id"], handle, uid))
                self.send_json(200, {"group": group_obj(conn, ws["id"], handle)})
                return
            if method == "DELETE":
                conn.execute("DELETE FROM groups WHERE workspace_id=? AND handle=?", (ws["id"], handle))
                self.send_empty(204)
                return
        raise ApiError(404, "not found")

    def workspace_invitations(self, conn, method, parts, user, ws, role):
        if role not in ("owner", "admin"):
            raise ApiError(403, "forbidden")
        if method == "POST" and len(parts) == 4:
            body = validate_json_body(self)
            code = secrets.token_urlsafe(18)
            expires_at = None
            if body.get("expires_in"):
                expires_at = ts(utcnow() + timedelta(seconds=int(body["expires_in"])))
            max_uses = body.get("max_uses")
            conn.execute(
                """
                INSERT INTO invitations(code,workspace_id,email,invited_username,expires_at,max_uses,uses,created_by,created_at)
                VALUES(?,?,?,?,?, ?,0,?,?)
                """,
                (code, ws["id"], body.get("email"), body.get("invited_username"), expires_at, max_uses, user["id"], ts()),
            )
            self.send_json(201, {"invitation": invitation_obj(conn, code)})
            return
        if method == "GET" and len(parts) == 4:
            rows = conn.execute("SELECT code FROM invitations WHERE workspace_id=? ORDER BY created_at DESC", (ws["id"],)).fetchall()
            self.send_json(200, {"invitations": [invitation_obj(conn, r["code"]) for r in rows]})
            return
        raise ApiError(404, "not found")

    def handle_channels(self, conn, method, parts, user, query):
        if len(parts) < 3:
            raise ApiError(404, "not found")
        channel = get_channel(conn, parts[2])
        if not channel:
            raise ApiError(404, "channel not found")
        if method == "POST" and len(parts) == 4 and parts[3] == "join":
            if channel["is_dm"]:
                raise ApiError(403, "cannot join a DM")
            if channel["is_private"]:
                raise ApiError(403, "private channel")
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (channel["workspace_id"],)).fetchone()
            role = member_role(conn, ws["id"], user["id"])
            if not role:
                if ws["join_mode"] != "open":
                    raise ApiError(403, "workspace is invite-only")
                conn.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES(?,?,?,?)",
                             (ws["id"], user["id"], "member", ts()))
                role = "member"
            if role == "guest":
                raise ApiError(403, "guests cannot join public channels")
            changed = not is_channel_member(conn, channel["id"], user["id"])
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)",
                         (channel["id"], user["id"], ts()))
            if changed:
                commit_event(conn, channel["id"], "member.joined", {"user": row_to_user(user)})
            conn.execute("COMMIT")
            self.send_json(200, {"channel": row_to_channel(channel)})
            return
        if method == "DELETE" and len(parts) == 5 and parts[3] == "members" and parts[4] == "me":
            conn.execute("BEGIN IMMEDIATE")
            existed = is_channel_member(conn, channel["id"], user["id"])
            conn.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?", (channel["id"], user["id"]))
            if existed:
                commit_event(conn, channel["id"], "member.left", {"user_id": user["id"]})
            conn.execute("COMMIT")
            self.send_empty(204)
            return
        if method == "PATCH" and len(parts) == 3:
            if not channel["workspace_id"] or not require_adminish(conn, channel["workspace_id"], user["id"]):
                raise ApiError(403, "forbidden")
            body = validate_json_body(self)
            updates = {}
            if "topic" in body:
                topic = str(body.get("topic") or "")
                if len(topic) > 250:
                    raise ApiError(400, "topic is too long")
                updates["topic"] = topic
            if "is_archived" in body:
                updates["is_archived"] = 1 if body.get("is_archived") else 0
            if "name" in body:
                name = str(body.get("name") or "")
                if not CHANNEL_RE.match(name):
                    raise ApiError(400, "invalid channel name")
                if conn.execute(
                    "SELECT 1 FROM channels WHERE workspace_id=? AND name=? AND id<>?",
                    (channel["workspace_id"], name, channel["id"]),
                ).fetchone():
                    raise ApiError(409, "channel already exists")
                updates["name"] = name
            if updates:
                conn.execute("BEGIN IMMEDIATE")
                fields = ",".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE channels SET {fields} WHERE id=?", (*updates.values(), channel["id"]))
                updated = get_channel(conn, channel["id"])
                commit_event(conn, channel["id"], "channel.updated", {"channel": row_to_channel(updated)})
                conn.execute("COMMIT")
            self.send_json(200, {"channel": row_to_channel(get_channel(conn, channel["id"]))})
            return
        if method == "GET" and len(parts) == 4 and parts[3] == "pins":
            if channel["is_private"] and not is_channel_member(conn, channel["id"], user["id"]):
                raise ApiError(403, "forbidden")
            rows = conn.execute(
                """
                SELECT m.* FROM messages m JOIN pins p ON p.message_id=m.id
                WHERE m.channel_id=? AND m.deleted_at IS NULL ORDER BY p.created_at DESC
                """,
                (channel["id"],),
            ).fetchall()
            self.send_json(200, {"messages": [message_obj(conn, r) for r in rows]})
            return
        if method == "GET" and len(parts) == 4 and parts[3] == "messages":
            if not can_access_channel(conn, channel, user["id"]):
                raise ApiError(403, "forbidden")
            limit = min(int(query.get("limit", ["50"])[0] or 50), 200)
            before = query.get("before", [None])[0]
            args = [channel["id"]]
            where = "channel_id=? AND parent_id IS NULL AND deleted_at IS NULL"
            if before:
                b = conn.execute("SELECT created_at FROM messages WHERE id=?", (before,)).fetchone()
                if b:
                    where += " AND created_at < ?"
                    args.append(b["created_at"])
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
            self.send_json(200, {"messages": [message_obj(conn, r) for r in rows]})
            return
        if method == "POST" and len(parts) == 4 and parts[3] == "messages":
            body = validate_json_body(self)
            result, status = create_message(
                conn, user, channel["id"], body.get("body", ""), body.get("parent_id"),
                body.get("file_ids") or [], body.get("blocks"),
            )
            self.send_json(status, result)
            return
        if method == "POST" and len(parts) == 4 and parts[3] == "read":
            if not can_access_channel(conn, channel, user["id"]):
                raise ApiError(403, "forbidden")
            body = validate_json_body(self)
            seq_no = int(body.get("last_read_seq", 0))
            current = conn.execute("SELECT last_read_seq FROM read_state WHERE user_id=? AND channel_id=?",
                                   (user["id"], channel["id"])).fetchone()
            if not current or seq_no > int(current["last_read_seq"]):
                conn.execute(
                    "INSERT INTO read_state(user_id,channel_id,last_read_seq,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_seq=excluded.last_read_seq, updated_at=excluded.updated_at",
                    (user["id"], channel["id"], seq_no, ts()),
                )
            row = conn.execute("SELECT * FROM read_state WHERE user_id=? AND channel_id=?", (user["id"], channel["id"])).fetchone()
            self.send_json(200, {"read_state": dict(row)})
            return
        raise ApiError(404, "not found")

    def handle_messages(self, conn, method, parts, user):
        if len(parts) < 3:
            raise ApiError(404, "not found")
        msg = conn.execute("SELECT * FROM messages WHERE id=?", (parts[2],)).fetchone()
        if not msg:
            raise ApiError(404, "message not found")
        channel = get_channel(conn, msg["channel_id"])
        if not can_access_channel(conn, channel, user["id"]):
            raise ApiError(403, "forbidden")
        if msg["deleted_at"] is not None:
            if method == "DELETE" and len(parts) == 3:
                self.send_empty(204)
                return
            raise ApiError(404, "message not found")
        if channel["is_archived"] and not (method == "GET" and len(parts) == 4 and parts[3] == "replies"):
            raise ApiError(423, "channel is archived")
        if method == "PATCH" and len(parts) == 3:
            if msg["author_id"] != user["id"]:
                raise ApiError(403, "forbidden")
            body = validate_json_body(self)
            text = str(body.get("body", ""))
            if text.strip() == "":
                raise ApiError(400, "message body cannot be empty")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE messages SET body=?, edited_at=? WHERE id=?", (text, ts(), msg["id"]))
            updated = message_obj(conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg["id"],)).fetchone())
            commit_event(conn, channel["id"], "message.edited", {"message": updated})
            conn.execute("COMMIT")
            self.send_json(200, {"message": updated})
            return
        if method == "DELETE" and len(parts) == 3:
            if msg["author_id"] != user["id"] and (not channel["workspace_id"] or member_role(conn, channel["workspace_id"], user["id"]) != "owner"):
                raise ApiError(403, "forbidden")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE messages SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (ts(), msg["id"]))
            conn.execute("DELETE FROM reactions WHERE message_id=?", (msg["id"],))
            if msg["parent_id"]:
                conn.execute(
                    "UPDATE messages SET reply_count=MAX(reply_count-1,0) WHERE id=?",
                    (msg["parent_id"],),
                )
            commit_event(conn, channel["id"], "message.deleted", {"message_id": msg["id"]})
            conn.execute("COMMIT")
            self.send_empty(204)
            return
        if method == "GET" and len(parts) == 4 and parts[3] == "replies":
            rows = conn.execute(
                "SELECT * FROM messages WHERE parent_id=? AND deleted_at IS NULL ORDER BY created_at ASC",
                (msg["id"],),
            ).fetchall()
            self.send_json(200, {"messages": [message_obj(conn, r) for r in rows]})
            return
        if len(parts) == 4 and parts[3] == "reactions":
            body = validate_json_body(self)
            emoji = str(body.get("emoji", ""))[:32]
            if not emoji:
                raise ApiError(400, "emoji required")
            if method == "POST":
                conn.execute("BEGIN IMMEDIATE")
                before = conn.total_changes
                conn.execute("INSERT OR IGNORE INTO reactions(message_id,user_id,emoji,created_at) VALUES(?,?,?,?)",
                             (msg["id"], user["id"], emoji, ts()))
                changed = conn.total_changes != before
                updated = message_obj(conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg["id"],)).fetchone())
                if changed:
                    commit_event(conn, channel["id"], "reaction.added", {"message_id": msg["id"], "emoji": emoji, "user_id": user["id"], "message": updated})
                conn.execute("COMMIT")
                self.send_json(200, {"message": updated})
                return
            if method == "DELETE":
                conn.execute("BEGIN IMMEDIATE")
                before = conn.total_changes
                conn.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",
                             (msg["id"], user["id"], emoji))
                changed = conn.total_changes != before
                updated = message_obj(conn, conn.execute("SELECT * FROM messages WHERE id=?", (msg["id"],)).fetchone())
                if changed:
                    commit_event(conn, channel["id"], "reaction.removed", {"message_id": msg["id"], "emoji": emoji, "user_id": user["id"], "message": updated})
                conn.execute("COMMIT")
                self.send_json(200, {"message": updated})
                return
        if len(parts) == 4 and parts[3] == "pin":
            if method == "POST":
                conn.execute("INSERT OR REPLACE INTO pins(message_id,user_id,created_at) VALUES(?,?,?)", (msg["id"], user["id"], ts()))
                self.send_json(200, {"message": message_obj(conn, msg)})
                return
            if method == "DELETE":
                conn.execute("DELETE FROM pins WHERE message_id=?", (msg["id"],))
                self.send_empty(204)
                return
        raise ApiError(404, "not found")

    def handle_dms(self, conn, method, parts, user):
        if method == "POST" and parts == ["api", "dms"]:
            body = validate_json_body(self)
            recipient = str(body.get("recipient_id", ""))
            other = get_user(conn, recipient)
            if not other or other["id"] == user["id"]:
                raise ApiError(404, "recipient not found")
            pair = sorted([user["id"], other["id"]])
            key = "dm:" + ":".join(pair)
            existing = conn.execute("SELECT * FROM channels WHERE dm_key=?", (key,)).fetchone()
            if existing:
                self.send_json(200, {"channel": row_to_channel(existing)})
                return
            cid = rid("c")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO channels(id,workspace_id,name,is_private,is_dm,topic,is_archived,created_at,head_seq,dm_key) VALUES(?,NULL,?,?,?,?,?, ?,0,?)",
                (cid, "dm-" + pair[0][-4:] + "-" + pair[1][-4:], 1, 1, "", 0, ts(), key),
            )
            for uid in pair:
                conn.execute("INSERT INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)", (cid, uid, ts()))
            conn.execute("COMMIT")
            self.send_json(201, {"channel": row_to_channel(get_channel(conn, cid))})
            return
        raise ApiError(404, "not found")

    def handle_files(self, conn, method, parts, user):
        if method == "POST" and parts == ["api", "files"]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > FILE_LIMIT + 1024 * 1024:
                raise ApiError(413, "file too large")
            ctype = self.headers.get("Content-Type", "")
            env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)}
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env, keep_blank_values=True)
            item = form["file"] if "file" in form else None
            if item is None or not getattr(item, "file", None):
                raise ApiError(400, "missing file part")
            content = item.file.read()
            if len(content) > FILE_LIMIT:
                raise ApiError(413, "file too large")
            fid = rid("f")
            filename = os.path.basename(item.filename or "upload.bin")
            content_type = item.type or "application/octet-stream"
            conn.execute(
                "INSERT INTO files(id,uploader_id,filename,content_type,size,content,attached_message_id,created_at) VALUES(?,?,?,?,?,?,NULL,?)",
                (fid, user["id"], filename, content_type, len(content), content, ts()),
            )
            self.send_json(201, {"file": file_obj(conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone())})
            return
        if len(parts) >= 3:
            f = conn.execute("SELECT * FROM files WHERE id=?", (parts[2],)).fetchone()
            if not f:
                raise ApiError(404, "file not found")
            if method == "GET" and len(parts) == 3:
                self.send_json(200, {"file": file_obj(f)})
                return
            if method == "GET" and len(parts) == 4 and parts[3] == "download":
                data = f["content"]
                self.send_response(200)
                self.send_header("Content-Type", f["content_type"])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f"attachment; filename={f['filename']!r}")
                self.end_headers()
                self.wfile.write(data)
                return
        raise ApiError(404, "not found")

    def handle_search(self, conn, method, query, user):
        if method != "GET":
            raise ApiError(404, "not found")
        q = (query.get("q", [""])[0] or "").strip()
        slug = query.get("workspace", [""])[0]
        ws = get_workspace_by_slug(conn, slug)
        if not ws or not member_role(conn, ws["id"], user["id"]):
            raise ApiError(403, "forbidden")
        rows = conn.execute(
            """
            SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id
            LEFT JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=?
            WHERE c.workspace_id=? AND m.deleted_at IS NULL AND m.body LIKE ?
              AND (c.is_private=0 OR cm.user_id IS NOT NULL)
            ORDER BY m.created_at DESC LIMIT 100
            """,
            (user["id"], ws["id"], f"%{q}%"),
        ).fetchall()
        self.send_json(200, {"messages": [message_obj(conn, r) for r in rows]})

    def handle_invitation_accept(self, conn, method, parts, user):
        if method == "POST" and len(parts) == 4 and parts[3] == "accept":
            inv = conn.execute("SELECT * FROM invitations WHERE code=?", (parts[2],)).fetchone()
            if not inv:
                raise ApiError(404, "bad invitation code")
            if inv["expires_at"] and inv["expires_at"] < ts():
                raise ApiError(400, "invitation expired")
            if inv["max_uses"] is not None and inv["uses"] >= inv["max_uses"]:
                raise ApiError(400, "invitation exhausted")
            if inv["invited_username"] and inv["invited_username"] != user["username"]:
                raise ApiError(403, "invitation is for another user")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE invitations SET uses=uses+1 WHERE code=?", (parts[2],))
            conn.execute("INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES(?,?,?,?)",
                         (inv["workspace_id"], user["id"], "member", ts()))
            general = conn.execute("SELECT * FROM channels WHERE workspace_id=? AND name='general' AND is_dm=0",
                                   (inv["workspace_id"],)).fetchone()
            if general:
                conn.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)",
                             (general["id"], user["id"], ts()))
                commit_event(conn, general["id"], "member.joined", {"user": row_to_user(user)})
            conn.execute("COMMIT")
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (inv["workspace_id"],)).fetchone()
            self.send_json(200, {"workspace": row_to_workspace(ws)})
            return
        raise ApiError(404, "not found")

    def handle_ws(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        conn = db()
        user = auth_user_from_token(conn, token)
        if not user:
            self.send_response(401)
            self.end_headers()
            conn.close()
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(400)
            self.end_headers()
            conn.close()
            return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        sock = self.request
        sock.settimeout(0.5)
        subs = {}
        try:
            while True:
                try:
                    msg = ws_recv(sock)
                    if msg is None:
                        break
                    data = json.loads(msg)
                    typ = data.get("type")
                    ch_id = str(data.get("channel_id", ""))
                    ch = get_channel(conn, ch_id)
                    if not ch or not can_access_channel(conn, ch, user["id"]):
                        continue
                    if typ == "subscribe":
                        head = latest_head(conn, ch_id)
                        subs[ch_id] = head
                        ws_send(sock, {"type": "subscribed", "channel_id": ch_id, "head_seq": head})
                    elif typ == "resume":
                        since = int(data.get("since_seq", 0))
                        earliest = conn.execute("SELECT MIN(seq) AS m FROM events WHERE channel_id=?", (ch_id,)).fetchone()["m"]
                        if earliest is not None and since < earliest - 1:
                            ws_send(sock, {"type": "resume.gap", "channel_id": ch_id, "earliest_seq": earliest})
                        rows = conn.execute("SELECT * FROM events WHERE channel_id=? AND seq>? ORDER BY seq", (ch_id, since)).fetchall()
                        for row in rows:
                            ws_send(sock, event_frame(row))
                        head = latest_head(conn, ch_id)
                        subs[ch_id] = head
                        ws_send(sock, {"type": "resumed", "channel_id": ch_id, "head_seq": head})
                except socket.timeout:
                    pass
                for ch_id, last in list(subs.items()):
                    rows = conn.execute("SELECT * FROM events WHERE channel_id=? AND seq>? ORDER BY seq", (ch_id, last)).fetchall()
                    for row in rows:
                        ws_send(sock, event_frame(row))
                        subs[ch_id] = row["seq"]
        except Exception:
            pass
        finally:
            conn.close()
            try:
                sock.close()
            except Exception:
                pass


def group_obj(conn, workspace_id, handle):
    row = conn.execute("SELECT * FROM groups WHERE workspace_id=? AND handle=?", (workspace_id, handle)).fetchone()
    if not row:
        return None
    members = conn.execute("SELECT user_id FROM group_members WHERE workspace_id=? AND handle=? ORDER BY user_id",
                           (workspace_id, handle)).fetchall()
    return {"handle": row["handle"], "name": row["name"], "member_user_ids": [m["user_id"] for m in members]}


def invitation_obj(conn, code):
    row = conn.execute("SELECT * FROM invitations WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    return {
        "code": row["code"],
        "workspace_id": row["workspace_id"],
        "email": row["email"],
        "invited_username": row["invited_username"],
        "expires_at": row["expires_at"],
        "max_uses": row["max_uses"],
        "uses": row["uses"],
        "created_at": row["created_at"],
    }


def ws_recv(sock):
    head = sock.recv(2)
    if not head:
        return None
    b1, b2 = head
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    mask = sock.recv(4) if masked else b""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if opcode == 8:
        return None
    if opcode == 9:
        return ""
    return data.decode("utf-8")


def ws_send(sock, obj):
    data = json_dumps(obj)
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(len(data))
    elif len(data) < 65536:
        header.append(126)
        header += struct.pack("!H", len(data))
    else:
        header.append(127)
        header += struct.pack("!Q", len(data))
    sock.sendall(bytes(header) + data)


class ThreadedIRCServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


IRC_CLIENTS = set()
IRC_LOCK = threading.Lock()


class IRCHandler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.nick = None
        self.user = None
        self.token = None
        self.registered = False
        self.joined = set()
        self.conn = db()
        with IRC_LOCK:
            IRC_CLIENTS.add(self)

    def finish(self):
        with IRC_LOCK:
            IRC_CLIENTS.discard(self)
        self.conn.close()
        super().finish()

    def send_line(self, line):
        try:
            self.wfile.write((line + "\r\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def numeric(self, code, text):
        nick = self.nick or "*"
        self.send_line(f":huddle {code} {nick} {text}")

    def handle(self):
        while True:
            raw = self.rfile.readline(4096)
            if not raw:
                break
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if not line:
                continue
            try:
                if self.handle_line(line) is False:
                    break
            except Exception:
                traceback.print_exc()

    def maybe_register(self):
        if self.registered or not self.nick or not self.user:
            return
        if not self.token:
            self.numeric("464", ":Password required")
            return
        u = auth_user_from_token(self.conn, self.token)
        if not u:
            self.numeric("464", ":Password incorrect")
            return
        self.user = u
        self.registered = True
        self.numeric("001", ":Welcome to Huddle IRC")
        self.numeric("002", ":Your host is huddle")
        self.numeric("003", f":This server was created {ts()}")
        self.numeric("004", "huddle 1.0 o o")
        self.numeric("005", "CHANTYPES=# NICKLEN=32 :are supported")

    def handle_line(self, line):
        if " :" in line:
            before, trailing = line.split(" :", 1)
            args = before.split()
            args.append(trailing)
        else:
            args = line.split()
        if not args:
            return
        cmd = args[0].upper()
        if cmd == "PASS":
            self.token = args[1] if len(args) > 1 else ""
            return
        if cmd == "NICK":
            nick = args[1] if len(args) > 1 else ""
            with IRC_LOCK:
                if any(c is not self and c.nick == nick for c in IRC_CLIENTS):
                    self.send_line(f":huddle 433 * {nick} :Nickname is already in use")
                    return
            self.nick = nick
            self.maybe_register()
            return
        if cmd == "USER":
            self.user_name = args[1] if len(args) > 1 else "user"
            self.user = self.user or True
            self.maybe_register()
            return
        if cmd == "PING":
            self.send_line(f":huddle PONG huddle :{args[-1] if len(args) > 1 else ''}")
            return
        if cmd == "QUIT":
            return False
        if not self.registered:
            self.numeric("464", ":Password required")
            return
        if cmd == "JOIN":
            for name in (args[1] if len(args) > 1 else "").split(","):
                self.irc_join(name)
            return
        if cmd == "PRIVMSG":
            if len(args) >= 3:
                self.irc_privmsg(args[1], args[2])
            return
        if cmd == "NAMES":
            self.irc_names(args[1] if len(args) > 1 else None)
            return
        if cmd in ("WHO", "LIST", "TOPIC", "MODE", "PONG"):
            self.numeric("366", "* :End")
            return
        self.numeric("421", f"{cmd} :Unknown command")
        return True

    def find_channel_by_irc_name(self, name):
        name = name.lstrip("#")
        rows = self.conn.execute("SELECT * FROM channels WHERE name=? AND is_dm=0", (name,)).fetchall()
        for ch in rows:
            if can_access_channel(self.conn, ch, self.user["id"]):
                return ch
            if not ch["is_private"] and ch["workspace_id"]:
                ws = self.conn.execute("SELECT * FROM workspaces WHERE id=?", (ch["workspace_id"],)).fetchone()
                if ws and ws["join_mode"] == "open":
                    return ch
        return None

    def prefix(self):
        return f":{self.nick}!{self.user['username']}@huddle"

    def irc_join(self, name):
        ch = self.find_channel_by_irc_name(name)
        if not ch:
            self.numeric("403", f"{name} :No such channel")
            return
        if not can_access_channel(self.conn, ch, self.user["id"]):
            role = member_role(self.conn, ch["workspace_id"], self.user["id"])
            if not role:
                self.conn.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES(?,?,?,?)",
                                  (ch["workspace_id"], self.user["id"], "member", ts()))
            self.conn.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id,joined_at) VALUES(?,?,?)",
                              (ch["id"], self.user["id"], ts()))
        self.joined.add(ch["id"])
        irc_name = "#" + ch["name"]
        self.send_line(f"{self.prefix()} JOIN :{irc_name}")
        if ch["topic"]:
            self.numeric("332", f"{irc_name} :{ch['topic']}")
        else:
            self.numeric("331", f"{irc_name} :No topic is set")
        self.irc_names(irc_name)

    def irc_names(self, name):
        ch = self.find_channel_by_irc_name(name) if name else None
        if not ch:
            self.numeric("366", f"{name or '*'} :End of /NAMES list")
            return
        rows = self.conn.execute(
            "SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=? ORDER BY u.username",
            (ch["id"],),
        ).fetchall()
        names = " ".join(r["username"] for r in rows)
        self.numeric("353", f"= #{ch['name']} :{names}")
        self.numeric("366", f"#{ch['name']} :End of /NAMES list")

    def irc_privmsg(self, name, body):
        ch = self.find_channel_by_irc_name(name)
        if not ch or ch["id"] not in self.joined:
            self.numeric("403", f"{name} :No such channel")
            return
        create_message(self.conn, self.user, ch["id"], body)


def irc_event_poller(stop):
    conn = db()
    last = conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM events").fetchone()["m"]
    while not stop.is_set():
        try:
            rows = conn.execute("SELECT * FROM events WHERE id>? ORDER BY id", (last,)).fetchall()
            for row in rows:
                last = row["id"]
                if row["type"] not in ("message.new", "message.reply"):
                    continue
                payload = json.loads(row["payload"])
                msg = payload.get("message") or {}
                ch = get_channel(conn, row["channel_id"])
                if not ch:
                    continue
                author = msg.get("author") or {}
                nick = author.get("username") or "user"
                line = f":{nick}!{nick}@huddle PRIVMSG #{ch['name']} :{msg.get('body','')}"
                with IRC_LOCK:
                    clients = list(IRC_CLIENTS)
                for c in clients:
                    if c.registered and row["channel_id"] in c.joined:
                        c.send_line(line)
            time.sleep(0.25)
        except Exception:
            traceback.print_exc()
            time.sleep(1)
    conn.close()


def run_http(node_id):
    init_db()
    port = int(os.environ.get("PORT") or PORTS[node_id])
    server = ThreadingHTTPServer((os.environ.get("HUDDLE_HTTP_HOST", "127.0.0.1"), port), HuddleHandler)
    server.node_id = node_id
    print(f"http node {node_id} listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


def run_irc():
    init_db()
    stop = threading.Event()
    t = threading.Thread(target=irc_event_poller, args=(stop,), daemon=True)
    t.start()
    server = ThreadedIRCServer(("0.0.0.0", IRC_PORT), IRCHandler)
    print(f"irc gateway listening on 0.0.0.0:{IRC_PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()


def supervisor():
    init_db()
    children = {}
    stopping = False

    def start(name, args):
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + args, cwd=APP_DIR)
        children[name] = (args, p)
        return p

    def stop_all(signum=None, frame=None):
        nonlocal stopping
        stopping = True
        for args, p in list(children.values()):
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        deadline = time.time() + 5
        while time.time() < deadline and any(p.poll() is None for args, p in children.values()):
            time.sleep(0.1)
        for args, p in list(children.values()):
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)
    for i in range(3):
        start(f"http-{i}", ["http", str(i)])
    start("irc", ["irc"])
    while True:
        time.sleep(1)
        if stopping:
            continue
        for name, (args, p) in list(children.items()):
            if p.poll() is not None:
                print(f"{name} exited with {p.returncode}; restarting", flush=True)
                start(name, args)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Huddle</title>
<style>
:root{--side:#3f1f5f;--side2:#2f1748;--line:#d9dbe1;--muted:#68707d;--bg:#f7f8fa;--text:#1f2328;--accent:#1264a3}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text);background:#fff}button,input,textarea,select{font:inherit}button{cursor:pointer}
.hidden{display:none!important}.auth-shell{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#f7f8fa,#e6ebf2)}
.auth-card{width:min(440px,calc(100vw - 32px));background:#fff;border:1px solid var(--line);border-radius:8px;padding:28px;box-shadow:0 16px 40px #0001}
.auth-card h1{margin:0 0 18px;font-size:28px}.field{display:grid;gap:6px;margin:12px 0}.field input,.field textarea,.field select{border:1px solid #bcc2cc;border-radius:6px;padding:10px;background:#fff}.error{color:#b00020;min-height:20px}.error:empty{display:none}.primary{background:var(--accent);color:white;border:0;border-radius:6px;padding:10px 14px}.plain{background:#fff;border:1px solid #b8bec9;border-radius:6px;padding:8px 10px}.icon{border:0;background:transparent;color:#4b5563;padding:4px 6px;border-radius:4px}.icon:hover{background:#eef0f4}
.app{height:100vh;display:grid;grid-template-columns:280px minmax(420px,1fr) auto;background:#fff}
.sidebar{background:var(--side);color:#fff;display:flex;flex-direction:column;min-width:0}.workspace{padding:16px;border-bottom:1px solid #ffffff22}.workspace strong{display:block;font-size:17px}.workspace small{color:#ded4e8}.side-actions{display:flex;gap:6px;margin-top:10px}
.side-section{padding:12px 10px}.side-title{display:flex;justify-content:space-between;align-items:center;color:#d8cee2;font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin:4px 6px}
.channel{width:100%;text-align:left;border:0;background:transparent;color:#f5f0f8;border-radius:6px;padding:7px 9px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.channel:hover,.channel.active{background:#ffffff25}
.main{min-width:0;display:flex;flex-direction:column}.channel-head{height:68px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 20px}.channel-head h2{font-size:19px;margin:0}.topic{color:var(--muted);font-size:13px}
.messages{flex:1;overflow:auto;padding:18px 20px;background:#fff}.empty{margin:60px auto;color:#59636f;text-align:center;max-width:380px}
.msg{display:grid;grid-template-columns:40px 1fr;gap:10px;padding:8px 4px;border-radius:6px}.msg:hover{background:#f6f7f9}.avatar{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,#0b84a5,#f6c85f);display:grid;place-items:center;color:white;font-weight:700}.meta{display:flex;align-items:baseline;gap:8px}.author{font-weight:700}.time{color:var(--muted);font-size:12px}.body{white-space:pre-wrap;word-break:break-word}.edited{color:var(--muted);font-size:12px}.toolbar{display:flex;gap:4px;margin-top:4px}.reply-count{color:var(--accent);font-size:13px;margin-top:2px}
.composer{border-top:1px solid var(--line);padding:12px 16px;display:flex;gap:8px}.composer textarea{flex:1;min-height:44px;max-height:120px;border:1px solid #b9c0cc;border-radius:6px;padding:10px;resize:vertical}.composer-error{color:#b00020;padding:0 16px 8px}
.thread{width:380px;border-left:1px solid var(--line);display:flex;flex-direction:column;background:#fbfbfc}.thread-head{height:68px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 16px}.thread-body{flex:1;overflow:auto;padding:12px}.thread-compose{border-top:1px solid var(--line);padding:10px;display:flex;gap:8px}.thread-compose textarea{flex:1;min-height:42px}
.modal{position:fixed;inset:0;background:#0004;display:grid;place-items:center;z-index:10}.dialog{background:white;border-radius:8px;border:1px solid var(--line);width:min(560px,calc(100vw - 32px));max-height:86vh;overflow:auto;padding:18px}.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:12px}.tabs button{border:0;background:white;padding:8px 10px}.pane{display:grid;gap:10px}.member{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #edf0f3;padding:8px 0}.badge{background:#e9eef5;border-radius:20px;padding:2px 8px}.emoji-grid{display:grid;grid-template-columns:repeat(6,34px);gap:4px;padding:6px;border:1px solid var(--line);background:white;border-radius:6px;width:max-content}.emoji-grid button{height:32px;border:1px solid #d7dbe3;background:#fff;border-radius:5px}.chip{border:1px solid #cbd3df;background:#f5f8fb;border-radius:14px;padding:2px 8px;margin-right:4px}
</style>
</head>
<body>
<div id="auth" class="auth-shell">
  <form data-testid="auth-form" class="auth-card">
    <h1 id="auth-title">Create your account</h1>
    <div class="field"><label>Username<input name="username" autocomplete="username"></label></div>
    <div class="field"><label>Display name<input name="display_name" autocomplete="name"></label></div>
    <div class="field"><label>Password<input name="password" type="password" autocomplete="current-password"></label></div>
    <div id="auth-error" class="error" role="alert"></div>
    <button data-testid="auth-submit" class="primary" type="submit">Sign up</button>
    <button data-testid="auth-toggle" class="plain" type="button">Log in instead</button>
  </form>
</div>
<div id="app" class="app hidden">
  <aside class="sidebar">
    <div data-testid="workspace-header" class="workspace">
      <strong id="workspace-name">Huddle</strong>
      <small data-testid="current-user" id="current-user"></small>
      <div class="side-actions">
        <button data-testid="workspace-settings-btn" class="plain" type="button">Settings</button>
        <button data-testid="logout-btn" class="plain" type="button">Logout</button>
      </div>
    </div>
    <div data-testid="empty-state-create-workspace" id="empty-workspace" class="side-section hidden">Create a workspace to start chatting.</div>
    <form data-testid="workspace-create-form" id="workspace-form" class="side-section hidden">
      <div class="field"><input name="name" placeholder="Workspace name"></div>
      <div class="field"><input name="slug" placeholder="workspace-slug"></div>
      <button class="primary" type="submit">Create workspace</button>
    </form>
    <form data-testid="join-workspace-form" id="join-form" class="side-section">
      <div class="field"><input name="code" placeholder="Invitation code"></div>
      <button data-testid="join-workspace-submit" class="plain" type="submit">Join</button>
      <div data-testid="join-workspace-error" id="join-error" class="error"></div>
    </form>
    <div class="side-section">
      <div class="side-title"><span>Channels</span><button data-testid="new-channel-btn" class="icon" title="New channel" type="button">+</button></div>
      <div data-testid="channel-list" id="channel-list"></div>
    </div>
    <div class="side-section">
      <div class="side-title"><span>DMs</span></div>
      <div data-testid="dms-list" id="dms-list"></div>
    </div>
  </aside>
  <main class="main">
    <header class="channel-head">
      <div><h2 data-testid="channel-title" id="channel-title">#general</h2><div data-testid="channel-topic" id="channel-topic" class="topic"></div></div>
      <button data-testid="channel-settings-btn" id="channel-settings-btn" class="plain" type="button">Channel settings</button>
    </header>
    <section data-testid="message-list" id="message-list" class="messages"><div class="empty">Choose or create a channel.</div></section>
    <div id="compose-error" class="composer-error"></div>
    <form id="message-form" class="composer">
      <textarea data-testid="message-input" name="body" placeholder="Message"></textarea>
      <button data-testid="send-btn" class="primary" type="submit">Send</button>
    </form>
  </main>
  <aside data-testid="thread-panel" id="thread-panel" class="thread hidden">
    <div class="thread-head"><strong>Thread</strong><button data-testid="close-thread" class="icon" type="button">×</button></div>
    <div id="thread-body" class="thread-body"></div>
    <form id="thread-form" class="thread-compose"><textarea data-testid="thread-input" name="body" placeholder="Reply"></textarea><button data-testid="thread-send" class="primary" type="submit">Send</button></form>
  </aside>
</div>
<div data-testid="create-channel-form" id="channel-modal" class="modal hidden">
  <form class="dialog">
    <h3>Create channel</h3>
    <div class="field"><input name="name" placeholder="channel-name"></div>
    <label><input data-testid="create-channel-private" name="is_private" type="checkbox"> Private</label>
    <div id="channel-create-error" class="error"></div>
    <button data-testid="create-channel-submit" class="primary" type="submit">Create</button>
    <button data-testid="create-channel-cancel" class="plain" type="button">Cancel</button>
  </form>
</div>
<div id="workspace-settings" class="modal hidden">
  <div class="dialog">
    <button data-testid="workspace-settings-close" class="icon" style="float:right" type="button">×</button>
    <div class="tabs"><button data-testid="settings-tab-general" type="button">General</button><button data-testid="settings-tab-members" type="button">Members</button><button data-testid="settings-tab-invitations" type="button">Invitations</button></div>
    <div data-testid="settings-pane-general" class="pane">
      <input data-testid="workspace-name-input" id="workspace-name-input">
      <select data-testid="workspace-join-mode" id="workspace-join-mode"><option value="open">open</option><option value="invite_only">invite_only</option></select>
      <button data-testid="workspace-general-submit" id="workspace-general-submit" class="primary" type="button">Save</button>
    </div>
    <div data-testid="settings-pane-members" class="pane"><div id="members-list"></div></div>
    <div data-testid="settings-pane-invitations" class="pane">
      <button data-testid="create-invitation-btn" id="create-invitation-btn" class="plain" type="button">Create invitation</button>
      <form data-testid="create-invitation-form" id="invite-form" class="hidden"><input name="invited_username" placeholder="username"><button data-testid="create-invitation-submit" class="primary">Create</button></form>
      <div data-testid="invitations-list" id="invitations-list"></div>
    </div>
  </div>
</div>
<div id="channel-settings" class="modal hidden">
  <div class="dialog">
    <button data-testid="channel-settings-close" class="icon" style="float:right" type="button">×</button>
    <h3 data-testid="channel-settings-title">Channel settings</h3>
    <div data-testid="channel-members-list" id="channel-members-list"></div>
    <form data-testid="channel-add-member-form" id="channel-add-member-form"><input data-testid="channel-add-member-input" name="username" placeholder="username"><button data-testid="channel-add-member-submit" class="plain">Add</button></form>
    <form id="topic-form"><textarea data-testid="channel-topic-input" name="topic"></textarea><button data-testid="channel-topic-submit" class="primary">Save topic</button></form>
    <button data-testid="archive-channel-btn" id="archive-channel-btn" class="plain" type="button">Archive</button>
    <button data-testid="unarchive-channel-btn" id="unarchive-channel-btn" class="plain" type="button">Unarchive</button>
  </div>
</div>
<script>
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
let token=localStorage.getItem('huddle.token')||new URLSearchParams(location.search).get('token')||''; let me=null, workspaces=[], workspace=null, channels=[], current=null, messages=[], ws=null, threadParent=null, authMode='register';
window.__huddle_token=token;
function api(path, opts={}){opts.headers=Object.assign({'content-type':'application/json'},opts.headers||{}); if(token) opts.headers.authorization='Bearer '+token; if(opts.body&&typeof opts.body!=='string') opts.body=JSON.stringify(opts.body); return fetch('/api'+path,opts).then(async r=>{let data={}; try{data=await r.json()}catch(e){} if(!r.ok){throw new Error(data.error||r.statusText)} return data})}
function showAuth(){ $('#auth').classList.remove('hidden'); $('#app').classList.add('hidden') }
function showApp(){ $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden') }
function setToken(t){token=t; window.__huddle_token=t; localStorage.setItem('huddle.token',t)}
function fmt(t){return new Date(t).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
async function boot(){ if(!token){showAuth(); return} try{me=(await api('/auth/me')).user; await loadWorkspaces(); showApp()}catch(e){localStorage.removeItem('huddle.token'); token=''; showAuth()} }
async function loadWorkspaces(){ workspaces=(await api('/workspaces')).workspaces; $('#current-user').textContent=me.display_name||me.username; if(!workspaces.length){workspace=null; channels=[]; current=null; $('#empty-workspace').classList.remove('hidden'); $('#workspace-form').classList.remove('hidden'); $('#workspace-name').textContent='No workspace'; renderChannels(); renderMessages(); return} $('#empty-workspace').classList.add('hidden'); $('#workspace-form').classList.add('hidden'); workspace=workspaces[0]; await loadWorkspace(workspace.slug) }
async function loadWorkspace(slug){ let data=await api('/workspaces/'+encodeURIComponent(slug)); workspace=data.workspace; channels=data.channels; $('#workspace-name').textContent=workspace.name; let saved=localStorage.getItem('huddle.channel.'+workspace.slug); if(saved&&channels.find(c=>c.id===saved)) current=channels.find(c=>c.id===saved); renderChannels(); if(!current||!channels.find(c=>c.id===current.id)) current=channels[0]||null; if(current) await selectChannel(current.id); else renderMessages() }
function renderChannels(){ let box=$('#channel-list'); box.innerHTML=''; channels.forEach(c=>{let b=document.createElement('button'); b.className='channel'+(current&&current.id===c.id?' active':''); b.dataset.testid='channel-entry'; b.dataset.channelId=c.id; b.dataset.channelName=c.name; b.textContent='#'+c.name; b.onclick=()=>selectChannel(c.id); box.appendChild(b)}) }
async function selectChannel(id){ current=channels.find(c=>c.id===id)||current; if(workspace&&current)localStorage.setItem('huddle.channel.'+workspace.slug,current.id); renderChannels(); $('#channel-title').textContent=current?'#'+current.name:'No channel'; $('#channel-topic').textContent=current?(current.topic||'No topic set'):''; $('#topic-form [name=topic]').value=current?current.topic:''; await loadMessages(); connectWS() }
async function loadMessages(){ if(!current){messages=[]; renderMessages(); return} messages=(await api('/channels/'+current.id+'/messages?limit=100')).messages.reverse(); renderMessages() }
function renderMessages(){ let box=$('#message-list'); box.innerHTML=''; if(!current){box.innerHTML='<div class="empty">Create a workspace and channel to begin.</div>'; return} if(!messages.length){box.innerHTML='<div class="empty">No messages here yet. Start the conversation with a short note.</div>'; return} messages.forEach(m=>box.appendChild(messageEl(m,false))) }
function messageEl(m, inThread){ let row=document.createElement('article'); row.className='msg'; row.dataset.testid='message'; row.dataset.messageId=m.id; let initials=(m.author?.display_name||m.author?.username||'?').slice(0,1).toUpperCase(); row.innerHTML=`<div class="avatar">${initials}</div><div><div class="meta"><span class="author">${esc(m.author?.display_name||m.author?.username||'User')}</span><span class="time">${fmt(m.created_at)}</span></div><div data-testid="message-body" class="body">${esc(m.body)}</div>${m.edited_at?'<span class="edited">edited</span>':''}<div class="reactions"></div><div class="reply-count">${m.reply_count?m.reply_count+' '+(m.reply_count===1?'reply':'replies'):''}</div><div class="toolbar"><button data-testid="open-thread-btn" class="icon" type="button">Thread</button><button class="icon edit" type="button">Edit</button><button class="icon del" type="button">Delete</button><button data-testid="reaction-button" class="icon react" type="button">React</button></div><div class="emoji-slot"></div></div>`;
  let reactions=$('.reactions',row); (m.reactions||[]).forEach(r=>{let c=document.createElement('button'); c.dataset.testid='reaction-chip'; c.className='chip'; c.textContent=r.emoji+' '+r.count; reactions.appendChild(c)});
  $('[data-testid=open-thread-btn]',row).onclick=()=>openThread(m); $('.del',row).onclick=async()=>{await api('/messages/'+m.id,{method:'DELETE'}); await loadMessages()}; $('.edit',row).onclick=()=>editMessage(row,m); $('.react',row).onclick=()=>emojiPicker(row,m); return row }
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function editMessage(row,m){ let body=$('[data-testid=message-body]',row); let ta=document.createElement('textarea'); ta.name='edit-body'; ta.value=m.body; let save=document.createElement('button'); save.className='primary'; save.textContent='Save'; body.replaceWith(ta); save.onclick=async()=>{await api('/messages/'+m.id,{method:'PATCH',body:{body:ta.value}}); await loadMessages()}; ta.after(save)}
function emojiPicker(row,m){ let slot=$('.emoji-slot',row); slot.innerHTML=''; let grid=document.createElement('div'); grid.dataset.testid='emoji-picker'; grid.className='emoji-grid'; ['👍','❤️','😂','🎉','👀','✅','🚀','😄','🙏','🔥','💡','👏'].forEach(e=>{let b=document.createElement('button'); b.dataset.testid='emoji-option'; b.type='button'; b.textContent=e; b.onclick=async()=>{await api('/messages/'+m.id+'/reactions',{method:'POST',body:{emoji:e}}); await loadMessages()}; grid.appendChild(b)}); slot.appendChild(grid)}
async function openThread(m){ threadParent=m; $('#thread-panel').classList.remove('hidden'); await renderThread() }
async function renderThread(){ let box=$('#thread-body'); box.innerHTML=''; if(!threadParent)return; box.appendChild(messageEl(threadParent,true)); let reps=(await api('/messages/'+threadParent.id+'/replies')).messages; reps.forEach(r=>box.appendChild(messageEl(r,true)))}
function connectWS(){ if(ws) ws.close(); if(!current||!token)return; let proto=location.protocol==='https:'?'wss':'ws'; ws=new WebSocket(proto+'://'+location.host+'/api/ws?token='+encodeURIComponent(token)); ws.onopen=()=>ws.send(JSON.stringify({type:'subscribe',channel_id:current.id})); ws.onmessage=e=>{let f=JSON.parse(e.data); if(f.channel_id!==current.id)return; if(['message.new','message.reply','message.edited','message.deleted','reaction.added','reaction.removed','channel.updated'].includes(f.type)){loadWorkspace(workspace.slug); if(threadParent) renderThread()}}}
$('#auth form').onsubmit=async e=>{e.preventDefault(); let fd=new FormData(e.target); $('#auth-error').textContent=''; let username=String(fd.get('username')||''), password=String(fd.get('password')||''); if(authMode==='register'&&!/^[A-Za-z0-9_]{2,32}$/.test(username)){ $('#auth-error').textContent='Username can use letters, digits, and underscores only.'; return } if(authMode==='register'&&password.length<8){ $('#auth-error').textContent='Password must be at least 8 characters.'; return } try{let path=authMode==='register'?'/auth/register':'/auth/login'; let body={username,password}; if(authMode==='register') body.display_name=fd.get('display_name'); let data=await api(path,{method:'POST',body}); setToken(data.token); me=data.user; await loadWorkspaces(); showApp()}catch(err){$('#auth-error').textContent=err.message}};
$('[data-testid=auth-toggle]').onclick=()=>{authMode=authMode==='register'?'login':'register'; $('#auth-title').textContent=authMode==='register'?'Create your account':'Log in'; $('[data-testid=auth-submit]').textContent=authMode==='register'?'Sign up':'Log in'; $('[data-testid=auth-toggle]').textContent=authMode==='register'?'Log in instead':'Create account instead'};
$('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token'); token=''; if(ws)ws.close(); showAuth()};
$('#workspace-form').onsubmit=async e=>{e.preventDefault(); let fd=new FormData(e.target); await api('/workspaces',{method:'POST',body:{name:fd.get('name'),slug:fd.get('slug')}}); await loadWorkspaces()};
$('#join-form').onsubmit=async e=>{e.preventDefault(); $('#join-error').textContent=''; try{let code=new FormData(e.target).get('code'); await api('/invitations/'+encodeURIComponent(code)+'/accept',{method:'POST'}); await loadWorkspaces()}catch(err){$('#join-error').textContent=err.message}};
$('[data-testid=new-channel-btn]').onclick=()=>{$('#channel-modal').classList.remove('hidden'); $('#channel-create-error').textContent=''};
$('[data-testid=create-channel-cancel]').onclick=()=>$('#channel-modal').classList.add('hidden');
$('#channel-modal form').onsubmit=async e=>{e.preventDefault(); let fd=new FormData(e.target); $('#channel-create-error').textContent=''; try{let data=await api('/workspaces/'+workspace.slug+'/channels',{method:'POST',body:{name:fd.get('name'),is_private:!!fd.get('is_private')}}); $('#channel-modal').classList.add('hidden'); await loadWorkspace(workspace.slug); await selectChannel(data.channel.id)}catch(err){$('#channel-create-error').textContent=err.message}};
$('#message-form').onsubmit=async e=>{e.preventDefault(); if(!current)return; let ta=$('[data-testid=message-input]'); $('#compose-error').textContent=''; if(!ta.value.trim()){ $('#compose-error').textContent='Message cannot be blank'; return } try{await api('/channels/'+current.id+'/messages',{method:'POST',body:{body:ta.value}}); ta.value=''; await loadMessages()}catch(err){$('#compose-error').textContent=err.message}};
$('#thread-form').onsubmit=async e=>{e.preventDefault(); let ta=$('[data-testid=thread-input]'); if(!ta.value.trim())return; await api('/channels/'+current.id+'/messages',{method:'POST',body:{body:ta.value,parent_id:threadParent.id}}); ta.value=''; let id=threadParent.id; await loadMessages(); threadParent=messages.find(m=>m.id===id)||threadParent; await renderThread()};
$('[data-testid=close-thread]').onclick=()=>$('#thread-panel').classList.add('hidden');
$('[data-testid=workspace-settings-btn]').onclick=async()=>{ $('#workspace-settings').classList.remove('hidden'); $('#workspace-name-input').value=workspace?.name||''; $('#workspace-join-mode').value=workspace?.join_mode||'open'; await renderMembers(); await renderInvites() };
$('[data-testid=workspace-settings-close]').onclick=()=>$('#workspace-settings').classList.add('hidden');
$('#workspace-general-submit').onclick=async()=>{await api('/workspaces/'+workspace.slug,{method:'PATCH',body:{name:$('#workspace-name-input').value,join_mode:$('#workspace-join-mode').value}}); await loadWorkspace(workspace.slug)};
async function renderMembers(){ if(!workspace)return; let data=await api('/workspaces/'+workspace.slug+'/members'); let box=$('#members-list'); box.innerHTML=''; data.members.forEach(m=>{let r=document.createElement('div'); r.dataset.testid='member-row'; r.className='member'; r.innerHTML=`<span>${esc(m.user_id)}</span><span data-testid="role-badge" class="badge">${m.role}</span><select data-testid="role-select"><option>owner</option><option>admin</option><option>member</option><option>guest</option></select>`; $('select',r).value=m.role; box.appendChild(r)})}
$('#create-invitation-btn').onclick=()=>$('#invite-form').classList.toggle('hidden');
$('#invite-form').onsubmit=async e=>{e.preventDefault(); let fd=new FormData(e.target); await api('/workspaces/'+workspace.slug+'/invitations',{method:'POST',body:{invited_username:fd.get('invited_username')||null,max_uses:1}}); await renderInvites()};
async function renderInvites(){ if(!workspace)return; let data=await api('/workspaces/'+workspace.slug+'/invitations').catch(()=>({invitations:[]})); let box=$('#invitations-list'); box.innerHTML=''; data.invitations.forEach(i=>{let d=document.createElement('div'); d.dataset.testid='invitation-code'; d.textContent=i.code; box.appendChild(d)})}
$('[data-testid=channel-settings-btn]').onclick=async()=>{ $('#channel-settings').classList.remove('hidden'); await renderChannelMembers() };
$('[data-testid=channel-settings-close]').onclick=()=>$('#channel-settings').classList.add('hidden');
async function renderChannelMembers(){ let data=await api('/workspaces/'+workspace.slug+'/members'); let box=$('#channel-members-list'); box.innerHTML=''; data.members.forEach(m=>{let r=document.createElement('div'); r.dataset.testid='channel-member-row'; r.className='member'; r.textContent=m.user_id+' '+m.role; box.appendChild(r)})}
$('#topic-form').onsubmit=async e=>{e.preventDefault(); await api('/channels/'+current.id,{method:'PATCH',body:{topic:new FormData(e.target).get('topic')}}); await loadWorkspace(workspace.slug)};
$('#archive-channel-btn').onclick=async()=>{await api('/channels/'+current.id,{method:'PATCH',body:{is_archived:true}}); await loadWorkspace(workspace.slug)};
$('#unarchive-channel-btn').onclick=async()=>{await api('/channels/'+current.id,{method:'PATCH',body:{is_archived:false}}); await loadWorkspace(workspace.slug)};
$('#channel-add-member-form').onsubmit=e=>{e.preventDefault();};
boot();
</script>
</body>
</html>"""


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "http":
        run_http(int(sys.argv[2]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "irc":
        run_irc()
    else:
        supervisor()
