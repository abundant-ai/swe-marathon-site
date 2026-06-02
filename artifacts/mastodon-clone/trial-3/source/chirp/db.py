"""SQLite storage for Chirp."""
import sqlite3, os, threading, time, json, secrets

DB_PATH = os.environ.get("CHIRP_DB", "/app/data/chirp.db")
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c

def q(sql, *args):
    return conn().execute(sql, args)

def qone(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return r

def qall(sql, *args):
    return conn().execute(sql, args).fetchall()

def execmany(sql, rows):
    conn().executemany(sql, rows)

def init():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        c = conn()
        with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
            c.executescript(f.read())
        _initialized = True
