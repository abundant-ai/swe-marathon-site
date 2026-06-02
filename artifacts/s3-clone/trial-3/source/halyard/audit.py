import os
import json
import threading
from .util import now_iso

AUDIT_PATH = os.environ.get('HALYARD_AUDIT_PATH', '/app/data/audit.log')
_lock = threading.Lock()

def _ensure():
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)

def log_event(*, tenant, access_key_id, method, path, bucket, key, status, request_id):
    _ensure()
    rec = {
        'ts': now_iso(),
        'tenant': tenant,
        'access_key_id': access_key_id,
        'method': method,
        'path': path,
        'bucket': bucket,
        'key': key,
        'status': int(status),
        'request_id': request_id,
    }
    line = json.dumps(rec, separators=(',', ':')) + '\n'
    with _lock:
        with open(AUDIT_PATH, 'a') as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

def read_events(limit=100, tenant=None):
    _ensure()
    if not os.path.exists(AUDIT_PATH):
        return []
    out = []
    with open(AUDIT_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if tenant and rec.get('tenant') != tenant:
                continue
            out.append(rec)
    out.reverse()
    return out[:limit]
