"""Background workers: lifecycle and notification outbox."""
import datetime
import json
import os
import threading
import time
import urllib.request
from xml.etree import ElementTree as ET

from .db import _connect, get_conn
from .util import gen_version_id, now_iso, safe_remove


_started = False
_started_lock = threading.Lock()


def start_workers():
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    t1 = threading.Thread(target=_lifecycle_loop, daemon=True)
    t1.start()
    t2 = threading.Thread(target=_outbox_loop, daemon=True)
    t2.start()


def _lifecycle_loop():
    tick = float(os.environ.get("S3CLONE_LIFECYCLE_TICK_SECONDS", "2"))
    while True:
        try:
            _run_lifecycle()
        except Exception:
            pass
        time.sleep(tick)


def _run_lifecycle():
    secs_per_day = float(os.environ.get("S3CLONE_LIFECYCLE_SECONDS_PER_DAY", "86400"))
    conn = _connect()
    try:
        cur = conn.execute("SELECT name, lifecycle, versioning FROM buckets WHERE lifecycle IS NOT NULL")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for b in rows:
        try:
            root = ET.fromstring(b["lifecycle"])
        except ET.ParseError:
            continue
        rules = []
        for rule in root.iter():
            tag = rule.tag.split("}")[-1] if "}" in rule.tag else rule.tag
            if tag == "Rule":
                status = None
                prefix = ""
                days = None
                rid = None
                for c in rule:
                    ct = c.tag.split("}")[-1] if "}" in c.tag else c.tag
                    if ct == "Status":
                        status = c.text
                    elif ct == "ID":
                        rid = c.text
                    elif ct == "Prefix":
                        prefix = c.text or ""
                    elif ct == "Filter":
                        for f in c:
                            ft = f.tag.split("}")[-1] if "}" in f.tag else f.tag
                            if ft == "Prefix":
                                prefix = f.text or ""
                    elif ct == "Expiration":
                        for e in c:
                            et = e.tag.split("}")[-1] if "}" in e.tag else e.tag
                            if et == "Days":
                                try:
                                    days = int(e.text)
                                except ValueError:
                                    pass
                if status == "Enabled" and days is not None:
                    rules.append((prefix, days))

        if not rules:
            continue
        _apply_lifecycle(b["name"], b["versioning"], rules, secs_per_day)


def _apply_lifecycle(bucket, versioning, rules, secs_per_day):
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT bucket, key, version_id, is_latest, is_delete_marker, storage_path, last_modified FROM objects "
            "WHERE bucket=? AND is_latest=1 AND is_delete_marker=0",
            (bucket,),
        )
        objs = [dict(r) for r in cur.fetchall()]
        now = datetime.datetime.utcnow()
        for o in objs:
            try:
                lm = datetime.datetime.strptime(o["last_modified"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            age_seconds = (now - lm).total_seconds()
            for prefix, days in rules:
                if not o["key"].startswith(prefix):
                    continue
                if age_seconds >= days * secs_per_day:
                    if versioning == "Enabled":
                        # Insert delete marker
                        dm_vid = gen_version_id()
                        conn.execute(
                            "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                            (bucket, o["key"]),
                        )
                        conn.execute(
                            "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, storage_path, created_at, last_modified) "
                            "VALUES (?,?,?,1,1,0,'',NULL,?,?)",
                            (bucket, o["key"], dm_vid, now_iso(), now_iso()),
                        )
                    else:
                        cur2 = conn.execute(
                            "SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                            (bucket, o["key"]),
                        )
                        rows = cur2.fetchall()
                        conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, o["key"]))
                        for r in rows:
                            if r["storage_path"]:
                                safe_remove(r["storage_path"])
                    break
    finally:
        conn.close()


def _outbox_loop():
    while True:
        try:
            _drain_outbox()
        except Exception:
            pass
        time.sleep(1.0)


def _drain_outbox():
    conn = _connect()
    try:
        now_t = time.time()
        cur = conn.execute(
            "SELECT id, endpoint, payload, attempts FROM notification_outbox WHERE next_attempt <= ? AND attempts < 5",
            (now_t,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for r in rows:
        delivered = _try_deliver(r["endpoint"], r["payload"])
        conn = _connect()
        try:
            if delivered:
                conn.execute("DELETE FROM notification_outbox WHERE id=?", (r["id"],))
            else:
                attempts = r["attempts"] + 1
                if attempts >= 5:
                    conn.execute("DELETE FROM notification_outbox WHERE id=?", (r["id"],))
                else:
                    backoff = 2 ** (attempts - 1)
                    conn.execute(
                        "UPDATE notification_outbox SET attempts=?, next_attempt=? WHERE id=?",
                        (attempts, time.time() + backoff, r["id"]),
                    )
        finally:
            conn.close()


def _try_deliver(endpoint, payload):
    try:
        req = urllib.request.Request(
            endpoint,
            data=payload.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
