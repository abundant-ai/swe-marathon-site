"""SQLite database access and schema for Chirp."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Optional

DB_PATH = os.environ.get("CHIRP_DB", "/app/data/chirp.db")
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def execute(sql: str, params: Iterable = ()):
    return get_conn().execute(sql, tuple(params))


def query_one(sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    row = cur.fetchone()
    cur.close()
    return row


def query_all(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    rows = cur.fetchall()
    cur.close()
    return rows


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    domain TEXT,
    acct TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    header TEXT NOT NULL DEFAULT '',
    locked INTEGER NOT NULL DEFAULT 0,
    bot INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_status_at TEXT,
    statuses_count INTEGER NOT NULL DEFAULT 0,
    followers_count INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_local INTEGER NOT NULL DEFAULT 1,
    is_suspended INTEGER NOT NULL DEFAULT 0,
    is_silenced INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,
    email TEXT,
    fields TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_accounts_acct ON accounts(acct);
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    csrf TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL UNIQUE,
    client_secret TEXT NOT NULL,
    name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    scopes TEXT NOT NULL,
    website TEXT,
    vapid_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    scopes TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT,
    code_challenge_method TEXT,
    created_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token TEXT PRIMARY KEY,
    app_id INTEGER REFERENCES apps(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    grant_type TEXT NOT NULL DEFAULT 'authorization_code'
);
CREATE INDEX IF NOT EXISTS idx_tokens_account ON oauth_tokens(account_id);

CREATE TABLE IF NOT EXISTS statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    spoiler_text TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'public',
    sensitive INTEGER NOT NULL DEFAULT 0,
    in_reply_to_id INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
    in_reply_to_account_id INTEGER,
    reblog_of_id INTEGER REFERENCES statuses(id) ON DELETE CASCADE,
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL,
    edited_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    favourites_count INTEGER NOT NULL DEFAULT 0,
    reblogs_count INTEGER NOT NULL DEFAULT 0,
    replies_count INTEGER NOT NULL DEFAULT 0,
    application_id INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    idempotency_key TEXT,
    poll_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_statuses_account ON statuses(account_id);
CREATE INDEX IF NOT EXISTS idx_statuses_created ON statuses(created_at);
CREATE INDEX IF NOT EXISTS idx_statuses_in_reply_to ON statuses(in_reply_to_id);
CREATE INDEX IF NOT EXISTS idx_statuses_reblog_of ON statuses(reblog_of_id);
CREATE INDEX IF NOT EXISTS idx_statuses_idem ON statuses(account_id, idempotency_key);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    spoiler_text TEXT NOT NULL DEFAULT '',
    sensitive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    notify INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_id);

CREATE TABLE IF NOT EXISTS blocks (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, target_id)
);

CREATE TABLE IF NOT EXISTS mutes (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    notifications INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (account_id, target_id)
);

CREATE TABLE IF NOT EXISTS favourites (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, status_id)
);

CREATE TABLE IF NOT EXISTS bookmarks (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, status_id)
);

CREATE TABLE IF NOT EXISTS pins (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, status_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    from_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    status_id INTEGER REFERENCES statuses(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_account ON notifications(account_id, created_at);

CREATE TABLE IF NOT EXISTS markers (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    timeline TEXT NOT NULL,
    last_read_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, timeline)
);

CREATE TABLE IF NOT EXISTS media_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    status_id INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    preview_url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    blurhash TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    file_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_media (
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media_attachments(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (status_id, media_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_tags (
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (status_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_status_tags_tag ON status_tags(tag_id);

CREATE TABLE IF NOT EXISTS status_mentions (
    status_id INTEGER NOT NULL REFERENCES statuses(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (status_id, account_id)
);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    replies_policy TEXT NOT NULL DEFAULT 'list',
    exclusive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_accounts (
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    PRIMARY KEY (list_id, account_id)
);

CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status_id INTEGER REFERENCES statuses(id) ON DELETE CASCADE,
    expires_at TEXT,
    multiple INTEGER NOT NULL DEFAULT 0,
    options TEXT NOT NULL,
    voters TEXT NOT NULL DEFAULT '{}',
    voters_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    target_account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    comment TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    status_ids TEXT NOT NULL DEFAULT '[]',
    forwarded INTEGER NOT NULL DEFAULT 0,
    action_taken INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    enqueued_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(queue, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    last_status_id INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
    unread INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    filter_action TEXT NOT NULL DEFAULT 'warn',
    keywords TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    token_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    keys_p256dh TEXT NOT NULL,
    keys_auth TEXT NOT NULL,
    alerts TEXT NOT NULL DEFAULT '{}',
    server_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        conn.executescript(SCHEMA)
        _initialized = True
