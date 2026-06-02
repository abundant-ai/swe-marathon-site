import sqlite3
import threading
import os
import time
import json

DB_PATH = os.environ.get('HALYARD_DB_PATH', '/app/data/halyard.db')
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tenants (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  quota_bytes INTEGER,
  quota_buckets INTEGER
);
CREATE TABLE IF NOT EXISTS access_keys (
  access_key_id TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  secret_access_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(tenant) REFERENCES tenants(name)
);
CREATE TABLE IF NOT EXISTS buckets (
  name TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  created_at TEXT NOT NULL,
  versioning TEXT NOT NULL DEFAULT 'Unversioned',
  cors TEXT,
  policy TEXT,
  lifecycle TEXT,
  notification TEXT,
  FOREIGN KEY(tenant) REFERENCES tenants(name)
);
CREATE TABLE IF NOT EXISTS objects (
  bucket TEXT NOT NULL,
  key TEXT NOT NULL,
  version_id TEXT NOT NULL,
  is_latest INTEGER NOT NULL,
  delete_marker INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  content_type TEXT,
  metadata TEXT,
  tagging TEXT,
  storage_path TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(bucket, key, version_id)
);
CREATE INDEX IF NOT EXISTS idx_objects_bucket_key ON objects(bucket, key);
CREATE INDEX IF NOT EXISTS idx_objects_latest ON objects(bucket, is_latest);
CREATE TABLE IF NOT EXISTS multipart (
  upload_id TEXT PRIMARY KEY,
  bucket TEXT NOT NULL,
  key TEXT NOT NULL,
  initiated_at TEXT NOT NULL,
  metadata TEXT,
  content_type TEXT,
  tagging TEXT
);
CREATE TABLE IF NOT EXISTS multipart_parts (
  upload_id TEXT NOT NULL,
  part_number INTEGER NOT NULL,
  etag TEXT NOT NULL,
  size INTEGER NOT NULL,
  storage_path TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  PRIMARY KEY(upload_id, part_number)
);
CREATE TABLE IF NOT EXISTS notification_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket TEXT NOT NULL,
  url TEXT NOT NULL,
  payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS console_sessions (
  session_id TEXT PRIMARY KEY,
  access_key_id TEXT NOT NULL,
  tenant TEXT NOT NULL,
  created_at TEXT NOT NULL
);
'''

def _conn():
    c = getattr(_local, 'conn', None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA foreign_keys=ON')
        c.row_factory = sqlite3.Row
        _local.conn = c
    return c

def init_db():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = _conn()
        c.executescript(SCHEMA)
        _initialized = True

def conn():
    init_db()
    return _conn()
