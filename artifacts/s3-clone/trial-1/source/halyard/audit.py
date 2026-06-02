import json
import os
import threading

from .util import now_iso_ms, DATA_DIR

AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")
_audit_lock = threading.Lock()


def write_event(tenant, access_key_id, method, path, bucket, key, status, request_id):
    event = {
        "ts": now_iso_ms(),
        "tenant": tenant,
        "access_key_id": access_key_id,
        "method": method,
        "path": path,
        "bucket": bucket,
        "key": key,
        "status": int(status),
        "request_id": request_id,
    }
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with _audit_lock:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    # also store in DB for fast filtered query
    try:
        from .db import get_conn
        conn = get_conn()
        conn.execute(
            "INSERT INTO audit_events (ts, tenant, access_key_id, method, path, bucket, key, status, request_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (event["ts"], tenant, access_key_id, method, path, bucket, key, int(status), request_id),
        )
    except Exception:
        pass
    return event


def list_events(limit=100, tenant=None):
    from .db import get_conn
    conn = get_conn()
    if tenant:
        cur = conn.execute(
            "SELECT ts, tenant, access_key_id, method, path, bucket, key, status, request_id FROM audit_events WHERE tenant = ? ORDER BY id DESC LIMIT ?",
            (tenant, limit),
        )
    else:
        cur = conn.execute(
            "SELECT ts, tenant, access_key_id, method, path, bucket, key, status, request_id FROM audit_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]
