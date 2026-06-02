import datetime
import hashlib
import os
import re
import secrets
import uuid


DATA_DIR = os.environ.get("HALYARD_DATA", "/app/data")


def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def now_iso_ms():
    return datetime.datetime.utcnow().isoformat()[:-3] + "Z"


def utc_now_ts():
    return datetime.datetime.utcnow().timestamp()


def gen_request_id():
    return uuid.uuid4().hex[:16].upper()


def gen_version_id():
    # 32-char base32-like
    return secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")[:32]


def gen_upload_id():
    return secrets.token_urlsafe(48).replace("=", "")


def gen_access_key_id():
    return "AKIA" + secrets.token_hex(8).upper()


def gen_secret_key():
    return secrets.token_urlsafe(30).replace("-", "x").replace("_", "y")[:40]


def gen_session_token():
    return secrets.token_urlsafe(32)


_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")


def valid_bucket_name(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 63:
        return False
    if not _BUCKET_NAME_RE.match(name):
        return False
    if ".." in name:
        return False
    if ".-" in name or "-." in name:
        return False
    # not IP address
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
        return False
    return True


def valid_tenant_name(name: str) -> bool:
    return bool(_TENANT_NAME_RE.match(name or ""))


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def storage_path_for(version_id: str) -> str:
    base = os.path.join(DATA_DIR, "objects", version_id[:2], version_id[2:4])
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, version_id)


def part_storage_path(upload_id: str, part_number: int) -> str:
    base = os.path.join(DATA_DIR, "parts", upload_id)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{part_number:05d}")


def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def safe_rmtree(path):
    import shutil
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path)
    except OSError:
        pass
