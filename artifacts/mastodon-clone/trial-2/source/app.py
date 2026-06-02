from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException as StarletteHTTPException


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
STATIC_DIR = ROOT / "static"
TEMPLATE_DIR = ROOT / "templates"
DB_PATH = Path(os.environ.get("CHIRP_DB_PATH", DATA_DIR / "chirp.sqlite3"))
INSTANCE_DOMAIN = os.environ.get("CHIRP_INSTANCE_DOMAIN", "chirp.local")
BASE_URL = os.environ.get("CHIRP_BASE_URL", "http://localhost:8000").rstrip("/")
CHAR_LIMIT = int(os.environ.get("CHIRP_STATUS_CHAR_LIMIT", "500"))
SESSION_COOKIE = "chirp_session"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

templates = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return dt.datetime.fromisoformat(value)


def iso_from_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_ts() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def secret_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 160_000).hex()
    return f"{salt}${digest}"


def password_ok(password: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    return hmac.compare_digest(password_hash(password, salt).split("$", 1)[1], digest)


def normalize_scope(scope: str | None) -> str:
    parts = []
    for item in (scope or "read write").replace(",", " ").split():
        if item and item not in parts:
            parts.append(item)
    return " ".join(parts) or "read"


def has_scope(granted: str, needed: str) -> bool:
    scopes = set(normalize_scope(granted).split())
    if needed in scopes:
        return True
    if needed.startswith("write:follows") and "follow" in scopes:
        return True
    if ":" in needed and needed.split(":", 1)[0] in scopes:
        return True
    if needed.startswith("admin:") and "admin" in scopes:
        return True
    if needed == "read:accounts" and "profile" in scopes:
        return True
    return False


def json_error(status: int, message: str, code: str = "error") -> JSONResponse:
    return JSONResponse({"error": message, "error_description": message, "code": code}, status_code=status)


async def request_data(request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
            return dict(body or {})
        except json.JSONDecodeError:
            return {}
    form = await request.form()
    data: dict[str, Any] = {}
    for key, value in form.multi_items():
        clean_key = key[:-2] if key.endswith("[]") else key
        if clean_key in data:
            if not isinstance(data[clean_key], list):
                data[clean_key] = [data[clean_key]]
            data[clean_key].append(value)
        else:
            data[clean_key] = value
    return data


def data_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or data.get(f"{key}[]")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    return [str(value)] if str(value) else []


def client_host(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def public_base(request: Request | None = None) -> str:
    if request is not None:
        return client_host(request).rstrip("/")
    return BASE_URL


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        raise
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; img-src 'self' data:; connect-src 'self'; form-action 'self'",
    )
    return response


def init_schema() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '',
                acct TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                avatar TEXT NOT NULL DEFAULT '',
                header TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0,
                bot INTEGER NOT NULL DEFAULT 0,
                discoverable INTEGER NOT NULL DEFAULT 1,
                password_hash TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                fields_json TEXT NOT NULL DEFAULT '[]',
                muted_until TEXT,
                suspended INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                client_secret TEXT NOT NULL,
                name TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                website TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT 'read write follow push',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_codes (
                code TEXT PRIMARY KEY,
                app_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                redirect_uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                code_challenge TEXT NOT NULL DEFAULT '',
                code_challenge_method TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                app_id INTEGER,
                account_id INTEGER,
                scope TEXT NOT NULL,
                token_type TEXT NOT NULL DEFAULT 'Bearer',
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                revoked INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                csrf TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS statuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                spoiler_text TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'public',
                language TEXT NOT NULL DEFAULT 'en',
                sensitive INTEGER NOT NULL DEFAULT 0,
                in_reply_to_id INTEGER,
                in_reply_to_account_id INTEGER,
                poll_id INTEGER,
                created_at TEXT NOT NULL,
                edited_at TEXT,
                deleted_at TEXT,
                uri TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                local INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS status_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                spoiler_text TEXT NOT NULL DEFAULT '',
                sensitive INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                status_id INTEGER,
                type TEXT NOT NULL DEFAULT 'image',
                url TEXT NOT NULL,
                preview_url TEXT NOT NULL,
                remote_url TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                blurhash TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                unattached INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS relationships (
                follower_id INTEGER NOT NULL,
                followed_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'following',
                show_reblogs INTEGER NOT NULL DEFAULT 1,
                notify INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY (follower_id, followed_id)
            );
            CREATE TABLE IF NOT EXISTS mutes (
                account_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                notifications INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, target_id)
            );
            CREATE TABLE IF NOT EXISTS blocks (
                account_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, target_id)
            );
            CREATE TABLE IF NOT EXISTS favourites (
                account_id INTEGER NOT NULL,
                status_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, status_id)
            );
            CREATE TABLE IF NOT EXISTS reblogs (
                account_id INTEGER NOT NULL,
                status_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, status_id)
            );
            CREATE TABLE IF NOT EXISTS bookmarks (
                account_id INTEGER NOT NULL,
                status_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, status_id)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                from_account_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                status_id INTEGER,
                created_at TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                account_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                status_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (account_id, key)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                target_account_id INTEGER NOT NULL,
                status_ids TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL DEFAULT 'other',
                comment TEXT NOT NULL DEFAULT '',
                forward INTEGER NOT NULL DEFAULT 0,
                rule_ids TEXT NOT NULL DEFAULT '[]',
                action_taken INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                replies_policy TEXT NOT NULL DEFAULT 'list',
                exclusive INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS list_accounts (
                list_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                PRIMARY KEY (list_id, account_id)
            );
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                expires_at TEXT,
                expired INTEGER NOT NULL DEFAULT 0,
                multiple INTEGER NOT NULL DEFAULT 0,
                hide_totals INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS poll_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                votes_count INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (poll_id, account_id, option_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                run_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_statuses_created ON statuses (id DESC, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_statuses_account ON statuses (account_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_notifications_account ON notifications (account_id, id DESC);
            """
        )


def audit(conn: sqlite3.Connection, account_id: int | None, action: str, target_type: str = "", target_id: Any = "", detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events (account_id, action, target_type, target_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (account_id, action, target_type, str(target_id or ""), json.dumps(detail or {}), now()),
    )


def svg_asset(name: str, label: str, color: str) -> str:
    path = STATIC_DIR / name
    if not path.exists():
        path.write_text(
            f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400"><rect width="400" height="400" rx="72" fill="{color}"/><circle cx="200" cy="156" r="72" fill="#fff" opacity=".92"/><rect x="88" y="246" width="224" height="94" rx="47" fill="#fff" opacity=".92"/><text x="200" y="226" text-anchor="middle" font-family="Arial, sans-serif" font-size="92" fill="{color}">{html.escape(label[:2].upper())}</text></svg>""",
            encoding="utf-8",
        )
    return f"/static/{name}"


def ensure_account(
    conn: sqlite3.Connection,
    username: str,
    display_name: str,
    note: str,
    password: str = "chirp",
    *,
    domain: str = "",
    is_admin: bool = False,
    color: str = "#6364ff",
    fields: list[dict[str, str]] | None = None,
) -> int:
    acct = username if not domain else f"{username}@{domain}"
    row = one(conn, "SELECT id FROM accounts WHERE acct = ?", (acct,))
    avatar = svg_asset(f"avatar-{acct.replace('@', '-').replace('.', '-')}.svg", username, color)
    header = svg_asset(f"header-{acct.replace('@', '-').replace('.', '-')}.svg", display_name[:2] or username[:2], "#563acc")
    if row:
        conn.execute(
            "UPDATE accounts SET display_name=?, note=?, avatar=?, header=?, is_admin=max(is_admin, ?) WHERE id=?",
            (display_name, note, avatar, header, 1 if is_admin else 0, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO accounts (username, domain, acct, display_name, note, avatar, header, password_hash, is_admin, created_at, fields_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (username, domain, acct, display_name, note, avatar, header, password_hash(password), 1 if is_admin else 0, now(), json.dumps(fields or [])),
    )
    return int(cur.lastrowid)


def ensure_app(conn: sqlite3.Connection, client_id: str, client_secret: str, name: str = "Chirp Seed App") -> int:
    row = one(conn, "SELECT id FROM apps WHERE client_id = ?", (client_id,))
    if row:
        conn.execute("UPDATE apps SET client_secret=?, name=?, scopes=? WHERE id=?", (client_secret, name, "read write follow push admin", row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO apps (client_id, client_secret, name, redirect_uri, website, scopes, created_at) VALUES (?,?,?,?,?,?,?)",
        (client_id, client_secret, name, "urn:ietf:wg:oauth:2.0:oob\nhttp://localhost", "", "read write follow push admin", now()),
    )
    return int(cur.lastrowid)


def ensure_token(conn: sqlite3.Connection, token: str, app_id: int | None, account_id: int | None, scopes: str, is_admin: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO tokens (token, app_id, account_id, scope, created_at, expires_at, revoked, is_admin)
        VALUES (?,?,?,?,?,?,0,?)
        ON CONFLICT(token) DO UPDATE SET app_id=excluded.app_id, account_id=excluded.account_id, scope=excluded.scope, revoked=0, is_admin=excluded.is_admin
        """,
        (token, app_id, account_id, normalize_scope(scopes), utc_ts(), None, 1 if is_admin else 0),
    )


def seed_status(conn: sqlite3.Connection, account_id: int, content: str, *, spoiler: str = "", visibility: str = "public", local: bool = True, media: bool = False) -> int:
    existing = one(conn, "SELECT id FROM statuses WHERE account_id=? AND content=? AND deleted_at IS NULL", (account_id, content))
    if existing:
        return int(existing["id"])
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=secrets.randbelow(500))).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    cur = conn.execute(
        """
        INSERT INTO statuses (account_id, content, spoiler_text, visibility, sensitive, created_at, local)
        VALUES (?,?,?,?,?,?,?)
        """,
        (account_id, content, spoiler, visibility, 1 if spoiler else 0, created, 1 if local else 0),
    )
    status_id = int(cur.lastrowid)
    conn.execute("UPDATE statuses SET uri=?, url=? WHERE id=?", (f"{BASE_URL}/users/{account_id}/statuses/{status_id}", f"{BASE_URL}/statuses/{status_id}", status_id))
    if media:
        media_name = f"seed-media-{status_id}.svg"
        (MEDIA_DIR / media_name).write_text(
            f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540"><rect width="960" height="540" fill="#191b22"/><path d="M0 420 C180 330 300 480 480 390 S780 330 960 250 V540 H0Z" fill="#6364ff"/><circle cx="740" cy="140" r="72" fill="#ffb84d"/><text x="64" y="112" font-family="Arial, sans-serif" font-size="48" fill="#fff">Chirp media</text></svg>""",
            encoding="utf-8",
        )
        conn.execute(
            "INSERT INTO media (account_id, status_id, url, preview_url, description, created_at, unattached) VALUES (?,?,?,?,?,?,0)",
            (account_id, status_id, f"/media/{media_name}", f"/media/{media_name}", "Seeded Chirp media attachment", now()),
        )
    return status_id


def seed_data() -> None:
    init_schema()
    with db() as conn:
        admin_token = os.environ.get("CHIRP_BOOTSTRAP_ADMIN_TOKEN", "chirp-admin-token")
        seed_token = os.environ.get("CHIRP_SEED_USER_TOKEN", "chirp-user-token")
        seed_user = os.environ.get("CHIRP_SEED_USER_ACCOUNT", "alice").strip("@") or "alice"
        client_id = os.environ.get("CHIRP_SEED_APP_CLIENT_ID", "chirp-seed-client")
        client_secret = os.environ.get("CHIRP_SEED_APP_CLIENT_SECRET", "chirp-seed-secret")
        app_id = ensure_app(conn, client_id, client_secret)
        admin_id = ensure_account(conn, "admin", "Chirp Admin", "Keeps this small self-hosted instance tidy.\nhttps://chirp.local/about", "admin", is_admin=True, color="#563acc")
        seed_id = ensure_account(conn, seed_user, "Alice Example", "Building calm social software.\nhttps://chirp.local", "chirp", color="#6364ff")
        ensure_token(conn, admin_token, app_id, admin_id, "read write follow push admin admin:read admin:write", True)
        ensure_token(conn, seed_token, app_id, seed_id, "read write follow push", False)
        people = [
            ("ben", "Ben Rivera", "Gardener, repair notes, local timelines.", "#8c8dff"),
            ("cora", "Cora Finch", "Writes release notes and tiny essays.", "#2fb344"),
            ("devon", "Devon Hart", "Accessibility testing for community tools.", "#f59f00"),
            ("erin", "Erin Cho", "Photos from walks and transit stops.", "#e03131"),
            ("faye", "Faye Okafor", "Maintains lists, polls, and community docs.", "#0c8599"),
            ("gale", "Gale Morgan", "Coffee, code, and moderation queues.", "#7048e8"),
            ("hugo", "Hugo Stern", "Remote account seeded for federated timelines.", "#5c7cfa"),
            ("iris", "Iris Vale", "Another remote voice for compatibility tests.", "#d6336c"),
        ]
        ids = {seed_user: seed_id, "admin": admin_id}
        for username, display, note, color in people:
            remote = username in {"hugo", "iris"}
            ids[username] = ensure_account(conn, username, display, note, "chirp", domain=("mastodon.social" if remote else ""), color=color)
        conn.execute(
            "INSERT OR IGNORE INTO relationships (follower_id, followed_id, created_at) VALUES (?,?,?)",
            (seed_id, ids["ben"], now()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO relationships (follower_id, followed_id, created_at) VALUES (?,?,?)",
            (seed_id, ids["cora"], now()),
        )
        seed_items = [
            (seed_id, "Morning check-in from Chirp. #introductions #selfhosted\nThe timeline keeps line breaks intact.", "", "public", True, False),
            (ids["ben"], "Planted basil next to the server rack, which is not official ops guidance. #gardening #selfhosted", "", "public", True, True),
            (ids["cora"], "Drafted a migration note: OAuth apps, lists, and notifications all deserve boring durability. #releaseNotes", "", "public", True, False),
            (ids["devon"], "Focus rings are part of the interface, not a debug mode. #accessibility", "", "public", True, False),
            (ids["erin"], "Two trains, one notebook, and a surprisingly good public timeline. #transit", "", "public", True, True),
            (ids["faye"], "Polls should expire predictably even when the demo clock is compressed. #polls #selfhosted", "", "public", True, False),
            (ids["gale"], "Moderation audit trails are only useful when they are easy to read later. #moderation", "", "unlisted", True, False),
            (admin_id, "Content warning example with operational detail behind the summary.", "ops note", "public", True, False),
            (ids["ben"], "A private note for followers about tomorrow's maintenance window.", "maintenance", "private", True, False),
            (ids["cora"], "Status actions should come back after reload: favourite, boost, bookmark, reply. #clients", "", "public", True, False),
            (ids["devon"], "UTF-8 punctuation stays as written: it’s fine, really - and em-dashes survive too.", "", "public", True, False),
            (ids["erin"], "Bare links auto-link in rendered content: https://docs.joinmastodon.org #api", "", "public", True, False),
            (ids["faye"], "Mentioning @alice should produce a link and a notification in the small instance flow. #mentions", "", "public", True, False),
            (ids["gale"], "Local timeline check. Public local statuses only, please. #local", "", "public", True, False),
            (ids["hugo"], "Remote seeded post for the federated tab. #federated #introductions", "", "public", False, False),
            (ids["iris"], "Another remote public status so clients can see a federated shape. #federated", "", "public", False, False),
            (seed_id, "Trying a second toot with #releaseNotes and a bookmark-worthy link https://chirp.local/statuses", "", "public", True, False),
            (ids["ben"], "Lists make home timelines easier to scan. #lists", "", "public", True, False),
            (ids["cora"], "Search should find people, posts, and hashtags without needing a remote service. #search", "", "public", True, False),
            (ids["devon"], "Reduced motion preference is honored in the web UI. #accessibility", "", "public", True, False),
            (ids["erin"], "Media descriptions matter as much as the pixels. #photos", "", "public", True, True),
            (ids["faye"], "Admin queues are quieter when jobs explain what they are waiting on. #admin", "", "public", True, False),
        ]
        for item in seed_items:
            seed_status(conn, item[0], item[1], spoiler=item[2], visibility=item[3], local=item[4], media=item[5])
        if not one(conn, "SELECT id FROM jobs"):
            for kind in ["media_cleanup", "poll_expiry", "trend_decay"]:
                conn.execute("INSERT INTO jobs (kind, payload, status, run_at, created_at, updated_at) VALUES (?,?,?,?,?,?)", (kind, "{}", "queued", now(), now(), now()))
        audit(conn, admin_id, "bootstrap", "instance", INSTANCE_DOMAIN, {"seed_user": seed_user})


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return None


async def api_auth(request: Request, scopes: list[str] | None = None, *, optional: bool = False, admin: bool = False) -> sqlite3.Row | None:
    token = bearer_token(request)
    if not token:
        if optional:
            return None
        raise StarletteHTTPException(status_code=401, detail="The access token is invalid")
    with db() as conn:
        row = one(
            conn,
            """
            SELECT tokens.*, accounts.username, accounts.acct, accounts.is_admin AS account_admin
            FROM tokens LEFT JOIN accounts ON accounts.id=tokens.account_id
            WHERE token=? AND revoked=0 AND (expires_at IS NULL OR expires_at > ?)
            """,
            (token, utc_ts()),
        )
    if not row:
        if optional:
            return None
        raise StarletteHTTPException(status_code=401, detail="The access token is invalid")
    if admin and not (row["is_admin"] or row["account_admin"]):
        raise StarletteHTTPException(status_code=403, detail="This method requires an admin token")
    for scope in scopes or []:
        if not has_scope(row["scope"], scope):
            raise StarletteHTTPException(status_code=403, detail=f"This method requires {scope}")
    return row


def get_session(request: Request) -> sqlite3.Row | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    with db() as conn:
        return one(
            conn,
            "SELECT sessions.*, accounts.* FROM sessions JOIN accounts ON accounts.id=sessions.account_id WHERE sid=? AND expires_at > ?",
            (sid, utc_ts()),
        )


def login_response(account_id: int, redirect_to: str = "/home") -> RedirectResponse:
    sid = secret_token("sess_")
    csrf = secret_token("csrf_")
    with db() as conn:
        conn.execute("INSERT INTO sessions (sid, account_id, csrf, expires_at) VALUES (?,?,?,?)", (sid, account_id, csrf, utc_ts() + 60 * 60 * 24 * 30))
    resp = RedirectResponse(redirect_to, status_code=303)
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", secure=False, max_age=60 * 60 * 24 * 30, path="/")
    return resp


def logout_response() -> RedirectResponse:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def require_csrf(request: Request, data: dict[str, Any]) -> sqlite3.Row:
    session = get_session(request)
    if not session:
        raise StarletteHTTPException(status_code=401, detail="Login required")
    supplied = request.headers.get("x-csrf-token") or str(data.get("csrf_token") or "")
    if not hmac.compare_digest(str(session["csrf"]), supplied):
        raise StarletteHTTPException(status_code=403, detail="CSRF validation failed")
    return session


def visibility_icon(visibility: str) -> str:
    return {"public": "&#127760;", "unlisted": "&#128065;", "private": "&#128274;", "direct": "&#9993;"}.get(visibility, "&#127760;")


def autolink_text(text: str) -> str:
    escaped = html.escape(text or "")

    def url_repl(match: re.Match[str]) -> str:
        url = match.group(0)
        return f'<a href="{url}" rel="nofollow noopener noreferrer">{url}</a>'

    escaped = re.sub(r"https?://[^\s<]+", url_repl, escaped)
    escaped = re.sub(r"(?<![\w/&])#([A-Za-z0-9_]+)", r'<a href="/tags/\1" class="hashtag">#\1</a>', escaped)
    escaped = re.sub(r"(?<![\w/&])@([A-Za-z0-9_]+)(?:@([A-Za-z0-9_.-]+))?", lambda m: f'<a href="/@{m.group(1)}" class="mention">@{m.group(1)}{("@"+m.group(2)) if m.group(2) else ""}</a>', escaped)
    return "<p>" + escaped.replace("\n", "<br>") + "</p>"


templates.filters["autolink"] = autolink_text
templates.filters["visibility_icon"] = visibility_icon


def account_counts(conn: sqlite3.Connection, account_id: int) -> dict[str, int]:
    return {
        "statuses": int(one(conn, "SELECT count(*) AS c FROM statuses WHERE account_id=? AND deleted_at IS NULL", (account_id,))["c"]),
        "followers": int(one(conn, "SELECT count(*) AS c FROM relationships WHERE followed_id=? AND state='following'", (account_id,))["c"]),
        "following": int(one(conn, "SELECT count(*) AS c FROM relationships WHERE follower_id=? AND state='following'", (account_id,))["c"]),
    }


def account_json(conn: sqlite3.Connection, row: sqlite3.Row, request: Request | None = None, *, source: bool = False) -> dict[str, Any]:
    counts = account_counts(conn, int(row["id"]))
    base = public_base(request)
    acct = row["acct"] if row["domain"] else row["username"]
    data = {
        "id": str(row["id"]),
        "username": row["username"],
        "acct": acct,
        "display_name": row["display_name"],
        "locked": bool(row["locked"]),
        "bot": bool(row["bot"]),
        "discoverable": bool(row["discoverable"]),
        "group": False,
        "created_at": row["created_at"],
        "note": autolink_text(row["note"]),
        "url": f"{base}/@{row['username']}" if not row["domain"] else f"https://{row['domain']}/@{row['username']}",
        "avatar": f"{base}{row['avatar']}" if row["avatar"].startswith("/") else row["avatar"],
        "avatar_static": f"{base}{row['avatar']}" if row["avatar"].startswith("/") else row["avatar"],
        "header": f"{base}{row['header']}" if row["header"].startswith("/") else row["header"],
        "header_static": f"{base}{row['header']}" if row["header"].startswith("/") else row["header"],
        "followers_count": counts["followers"],
        "following_count": counts["following"],
        "statuses_count": counts["statuses"],
        "last_status_at": None,
        "noindex": False,
        "emojis": [],
        "roles": [],
        "fields": json.loads(row["fields_json"] or "[]"),
    }
    if source:
        data["source"] = {
            "note": row["note"],
            "fields": json.loads(row["fields_json"] or "[]"),
            "privacy": "public",
            "sensitive": False,
            "language": "en",
            "follow_requests_count": 0,
        }
    return data


def status_counts(conn: sqlite3.Connection, status_id: int) -> dict[str, int]:
    return {
        "replies": int(one(conn, "SELECT count(*) AS c FROM statuses WHERE in_reply_to_id=? AND deleted_at IS NULL", (status_id,))["c"]),
        "reblogs": int(one(conn, "SELECT count(*) AS c FROM reblogs WHERE status_id=?", (status_id,))["c"]),
        "favourites": int(one(conn, "SELECT count(*) AS c FROM favourites WHERE status_id=?", (status_id,))["c"]),
    }


def visible_status_filter(viewer_id: int | None) -> tuple[str, list[Any]]:
    if viewer_id is None:
        return "s.visibility IN ('public','unlisted')", []
    return (
        """
        (
            s.visibility IN ('public','unlisted')
            OR s.account_id=?
            OR (s.visibility='private' AND EXISTS (SELECT 1 FROM relationships r WHERE r.follower_id=? AND r.followed_id=s.account_id AND r.state='following'))
            OR (s.visibility='direct' AND (s.account_id=? OR s.content LIKE ?))
        )
        """,
        [viewer_id, viewer_id, viewer_id, f"%@{account_username(viewer_id)}%"],
    )


def account_username(account_id: int) -> str:
    with db() as conn:
        row = one(conn, "SELECT username FROM accounts WHERE id=?", (account_id,))
        return str(row["username"]) if row else ""


def is_hidden_by_viewer(conn: sqlite3.Connection, viewer_id: int | None, author_id: int) -> bool:
    if viewer_id is None:
        return False
    if one(conn, "SELECT 1 FROM mutes WHERE account_id=? AND target_id=?", (viewer_id, author_id)):
        return True
    if one(conn, "SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", (viewer_id, author_id)):
        return True
    if one(conn, "SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", (author_id, viewer_id)):
        return True
    return False


def media_json(conn: sqlite3.Connection, status_id: int, request: Request | None = None) -> list[dict[str, Any]]:
    base = public_base(request)
    rows = all_rows(conn, "SELECT * FROM media WHERE status_id=? ORDER BY id", (status_id,))
    out = []
    for row in rows:
        url = row["url"]
        preview = row["preview_url"] or url
        out.append(
            {
                "id": str(row["id"]),
                "type": row["type"],
                "url": f"{base}{url}" if url.startswith("/") else url,
                "preview_url": f"{base}{preview}" if preview.startswith("/") else preview,
                "remote_url": row["remote_url"] or None,
                "text_url": None,
                "meta": json.loads(row["meta_json"] or "{}"),
                "description": row["description"],
                "blurhash": row["blurhash"],
            }
        )
    return out


def poll_json(conn: sqlite3.Connection, poll_id: int | None, viewer_id: int | None = None) -> dict[str, Any] | None:
    if not poll_id:
        return None
    poll = one(conn, "SELECT * FROM polls WHERE id=?", (poll_id,))
    if not poll:
        return None
    expired = bool(poll["expired"])
    if poll["expires_at"] and parse_time(poll["expires_at"]) <= dt.datetime.now(dt.timezone.utc):
        expired = True
    options = all_rows(conn, "SELECT * FROM poll_options WHERE poll_id=? ORDER BY position, id", (poll_id,))
    voted = False
    own_votes: list[int] = []
    if viewer_id:
        votes = all_rows(conn, "SELECT option_id FROM poll_votes WHERE poll_id=? AND account_id=?", (poll_id, viewer_id))
        own_votes = [int(v["option_id"]) for v in votes]
        voted = bool(votes)
    return {
        "id": str(poll_id),
        "expires_at": poll["expires_at"],
        "expired": expired,
        "multiple": bool(poll["multiple"]),
        "votes_count": int(one(conn, "SELECT count(*) AS c FROM poll_votes WHERE poll_id=?", (poll_id,))["c"]),
        "voters_count": int(one(conn, "SELECT count(DISTINCT account_id) AS c FROM poll_votes WHERE poll_id=?", (poll_id,))["c"]),
        "voted": voted,
        "own_votes": [str(v) for v in own_votes],
        "options": [{"title": r["title"], "votes_count": r["votes_count"]} for r in options],
        "emojis": [],
    }


def status_json(conn: sqlite3.Connection, row: sqlite3.Row, request: Request | None = None, viewer_id: int | None = None) -> dict[str, Any]:
    author = one(conn, "SELECT * FROM accounts WHERE id=?", (row["account_id"],))
    counts = status_counts(conn, int(row["id"]))
    tags = [{"name": m.group(1).lower(), "url": f"{public_base(request)}/tags/{m.group(1).lower()}"} for m in re.finditer(r"(?<![\w/])#([A-Za-z0-9_]+)", row["content"])]
    mentions = []
    for m in re.finditer(r"(?<![\w/])@([A-Za-z0-9_]+)(?:@([A-Za-z0-9_.-]+))?", row["content"]):
        acct_row = one(conn, "SELECT * FROM accounts WHERE username=? COLLATE NOCASE", (m.group(1),))
        if acct_row:
            mentions.append({"id": str(acct_row["id"]), "username": acct_row["username"], "acct": acct_row["acct"], "url": f"{public_base(request)}/@{acct_row['username']}"})
    favourited = bool(viewer_id and one(conn, "SELECT 1 FROM favourites WHERE account_id=? AND status_id=?", (viewer_id, row["id"])))
    reblogged = bool(viewer_id and one(conn, "SELECT 1 FROM reblogs WHERE account_id=? AND status_id=?", (viewer_id, row["id"])))
    bookmarked = bool(viewer_id and one(conn, "SELECT 1 FROM bookmarks WHERE account_id=? AND status_id=?", (viewer_id, row["id"])))
    base = public_base(request)
    return {
        "id": str(row["id"]),
        "created_at": row["created_at"],
        "in_reply_to_id": str(row["in_reply_to_id"]) if row["in_reply_to_id"] else None,
        "in_reply_to_account_id": str(row["in_reply_to_account_id"]) if row["in_reply_to_account_id"] else None,
        "sensitive": bool(row["sensitive"]),
        "spoiler_text": row["spoiler_text"],
        "visibility": row["visibility"],
        "language": row["language"],
        "uri": row["uri"] or f"{base}/statuses/{row['id']}",
        "url": row["url"] or f"{base}/statuses/{row['id']}",
        "replies_count": counts["replies"],
        "reblogs_count": counts["reblogs"],
        "favourites_count": counts["favourites"],
        "edited_at": row["edited_at"],
        "content": autolink_text(row["content"]),
        "reblog": None,
        "application": {"name": "Chirp", "website": base},
        "account": account_json(conn, author, request) if author else {},
        "media_attachments": media_json(conn, int(row["id"]), request),
        "mentions": mentions,
        "tags": tags,
        "emojis": [],
        "card": None,
        "poll": poll_json(conn, row["poll_id"], viewer_id),
        "favourited": favourited,
        "reblogged": reblogged,
        "muted": False,
        "bookmarked": bookmarked,
        "pinned": False,
        "filtered": [],
    }


def status_row(conn: sqlite3.Connection, status_id: int, viewer_id: int | None = None) -> sqlite3.Row | None:
    row = one(conn, "SELECT * FROM statuses WHERE id=? AND deleted_at IS NULL", (status_id,))
    if not row:
        return None
    sql, params = visible_status_filter(viewer_id)
    visible = one(conn, f"SELECT 1 FROM statuses s WHERE s.id=? AND {sql}", (status_id, *params))
    if not visible or is_hidden_by_viewer(conn, viewer_id, int(row["account_id"])):
        return None
    return row


def paged_statuses(
    conn: sqlite3.Connection,
    request: Request,
    base_sql: str,
    params: list[Any],
    viewer_id: int | None,
    *,
    default_limit: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    qp = request.query_params
    limit = max(1, min(int(qp.get("limit", default_limit)), 80))
    where = [base_sql, "s.deleted_at IS NULL"]
    qparams = list(params)
    vis_sql, vis_params = visible_status_filter(viewer_id)
    where.append(vis_sql)
    qparams.extend(vis_params)
    if qp.get("max_id"):
        where.append("s.id < ?")
        qparams.append(int(qp["max_id"]))
    if qp.get("since_id"):
        where.append("s.id > ?")
        qparams.append(int(qp["since_id"]))
    if qp.get("min_id"):
        where.append("s.id > ?")
        qparams.append(int(qp["min_id"]))
    qparams.append(limit)
    rows = all_rows(conn, f"SELECT s.* FROM statuses s WHERE {' AND '.join(where)} ORDER BY s.id DESC LIMIT ?", tuple(qparams))
    visible = [r for r in rows if not is_hidden_by_viewer(conn, viewer_id, int(r["account_id"]))]
    items = [status_json(conn, row, request, viewer_id) for row in visible]
    headers: dict[str, str] = {}
    if items:
        next_q = dict(request.query_params)
        next_q["max_id"] = items[-1]["id"]
        prev_q = dict(request.query_params)
        prev_q["since_id"] = items[0]["id"]
        base_url = str(request.url).split("?", 1)[0]
        headers["Link"] = f'<{base_url}?{urlencode(next_q)}>; rel="next", <{base_url}?{urlencode(prev_q)}>; rel="prev"'
        etag_src = ",".join(i["id"] + (i.get("edited_at") or "") for i in items)
        headers["ETag"] = '"' + hashlib.sha256(etag_src.encode()).hexdigest()[:24] + '"'
    if request.headers.get("if-none-match") and headers.get("ETag") == request.headers.get("if-none-match"):
        headers["__not_modified__"] = "1"
    return items, headers


def notification_json(conn: sqlite3.Connection, row: sqlite3.Row, request: Request) -> dict[str, Any]:
    account = one(conn, "SELECT * FROM accounts WHERE id=?", (row["from_account_id"],))
    status = status_row(conn, int(row["status_id"]), int(row["account_id"])) if row["status_id"] else None
    return {
        "id": str(row["id"]),
        "type": row["type"],
        "created_at": row["created_at"],
        "account": account_json(conn, account, request) if account else None,
        "status": status_json(conn, status, request, int(row["account_id"])) if status else None,
    }


def create_notification(conn: sqlite3.Connection, account_id: int, from_account_id: int, type_: str, status_id: int | None = None) -> None:
    if account_id == from_account_id:
        return
    conn.execute(
        "INSERT INTO notifications (account_id, from_account_id, type, status_id, created_at) VALUES (?,?,?,?,?)",
        (account_id, from_account_id, type_, status_id, now()),
    )


def create_status(conn: sqlite3.Connection, account_id: int, data: dict[str, Any], request: Request | None = None) -> int:
    content = str(data.get("status") or data.get("content") or "")[:5000]
    if not content.strip() and not data_list(data, "media_ids"):
        raise StarletteHTTPException(status_code=422, detail="Validation failed: Status can't be blank")
    visibility = str(data.get("visibility") or "public")
    if visibility not in {"public", "unlisted", "private", "direct"}:
        visibility = "public"
    spoiler = str(data.get("spoiler_text") or data.get("spoiler") or "")
    sensitive = 1 if str(data.get("sensitive") or "").lower() in {"1", "true", "on"} or spoiler else 0
    in_reply_to_id = int(data["in_reply_to_id"]) if data.get("in_reply_to_id") else None
    in_reply_to_account_id = None
    if in_reply_to_id:
        parent = one(conn, "SELECT account_id FROM statuses WHERE id=? AND deleted_at IS NULL", (in_reply_to_id,))
        if parent:
            in_reply_to_account_id = int(parent["account_id"])
    poll_id = None
    options = data_list(data, "poll[options]") or data_list(data, "poll_options")
    if options:
        expires_in = int(data.get("poll[expires_in]") or data.get("poll_expires_in") or 86400)
        expires_rate = float(os.environ.get("CHIRP_POLL_EXPIRY_PER_MINUTE", "1") or "1")
        expires_in = max(1, int(expires_in / max(expires_rate, 0.01)))
        expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        cur = conn.execute(
            "INSERT INTO polls (account_id, expires_at, multiple, hide_totals, created_at) VALUES (?,?,?,?,?)",
            (account_id, expires_at, 1 if str(data.get("poll[multiple]") or "").lower() in {"1", "true", "on"} else 0, 0, now()),
        )
        poll_id = int(cur.lastrowid)
        for idx, option in enumerate(options):
            conn.execute("INSERT INTO poll_options (poll_id, title, position) VALUES (?,?,?)", (poll_id, option[:100], idx))
    cur = conn.execute(
        """
        INSERT INTO statuses (account_id, content, spoiler_text, visibility, language, sensitive, in_reply_to_id, in_reply_to_account_id, poll_id, created_at, local)
        VALUES (?,?,?,?,?,?,?,?,?,?,1)
        """,
        (account_id, content, spoiler, visibility, str(data.get("language") or "en"), sensitive, in_reply_to_id, in_reply_to_account_id, poll_id, now()),
    )
    status_id = int(cur.lastrowid)
    base = public_base(request)
    conn.execute("UPDATE statuses SET uri=?, url=? WHERE id=?", (f"{base}/users/{account_id}/statuses/{status_id}", f"{base}/statuses/{status_id}", status_id))
    for media_id in data_list(data, "media_ids"):
        conn.execute("UPDATE media SET status_id=?, unattached=0 WHERE id=? AND account_id=?", (status_id, media_id, account_id))
    if in_reply_to_account_id:
        create_notification(conn, in_reply_to_account_id, account_id, "mention", status_id)
    for match in re.finditer(r"(?<![\w/])@([A-Za-z0-9_]+)", content):
        mentioned = one(conn, "SELECT id FROM accounts WHERE username=? COLLATE NOCASE AND domain=''", (match.group(1),))
        if mentioned:
            create_notification(conn, int(mentioned["id"]), account_id, "mention", status_id)
    audit(conn, account_id, "status.create", "status", status_id, {"visibility": visibility})
    return status_id


def trend_tags(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = all_rows(conn, "SELECT content FROM statuses WHERE deleted_at IS NULL AND visibility IN ('public','unlisted') ORDER BY id DESC LIMIT 200")
    counts: dict[str, int] = {}
    for row in rows:
        for match in re.finditer(r"(?<![\w/])#([A-Za-z0-9_]+)", row["content"]):
            tag = match.group(1).lower()
            counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"name": name, "url": f"{BASE_URL}/tags/{name}", "history": [{"day": str(int(time.time() // 86400)), "uses": str(count), "accounts": str(count)}]} for name, count in ranked]


def render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    session = get_session(request)
    with db() as conn:
        unread = 0
        if session:
            unread = int(one(conn, "SELECT count(*) AS c FROM notifications WHERE account_id=? AND read=0", (session["account_id"],))["c"])
        trends = trend_tags(conn, 6)
    base = {
        "request": request,
        "current_user": session,
        "csrf_token": session["csrf"] if session else "",
        "unread_count": unread,
        "trends": trends,
        "instance_domain": INSTANCE_DOMAIN,
        "char_limit": CHAR_LIMIT,
    }
    base.update(context)
    return HTMLResponse(templates.get_template(template).render(**base), status_code=status_code)


def render_status_partial(request: Request, status_id: int, viewer_id: int | None) -> HTMLResponse:
    session = get_session(request)
    with db() as conn:
        row = status_row(conn, status_id, viewer_id)
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Status not found")
        status = status_json(conn, row, request, viewer_id)
    return HTMLResponse(templates.get_template("_status.html").render(request=request, status=status, current_user=session, csrf_token=session["csrf"] if session else "", instance_domain=INSTANCE_DOMAIN, char_limit=CHAR_LIMIT))


@app.on_event("startup")
async def startup() -> None:
    seed_data()
    asyncio.create_task(maintenance_loop())


async def maintenance_loop() -> None:
    while True:
        try:
            with db() as conn:
                conn.execute("UPDATE polls SET expired=1 WHERE expired=0 AND expires_at IS NOT NULL AND expires_at <= ?", (now(),))
                hours = float(os.environ.get("CHIRP_MEDIA_CLEANUP_PER_HOUR", "1") or "1")
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(0.05, 1 / max(hours, 0.01)))
                unattached = all_rows(conn, "SELECT * FROM media WHERE unattached=1 AND created_at < ?", (cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z"),))
                for row in unattached:
                    if row["url"].startswith("/media/"):
                        try:
                            (MEDIA_DIR / Path(row["url"]).name).unlink(missing_ok=True)
                        except OSError:
                            pass
                    conn.execute("DELETE FROM media WHERE id=?", (row["id"],))
                conn.execute("UPDATE jobs SET status='queued', updated_at=? WHERE kind IN ('media_cleanup','poll_expiry','trend_decay')", (now(),))
        except Exception:
            pass
        await asyncio.sleep(15)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/api/", "/oauth/", "/.well-known/", "/_admin")):
        return json_error(exc.status_code, str(exc.detail))
    if exc.status_code == 404:
        return render(request, "error.html", {"title": "Not found", "message": "That Chirp page does not exist."}, 404)
    if exc.status_code in {401, 403}:
        return render(request, "error.html", {"title": "Sign in required", "message": "Please sign in to continue."}, exc.status_code)
    return render(request, "error.html", {"title": "Something went wrong", "message": str(exc.detail)}, exc.status_code)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    if request.url.path.startswith(("/api/", "/oauth/", "/.well-known/", "/_admin")):
        return json_error(500, "Internal server error")
    return render(request, "error.html", {"title": "Server error", "message": "Chirp hit an internal error."}, 500)


@app.get("/_health")
async def health():
    return {"status": "ok"}


@app.get("/")
@app.get("/public")
async def public_page(request: Request):
    viewer = get_session(request)
    viewer_id = int(viewer["account_id"]) if viewer else None
    with db() as conn:
        rows, _ = paged_statuses(conn, request, "s.visibility IN ('public','unlisted')", [], viewer_id, default_limit=30)
    return render(request, "timeline.html", {"title": "Chirp Public", "statuses": rows, "active_tab": "public", "visitor": not bool(viewer)})


@app.get("/home")
async def home_page(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        rows, _ = paged_statuses(conn, request, "(s.account_id=? OR EXISTS (SELECT 1 FROM relationships r WHERE r.follower_id=? AND r.followed_id=s.account_id AND r.state='following'))", [session["account_id"], session["account_id"]], int(session["account_id"]))
    return render(request, "timeline.html", {"title": "Home", "statuses": rows, "active_tab": "home", "visitor": False})


@app.get("/fragments/timeline")
async def timeline_fragment(request: Request):
    session = get_session(request)
    viewer_id = int(session["account_id"]) if session else None
    kind = request.query_params.get("type", "local")
    with db() as conn:
        if kind == "home" and viewer_id:
            rows, _ = paged_statuses(conn, request, "(s.account_id=? OR EXISTS (SELECT 1 FROM relationships r WHERE r.follower_id=? AND r.followed_id=s.account_id AND r.state='following'))", [viewer_id, viewer_id], viewer_id)
        elif kind == "federated":
            rows, _ = paged_statuses(conn, request, "s.visibility IN ('public','unlisted')", [], viewer_id)
        else:
            rows, _ = paged_statuses(conn, request, "s.local=1 AND s.visibility IN ('public','unlisted')", [], viewer_id)
    html_body = templates.get_template("_timeline.html").render(request=request, statuses=rows, current_user=session, csrf_token=session["csrf"] if session else "", instance_domain=INSTANCE_DOMAIN)
    return HTMLResponse(html_body)


@app.get("/login")
async def login_form(request: Request):
    return render(request, "auth.html", {"mode": "login", "title": "Sign in"})


@app.post("/login")
async def login_post(request: Request):
    data = await request_data(request)
    username = str(data.get("username") or "").strip().lstrip("@")
    password = str(data.get("password") or "")
    with db() as conn:
        acct = one(conn, "SELECT * FROM accounts WHERE (username=? OR acct=?) AND domain=''", (username, username))
        if not acct or not password_ok(password, acct["password_hash"]):
            return render(request, "auth.html", {"mode": "login", "title": "Sign in", "error": "Invalid username or password."}, 400)
        return login_response(int(acct["id"]), "/home")


@app.get("/signup")
async def signup_form(request: Request):
    return render(request, "auth.html", {"mode": "signup", "title": "Create account"})


@app.post("/signup")
async def signup_post(request: Request):
    data = await request_data(request)
    username = re.sub(r"[^A-Za-z0-9_]", "", str(data.get("username") or "").strip())
    password = str(data.get("password") or "")
    display = str(data.get("display_name") or username)
    if not username or len(password) < 4:
        return render(request, "auth.html", {"mode": "signup", "title": "Create account", "error": "Choose a username and a password with at least four characters."}, 400)
    with db() as conn:
        if one(conn, "SELECT 1 FROM accounts WHERE username=? AND domain=''", (username,)):
            return render(request, "auth.html", {"mode": "signup", "title": "Create account", "error": "That username is already taken."}, 400)
        account_id = ensure_account(conn, username, display, "New on Chirp.", password, color="#6364ff")
        audit(conn, account_id, "account.signup", "account", account_id)
    return login_response(account_id, "/home")


@app.post("/logout")
async def logout(request: Request):
    session = get_session(request)
    data = await request_data(request)
    if session and hmac.compare_digest(session["csrf"], str(data.get("csrf_token") or "")):
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE sid=?", (session["sid"],))
    return logout_response()


@app.get("/@{username}")
async def profile_page(request: Request, username: str):
    session = get_session(request)
    with db() as conn:
        account = one(conn, "SELECT * FROM accounts WHERE username=? COLLATE NOCASE AND domain=''", (username,))
        if not account:
            raise StarletteHTTPException(status_code=404, detail="Account not found")
        statuses, _ = paged_statuses(conn, request, "s.account_id=?", [account["id"]], int(session["account_id"]) if session else None)
        rel = None
        if session:
            rel = relationship_state(conn, int(session["account_id"]), [int(account["id"])])[0]
        return render(request, "profile.html", {"profile": account_json(conn, account, request, source=bool(session and session["account_id"] == account["id"])), "profile_row": account, "statuses": statuses, "relationship": rel})


@app.get("/users/{acct:path}")
async def legacy_user(request: Request, acct: str):
    if acct.endswith(".json"):
        username = acct[:-5].split("@", 1)[0]
        with db() as conn:
            row = one(conn, "SELECT * FROM accounts WHERE username=? COLLATE NOCASE AND domain=''", (username,))
            if not row:
                raise StarletteHTTPException(status_code=404, detail="Record not found")
            base = public_base(request)
            return JSONResponse(
                {
                    "@context": ["https://www.w3.org/ns/activitystreams"],
                    "id": f"{base}/users/{row['username']}",
                    "type": "Person",
                    "preferredUsername": row["username"],
                    "name": row["display_name"],
                    "summary": autolink_text(row["note"]),
                    "url": f"{base}/@{row['username']}",
                    "icon": {"type": "Image", "url": f"{base}{row['avatar']}"},
                    "image": {"type": "Image", "url": f"{base}{row['header']}"},
                }
            )
    username = acct.split("@", 1)[0]
    return RedirectResponse(f"/@{username}", status_code=302)


@app.get("/tags/{name}")
async def tag_page(request: Request, name: str):
    session = get_session(request)
    with db() as conn:
        rows, _ = paged_statuses(conn, request, "s.content LIKE ?", [f"%#{name}%"], int(session["account_id"]) if session else None)
    return render(request, "timeline.html", {"title": f"#{name}", "statuses": rows, "active_tab": "tag", "tag": name, "visitor": not bool(session)})


@app.get("/statuses/{status_id}")
async def status_detail(request: Request, status_id: int):
    session = get_session(request)
    viewer_id = int(session["account_id"]) if session else None
    with db() as conn:
        target = status_row(conn, status_id, viewer_id)
        if not target:
            raise StarletteHTTPException(status_code=404, detail="Status not found")
        ancestors = []
        cur = target
        while cur["in_reply_to_id"]:
            parent = status_row(conn, int(cur["in_reply_to_id"]), viewer_id)
            if not parent:
                break
            ancestors.insert(0, status_json(conn, parent, request, viewer_id))
            cur = parent
        descendants = [status_json(conn, r, request, viewer_id) for r in all_rows(conn, "SELECT * FROM statuses WHERE in_reply_to_id=? AND deleted_at IS NULL ORDER BY id", (status_id,))]
        return render(request, "status_detail.html", {"target": status_json(conn, target, request, viewer_id), "ancestors": ancestors, "descendants": descendants})


@app.get("/notifications")
async def notifications_page(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM notifications WHERE account_id=? ORDER BY id DESC LIMIT 80", (session["account_id"],))
        notes = [notification_json(conn, row, request) for row in rows]
        conn.execute("UPDATE notifications SET read=1 WHERE account_id=?", (session["account_id"],))
    return render(request, "notifications.html", {"notifications": notes})


@app.get("/events")
async def sse_events(request: Request):
    session = get_session(request)
    if not session:
        raise StarletteHTTPException(status_code=401, detail="Login required")
    account_id = int(session["account_id"])

    async def gen():
        last_id = 0
        while True:
            if await request.is_disconnected():
                break
            with db() as conn:
                unread = int(one(conn, "SELECT count(*) AS c FROM notifications WHERE account_id=? AND read=0", (account_id,))["c"])
                latest = one(conn, "SELECT id FROM notifications WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,))
                latest_id = int(latest["id"]) if latest else 0
            if latest_id != last_id:
                last_id = latest_id
                yield f"event: notification\ndata: {json.dumps({'unread': unread, 'latest_id': latest_id})}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/ui/statuses")
async def ui_post_status(request: Request):
    data = await request_data(request)
    session = require_csrf(request, data)
    with db() as conn:
        status_id = create_status(conn, int(session["account_id"]), data, request)
    if request.headers.get("hx-request"):
        return render_status_partial(request, status_id, int(session["account_id"]))
    return RedirectResponse("/home", status_code=303)


@app.post("/ui/statuses/{status_id}/{action}")
async def ui_status_action(request: Request, status_id: int, action: str):
    data = await request_data(request)
    session = require_csrf(request, data)
    with db() as conn:
        if action == "edit":
            row = one(conn, "SELECT * FROM statuses WHERE id=? AND deleted_at IS NULL", (status_id,))
            if not row:
                raise StarletteHTTPException(status_code=404, detail="Status not found")
            if int(row["account_id"]) != int(session["account_id"]):
                raise StarletteHTTPException(status_code=403, detail="Not your status")
            conn.execute("INSERT INTO status_edits (status_id, account_id, content, spoiler_text, sensitive, created_at) VALUES (?,?,?,?,?,?)", (status_id, session["account_id"], row["content"], row["spoiler_text"], row["sensitive"], now()))
            conn.execute("UPDATE statuses SET content=?, spoiler_text=?, edited_at=? WHERE id=?", (str(data.get("status") or row["content"]), str(data.get("spoiler_text") or row["spoiler_text"]), now(), status_id))
            audit(conn, int(session["account_id"]), "status.edit", "status", status_id)
        else:
            perform_status_action(conn, int(session["account_id"]), status_id, action)
    return render_status_partial(request, status_id, int(session["account_id"]))


@app.post("/ui/profile/{account_id}/{action}")
async def ui_profile_action(request: Request, account_id: int, action: str):
    data = await request_data(request)
    session = require_csrf(request, data)
    with db() as conn:
        perform_account_action(conn, int(session["account_id"]), account_id, action, data)
    return RedirectResponse(request.headers.get("referer", "/home"), status_code=303)


@app.post("/ui/profile")
async def ui_update_profile(request: Request):
    data = await request_data(request)
    session = require_csrf(request, data)
    fields = []
    for idx in range(4):
        name = str(data.get(f"field_{idx}_name") or "").strip()
        value = str(data.get(f"field_{idx}_value") or "").strip()
        if name or value:
            fields.append({"name": name, "value": autolink_text(value), "verified_at": None})
    with db() as conn:
        avatar = str(data.get("avatar") or "").strip()
        header = str(data.get("header") or "").strip()
        current = one(conn, "SELECT avatar, header FROM accounts WHERE id=?", (session["account_id"],))
        conn.execute(
            "UPDATE accounts SET display_name=?, note=?, avatar=?, header=?, fields_json=? WHERE id=?",
            (str(data.get("display_name") or ""), str(data.get("note") or ""), avatar or current["avatar"], header or current["header"], json.dumps(fields), session["account_id"]),
        )
        audit(conn, int(session["account_id"]), "account.update", "account", session["account_id"])
    return RedirectResponse(f"/@{session['username']}", status_code=303)


def perform_status_action(conn: sqlite3.Connection, account_id: int, status_id: int, action: str) -> None:
    status = one(conn, "SELECT * FROM statuses WHERE id=? AND deleted_at IS NULL", (status_id,))
    if not status:
        raise StarletteHTTPException(status_code=404, detail="Status not found")
    if action == "favourite":
        inserted = conn.execute("INSERT OR IGNORE INTO favourites (account_id, status_id, created_at) VALUES (?,?,?)", (account_id, status_id, now())).rowcount
        if inserted:
            create_notification(conn, int(status["account_id"]), account_id, "favourite", status_id)
    elif action == "unfavourite":
        conn.execute("DELETE FROM favourites WHERE account_id=? AND status_id=?", (account_id, status_id))
    elif action == "reblog":
        inserted = conn.execute("INSERT OR IGNORE INTO reblogs (account_id, status_id, created_at) VALUES (?,?,?)", (account_id, status_id, now())).rowcount
        if inserted:
            create_notification(conn, int(status["account_id"]), account_id, "reblog", status_id)
    elif action == "unreblog":
        conn.execute("DELETE FROM reblogs WHERE account_id=? AND status_id=?", (account_id, status_id))
    elif action == "bookmark":
        conn.execute("INSERT OR IGNORE INTO bookmarks (account_id, status_id, created_at) VALUES (?,?,?)", (account_id, status_id, now()))
    elif action == "unbookmark":
        conn.execute("DELETE FROM bookmarks WHERE account_id=? AND status_id=?", (account_id, status_id))
    elif action == "delete":
        if int(status["account_id"]) != account_id:
            raise StarletteHTTPException(status_code=403, detail="Not your status")
        conn.execute("UPDATE statuses SET deleted_at=? WHERE id=?", (now(), status_id))
    else:
        raise StarletteHTTPException(status_code=404, detail="Unknown action")
    audit(conn, account_id, f"status.{action}", "status", status_id)


def perform_account_action(conn: sqlite3.Connection, account_id: int, target_id: int, action: str, data: dict[str, Any] | None = None) -> None:
    if account_id == target_id and action in {"follow", "block", "mute"}:
        return
    if action == "follow":
        conn.execute("INSERT OR REPLACE INTO relationships (follower_id, followed_id, state, created_at) VALUES (?,?,?,?)", (account_id, target_id, "following", now()))
        create_notification(conn, target_id, account_id, "follow", None)
    elif action == "unfollow":
        conn.execute("DELETE FROM relationships WHERE follower_id=? AND followed_id=?", (account_id, target_id))
    elif action == "mute":
        conn.execute("INSERT OR IGNORE INTO mutes (account_id, target_id, notifications, created_at) VALUES (?,?,?,?)", (account_id, target_id, 1, now()))
    elif action == "unmute":
        conn.execute("DELETE FROM mutes WHERE account_id=? AND target_id=?", (account_id, target_id))
    elif action == "block":
        conn.execute("INSERT OR IGNORE INTO blocks (account_id, target_id, created_at) VALUES (?,?,?)", (account_id, target_id, now()))
        conn.execute("DELETE FROM relationships WHERE (follower_id=? AND followed_id=?) OR (follower_id=? AND followed_id=?)", (account_id, target_id, target_id, account_id))
    elif action == "unblock":
        conn.execute("DELETE FROM blocks WHERE account_id=? AND target_id=?", (account_id, target_id))
    elif action == "report":
        data = data or {}
        conn.execute(
            "INSERT INTO reports (account_id, target_account_id, status_ids, category, comment, forward, rule_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, target_id, json.dumps(data_list(data, "status_ids")), str(data.get("category") or "other"), str(data.get("comment") or ""), 0, "[]", now()),
        )
    else:
        raise StarletteHTTPException(status_code=404, detail="Unknown action")
    audit(conn, account_id, f"account.{action}", "account", target_id)


def relationship_state(conn: sqlite3.Connection, account_id: int, ids: list[int]) -> list[dict[str, Any]]:
    out = []
    for target_id in ids:
        following = bool(one(conn, "SELECT 1 FROM relationships WHERE follower_id=? AND followed_id=? AND state='following'", (account_id, target_id)))
        followed_by = bool(one(conn, "SELECT 1 FROM relationships WHERE follower_id=? AND followed_id=? AND state='following'", (target_id, account_id)))
        muting = bool(one(conn, "SELECT 1 FROM mutes WHERE account_id=? AND target_id=?", (account_id, target_id)))
        blocking = bool(one(conn, "SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", (account_id, target_id)))
        out.append(
            {
                "id": str(target_id),
                "following": following,
                "showing_reblogs": True,
                "notifying": False,
                "followed_by": followed_by,
                "blocking": blocking,
                "blocked_by": bool(one(conn, "SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", (target_id, account_id))),
                "muting": muting,
                "muting_notifications": muting,
                "requested": False,
                "domain_blocking": False,
                "endorsed": False,
                "note": "",
            }
        )
    return out


@app.post("/api/v1/apps")
async def api_create_app(request: Request):
    data = await request_data(request)
    client_id = secret_token("client_")
    client_secret = secret_token("secret_")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO apps (client_id, client_secret, name, redirect_uri, website, scopes, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                client_id,
                client_secret,
                str(data.get("client_name") or "Chirp Client"),
                str(data.get("redirect_uris") or data.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob"),
                str(data.get("website") or ""),
                normalize_scope(str(data.get("scopes") or "read write follow push")),
                now(),
            ),
        )
        app_id = int(cur.lastrowid)
    return {"id": str(app_id), "name": str(data.get("client_name") or "Chirp Client"), "website": str(data.get("website") or ""), "redirect_uri": str(data.get("redirect_uris") or data.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob"), "client_id": client_id, "client_secret": client_secret, "vapid_key": "BCHIRPseededVapidKey"}


@app.get("/oauth/authorize")
async def oauth_authorize_get(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login?" + urlencode({"next": str(request.url)}), status_code=303)
    return render(request, "authorize.html", {"query": dict(request.query_params)})


@app.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse("/login", status_code=303)
    data = await request_data(request)
    client_id = str(data.get("client_id") or "")
    redirect_uri = str(data.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob")
    scope = normalize_scope(str(data.get("scope") or "read"))
    with db() as conn:
        app_row = one(conn, "SELECT * FROM apps WHERE client_id=?", (client_id,))
        if not app_row:
            raise StarletteHTTPException(status_code=400, detail="Invalid client_id")
        code = secret_token("code_")
        conn.execute(
            "INSERT INTO oauth_codes (code, app_id, account_id, redirect_uri, scope, code_challenge, code_challenge_method, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (code, app_row["id"], session["account_id"], redirect_uri, scope, str(data.get("code_challenge") or ""), str(data.get("code_challenge_method") or ""), utc_ts() + 600),
        )
    if redirect_uri == "urn:ietf:wg:oauth:2.0:oob":
        return PlainTextResponse(code)
    sep = "&" if "?" in redirect_uri else "?"
    state = str(data.get("state") or "")
    q = {"code": code}
    if state:
        q["state"] = state
    return RedirectResponse(redirect_uri + sep + urlencode(q), status_code=302)


def verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if not challenge:
        return True
    if method == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return hmac.compare_digest(expected, challenge)
    return hmac.compare_digest(verifier, challenge)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    data = await request_data(request)
    grant_type = str(data.get("grant_type") or "authorization_code")
    client_id = str(data.get("client_id") or "")
    client_secret = str(data.get("client_secret") or "")
    with db() as conn:
        app_row = one(conn, "SELECT * FROM apps WHERE client_id=?", (client_id,))
        if not app_row or (client_secret and not hmac.compare_digest(client_secret, app_row["client_secret"])):
            return json_error(401, "Invalid client credentials", "invalid_client")
        account_id = None
        scope = normalize_scope(str(data.get("scope") or app_row["scopes"]))
        if grant_type == "authorization_code":
            code = str(data.get("code") or "")
            code_row = one(conn, "SELECT * FROM oauth_codes WHERE code=? AND used=0 AND expires_at>?", (code, utc_ts()))
            if not code_row or int(code_row["app_id"]) != int(app_row["id"]):
                return json_error(400, "Invalid authorization code", "invalid_grant")
            if not verify_pkce(str(data.get("code_verifier") or ""), code_row["code_challenge"], code_row["code_challenge_method"]):
                return json_error(400, "Invalid PKCE verifier", "invalid_grant")
            account_id = int(code_row["account_id"])
            scope = code_row["scope"]
            conn.execute("UPDATE oauth_codes SET used=1 WHERE code=?", (code,))
        elif grant_type == "password":
            username = str(data.get("username") or "").strip().lstrip("@")
            password = str(data.get("password") or "")
            acct = one(conn, "SELECT * FROM accounts WHERE (username=? OR acct=?) AND domain=''", (username, username))
            if not acct or not password_ok(password, acct["password_hash"]):
                return json_error(400, "Invalid username or password", "invalid_grant")
            account_id = int(acct["id"])
        elif grant_type == "client_credentials":
            account_id = None
        elif grant_type == "refresh_token":
            return json_error(400, "Refresh tokens are not issued by this Chirp build", "unsupported_grant_type")
        else:
            return json_error(400, "Unsupported grant type", "unsupported_grant_type")
        token = secret_token("tok_")
        is_admin = False
        if account_id:
            acct = one(conn, "SELECT is_admin FROM accounts WHERE id=?", (account_id,))
            is_admin = bool(acct and acct["is_admin"])
        conn.execute("INSERT INTO tokens (token, app_id, account_id, scope, created_at, is_admin) VALUES (?,?,?,?,?,?)", (token, app_row["id"], account_id, scope, utc_ts(), 1 if is_admin else 0))
    return {"access_token": token, "token_type": "Bearer", "scope": scope, "created_at": utc_ts()}


@app.post("/oauth/revoke")
async def oauth_revoke(request: Request):
    data = await request_data(request)
    token = str(data.get("token") or "")
    with db() as conn:
        conn.execute("UPDATE tokens SET revoked=1 WHERE token=?", (token,))
    return {}


@app.post("/oauth/introspect")
async def oauth_introspect(request: Request):
    data = await request_data(request)
    token = str(data.get("token") or bearer_token(request) or "")
    with db() as conn:
        row = one(
            conn,
            "SELECT tokens.*, accounts.username, accounts.acct FROM tokens LEFT JOIN accounts ON accounts.id=tokens.account_id WHERE token=? AND revoked=0",
            (token,),
        )
        if not row or (row["expires_at"] and int(row["expires_at"]) <= utc_ts()):
            return {"active": False}
        app_row = one(conn, "SELECT client_id FROM apps WHERE id=?", (row["app_id"],)) if row["app_id"] else None
    out = {"active": True, "scope": row["scope"], "client_id": app_row["client_id"] if app_row else None, "token_type": "Bearer", "iat": row["created_at"]}
    if row["expires_at"]:
        out["exp"] = row["expires_at"]
    if row["account_id"]:
        out.update({"sub": str(row["account_id"]), "username": row["username"], "acct": row["acct"]})
    return out


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = public_base(request)
    scopes = ["profile", "read", "write", "follow", "push", "read:accounts", "read:statuses", "read:notifications", "read:lists", "write:statuses", "write:media", "write:favourites", "write:bookmarks", "write:follows", "write:lists", "admin:read", "admin:write"]
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "introspection_endpoint": f"{base}/oauth/introspect",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials", "password"],
        "code_challenge_methods_supported": ["plain", "S256"],
        "scopes_supported": scopes,
        "app_registration_endpoint": f"{base}/api/v1/apps",
        "service_documentation": "https://docs.joinmastodon.org/",
    }


@app.get("/.well-known/webfinger")
async def webfinger(request: Request):
    resource = request.query_params.get("resource", "")
    acct = resource.removeprefix("acct:").split("@", 1)[0].lstrip("@")
    with db() as conn:
        row = one(conn, "SELECT * FROM accounts WHERE username=? COLLATE NOCASE AND domain=''", (acct,))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        base = public_base(request)
        subject = f"acct:{row['username']}@{INSTANCE_DOMAIN}"
        return {
            "subject": subject,
            "aliases": [f"{base}/@{row['username']}"],
            "links": [
                {"rel": "self", "type": "application/activity+json", "href": f"{base}/users/{row['username']}"},
                {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html", "href": f"{base}/@{row['username']}"},
            ],
        }


@app.get("/users/{username}.json")
@app.get("/users/{username}")
async def actor_document(request: Request, username: str):
    with db() as conn:
        row = one(conn, "SELECT * FROM accounts WHERE username=? COLLATE NOCASE AND domain=''", (username,))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        base = public_base(request)
        return {
            "@context": ["https://www.w3.org/ns/activitystreams"],
            "id": f"{base}/users/{row['username']}",
            "type": "Person",
            "preferredUsername": row["username"],
            "name": row["display_name"],
            "summary": autolink_text(row["note"]),
            "url": f"{base}/@{row['username']}",
            "icon": {"type": "Image", "url": f"{base}{row['avatar']}"},
            "image": {"type": "Image", "url": f"{base}{row['header']}"},
        }


@app.get("/api/v1/instance")
@app.get("/api/v2/instance")
async def api_instance(request: Request):
    base = public_base(request)
    with db() as conn:
        user_count = one(conn, "SELECT count(*) AS c FROM accounts WHERE domain=''", ())["c"]
        status_count = one(conn, "SELECT count(*) AS c FROM statuses WHERE deleted_at IS NULL", ())["c"]
    return {
        "uri": INSTANCE_DOMAIN,
        "domain": INSTANCE_DOMAIN,
        "title": "Chirp",
        "short_description": "A small self-hosted Mastodon-compatible social server.",
        "description": "Chirp speaks the Mastodon v1 REST API for local clients.",
        "email": "admin@chirp.local",
        "version": "4.3.0 (compatible; Chirp)",
        "urls": {"streaming_api": base},
        "stats": {"user_count": user_count, "status_count": status_count, "domain_count": 3},
        "thumbnail": f"{base}/static/header-admin.svg",
        "languages": ["en"],
        "registrations": True,
        "approval_required": False,
        "invites_enabled": True,
        "configuration": {"statuses": {"max_characters": CHAR_LIMIT, "max_media_attachments": 4, "characters_reserved_per_url": 23}, "media_attachments": {"supported_mime_types": ["image/png", "image/jpeg", "image/gif", "image/svg+xml"], "image_size_limit": 10_485_760}},
        "contact_account": None,
        "rules": [],
    }


@app.get("/api/v1/custom_emojis")
async def custom_emojis():
    return []


@app.get("/api/v1/announcements")
async def api_announcements(request: Request):
    await api_auth(request, ["read"], optional=True)
    return []


@app.get("/api/v1/preferences")
async def api_preferences(request: Request):
    await api_auth(request, ["read:accounts"])
    return {
        "posting:default:visibility": "public",
        "posting:default:sensitive": False,
        "posting:default:language": "en",
        "reading:expand:media": "default",
        "reading:expand:spoilers": False,
    }


@app.get("/api/v1/markers")
async def api_get_markers(request: Request):
    token = await api_auth(request, ["read"])
    timelines = [v for k, v in request.query_params.multi_items() if k in {"timeline", "timeline[]"}]
    return {name: {"last_read_id": "0", "version": 0, "updated_at": now()} for name in timelines}


@app.post("/api/v1/markers")
async def api_save_markers(request: Request):
    await api_auth(request, ["write"])
    data = await request_data(request)
    out = {}
    for key, value in data.items():
        if key.endswith("[last_read_id]"):
            name = key.split("[", 1)[0]
            out[name] = {"last_read_id": str(value), "version": 1, "updated_at": now()}
    return out


@app.get("/api/v1/domain_blocks")
async def api_domain_blocks(request: Request):
    await api_auth(request, ["read:blocks"])
    return []


@app.get("/api/v1/followed_tags")
async def api_followed_tags(request: Request):
    await api_auth(request, ["read:follows"])
    return []


@app.get("/api/v1/tags/{tag}")
async def api_tag(request: Request, tag: str):
    await api_auth(request, ["read"], optional=True)
    return {"name": tag.lower(), "url": f"{public_base(request)}/tags/{tag.lower()}", "history": [{"day": str(int(time.time() // 86400)), "uses": "1", "accounts": "1"}], "following": False}


@app.get("/api/v1/trends/tags")
async def api_trends():
    with db() as conn:
        return trend_tags(conn)


@app.get("/api/v1/apps/verify_credentials")
async def api_app_verify(request: Request):
    token = await api_auth(request, ["read"], optional=False)
    with db() as conn:
        app_row = one(conn, "SELECT * FROM apps WHERE id=?", (token["app_id"],)) if token["app_id"] else None
        if not app_row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return {"name": app_row["name"], "website": app_row["website"], "vapid_key": "BCHIRPseededVapidKey", "client_id": app_row["client_id"]}


@app.get("/api/v1/accounts/verify_credentials")
async def api_verify_credentials(request: Request):
    token = await api_auth(request, ["read:accounts"])
    with db() as conn:
        acct = one(conn, "SELECT * FROM accounts WHERE id=?", (token["account_id"],))
        return account_json(conn, acct, request, source=True)


@app.patch("/api/v1/accounts/update_credentials")
async def api_update_credentials(request: Request):
    token = await api_auth(request, ["write:accounts"])
    data = await request_data(request)
    fields = []
    attrs = data.get("fields_attributes")
    if isinstance(attrs, dict):
        for value in attrs.values():
            if isinstance(value, dict):
                fields.append({"name": str(value.get("name") or ""), "value": autolink_text(str(value.get("value") or "")), "verified_at": None})
    with db() as conn:
        conn.execute(
            "UPDATE accounts SET display_name=COALESCE(NULLIF(?,''), display_name), note=COALESCE(?, note), fields_json=CASE WHEN ?!='[]' THEN ? ELSE fields_json END WHERE id=?",
            (str(data.get("display_name") or ""), str(data.get("note")) if data.get("note") is not None else None, json.dumps(fields), json.dumps(fields), token["account_id"]),
        )
        acct = one(conn, "SELECT * FROM accounts WHERE id=?", (token["account_id"],))
        return account_json(conn, acct, request, source=True)


@app.post("/api/v1/accounts")
async def api_account_create(request: Request):
    data = await request_data(request)
    username = re.sub(r"[^A-Za-z0-9_]", "", str(data.get("username") or ""))
    password = str(data.get("password") or secrets.token_urlsafe(8))
    with db() as conn:
        if not username or one(conn, "SELECT 1 FROM accounts WHERE username=? AND domain=''", (username,)):
            return json_error(422, "Validation failed")
        account_id = ensure_account(conn, username, str(data.get("display_name") or username), str(data.get("reason") or "New on Chirp."), password)
        acct = one(conn, "SELECT * FROM accounts WHERE id=?", (account_id,))
        return account_json(conn, acct, request, source=True)


@app.get("/api/v1/accounts/search")
async def api_account_search(request: Request):
    await api_auth(request, ["read:accounts"], optional=True)
    q = f"%{request.query_params.get('q', '')}%"
    limit = max(1, min(int(request.query_params.get("limit", 20)), 80))
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM accounts WHERE username LIKE ? OR acct LIKE ? OR display_name LIKE ? ORDER BY id LIMIT ?", (q, q, q, limit))
        return [account_json(conn, r, request) for r in rows]


@app.get("/api/v1/accounts/lookup")
async def api_account_lookup(request: Request):
    acct = request.query_params.get("acct", "").lstrip("@")
    with db() as conn:
        row = one(conn, "SELECT * FROM accounts WHERE acct=? OR username=?", (acct, acct.split("@", 1)[0]))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return account_json(conn, row, request)


@app.get("/api/v1/accounts/relationships")
async def api_relationships(request: Request):
    token = await api_auth(request, ["read:follows"])
    ids = [int(v) for k, v in request.query_params.multi_items() if k in {"id", "id[]"}]
    with db() as conn:
        return relationship_state(conn, int(token["account_id"]), ids)


@app.get("/api/v1/accounts/{account_id}")
async def api_account(request: Request, account_id: int):
    with db() as conn:
        row = one(conn, "SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return account_json(conn, row, request)


@app.get("/api/v1/accounts/{account_id}/statuses")
async def api_account_statuses(request: Request, account_id: int):
    token = await api_auth(request, ["read:statuses"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    exclude_replies = request.query_params.get("exclude_replies") == "true"
    only_media = request.query_params.get("only_media") == "true"
    base = "s.account_id=?"
    params: list[Any] = [account_id]
    if exclude_replies:
        base += " AND s.in_reply_to_id IS NULL"
    if only_media:
        base += " AND EXISTS (SELECT 1 FROM media m WHERE m.status_id=s.id)"
    with db() as conn:
        items, headers = paged_statuses(conn, request, base, params, viewer_id)
    if headers.pop("__not_modified__", None):
        return Response(status_code=304, headers=headers)
    return JSONResponse(items, headers=headers)


@app.get("/api/v1/accounts/{account_id}/followers")
async def api_followers(request: Request, account_id: int):
    await api_auth(request, ["read:follows"], optional=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT a.* FROM accounts a JOIN relationships r ON r.follower_id=a.id WHERE r.followed_id=? ORDER BY r.created_at DESC", (account_id,))
        return [account_json(conn, r, request) for r in rows]


@app.get("/api/v1/accounts/{account_id}/following")
async def api_following(request: Request, account_id: int):
    await api_auth(request, ["read:follows"], optional=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT a.* FROM accounts a JOIN relationships r ON r.followed_id=a.id WHERE r.follower_id=? ORDER BY r.created_at DESC", (account_id,))
        return [account_json(conn, r, request) for r in rows]


@app.get("/api/v1/accounts/{account_id}/lists")
async def api_account_lists(request: Request, account_id: int):
    token = await api_auth(request, ["read:lists"])
    with db() as conn:
        rows = all_rows(
            conn,
            "SELECT l.* FROM lists l JOIN list_accounts la ON la.list_id=l.id WHERE l.account_id=? AND la.account_id=? ORDER BY l.id",
            (token["account_id"], account_id),
        )
        return [{"id": str(r["id"]), "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": bool(r["exclusive"])} for r in rows]


@app.post("/api/v1/accounts/{account_id}/{action}")
async def api_account_action(request: Request, account_id: int, action: str):
    token = await api_auth(request, ["write:follows"])
    data = await request_data(request)
    with db() as conn:
        perform_account_action(conn, int(token["account_id"]), account_id, action, data)
        return relationship_state(conn, int(token["account_id"]), [account_id])[0]


@app.get("/api/v1/timelines/public")
async def api_public_timeline(request: Request):
    token = await api_auth(request, ["read:statuses"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    local = request.query_params.get("local") == "true"
    remote = request.query_params.get("remote") == "true"
    base = "s.visibility IN ('public','unlisted')"
    if local:
        base += " AND s.local=1"
    if remote:
        base += " AND s.local=0"
    with db() as conn:
        items, headers = paged_statuses(conn, request, base, [], viewer_id)
    if headers.pop("__not_modified__", None):
        return Response(status_code=304, headers=headers)
    return JSONResponse(items, headers=headers)


@app.get("/api/v1/timelines/home")
async def api_home_timeline(request: Request):
    token = await api_auth(request, ["read:statuses"])
    viewer_id = int(token["account_id"])
    with db() as conn:
        items, headers = paged_statuses(conn, request, "(s.account_id=? OR EXISTS (SELECT 1 FROM relationships r WHERE r.follower_id=? AND r.followed_id=s.account_id AND r.state='following'))", [viewer_id, viewer_id], viewer_id)
    if headers.pop("__not_modified__", None):
        return Response(status_code=304, headers=headers)
    return JSONResponse(items, headers=headers)


@app.get("/api/v1/timelines/tag/{tag}")
async def api_tag_timeline(request: Request, tag: str):
    token = await api_auth(request, ["read:statuses"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    with db() as conn:
        items, headers = paged_statuses(conn, request, "s.content LIKE ?", [f"%#{tag}%"], viewer_id)
    if headers.pop("__not_modified__", None):
        return Response(status_code=304, headers=headers)
    return JSONResponse(items, headers=headers)


@app.get("/api/v1/statuses/{status_id}")
async def api_get_status(request: Request, status_id: int):
    token = await api_auth(request, ["read:statuses"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    with db() as conn:
        row = status_row(conn, status_id, viewer_id)
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        item = status_json(conn, row, request, viewer_id)
        etag = '"' + hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:24] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(item, headers={"ETag": etag})


@app.get("/api/v1/statuses/{status_id}/context")
async def api_status_context(request: Request, status_id: int):
    token = await api_auth(request, ["read:statuses"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    with db() as conn:
        target = status_row(conn, status_id, viewer_id)
        if not target:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        ancestors = []
        cur = target
        while cur["in_reply_to_id"]:
            parent = status_row(conn, int(cur["in_reply_to_id"]), viewer_id)
            if not parent:
                break
            ancestors.insert(0, status_json(conn, parent, request, viewer_id))
            cur = parent
        descendants = [status_json(conn, r, request, viewer_id) for r in all_rows(conn, "SELECT * FROM statuses WHERE in_reply_to_id=? AND deleted_at IS NULL ORDER BY id", (status_id,))]
        return {"ancestors": ancestors, "descendants": descendants}


@app.get("/api/v1/statuses/{status_id}/source")
async def api_status_source(request: Request, status_id: int):
    token = await api_auth(request, ["read:statuses"])
    with db() as conn:
        row = status_row(conn, status_id, int(token["account_id"]))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return {"id": str(row["id"]), "text": row["content"], "spoiler_text": row["spoiler_text"]}


@app.get("/api/v1/statuses/{status_id}/history")
async def api_status_history(request: Request, status_id: int):
    await api_auth(request, ["read:statuses"], optional=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM status_edits WHERE status_id=? ORDER BY id", (status_id,))
        return [{"content": autolink_text(r["content"]), "spoiler_text": r["spoiler_text"], "sensitive": bool(r["sensitive"]), "created_at": r["created_at"], "account": None, "poll": None, "media_attachments": [], "emojis": []} for r in rows]


@app.post("/api/v1/statuses")
async def api_create_status(request: Request):
    token = await api_auth(request, ["write:statuses"])
    data = await request_data(request)
    idem = request.headers.get("idempotency-key")
    with db() as conn:
        if idem:
            existing = one(conn, "SELECT status_id FROM idempotency_keys WHERE account_id=? AND key=?", (token["account_id"], idem))
            if existing:
                row = status_row(conn, int(existing["status_id"]), int(token["account_id"]))
                return status_json(conn, row, request, int(token["account_id"]))
        status_id = create_status(conn, int(token["account_id"]), data, request)
        if idem:
            conn.execute("INSERT OR REPLACE INTO idempotency_keys (account_id, key, status_id, created_at) VALUES (?,?,?,?)", (token["account_id"], idem, status_id, now()))
        row = status_row(conn, status_id, int(token["account_id"]))
        return status_json(conn, row, request, int(token["account_id"]))


@app.put("/api/v1/statuses/{status_id}")
async def api_edit_status(request: Request, status_id: int):
    token = await api_auth(request, ["write:statuses"])
    data = await request_data(request)
    with db() as conn:
        row = one(conn, "SELECT * FROM statuses WHERE id=? AND deleted_at IS NULL", (status_id,))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        if int(row["account_id"]) != int(token["account_id"]):
            raise StarletteHTTPException(status_code=403, detail="Not your status")
        conn.execute("INSERT INTO status_edits (status_id, account_id, content, spoiler_text, sensitive, created_at) VALUES (?,?,?,?,?,?)", (status_id, token["account_id"], row["content"], row["spoiler_text"], row["sensitive"], now()))
        conn.execute(
            "UPDATE statuses SET content=?, spoiler_text=?, sensitive=?, edited_at=? WHERE id=?",
            (str(data.get("status") or row["content"]), str(data.get("spoiler_text") or row["spoiler_text"]), 1 if str(data.get("sensitive") or row["sensitive"]).lower() in {"1", "true", "on"} else 0, now(), status_id),
        )
        return status_json(conn, one(conn, "SELECT * FROM statuses WHERE id=?", (status_id,)), request, int(token["account_id"]))


@app.delete("/api/v1/statuses/{status_id}")
async def api_delete_status(request: Request, status_id: int):
    token = await api_auth(request, ["write:statuses"])
    with db() as conn:
        row = one(conn, "SELECT * FROM statuses WHERE id=? AND deleted_at IS NULL", (status_id,))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        if int(row["account_id"]) != int(token["account_id"]):
            raise StarletteHTTPException(status_code=403, detail="Not your status")
        conn.execute("UPDATE statuses SET deleted_at=? WHERE id=?", (now(), status_id))
        return status_json(conn, row, request, int(token["account_id"]))


@app.post("/api/v1/statuses/{status_id}/{action}")
async def api_status_action(request: Request, status_id: int, action: str):
    scope = "write:favourites" if "favourite" in action else "write:bookmarks" if "bookmark" in action else "write:statuses"
    token = await api_auth(request, [scope])
    with db() as conn:
        perform_status_action(conn, int(token["account_id"]), status_id, action)
        row = status_row(conn, status_id, int(token["account_id"]))
        return status_json(conn, row, request, int(token["account_id"]))


@app.get("/api/v1/favourites")
async def api_favourites(request: Request):
    token = await api_auth(request, ["read:favourites"])
    with db() as conn:
        rows = all_rows(conn, "SELECT s.* FROM statuses s JOIN favourites f ON f.status_id=s.id WHERE f.account_id=? AND s.deleted_at IS NULL ORDER BY f.created_at DESC", (token["account_id"],))
        return [status_json(conn, r, request, int(token["account_id"])) for r in rows]


@app.get("/api/v1/bookmarks")
async def api_bookmarks(request: Request):
    token = await api_auth(request, ["read:bookmarks"])
    with db() as conn:
        rows = all_rows(conn, "SELECT s.* FROM statuses s JOIN bookmarks b ON b.status_id=s.id WHERE b.account_id=? AND s.deleted_at IS NULL ORDER BY b.created_at DESC", (token["account_id"],))
        return [status_json(conn, r, request, int(token["account_id"])) for r in rows]


@app.post("/api/v1/media")
@app.post("/api/v2/media")
async def api_upload_media(request: Request, file: UploadFile = File(None)):
    token = await api_auth(request, ["write:media"])
    form = await request.form()
    upload = file or form.get("file") or form.get("media")
    if not isinstance(upload, UploadFile):
        return json_error(422, "No media file supplied")
    suffix = Path(upload.filename or "media.bin").suffix.lower()[:12] or ".bin"
    media_id = None
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO media (account_id, url, preview_url, description, created_at, unattached, meta_json) VALUES (?,?,?,?,?,?,?)",
            (token["account_id"], "", "", str(form.get("description") or ""), now(), 1, json.dumps({"original": {"width": 0, "height": 0, "size": "0x0", "aspect": 1}})),
        )
        media_id = int(cur.lastrowid)
        dest = MEDIA_DIR / f"{media_id}{suffix}"
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        url = f"/media/{dest.name}"
        conn.execute("UPDATE media SET url=?, preview_url=?, type=? WHERE id=?", (url, url, "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"} else "unknown", media_id))
        row = one(conn, "SELECT * FROM media WHERE id=?", (media_id,))
        return media_json(conn, 0, request)[0] if False else {
            "id": str(row["id"]),
            "type": row["type"],
            "url": f"{public_base(request)}{row['url']}",
            "preview_url": f"{public_base(request)}{row['preview_url']}",
            "remote_url": None,
            "text_url": None,
            "meta": json.loads(row["meta_json"] or "{}"),
            "description": row["description"],
            "blurhash": row["blurhash"],
        }


@app.get("/api/v1/notifications")
async def api_notifications(request: Request):
    token = await api_auth(request, ["read:notifications"])
    limit = max(1, min(int(request.query_params.get("limit", 20)), 80))
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM notifications WHERE account_id=? ORDER BY id DESC LIMIT ?", (token["account_id"], limit))
        return [notification_json(conn, r, request) for r in rows]


@app.get("/api/v1/notifications/{notification_id}")
async def api_notification(request: Request, notification_id: int):
    token = await api_auth(request, ["read:notifications"])
    with db() as conn:
        row = one(conn, "SELECT * FROM notifications WHERE id=? AND account_id=?", (notification_id, token["account_id"]))
        if not row:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return notification_json(conn, row, request)


@app.post("/api/v1/notifications/clear")
async def api_notifications_clear(request: Request):
    token = await api_auth(request, ["write:notifications"])
    with db() as conn:
        conn.execute("DELETE FROM notifications WHERE account_id=?", (token["account_id"],))
    return {}


@app.post("/api/v1/notifications/{notification_id}/dismiss")
async def api_notification_dismiss(request: Request, notification_id: int):
    token = await api_auth(request, ["write:notifications"])
    with db() as conn:
        conn.execute("DELETE FROM notifications WHERE id=? AND account_id=?", (notification_id, token["account_id"]))
    return {}


@app.get("/api/v2/search")
async def api_search(request: Request):
    token = await api_auth(request, ["read:search"], optional=True)
    viewer_id = int(token["account_id"]) if token and token["account_id"] else None
    q = request.query_params.get("q", "")
    limit = max(1, min(int(request.query_params.get("limit", 20)), 80))
    with db() as conn:
        accounts = all_rows(conn, "SELECT * FROM accounts WHERE username LIKE ? OR acct LIKE ? OR display_name LIKE ? LIMIT ?", (f"%{q}%", f"%{q}%", f"%{q}%", limit))
        statuses = all_rows(conn, "SELECT * FROM statuses WHERE content LIKE ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?", (f"%{q}%", limit))
        tags = [t for t in trend_tags(conn, limit) if q.lower().lstrip("#") in t["name"]]
        return {
            "accounts": [account_json(conn, a, request) for a in accounts],
            "statuses": [status_json(conn, s, request, viewer_id) for s in statuses if status_row(conn, int(s["id"]), viewer_id)],
            "hashtags": tags,
        }


@app.get("/api/v1/polls/{poll_id}")
async def api_poll(request: Request, poll_id: int):
    token = await api_auth(request, ["read:statuses"], optional=True)
    with db() as conn:
        poll = poll_json(conn, poll_id, int(token["account_id"]) if token and token["account_id"] else None)
        if not poll:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return poll


@app.post("/api/v1/polls/{poll_id}/votes")
async def api_poll_vote(request: Request, poll_id: int):
    token = await api_auth(request, ["write:statuses"])
    data = await request_data(request)
    choices = [int(v) for v in data_list(data, "choices")]
    with db() as conn:
        poll = one(conn, "SELECT * FROM polls WHERE id=?", (poll_id,))
        if not poll:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        if not poll["multiple"] and len(choices) > 1:
            choices = choices[:1]
        options = all_rows(conn, "SELECT * FROM poll_options WHERE poll_id=? ORDER BY position", (poll_id,))
        for choice in choices:
            if 0 <= choice < len(options):
                option_id = int(options[choice]["id"])
                inserted = conn.execute("INSERT OR IGNORE INTO poll_votes (poll_id, account_id, option_id, created_at) VALUES (?,?,?,?)", (poll_id, token["account_id"], option_id, now())).rowcount
                if inserted:
                    conn.execute("UPDATE poll_options SET votes_count=votes_count+1 WHERE id=?", (option_id,))
        return poll_json(conn, poll_id, int(token["account_id"]))


@app.get("/api/v1/lists")
async def api_lists(request: Request):
    token = await api_auth(request, ["read:lists"])
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM lists WHERE account_id=? ORDER BY id", (token["account_id"],))
        return [{"id": str(r["id"]), "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": bool(r["exclusive"])} for r in rows]


@app.post("/api/v1/lists")
async def api_list_create(request: Request):
    token = await api_auth(request, ["write:lists"])
    data = await request_data(request)
    with db() as conn:
        cur = conn.execute("INSERT INTO lists (account_id, title, replies_policy, exclusive, created_at) VALUES (?,?,?,?,?)", (token["account_id"], str(data.get("title") or "New list"), str(data.get("replies_policy") or "list"), 1 if str(data.get("exclusive") or "").lower() in {"1", "true", "on"} else 0, now()))
        r = one(conn, "SELECT * FROM lists WHERE id=?", (cur.lastrowid,))
        return {"id": str(r["id"]), "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": bool(r["exclusive"])}


@app.get("/api/v1/lists/{list_id}")
async def api_list_get(request: Request, list_id: int):
    token = await api_auth(request, ["read:lists"])
    with db() as conn:
        r = one(conn, "SELECT * FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"]))
        if not r:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return {"id": str(r["id"]), "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": bool(r["exclusive"])}


@app.put("/api/v1/lists/{list_id}")
async def api_list_update(request: Request, list_id: int):
    token = await api_auth(request, ["write:lists"])
    data = await request_data(request)
    with db() as conn:
        conn.execute("UPDATE lists SET title=COALESCE(NULLIF(?,''), title), replies_policy=COALESCE(NULLIF(?,''), replies_policy), exclusive=? WHERE id=? AND account_id=?", (str(data.get("title") or ""), str(data.get("replies_policy") or ""), 1 if str(data.get("exclusive") or "").lower() in {"1", "true", "on"} else 0, list_id, token["account_id"]))
        r = one(conn, "SELECT * FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"]))
        if not r:
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        return {"id": str(r["id"]), "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": bool(r["exclusive"])}


@app.delete("/api/v1/lists/{list_id}")
async def api_list_delete(request: Request, list_id: int):
    token = await api_auth(request, ["write:lists"])
    with db() as conn:
        conn.execute("DELETE FROM list_accounts WHERE list_id IN (SELECT id FROM lists WHERE id=? AND account_id=?)", (list_id, token["account_id"]))
        conn.execute("DELETE FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"]))
    return {}


@app.get("/api/v1/lists/{list_id}/accounts")
async def api_list_accounts(request: Request, list_id: int):
    token = await api_auth(request, ["read:lists"])
    with db() as conn:
        if not one(conn, "SELECT 1 FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"])):
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        rows = all_rows(conn, "SELECT a.* FROM accounts a JOIN list_accounts la ON la.account_id=a.id WHERE la.list_id=? ORDER BY a.id", (list_id,))
        return [account_json(conn, r, request) for r in rows]


@app.post("/api/v1/lists/{list_id}/accounts")
async def api_list_accounts_add(request: Request, list_id: int):
    token = await api_auth(request, ["write:lists"])
    data = await request_data(request)
    ids = [int(v) for v in data_list(data, "account_ids")]
    with db() as conn:
        if not one(conn, "SELECT 1 FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"])):
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        for account_id in ids:
            conn.execute("INSERT OR IGNORE INTO list_accounts (list_id, account_id) VALUES (?,?)", (list_id, account_id))
    return {}


@app.delete("/api/v1/lists/{list_id}/accounts")
async def api_list_accounts_delete(request: Request, list_id: int):
    token = await api_auth(request, ["write:lists"])
    data = await request_data(request)
    ids = [int(v) for v in data_list(data, "account_ids")]
    with db() as conn:
        for account_id in ids:
            conn.execute("DELETE FROM list_accounts WHERE list_id=? AND account_id=? AND EXISTS (SELECT 1 FROM lists WHERE id=? AND account_id=?)", (list_id, account_id, list_id, token["account_id"]))
    return {}


@app.get("/api/v1/timelines/list/{list_id}")
async def api_list_timeline(request: Request, list_id: int):
    token = await api_auth(request, ["read:lists"])
    with db() as conn:
        if not one(conn, "SELECT 1 FROM lists WHERE id=? AND account_id=?", (list_id, token["account_id"])):
            raise StarletteHTTPException(status_code=404, detail="Record not found")
        items, headers = paged_statuses(conn, request, "EXISTS (SELECT 1 FROM list_accounts la WHERE la.list_id=? AND la.account_id=s.account_id)", [list_id], int(token["account_id"]))
    if headers.pop("__not_modified__", None):
        return Response(status_code=304, headers=headers)
    return JSONResponse(items, headers=headers)


@app.post("/api/v1/reports")
async def api_reports(request: Request):
    data = await request_data(request)
    token = None
    session = None
    if bearer_token(request):
        token = await api_auth(request, ["write:reports"])
        reporter_id = int(token["account_id"])
    else:
        session = require_csrf(request, data)
        reporter_id = int(session["account_id"])
    target_id = int(data.get("account_id") or data.get("target_account_id") or 0)
    if not target_id:
        return json_error(422, "account_id is required")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO reports (account_id, target_account_id, status_ids, category, comment, forward, rule_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (reporter_id, target_id, json.dumps(data_list(data, "status_ids")), str(data.get("category") or "other"), str(data.get("comment") or ""), 1 if str(data.get("forward") or "").lower() in {"1", "true", "on"} else 0, json.dumps(data_list(data, "rule_ids")), now()),
        )
        audit(conn, reporter_id, "report.create", "report", cur.lastrowid)
        if session and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(request.headers.get("referer", "/home"), status_code=303)
        return {"id": str(cur.lastrowid), "action_taken": False, "action_taken_at": None, "category": str(data.get("category") or "other"), "comment": str(data.get("comment") or ""), "forwarded": False, "created_at": now(), "status_ids": data_list(data, "status_ids"), "rule_ids": data_list(data, "rule_ids"), "target_account": account_json(conn, one(conn, "SELECT * FROM accounts WHERE id=?", (target_id,)), request)}


@app.get("/_admin/queues")
async def admin_queues(request: Request):
    await api_auth(request, ["admin:read"], admin=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT kind, status, count(*) AS count FROM jobs GROUP BY kind, status ORDER BY kind")
        return {"queues": [{"kind": r["kind"], "status": r["status"], "count": r["count"]} for r in rows]}


@app.get("/_admin/audit")
async def admin_audit(request: Request):
    await api_auth(request, ["admin:read"], admin=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM audit_events ORDER BY id DESC LIMIT 100")
        return {"events": [dict(r) for r in rows]}


@app.get("/_admin/reports")
async def admin_reports(request: Request):
    await api_auth(request, ["admin:read"], admin=True)
    with db() as conn:
        rows = all_rows(conn, "SELECT * FROM reports ORDER BY id DESC LIMIT 100")
        return {"reports": [dict(r) for r in rows]}
