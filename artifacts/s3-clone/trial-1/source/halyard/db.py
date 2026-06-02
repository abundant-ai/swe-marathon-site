import os
import sqlite3
import threading

DB_PATH = os.environ.get("HALYARD_DB", "/app/data/halyard.db")
DATA_DIR = os.environ.get("HALYARD_DATA", "/app/data")

_lock = threading.Lock()
_local = threading.local()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _connect()
        _local.conn = c
    return c


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "objects"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "parts"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "tmp"), exist_ok=True)
    conn = _connect()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS tenants (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        quota_bytes INTEGER,
        quota_buckets INTEGER
    );

    CREATE TABLE IF NOT EXISTS access_keys (
        access_key_id TEXT PRIMARY KEY,
        secret_access_key TEXT NOT NULL,
        tenant TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(tenant) REFERENCES tenants(name) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS buckets (
        name TEXT PRIMARY KEY,
        tenant TEXT NOT NULL,
        created_at TEXT NOT NULL,
        versioning TEXT NOT NULL DEFAULT 'Disabled',
        cors TEXT,
        lifecycle TEXT,
        policy TEXT,
        notification TEXT,
        FOREIGN KEY(tenant) REFERENCES tenants(name) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS buckets_tenant_idx ON buckets(tenant);

    CREATE TABLE IF NOT EXISTS objects (
        bucket TEXT NOT NULL,
        key TEXT NOT NULL,
        version_id TEXT NOT NULL,
        is_latest INTEGER NOT NULL DEFAULT 1,
        is_delete_marker INTEGER NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0,
        etag TEXT NOT NULL DEFAULT '',
        content_type TEXT,
        content_disposition TEXT,
        content_encoding TEXT,
        cache_control TEXT,
        metadata TEXT,
        tagging TEXT,
        storage_path TEXT,
        created_at TEXT NOT NULL,
        last_modified TEXT NOT NULL,
        PRIMARY KEY (bucket, key, version_id),
        FOREIGN KEY(bucket) REFERENCES buckets(name) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS objects_bucket_key_idx ON objects(bucket, key);
    CREATE INDEX IF NOT EXISTS objects_latest_idx ON objects(bucket, is_latest);

    CREATE TABLE IF NOT EXISTS multipart_uploads (
        upload_id TEXT PRIMARY KEY,
        bucket TEXT NOT NULL,
        key TEXT NOT NULL,
        initiated_at TEXT NOT NULL,
        content_type TEXT,
        content_disposition TEXT,
        content_encoding TEXT,
        cache_control TEXT,
        metadata TEXT,
        tagging TEXT,
        FOREIGN KEY(bucket) REFERENCES buckets(name) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS mpu_bucket_idx ON multipart_uploads(bucket);

    CREATE TABLE IF NOT EXISTS multipart_parts (
        upload_id TEXT NOT NULL,
        part_number INTEGER NOT NULL,
        size INTEGER NOT NULL,
        etag TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        last_modified TEXT NOT NULL,
        PRIMARY KEY (upload_id, part_number),
        FOREIGN KEY(upload_id) REFERENCES multipart_uploads(upload_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        tenant TEXT,
        access_key_id TEXT,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        bucket TEXT,
        key TEXT,
        status INTEGER NOT NULL,
        request_id TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_events(id DESC);
    CREATE INDEX IF NOT EXISTS audit_tenant_idx ON audit_events(tenant, id DESC);

    CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        payload TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS console_sessions (
        token TEXT PRIMARY KEY,
        access_key_id TEXT NOT NULL,
        tenant TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()
