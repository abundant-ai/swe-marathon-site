#!/usr/bin/env python3
import base64
import datetime as dt
import fnmatch
import hashlib
import hmac
import json
import os
import queue
import random
import re
import shutil
import sqlite3
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DATA_DIR = "/app/data"
BLOB_DIR = os.path.join(DATA_DIR, "blobs")
PART_DIR = os.path.join(DATA_DIR, "parts")
DB_PATH = os.path.join(DATA_DIR, "state.sqlite3")
AUDIT_PATH = os.path.join(DATA_DIR, "audit.log")
REGION = "us-east-1"
SERVICE = "s3"
DEFAULT_TENANT = "default"
DEFAULT_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
DEFAULT_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ADMIN_TOKEN = os.environ.get("HALYARD_ADMIN_TOKEN", "halyard-admin-dev-token")
SESSION_SECRET = os.environ.get("HALYARD_SESSION_SECRET", "halyard-session-dev-secret")
LIFECYCLE_TICK = float(os.environ.get("S3CLONE_LIFECYCLE_TICK_SECONDS", "2"))
SECONDS_PER_DAY = float(os.environ.get("S3CLONE_LIFECYCLE_SECONDS_PER_DAY", "86400"))

audit_lock = threading.Lock()
notify_event = threading.Event()


ERROR_STATUS = {
    "NoSuchBucket": 404,
    "NoSuchKey": 404,
    "NoSuchVersion": 404,
    "NoSuchUpload": 404,
    "NoSuchCORSConfiguration": 404,
    "NoSuchLifecycleConfiguration": 404,
    "NoSuchBucketPolicy": 404,
    "BucketAlreadyOwnedByYou": 409,
    "BucketAlreadyExists": 409,
    "BucketNotEmpty": 409,
    "SignatureDoesNotMatch": 403,
    "InvalidAccessKeyId": 403,
    "AccessDenied": 403,
    "CORSForbidden": 403,
    "QuotaExceeded": 403,
    "TooManyBuckets": 403,
    "InvalidArgument": 400,
    "InvalidBucketName": 400,
    "InvalidPart": 400,
    "InvalidPartOrder": 400,
    "InvalidTag": 400,
    "EntityTooLarge": 400,
    "EntityTooSmall": 400,
    "PreconditionFailed": 412,
    "InvalidRange": 416,
    "MalformedXML": 400,
    "MalformedPolicy": 400,
}


class S3Error(Exception):
    def __init__(self, code, message=None, status=None):
        super().__init__(code)
        self.code = code
        self.message = message or code
        self.status = status or ERROR_STATUS.get(code, 400)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts=None):
    if ts is None:
        ts = now()
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return now()
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def xml_escape(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def child_text(elem, name, default=""):
    if elem is None:
        return default
    for c in list(elem):
        if strip_ns(c.tag) == name:
            return c.text or ""
    return default


def children(elem, name):
    if elem is None:
        return []
    return [c for c in list(elem) if strip_ns(c.tag) == name]


def parse_xml(body):
    try:
        return ET.fromstring(body or b"<Empty/>")
    except ET.ParseError:
        raise S3Error("MalformedXML")


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BLOB_DIR, exist_ok=True)
    os.makedirs(PART_DIR, exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    ensure_dirs()
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tenants (
          name TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          quota_bytes INTEGER NULL,
          quota_buckets INTEGER NULL
        );
        CREATE TABLE IF NOT EXISTS access_keys (
          access_key_id TEXT PRIMARY KEY,
          secret_access_key TEXT NOT NULL,
          tenant TEXT NOT NULL REFERENCES tenants(name) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS buckets (
          name TEXT PRIMARY KEY,
          tenant TEXT NOT NULL REFERENCES tenants(name) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          versioning TEXT NOT NULL DEFAULT '',
          cors_xml TEXT NULL,
          lifecycle_xml TEXT NULL,
          lifecycle_json TEXT NULL,
          policy_json TEXT NULL,
          notification_xml TEXT NULL,
          notification_json TEXT NULL
        );
        CREATE TABLE IF NOT EXISTS objects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          bucket TEXT NOT NULL REFERENCES buckets(name) ON DELETE CASCADE,
          key TEXT NOT NULL,
          version_id TEXT NULL,
          is_latest INTEGER NOT NULL,
          is_delete_marker INTEGER NOT NULL,
          data_path TEXT NULL,
          size INTEGER NOT NULL,
          etag TEXT NULL,
          content_type TEXT NULL,
          metadata_json TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          last_modified TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_objects_bucket_key_latest ON objects(bucket, key, is_latest);
        CREATE INDEX IF NOT EXISTS idx_objects_bucket_key_version ON objects(bucket, key, version_id);
        CREATE TABLE IF NOT EXISTS uploads (
          upload_id TEXT PRIMARY KEY,
          bucket TEXT NOT NULL,
          key TEXT NOT NULL,
          tenant TEXT NOT NULL,
          initiated TEXT NOT NULL,
          content_type TEXT NULL,
          metadata_json TEXT NOT NULL,
          tags_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parts (
          upload_id TEXT NOT NULL REFERENCES uploads(upload_id) ON DELETE CASCADE,
          part_number INTEGER NOT NULL,
          etag TEXT NOT NULL,
          md5_hex TEXT NOT NULL,
          size INTEGER NOT NULL,
          data_path TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(upload_id, part_number)
        );
        CREATE TABLE IF NOT EXISTS outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          endpoint TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at REAL NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    created = iso()
    conn.execute(
        "INSERT OR IGNORE INTO tenants(name,created_at,quota_bytes,quota_buckets) VALUES(?,?,NULL,NULL)",
        (DEFAULT_TENANT, created),
    )
    conn.execute(
        """INSERT OR IGNORE INTO access_keys(access_key_id,secret_access_key,tenant,created_at,active)
           VALUES(?,?,?,?,1)""",
        (DEFAULT_ACCESS_KEY, DEFAULT_SECRET, DEFAULT_TENANT, created),
    )
    conn.close()


def random_key_id():
    alphabet = string.ascii_uppercase + string.digits
    return "HKIA" + "".join(random.choice(alphabet) for _ in range(16))


def random_secret():
    raw = os.urandom(30)
    return base64.b64encode(raw).decode("ascii")


def validate_bucket_name(name):
    if not isinstance(name, str) or len(name) < 3 or len(name) > 63:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", name):
        return False
    if ".." in name or ".-" in name or "-." in name:
        return False
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name):
        return False
    return True


def validate_tenant_name(name):
    return isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]", name or "") is not None


def qd(query):
    return urllib.parse.parse_qs(query, keep_blank_values=True)


def query_has(q, name):
    return name in q


def first(q, name, default=None):
    vals = q.get(name)
    return vals[0] if vals else default


def parse_path(path):
    parts = path.split("/", 2)
    if path == "/" or path == "":
        return None, None
    bucket = urllib.parse.unquote(parts[1]) if len(parts) > 1 else None
    key = urllib.parse.unquote(parts[2]) if len(parts) > 2 else None
    return bucket, key


def quote_etag(hex_or_quoted):
    if hex_or_quoted is None:
        return None
    s = str(hex_or_quoted)
    return s if s.startswith('"') else f'"{s}"'


def md5_hexdigest(data):
    return hashlib.md5(data).hexdigest()


def sha256_hexdigest(data):
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret, datestamp, region=REGION, service=SERVICE):
    k_date = hmac_sha256(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    return hmac_sha256(k_service, "aws4_request")


def canonical_uri(path):
    return urllib.parse.quote(urllib.parse.unquote(path or "/"), safe="/-_.~")


def aws_quote(s):
    return urllib.parse.quote(str(s), safe="-_.~")


def canonical_query(params, exclude_sig=False):
    pairs = []
    for k, vals in params.items():
        if exclude_sig and k == "X-Amz-Signature":
            continue
        for v in vals:
            pairs.append((aws_quote(k), aws_quote(v)))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def canonical_headers(handler, signed_headers):
    lines = []
    for h in signed_headers:
        value = handler.headers.get(h, "")
        value = " ".join(value.strip().split())
        lines.append(f"{h}:{value}\n")
    return "".join(lines)


def parse_authorization(value):
    if not value or not value.startswith("AWS4-HMAC-SHA256 "):
        return None
    rest = value[len("AWS4-HMAC-SHA256 ") :]
    out = {}
    for piece in rest.split(","):
        if "=" not in piece:
            continue
        k, v = piece.strip().split("=", 1)
        out[k] = v
    return out


def parse_amz_date(value):
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        raise S3Error("AccessDenied", "Invalid X-Amz-Date")


def check_clock(ts):
    if abs((now() - ts).total_seconds()) > 900:
        raise S3Error("AccessDenied", "Request time skewed")


def parse_tagging_header(value):
    if not value:
        return {}
    tags = {}
    for pair in value.split("&"):
        if not pair:
            continue
        if "=" not in pair:
            raise S3Error("InvalidTag")
        k, v = pair.split("=", 1)
        k = urllib.parse.unquote_plus(k)
        v = urllib.parse.unquote_plus(v)
        if not k or len(tags) >= 10 or k in tags:
            raise S3Error("InvalidTag")
        tags[k] = v
    return tags


def tags_to_xml(tags):
    body = "<Tagging><TagSet>"
    for k, v in (tags or {}).items():
        body += f"<Tag><Key>{xml_escape(k)}</Key><Value>{xml_escape(v)}</Value></Tag>"
    return body + "</TagSet></Tagging>"


def parse_tagging_xml(body):
    root = parse_xml(body)
    tagset = None
    if strip_ns(root.tag) == "Tagging":
        for c in root.iter():
            if strip_ns(c.tag) == "TagSet":
                tagset = c
                break
    if tagset is None:
        return {}
    tags = {}
    for tag in children(tagset, "Tag"):
        k = child_text(tag, "Key")
        v = child_text(tag, "Value")
        if not k or len(tags) >= 10 or k in tags:
            raise S3Error("InvalidTag")
        tags[k] = v
    return tags


def collect_metadata(headers):
    meta = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith("x-amz-meta-"):
            meta[lk[len("x-amz-meta-") :]] = v
    return meta


def append_audit(event):
    line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    with audit_lock:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def tenant_for_key(conn, access_key_id):
    if not access_key_id:
        return None
    row = conn.execute(
        "SELECT access_key_id,secret_access_key,tenant FROM access_keys WHERE access_key_id=? AND active=1",
        (access_key_id,),
    ).fetchone()
    return row


def bucket_row(conn, name):
    return conn.execute("SELECT * FROM buckets WHERE name=?", (name,)).fetchone()


def object_latest(conn, bucket, key):
    return conn.execute(
        """SELECT * FROM objects WHERE bucket=? AND key=? AND is_latest=1
           ORDER BY id DESC LIMIT 1""",
        (bucket, key),
    ).fetchone()


def object_version(conn, bucket, key, version_id):
    if version_id in (None, ""):
        return object_latest(conn, bucket, key)
    if version_id == "null":
        return conn.execute(
            "SELECT * FROM objects WHERE bucket=? AND key=? AND version_id IS NULL ORDER BY id DESC LIMIT 1",
            (bucket, key),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=? ORDER BY id DESC LIMIT 1",
        (bucket, key, version_id),
    ).fetchone()


def live_bytes_for_tenant(conn, tenant):
    row = conn.execute(
        """SELECT COALESCE(SUM(o.size),0) AS n
           FROM objects o JOIN buckets b ON b.name=o.bucket
           WHERE b.tenant=? AND o.is_latest=1 AND o.is_delete_marker=0""",
        (tenant,),
    ).fetchone()
    return int(row["n"] or 0)


def current_live_size(conn, bucket, key):
    row = object_latest(conn, bucket, key)
    if row and not row["is_delete_marker"]:
        return int(row["size"])
    return 0


def bucket_count_for_tenant(conn, tenant):
    row = conn.execute("SELECT COUNT(*) AS n FROM buckets WHERE tenant=?", (tenant,)).fetchone()
    return int(row["n"])


def check_bucket_quota(conn, tenant):
    t = conn.execute("SELECT quota_buckets FROM tenants WHERE name=?", (tenant,)).fetchone()
    if t and t["quota_buckets"] is not None and bucket_count_for_tenant(conn, tenant) >= int(t["quota_buckets"]):
        raise S3Error("TooManyBuckets")


def check_byte_quota(conn, tenant, added_bytes):
    t = conn.execute("SELECT quota_bytes FROM tenants WHERE name=?", (tenant,)).fetchone()
    if t and t["quota_bytes"] is not None and live_bytes_for_tenant(conn, tenant) + int(added_bytes) > int(t["quota_bytes"]):
        raise S3Error("QuotaExceeded")


def write_blob(data):
    name = str(uuid.uuid4())
    path = os.path.join(BLOB_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def copy_to_blob(src_path):
    name = str(uuid.uuid4())
    path = os.path.join(BLOB_DIR, name)
    tmp = path + ".tmp"
    with open(src_path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, path)
    return path


def save_object(conn, bucket, key, body, content_type, metadata, tags, tenant, versioning):
    check_byte_quota(conn, tenant, len(body) - current_live_size(conn, bucket, key))
    etag = quote_etag(md5_hexdigest(body))
    path = write_blob(body)
    created = iso()
    version_id = str(uuid.uuid4()) if versioning == "Enabled" else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if versioning == "Enabled":
            conn.execute("UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1", (bucket, key))
        else:
            conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
        conn.execute(
            """INSERT INTO objects(bucket,key,version_id,is_latest,is_delete_marker,data_path,size,etag,
                                    content_type,metadata_json,tags_json,created_at,last_modified)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bucket,
                key,
                version_id,
                1,
                0,
                path,
                len(body),
                etag,
                content_type or "binary/octet-stream",
                json.dumps(metadata or {}),
                json.dumps(tags or {}),
                created,
                created,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return etag, version_id


def insert_object_from_path(conn, bucket, key, path, size, etag, content_type, metadata, tags, tenant, versioning):
    check_byte_quota(conn, tenant, size - current_live_size(conn, bucket, key))
    new_path = copy_to_blob(path)
    created = iso()
    version_id = str(uuid.uuid4()) if versioning == "Enabled" else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if versioning == "Enabled":
            conn.execute("UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1", (bucket, key))
        else:
            conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
        conn.execute(
            """INSERT INTO objects(bucket,key,version_id,is_latest,is_delete_marker,data_path,size,etag,
                                    content_type,metadata_json,tags_json,created_at,last_modified)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bucket,
                key,
                version_id,
                1,
                0,
                new_path,
                size,
                etag,
                content_type or "binary/octet-stream",
                json.dumps(metadata or {}),
                json.dumps(tags or {}),
                created,
                created,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        try:
            os.unlink(new_path)
        except OSError:
            pass
        raise
    return version_id


def delete_object_semantics(conn, bucket, key, version_id, versioning):
    if version_id:
        row = object_version(conn, bucket, key, version_id)
        if not row:
            raise S3Error("NoSuchVersion")
        was_latest = int(row["is_latest"]) == 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM objects WHERE id=?", (row["id"],))
            if was_latest:
                prev = conn.execute(
                    "SELECT id FROM objects WHERE bucket=? AND key=? ORDER BY id DESC LIMIT 1",
                    (bucket, key),
                ).fetchone()
                if prev:
                    conn.execute("UPDATE objects SET is_latest=1 WHERE id=?", (prev["id"],))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return False, version_id
    if versioning == "Enabled":
        created = iso()
        marker_vid = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1", (bucket, key))
            conn.execute(
                """INSERT INTO objects(bucket,key,version_id,is_latest,is_delete_marker,data_path,size,etag,
                                        content_type,metadata_json,tags_json,created_at,last_modified)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bucket, key, marker_vid, 1, 1, None, 0, None, None, "{}", "{}", created, created),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True, marker_vid
    conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
    return False, None


def policy_actions_valid(policy):
    allowed = {"s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:*"}
    stmts = policy.get("Statement")
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        return False
    for st in stmts:
        actions = st.get("Action")
        if isinstance(actions, str):
            actions = [actions]
        if not isinstance(actions, list) or any(a not in allowed for a in actions):
            return False
        if st.get("Effect") not in ("Allow", "Deny"):
            return False
    return True


def principal_matches(principal, tenant):
    if principal == "*":
        return True
    if isinstance(principal, dict) and "AWS" in principal:
        aws = principal["AWS"]
        if aws == "*":
            return True
        if isinstance(aws, str):
            aws = [aws]
        if isinstance(aws, list) and tenant:
            return f"arn:aws:iam::{tenant}:root" in aws
    return False


def action_matches(rule_action, action):
    actions = rule_action if isinstance(rule_action, list) else [rule_action]
    return "s3:*" in actions or action in actions


def resource_matches(rule_resource, arn):
    resources = rule_resource if isinstance(rule_resource, list) else [rule_resource]
    for r in resources:
        if not isinstance(r, str):
            continue
        if r.endswith("*"):
            if arn.startswith(r[:-1]):
                return True
        elif r == arn:
            return True
    return False


def condition_matches(condition, context):
    if not condition:
        return True
    for op, values in condition.items():
        if op not in ("StringEquals", "StringLike"):
            return False
        if not isinstance(values, dict):
            return False
        for k, expected in values.items():
            actual = context.get(k)
            opts = expected if isinstance(expected, list) else [expected]
            if op == "StringEquals" and actual not in opts:
                return False
            if op == "StringLike" and not any(fnmatch.fnmatchcase(str(actual or ""), str(pat)) for pat in opts):
                return False
    return True


def policy_allows(bucket, bucket_row_obj, tenant, action, key=None, context=None):
    raw = bucket_row_obj["policy_json"] if bucket_row_obj else None
    if not raw:
        return False
    try:
        policy = json.loads(raw)
    except Exception:
        return False
    stmts = policy.get("Statement")
    if isinstance(stmts, dict):
        stmts = [stmts]
    arn = f"arn:aws:s3:::{bucket}" if key is None else f"arn:aws:s3:::{bucket}/{key}"
    allowed = False
    for st in stmts or []:
        if not principal_matches(st.get("Principal"), tenant):
            continue
        if not action_matches(st.get("Action"), action):
            continue
        if not resource_matches(st.get("Resource"), arn):
            continue
        if not condition_matches(st.get("Condition"), context or {}):
            continue
        if st.get("Effect") == "Deny":
            return False
        if st.get("Effect") == "Allow":
            allowed = True
    return allowed


def method_action(method, key):
    if method in ("GET", "HEAD"):
        return "s3:GetObject" if key is not None else "s3:ListBucket"
    if method == "PUT":
        return "s3:PutObject" if key is not None else None
    return None


def parse_lifecycle(xml_text):
    if not xml_text:
        return []
    root = parse_xml(xml_text.encode("utf-8"))
    rules = []
    for r in children(root, "Rule"):
        status = child_text(r, "Status")
        prefix = child_text(r, "Prefix")
        filt = next((c for c in list(r) if strip_ns(c.tag) == "Filter"), None)
        if filt is not None:
            prefix = child_text(filt, "Prefix", prefix)
        exp = next((c for c in list(r) if strip_ns(c.tag) == "Expiration"), None)
        days = child_text(exp, "Days")
        try:
            days = int(days)
        except Exception:
            continue
        rules.append({"Status": status, "Prefix": prefix or "", "Days": days})
    return rules


def parse_notification(xml_text):
    if not xml_text:
        return []
    root = parse_xml(xml_text.encode("utf-8"))
    configs = []
    for cfg in list(root):
        tag = strip_ns(cfg.tag)
        if tag not in ("CloudFunctionConfiguration", "QueueConfiguration", "TopicConfiguration"):
            continue
        endpoint = (
            child_text(cfg, "CloudFunction")
            or child_text(cfg, "Queue")
            or child_text(cfg, "Topic")
            or child_text(cfg, "Endpoint")
            or child_text(cfg, "Url")
            or child_text(cfg, "URL")
        )
        events = [e.text or "" for e in children(cfg, "Event")]
        rules = []
        for f in children(cfg, "Filter"):
            for sk in children(f, "S3Key"):
                for fr in children(sk, "FilterRule"):
                    rules.append({"Name": child_text(fr, "Name"), "Value": child_text(fr, "Value")})
        configs.append({"Id": child_text(cfg, "Id"), "Endpoint": endpoint, "Events": events, "Rules": rules})
    return configs


def notification_event_matches(want, event_name):
    if want == event_name:
        return True
    if want.endswith(":*"):
        return event_name.startswith(want[:-1])
    return False


def enqueue_notifications(conn, bucket_name, key, event_name):
    b = bucket_row(conn, bucket_name)
    if not b or not b["notification_json"]:
        return
    configs = json.loads(b["notification_json"])
    records_by_endpoint = {}
    for cfg in configs:
        endpoint = cfg.get("Endpoint")
        if not endpoint:
            continue
        if not any(notification_event_matches(ev, event_name) for ev in cfg.get("Events") or []):
            continue
        ok = True
        for rule in cfg.get("Rules") or []:
            name = (rule.get("Name") or "").lower()
            val = rule.get("Value") or ""
            if name == "prefix" and not key.startswith(val):
                ok = False
            if name == "suffix" and not key.endswith(val):
                ok = False
        if not ok:
            continue
        rec = {
            "eventVersion": "2.1",
            "eventSource": "halyard:s3",
            "eventTime": iso(),
            "eventName": event_name,
            "s3": {"bucket": {"name": bucket_name}, "object": {"key": key}},
        }
        records_by_endpoint.setdefault(endpoint, []).append(rec)
    for endpoint, records in records_by_endpoint.items():
        conn.execute(
            "INSERT INTO outbox(endpoint,payload_json,attempts,next_attempt_at,created_at) VALUES(?,?,?,?,?)",
            (endpoint, json.dumps({"Records": records}), 0, time.time(), iso()),
        )
    if records_by_endpoint:
        notify_event.set()


def auth_cookie(access_key_id):
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), access_key_id.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{access_key_id}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def parse_cookie(header):
    cookies = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def cookie_access_key(cookie_value):
    if not cookie_value:
        return None
    try:
        raw = base64.urlsafe_b64decode(cookie_value.encode("ascii")).decode("utf-8")
        access_key_id, sig = raw.split(":", 1)
    except Exception:
        return None
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), access_key_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return access_key_id


class HalyardHandler(BaseHTTPRequestHandler):
    server_version = "Halyard/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_OPTIONS(self):
        self.dispatch()

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def read_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length) if length else b""

    def send_body(self, status, body=b"", headers=None, content_type=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = dict(headers or {})
        lower_keys = {k.lower() for k in headers}
        self.send_response(status)
        self.send_header("x-amz-request-id", getattr(self, "request_id", str(uuid.uuid4())))
        if content_type and "content-type" not in lower_keys:
            self.send_header("Content-Type", content_type)
        for k, v in headers.items():
            self.send_header(k, str(v))
        if "content-length" not in lower_keys:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def send_xml(self, status, body, headers=None):
        self.send_body(status, body, headers, "application/xml")

    def send_json(self, status, obj, headers=None):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_body(status, body, headers, "application/json")

    def send_s3_error(self, err):
        body = (
            f"<Error><Code>{xml_escape(err.code)}</Code><Message>{xml_escape(err.message)}</Message>"
            f"<RequestId>{xml_escape(getattr(self, 'request_id', ''))}</RequestId></Error>"
        )
        self.send_xml(err.status, body)
        return err.status

    def dispatch(self):
        self.request_id = str(uuid.uuid4())
        self.audit_tenant = None
        self.audit_access_key = None
        self.audit_bucket = None
        self.audit_key = None
        status = 500
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path == "/_health":
                status = 200
                self.send_json(200, {"status": "ok"})
            elif parsed.path.startswith("/_admin"):
                status = self.handle_admin(parsed)
            elif parsed.path.startswith("/console"):
                status = self.handle_console(parsed)
            else:
                status = self.handle_s3(parsed)
        except S3Error as e:
            status = self.send_s3_error(e)
        except Exception as e:
            status = 500
            if parsed.path.startswith("/_admin") or parsed.path.startswith("/console/api"):
                self.send_json(500, {"error": "InternalError", "message": str(e)})
            else:
                self.send_s3_error(S3Error("InternalError", "InternalError", 500))
        finally:
            if self.command in ("PUT", "POST", "DELETE") and (not parsed.path.startswith("/console")):
                if parsed.path.startswith("/_admin"):
                    if self.command in ("POST", "DELETE"):
                        self.write_audit(status, parsed, admin=True)
                elif not parsed.path.startswith("/_health"):
                    self.write_audit(status, parsed, admin=False)

    def write_audit(self, status, parsed, admin=False):
        bucket, key = parse_path(parsed.path)
        if admin:
            bucket = None
            key = None
        event = {
            "ts": iso(),
            "tenant": self.audit_tenant,
            "access_key_id": self.audit_access_key,
            "method": self.command,
            "path": self.path,
            "bucket": self.audit_bucket if self.audit_bucket is not None else bucket,
            "key": self.audit_key if self.audit_key is not None else key,
            "status": int(status),
            "request_id": self.request_id,
        }
        append_audit(event)

    def authenticate_s3(self, conn, parsed, body, optional=False):
        q = qd(parsed.query)
        auth = self.headers.get("Authorization")
        access_key_id = None
        tenant = None
        keyrow = None
        if "X-Amz-Algorithm" in q:
            if first(q, "X-Amz-Algorithm") != "AWS4-HMAC-SHA256":
                raise S3Error("SignatureDoesNotMatch")
            cred = first(q, "X-Amz-Credential")
            sig = first(q, "X-Amz-Signature")
            amz_date = first(q, "X-Amz-Date")
            expires = int(first(q, "X-Amz-Expires", "0"))
            signed_headers = first(q, "X-Amz-SignedHeaders", "host").split(";")
            if not cred or not sig or not amz_date:
                raise S3Error("AccessDenied")
            parts = cred.split("/")
            access_key_id = parts[0]
            keyrow = tenant_for_key(conn, access_key_id)
            if not keyrow:
                raise S3Error("InvalidAccessKeyId")
            tenant = keyrow["tenant"]
            ts = parse_amz_date(amz_date)
            if (now() - ts).total_seconds() > expires or (ts - now()).total_seconds() > 900:
                raise S3Error("AccessDenied", "Request has expired")
            payload_hash = first(q, "X-Amz-Content-Sha256", "UNSIGNED-PAYLOAD")
            can_req = "\n".join(
                [
                    self.command,
                    canonical_uri(parsed.path),
                    canonical_query(q, exclude_sig=True),
                    canonical_headers(self, signed_headers),
                    ";".join(signed_headers),
                    payload_hash,
                ]
            )
            scope = "/".join(parts[1:5])
            sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, sha256_hexdigest(can_req.encode("utf-8"))])
            expected = hmac.new(signing_key(keyrow["secret_access_key"], parts[1], parts[2], parts[3]), sts.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig):
                raise S3Error("SignatureDoesNotMatch")
        elif auth:
            parsed_auth = parse_authorization(auth)
            if not parsed_auth:
                raise S3Error("AccessDenied")
            cred = parsed_auth.get("Credential")
            sig = parsed_auth.get("Signature")
            signed_headers = (parsed_auth.get("SignedHeaders") or "").split(";")
            if not cred or not sig or not signed_headers:
                raise S3Error("AccessDenied")
            parts = cred.split("/")
            access_key_id = parts[0]
            keyrow = tenant_for_key(conn, access_key_id)
            if not keyrow:
                raise S3Error("InvalidAccessKeyId")
            tenant = keyrow["tenant"]
            amz_date = self.headers.get("x-amz-date") or self.headers.get("date")
            if not amz_date:
                raise S3Error("AccessDenied")
            ts = parse_amz_date(amz_date) if "T" in amz_date else now()
            if "T" in amz_date:
                check_clock(ts)
            payload_hash = self.headers.get("x-amz-content-sha256") or sha256_hexdigest(body)
            can_req = "\n".join(
                [
                    self.command,
                    canonical_uri(parsed.path),
                    canonical_query(q),
                    canonical_headers(self, signed_headers),
                    ";".join(signed_headers),
                    payload_hash,
                ]
            )
            scope = "/".join(parts[1:5])
            sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, sha256_hexdigest(can_req.encode("utf-8"))])
            expected = hmac.new(signing_key(keyrow["secret_access_key"], parts[1], parts[2], parts[3]), sts.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig):
                raise S3Error("SignatureDoesNotMatch")
        elif optional:
            return None, None
        else:
            raise S3Error("AccessDenied")
        self.audit_tenant = tenant
        self.audit_access_key = access_key_id
        return tenant, access_key_id

    def require_bucket_access(self, conn, bucket_name, tenant, method, key=None, owner_only=False, context=None):
        b = bucket_row(conn, bucket_name)
        if not b:
            raise S3Error("NoSuchBucket")
        self.audit_bucket = bucket_name
        self.audit_key = key
        if tenant and b["tenant"] == tenant:
            return b
        if owner_only:
            raise S3Error("AccessDenied")
        action = method_action(method, key)
        ctx = dict(context or {})
        if tenant:
            ctx.setdefault("aws:PrincipalArn", f"arn:aws:iam::{tenant}:root")
        if action and policy_allows(bucket_name, b, tenant, action, key, ctx):
            return b
        raise S3Error("AccessDenied")

    def handle_s3(self, parsed):
        q = qd(parsed.query)
        bucket, key = parse_path(parsed.path)
        body = b"" if self.command in ("GET", "HEAD", "OPTIONS") else self.read_body()
        conn = db()
        try:
            if self.command == "OPTIONS":
                return self.handle_options(conn, bucket, key)
            optional = self.command in ("GET", "HEAD")
            tenant, access_key_id = self.authenticate_s3(conn, parsed, body, optional=optional)
            if bucket is None:
                if self.command == "GET":
                    if not tenant:
                        raise S3Error("AccessDenied")
                    return self.list_buckets(conn, tenant)
                raise S3Error("InvalidArgument")
            if key is None:
                return self.handle_bucket(conn, bucket, q, tenant, body)
            return self.handle_object(conn, bucket, key, q, tenant, body)
        finally:
            conn.close()

    def handle_bucket(self, conn, bucket, q, tenant, body):
        if self.command == "PUT" and not q:
            if not tenant:
                raise S3Error("AccessDenied")
            return self.create_bucket(conn, bucket, tenant)
        ctx = {"s3:prefix": first(q, "prefix", "") or "", "s3:delimiter": first(q, "delimiter", "") or ""}
        b = self.require_bucket_access(conn, bucket, tenant, self.command, None, owner_only=self.bucket_subresource_owner_only(q), context=ctx)
        if self.command == "HEAD":
            self.send_body(200, b"", {})
            return 200
        if self.command == "DELETE" and not q:
            return self.delete_bucket(conn, bucket, b)
        if self.command == "GET" and (query_has(q, "list-type") or not q):
            return self.list_objects_v2(conn, bucket, q)
        if query_has(q, "versions") and self.command == "GET":
            return self.list_versions(conn, bucket, q)
        if query_has(q, "uploads") and self.command == "GET":
            return self.list_multipart_uploads(conn, bucket)
        if query_has(q, "delete") and self.command == "POST":
            return self.multi_delete(conn, bucket, b, body)
        if query_has(q, "versioning"):
            return self.versioning(conn, bucket, body)
        if query_has(q, "cors"):
            return self.cors(conn, bucket, body)
        if query_has(q, "lifecycle"):
            return self.lifecycle(conn, bucket, body)
        if query_has(q, "policy"):
            return self.policy(conn, bucket, body)
        if query_has(q, "notification"):
            return self.notification(conn, bucket, body)
        if query_has(q, "location") and self.command == "GET":
            self.send_xml(200, '<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>')
            return 200
        raise S3Error("InvalidArgument")

    def bucket_subresource_owner_only(self, q):
        for name in ("versioning", "cors", "lifecycle", "policy", "notification"):
            if query_has(q, name):
                return True
        return False

    def create_bucket(self, conn, bucket, tenant):
        if not validate_bucket_name(bucket):
            raise S3Error("InvalidBucketName")
        existing = bucket_row(conn, bucket)
        if existing:
            if existing["tenant"] == tenant:
                raise S3Error("BucketAlreadyOwnedByYou")
            raise S3Error("BucketAlreadyExists")
        check_bucket_quota(conn, tenant)
        conn.execute(
            "INSERT INTO buckets(name,tenant,created_at) VALUES(?,?,?)",
            (bucket, tenant, iso()),
        )
        self.send_xml(200, "")
        return 200

    def delete_bucket(self, conn, bucket, b):
        rows = conn.execute("SELECT 1 FROM objects WHERE bucket=? LIMIT 1", (bucket,)).fetchone()
        uploads = conn.execute("SELECT 1 FROM uploads WHERE bucket=? LIMIT 1", (bucket,)).fetchone()
        if rows or uploads:
            raise S3Error("BucketNotEmpty")
        conn.execute("DELETE FROM buckets WHERE name=?", (bucket,))
        self.send_xml(204, "")
        return 204

    def list_buckets(self, conn, tenant):
        rows = conn.execute("SELECT name,created_at FROM buckets WHERE tenant=? ORDER BY name", (tenant,)).fetchall()
        buckets = "".join(
            f"<Bucket><Name>{xml_escape(r['name'])}</Name><CreationDate>{xml_escape(r['created_at'])}</CreationDate></Bucket>"
            for r in rows
        )
        body = (
            '<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Owner><ID>{xml_escape(tenant)}</ID><DisplayName>{xml_escape(tenant)}</DisplayName></Owner>"
            f"<Buckets>{buckets}</Buckets></ListAllMyBucketsResult>"
        )
        self.send_xml(200, body)
        return 200

    def list_objects_v2(self, conn, bucket, q):
        prefix = first(q, "prefix", "") or ""
        delimiter = first(q, "delimiter")
        token = first(q, "continuation-token")
        start_after = first(q, "start-after", "") or ""
        max_keys = min(int(first(q, "max-keys", "1000") or "1000"), 1000)
        marker = token or start_after
        rows = conn.execute(
            """SELECT key,size,etag,last_modified FROM objects
               WHERE bucket=? AND is_latest=1 AND is_delete_marker=0 AND key LIKE ?
               ORDER BY key""",
            (bucket, prefix + "%"),
        ).fetchall()
        contents = []
        common = set()
        for r in rows:
            k = r["key"]
            if marker and k <= marker:
                continue
            if delimiter:
                rest = k[len(prefix) :]
                if delimiter in rest:
                    common.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                    continue
            contents.append(r)
        combined = [("p", p) for p in sorted(common)] + [("o", r) for r in contents]
        combined.sort(key=lambda x: x[1] if x[0] == "p" else x[1]["key"])
        page = combined[:max_keys]
        truncated = len(combined) > max_keys
        next_token = ""
        if truncated:
            last = page[-1]
            next_token = last[1] if last[0] == "p" else last[1]["key"]
        xml = [f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
        xml.append(f"<Name>{xml_escape(bucket)}</Name><Prefix>{xml_escape(prefix)}</Prefix><KeyCount>{len(page)}</KeyCount><MaxKeys>{max_keys}</MaxKeys>")
        if delimiter:
            xml.append(f"<Delimiter>{xml_escape(delimiter)}</Delimiter>")
        xml.append(f"<IsTruncated>{str(truncated).lower()}</IsTruncated>")
        if truncated:
            xml.append(f"<NextContinuationToken>{xml_escape(next_token)}</NextContinuationToken>")
        for kind, item in page:
            if kind == "p":
                xml.append(f"<CommonPrefixes><Prefix>{xml_escape(item)}</Prefix></CommonPrefixes>")
            else:
                xml.append(
                    f"<Contents><Key>{xml_escape(item['key'])}</Key><LastModified>{xml_escape(item['last_modified'])}</LastModified>"
                    f"<ETag>{xml_escape(item['etag'])}</ETag><Size>{item['size']}</Size><StorageClass>STANDARD</StorageClass></Contents>"
                )
        xml.append("</ListBucketResult>")
        self.send_xml(200, "".join(xml))
        return 200

    def list_versions(self, conn, bucket, q):
        rows = conn.execute("SELECT * FROM objects WHERE bucket=? ORDER BY key,id DESC", (bucket,)).fetchall()
        xml = [f'<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>{xml_escape(bucket)}</Name>']
        for r in rows:
            tag = "DeleteMarker" if r["is_delete_marker"] else "Version"
            xml.append(
                f"<{tag}><Key>{xml_escape(r['key'])}</Key><VersionId>{xml_escape(r['version_id'] or 'null')}</VersionId>"
                f"<IsLatest>{str(bool(r['is_latest'])).lower()}</IsLatest><LastModified>{xml_escape(r['last_modified'])}</LastModified>"
            )
            if not r["is_delete_marker"]:
                xml.append(f"<ETag>{xml_escape(r['etag'])}</ETag><Size>{r['size']}</Size><StorageClass>STANDARD</StorageClass>")
            xml.append(f"</{tag}>")
        xml.append("</ListVersionsResult>")
        self.send_xml(200, "".join(xml))
        return 200

    def handle_object(self, conn, bucket, key, q, tenant, body):
        owner_only = False
        if query_has(q, "uploadId") or query_has(q, "tagging"):
            owner_only = False
        b = self.require_bucket_access(conn, bucket, tenant, self.command, key, owner_only=owner_only)
        if self.command == "POST" and query_has(q, "uploads"):
            return self.initiate_multipart(conn, b, bucket, key)
        if query_has(q, "uploadId"):
            upload_id = first(q, "uploadId")
            if self.command == "PUT" and query_has(q, "partNumber"):
                return self.upload_part(conn, bucket, key, upload_id, int(first(q, "partNumber")), body)
            if self.command == "POST":
                return self.complete_multipart(conn, b, bucket, key, upload_id, body)
            if self.command == "DELETE":
                return self.abort_multipart(conn, upload_id)
            if self.command == "GET":
                return self.list_parts(conn, upload_id)
        if query_has(q, "tagging"):
            return self.object_tagging(conn, bucket, key, q, body)
        if self.command == "GET" or self.command == "HEAD":
            return self.get_object(conn, bucket, key, q)
        if self.command == "PUT":
            if self.headers.get("x-amz-copy-source"):
                return self.copy_object(conn, b, bucket, key, tenant)
            return self.put_object(conn, b, bucket, key, tenant, body)
        if self.command == "DELETE":
            return self.delete_object(conn, b, bucket, key, q)
        raise S3Error("InvalidArgument")

    def put_object(self, conn, b, bucket, key, tenant, body):
        cur = object_latest(conn, bucket, key)
        self.check_write_condition(cur)
        metadata = collect_metadata(self.headers)
        tags = parse_tagging_header(self.headers.get("x-amz-tagging"))
        content_type = self.headers.get("Content-Type") or "binary/octet-stream"
        etag, version_id = save_object(conn, bucket, key, body, content_type, metadata, tags, tenant, b["versioning"])
        enqueue_notifications(conn, bucket, key, "s3:ObjectCreated:Put")
        headers = {"ETag": etag}
        if version_id:
            headers["x-amz-version-id"] = version_id
        self.send_xml(200, "", headers)
        return 200

    def check_write_condition(self, cur):
        inm = self.headers.get("If-None-Match")
        im = self.headers.get("If-Match")
        exists = cur is not None and not cur["is_delete_marker"]
        if inm == "*" and exists:
            raise S3Error("PreconditionFailed")
        if im and (not exists or quote_etag(cur["etag"]) != quote_etag(im)):
            raise S3Error("PreconditionFailed")

    def get_object(self, conn, bucket, key, q):
        version_id = first(q, "versionId")
        row = object_version(conn, bucket, key, version_id)
        if not row:
            raise S3Error("NoSuchKey" if not version_id else "NoSuchVersion")
        if row["is_delete_marker"]:
            raise S3Error("NoSuchKey")
        if self.headers.get("If-Match") and quote_etag(self.headers.get("If-Match")) != row["etag"]:
            raise S3Error("PreconditionFailed")
        if self.headers.get("If-None-Match") and quote_etag(self.headers.get("If-None-Match")) == row["etag"]:
            self.send_body(304, b"", {"ETag": row["etag"]})
            return 304
        size = int(row["size"])
        start, end = 0, size - 1
        status = 200
        headers = {
            "ETag": row["etag"],
            "Last-Modified": row["last_modified"],
            "Accept-Ranges": "bytes",
            "Content-Type": row["content_type"] or "binary/octet-stream",
        }
        if row["version_id"]:
            headers["x-amz-version-id"] = row["version_id"]
        for mk, mv in json.loads(row["metadata_json"] or "{}").items():
            headers["x-amz-meta-" + mk] = mv
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m:
                raise S3Error("InvalidRange")
            a, bnd = m.group(1), m.group(2)
            if a == "" and bnd == "":
                raise S3Error("InvalidRange")
            if a == "":
                n = int(bnd)
                start = max(size - n, 0)
                end = size - 1
            else:
                start = int(a)
                end = int(bnd) if bnd else size - 1
            if start >= size or end < start:
                raise S3Error("InvalidRange")
            end = min(end, size - 1)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1) if size else 0
        headers["Content-Length"] = str(length)
        with open(row["data_path"], "rb") as f:
            f.seek(start)
            data = f.read(length)
        self.send_body(status, data, headers, row["content_type"] or "binary/octet-stream")
        return status

    def delete_object(self, conn, b, bucket, key, q):
        marker, vid = delete_object_semantics(conn, bucket, key, first(q, "versionId"), b["versioning"])
        enqueue_notifications(conn, bucket, key, "s3:ObjectRemoved:Delete")
        headers = {}
        if marker:
            headers["x-amz-delete-marker"] = "true"
        if vid:
            headers["x-amz-version-id"] = vid
        self.send_xml(204, "", headers)
        return 204

    def copy_object(self, conn, dest_bucket_row, dest_bucket, dest_key, tenant):
        raw = self.headers.get("x-amz-copy-source")
        src = urllib.parse.unquote(raw.lstrip("/"))
        if "?" in src:
            src_path, src_qs = src.split("?", 1)
            src_q = qd(src_qs)
        else:
            src_path, src_q = src, {}
        src_bucket, _, src_key = src_path.partition("/")
        if not src_bucket or not src_key:
            raise S3Error("InvalidArgument")
        sb = bucket_row(conn, src_bucket)
        if not sb:
            raise S3Error("NoSuchBucket")
        if tenant != sb["tenant"] and not policy_allows(src_bucket, sb, tenant, "s3:GetObject", src_key):
            raise S3Error("AccessDenied")
        src_obj = object_version(conn, src_bucket, src_key, first(src_q, "versionId"))
        if not src_obj or src_obj["is_delete_marker"]:
            raise S3Error("NoSuchKey")
        self.check_write_condition(object_latest(conn, dest_bucket, dest_key))
        meta_directive = (self.headers.get("x-amz-metadata-directive") or "COPY").upper()
        tag_directive = (self.headers.get("x-amz-tagging-directive") or "COPY").upper()
        metadata = collect_metadata(self.headers) if meta_directive == "REPLACE" else json.loads(src_obj["metadata_json"] or "{}")
        tags = parse_tagging_header(self.headers.get("x-amz-tagging")) if tag_directive == "REPLACE" else json.loads(src_obj["tags_json"] or "{}")
        ctype = self.headers.get("Content-Type") if meta_directive == "REPLACE" else src_obj["content_type"]
        vid = insert_object_from_path(
            conn,
            dest_bucket,
            dest_key,
            src_obj["data_path"],
            int(src_obj["size"]),
            src_obj["etag"],
            ctype,
            metadata,
            tags,
            tenant,
            dest_bucket_row["versioning"],
        )
        enqueue_notifications(conn, dest_bucket, dest_key, "s3:ObjectCreated:Put")
        headers = {"ETag": src_obj["etag"]}
        if vid:
            headers["x-amz-version-id"] = vid
        result = (
            '<CopyObjectResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<LastModified>{iso()}</LastModified><ETag>{xml_escape(src_obj['etag'])}</ETag></CopyObjectResult>"
        )
        self.send_xml(200, result, headers)
        return 200

    def object_tagging(self, conn, bucket, key, q, body):
        row = object_version(conn, bucket, key, first(q, "versionId"))
        if not row or row["is_delete_marker"]:
            raise S3Error("NoSuchKey")
        if self.command == "GET":
            self.send_xml(200, tags_to_xml(json.loads(row["tags_json"] or "{}")))
            return 200
        if self.command == "PUT":
            tags = parse_tagging_xml(body)
            conn.execute("UPDATE objects SET tags_json=? WHERE id=?", (json.dumps(tags), row["id"]))
            self.send_xml(200, "")
            return 200
        if self.command == "DELETE":
            conn.execute("UPDATE objects SET tags_json='{}' WHERE id=?", (row["id"],))
            self.send_xml(204, "")
            return 204
        raise S3Error("InvalidArgument")

    def initiate_multipart(self, conn, b, bucket, key):
        upload_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO uploads(upload_id,bucket,key,tenant,initiated,content_type,metadata_json,tags_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                upload_id,
                bucket,
                key,
                b["tenant"],
                iso(),
                self.headers.get("Content-Type") or "binary/octet-stream",
                json.dumps(collect_metadata(self.headers)),
                json.dumps(parse_tagging_header(self.headers.get("x-amz-tagging"))),
            ),
        )
        body = (
            '<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Bucket>{xml_escape(bucket)}</Bucket><Key>{xml_escape(key)}</Key><UploadId>{upload_id}</UploadId>"
            "</InitiateMultipartUploadResult>"
        )
        self.send_xml(200, body)
        return 200

    def upload_part(self, conn, bucket, key, upload_id, part_number, body):
        up = conn.execute("SELECT * FROM uploads WHERE upload_id=? AND bucket=? AND key=?", (upload_id, bucket, key)).fetchone()
        if not up:
            raise S3Error("NoSuchUpload")
        if part_number < 1 or part_number > 10000:
            raise S3Error("InvalidArgument")
        os.makedirs(PART_DIR, exist_ok=True)
        path = os.path.join(PART_DIR, f"{upload_id}-{part_number}-{uuid.uuid4()}")
        with open(path, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        etag = quote_etag(md5_hexdigest(body))
        conn.execute(
            """INSERT INTO parts(upload_id,part_number,etag,md5_hex,size,data_path,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(upload_id,part_number) DO UPDATE SET etag=excluded.etag,md5_hex=excluded.md5_hex,
               size=excluded.size,data_path=excluded.data_path,created_at=excluded.created_at""",
            (upload_id, part_number, etag, etag.strip('"'), len(body), path, iso()),
        )
        self.send_xml(200, "", {"ETag": etag})
        return 200

    def complete_multipart(self, conn, b, bucket, key, upload_id, body):
        up = conn.execute("SELECT * FROM uploads WHERE upload_id=? AND bucket=? AND key=?", (upload_id, bucket, key)).fetchone()
        if not up:
            raise S3Error("NoSuchUpload")
        root = parse_xml(body)
        wanted = []
        for part in children(root, "Part"):
            try:
                pn = int(child_text(part, "PartNumber"))
            except Exception:
                raise S3Error("InvalidPart")
            wanted.append((pn, quote_etag(child_text(part, "ETag"))))
        if [p for p, _ in wanted] != sorted([p for p, _ in wanted]):
            raise S3Error("InvalidPartOrder")
        part_rows = []
        for pn, et in wanted:
            pr = conn.execute("SELECT * FROM parts WHERE upload_id=? AND part_number=?", (upload_id, pn)).fetchone()
            if not pr or pr["etag"] != et:
                raise S3Error("InvalidPart")
            part_rows.append(pr)
        for pr in part_rows[:-1]:
            if int(pr["size"]) < 5 * 1024 * 1024:
                raise S3Error("EntityTooSmall")
        total = sum(int(p["size"]) for p in part_rows)
        check_byte_quota(conn, b["tenant"], total - current_live_size(conn, bucket, key))
        md5_raw = b"".join(bytes.fromhex(p["md5_hex"]) for p in part_rows)
        final_etag = quote_etag(hashlib.md5(md5_raw).hexdigest() + f"-{len(part_rows)}")
        tmp = os.path.join(BLOB_DIR, str(uuid.uuid4()) + ".tmp")
        with open(tmp, "wb") as out:
            for pr in part_rows:
                with open(pr["data_path"], "rb") as inp:
                    shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        final_path = tmp[:-4]
        os.replace(tmp, final_path)
        created = iso()
        vid = str(uuid.uuid4()) if b["versioning"] == "Enabled" else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if b["versioning"] == "Enabled":
                conn.execute("UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1", (bucket, key))
            else:
                conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
            conn.execute(
                """INSERT INTO objects(bucket,key,version_id,is_latest,is_delete_marker,data_path,size,etag,
                                        content_type,metadata_json,tags_json,created_at,last_modified)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    bucket,
                    key,
                    vid,
                    1,
                    0,
                    final_path,
                    total,
                    final_etag,
                    up["content_type"],
                    up["metadata_json"],
                    up["tags_json"],
                    created,
                    created,
                ),
            )
            conn.execute("DELETE FROM parts WHERE upload_id=?", (upload_id,))
            conn.execute("DELETE FROM uploads WHERE upload_id=?", (upload_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        enqueue_notifications(conn, bucket, key, "s3:ObjectCreated:CompleteMultipartUpload")
        headers = {"ETag": final_etag}
        if vid:
            headers["x-amz-version-id"] = vid
        result = (
            '<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Location>/{xml_escape(bucket)}/{xml_escape(key)}</Location><Bucket>{xml_escape(bucket)}</Bucket>"
            f"<Key>{xml_escape(key)}</Key><ETag>{xml_escape(final_etag)}</ETag></CompleteMultipartUploadResult>"
        )
        self.send_xml(200, result, headers)
        return 200

    def abort_multipart(self, conn, upload_id):
        up = conn.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
        if not up:
            raise S3Error("NoSuchUpload")
        parts = conn.execute("SELECT data_path FROM parts WHERE upload_id=?", (upload_id,)).fetchall()
        conn.execute("DELETE FROM parts WHERE upload_id=?", (upload_id,))
        conn.execute("DELETE FROM uploads WHERE upload_id=?", (upload_id,))
        for p in parts:
            try:
                os.unlink(p["data_path"])
            except OSError:
                pass
        self.send_xml(204, "")
        return 204

    def list_parts(self, conn, upload_id):
        up = conn.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
        if not up:
            raise S3Error("NoSuchUpload")
        rows = conn.execute("SELECT * FROM parts WHERE upload_id=? ORDER BY part_number", (upload_id,)).fetchall()
        xml = [
            '<ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
            f"<Bucket>{xml_escape(up['bucket'])}</Bucket><Key>{xml_escape(up['key'])}</Key><UploadId>{xml_escape(upload_id)}</UploadId>",
        ]
        for r in rows:
            xml.append(
                f"<Part><PartNumber>{r['part_number']}</PartNumber><LastModified>{xml_escape(r['created_at'])}</LastModified>"
                f"<ETag>{xml_escape(r['etag'])}</ETag><Size>{r['size']}</Size></Part>"
            )
        xml.append("</ListPartsResult>")
        self.send_xml(200, "".join(xml))
        return 200

    def list_multipart_uploads(self, conn, bucket):
        rows = conn.execute("SELECT * FROM uploads WHERE bucket=? ORDER BY key", (bucket,)).fetchall()
        xml = [f'<ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Bucket>{xml_escape(bucket)}</Bucket>']
        for r in rows:
            xml.append(
                f"<Upload><Key>{xml_escape(r['key'])}</Key><UploadId>{xml_escape(r['upload_id'])}</UploadId>"
                f"<Initiated>{xml_escape(r['initiated'])}</Initiated><StorageClass>STANDARD</StorageClass></Upload>"
            )
        xml.append("</ListMultipartUploadsResult>")
        self.send_xml(200, "".join(xml))
        return 200

    def multi_delete(self, conn, bucket, b, body):
        root = parse_xml(body)
        quiet = child_text(root, "Quiet").lower() == "true"
        xml = ['<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
        for obj in children(root, "Object"):
            k = child_text(obj, "Key")
            vid = child_text(obj, "VersionId", None)
            try:
                marker, deleted_vid = delete_object_semantics(conn, bucket, k, vid, b["versioning"])
                enqueue_notifications(conn, bucket, k, "s3:ObjectRemoved:Delete")
                if not quiet:
                    xml.append(f"<Deleted><Key>{xml_escape(k)}</Key>")
                    if deleted_vid:
                        xml.append(f"<VersionId>{xml_escape(deleted_vid)}</VersionId>")
                    if marker:
                        xml.append("<DeleteMarker>true</DeleteMarker>")
                    xml.append("</Deleted>")
            except S3Error as e:
                xml.append(f"<Error><Key>{xml_escape(k)}</Key><Code>{e.code}</Code><Message>{xml_escape(e.message)}</Message></Error>")
        xml.append("</DeleteResult>")
        self.send_xml(200, "".join(xml))
        return 200

    def versioning(self, conn, bucket, body):
        if self.command == "GET":
            b = bucket_row(conn, bucket)
            status = b["versioning"]
            body = '<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            if status:
                body += f"<Status>{xml_escape(status)}</Status>"
            body += "</VersioningConfiguration>"
            self.send_xml(200, body)
            return 200
        if self.command == "PUT":
            root = parse_xml(body)
            status = child_text(root, "Status")
            if status not in ("Enabled", "Suspended", ""):
                raise S3Error("MalformedXML")
            conn.execute("UPDATE buckets SET versioning=? WHERE name=?", (status, bucket))
            self.send_xml(200, "")
            return 200
        raise S3Error("InvalidArgument")

    def cors(self, conn, bucket, body):
        b = bucket_row(conn, bucket)
        if self.command == "GET":
            if not b["cors_xml"]:
                raise S3Error("NoSuchCORSConfiguration")
            self.send_xml(200, b["cors_xml"])
            return 200
        if self.command == "PUT":
            parse_xml(body)
            conn.execute("UPDATE buckets SET cors_xml=? WHERE name=?", (body.decode("utf-8"), bucket))
            self.send_xml(200, "")
            return 200
        if self.command == "DELETE":
            conn.execute("UPDATE buckets SET cors_xml=NULL WHERE name=?", (bucket,))
            self.send_xml(204, "")
            return 204
        raise S3Error("InvalidArgument")

    def lifecycle(self, conn, bucket, body):
        b = bucket_row(conn, bucket)
        if self.command == "GET":
            if not b["lifecycle_xml"]:
                raise S3Error("NoSuchLifecycleConfiguration")
            self.send_xml(200, b["lifecycle_xml"])
            return 200
        if self.command == "PUT":
            text = body.decode("utf-8")
            rules = parse_lifecycle(text)
            conn.execute("UPDATE buckets SET lifecycle_xml=?, lifecycle_json=? WHERE name=?", (text, json.dumps(rules), bucket))
            self.send_xml(200, "")
            return 200
        if self.command == "DELETE":
            conn.execute("UPDATE buckets SET lifecycle_xml=NULL,lifecycle_json=NULL WHERE name=?", (bucket,))
            self.send_xml(204, "")
            return 204
        raise S3Error("InvalidArgument")

    def policy(self, conn, bucket, body):
        b = bucket_row(conn, bucket)
        if self.command == "GET":
            if not b["policy_json"]:
                raise S3Error("NoSuchBucketPolicy")
            self.send_body(200, b["policy_json"].encode("utf-8"), {}, "application/json")
            return 200
        if self.command == "PUT":
            try:
                policy = json.loads(body.decode("utf-8"))
            except Exception:
                raise S3Error("MalformedPolicy")
            if not isinstance(policy, dict) or "Version" not in policy or not policy_actions_valid(policy):
                raise S3Error("MalformedPolicy")
            conn.execute("UPDATE buckets SET policy_json=? WHERE name=?", (json.dumps(policy, separators=(",", ":")), bucket))
            self.send_xml(204, "")
            return 204
        if self.command == "DELETE":
            conn.execute("UPDATE buckets SET policy_json=NULL WHERE name=?", (bucket,))
            self.send_xml(204, "")
            return 204
        raise S3Error("InvalidArgument")

    def notification(self, conn, bucket, body):
        b = bucket_row(conn, bucket)
        if self.command == "GET":
            text = b["notification_xml"] or '<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
            self.send_xml(200, text)
            return 200
        if self.command == "PUT":
            text = body.decode("utf-8") if body else '<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
            configs = parse_notification(text)
            conn.execute("UPDATE buckets SET notification_xml=?, notification_json=? WHERE name=?", (text, json.dumps(configs), bucket))
            self.send_xml(200, "")
            return 200
        raise S3Error("InvalidArgument")

    def handle_options(self, conn, bucket, key):
        if not bucket:
            raise S3Error("CORSForbidden")
        b = bucket_row(conn, bucket)
        if not b:
            raise S3Error("NoSuchBucket")
        origin = self.headers.get("Origin")
        req_method = self.headers.get("Access-Control-Request-Method")
        if not origin or not b["cors_xml"]:
            raise S3Error("CORSForbidden")
        root = parse_xml(b["cors_xml"].encode("utf-8"))
        for rule in children(root, "CORSRule"):
            origins = [c.text or "" for c in children(rule, "AllowedOrigin")]
            methods = [c.text or "" for c in children(rule, "AllowedMethod")]
            headers = [c.text or "" for c in children(rule, "AllowedHeader")]
            if ("*" in origins or origin in origins) and req_method in methods:
                resp_headers = {
                    "Access-Control-Allow-Origin": "*" if "*" in origins else origin,
                    "Access-Control-Allow-Methods": ", ".join(methods),
                    "Access-Control-Allow-Headers": ", ".join(headers or ["*"]),
                }
                self.send_body(200, b"", resp_headers)
                return 200
        raise S3Error("CORSForbidden")

    def handle_admin(self, parsed):
        if self.headers.get("Authorization") != f"Bearer {ADMIN_TOKEN}":
            self.send_json(401, {"error": "unauthorized"})
            return 401
        conn = db()
        try:
            body = self.read_body() if self.command in ("POST", "DELETE", "PUT") else b""
            path = parsed.path[len("/_admin") :].strip("/")
            parts = path.split("/") if path else []
            if self.command == "GET" and parts == ["tenants"]:
                rows = conn.execute("SELECT * FROM tenants ORDER BY name").fetchall()
                self.send_json(200, {"tenants": [self.tenant_obj(r) for r in rows]})
                return 200
            if self.command == "POST" and parts == ["tenants"]:
                return self.admin_create_tenant(conn, body)
            if len(parts) >= 2 and parts[0] == "tenants":
                name = parts[1]
                if self.command == "GET" and len(parts) == 2:
                    r = conn.execute("SELECT * FROM tenants WHERE name=?", (name,)).fetchone()
                    if not r:
                        self.send_json(404, {"error": "NoSuchTenant"})
                        return 404
                    self.send_json(200, {"tenant": self.tenant_obj(r)})
                    return 200
                if self.command == "DELETE" and len(parts) == 2:
                    return self.admin_delete_tenant(conn, name)
                if len(parts) == 3 and parts[2] == "access-keys" and self.command == "POST":
                    return self.admin_create_key(conn, name)
                if len(parts) == 3 and parts[2] == "access-keys" and self.command == "GET":
                    return self.admin_list_keys(conn, name)
                if len(parts) == 4 and parts[2] == "access-keys" and self.command == "DELETE":
                    return self.admin_delete_key(conn, name, parts[3])
            if self.command == "GET" and parts == ["stats"]:
                return self.admin_stats(conn)
            if self.command == "GET" and parts == ["audit"]:
                return self.admin_audit(parsed)
            self.send_json(404, {"error": "NotFound", "message": "not found"})
            return 404
        finally:
            conn.close()

    def tenant_obj(self, r):
        return {"name": r["name"], "created_at": r["created_at"], "quota_bytes": r["quota_bytes"], "quota_buckets": r["quota_buckets"]}

    def admin_create_tenant(self, conn, body):
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            self.send_json(400, {"error": "InvalidJSON", "message": "invalid json"})
            return 400
        name = data.get("name")
        qb = data.get("quota_bytes")
        qbk = data.get("quota_buckets")
        if not validate_tenant_name(name) or (qb is not None and (not isinstance(qb, int) or qb <= 0)) or (qbk is not None and (not isinstance(qbk, int) or qbk <= 0)):
            self.send_json(400, {"error": "InvalidArgument", "message": "invalid tenant or quota"})
            return 400
        if conn.execute("SELECT 1 FROM tenants WHERE name=?", (name,)).fetchone():
            self.send_json(409, {"error": "TenantAlreadyExists", "message": "tenant exists"})
            return 409
        created = iso()
        ak, sec = random_key_id(), random_secret()
        conn.execute("INSERT INTO tenants(name,created_at,quota_bytes,quota_buckets) VALUES(?,?,?,?)", (name, created, qb, qbk))
        conn.execute("INSERT INTO access_keys(access_key_id,secret_access_key,tenant,created_at,active) VALUES(?,?,?,?,1)", (ak, sec, name, created))
        r = conn.execute("SELECT * FROM tenants WHERE name=?", (name,)).fetchone()
        self.audit_tenant = name
        self.audit_access_key = None
        self.send_json(201, {"tenant": self.tenant_obj(r), "access_key": {"access_key_id": ak, "secret_access_key": sec, "created_at": created}})
        return 201

    def admin_delete_tenant(self, conn, name):
        if name == DEFAULT_TENANT:
            self.send_json(403, {"error": "CannotDeleteDefaultTenant"})
            return 403
        if not conn.execute("SELECT 1 FROM tenants WHERE name=?", (name,)).fetchone():
            self.send_json(404, {"error": "NoSuchTenant"})
            return 404
        if conn.execute("SELECT 1 FROM buckets WHERE tenant=? LIMIT 1", (name,)).fetchone():
            self.send_json(409, {"error": "TenantNotEmpty"})
            return 409
        conn.execute("DELETE FROM tenants WHERE name=?", (name,))
        self.audit_tenant = name
        self.send_body(204, b"", {}, "application/json")
        return 204

    def admin_create_key(self, conn, name):
        if not conn.execute("SELECT 1 FROM tenants WHERE name=?", (name,)).fetchone():
            self.send_json(404, {"error": "NoSuchTenant"})
            return 404
        ak, sec, created = random_key_id(), random_secret(), iso()
        while conn.execute("SELECT 1 FROM access_keys WHERE access_key_id=?", (ak,)).fetchone():
            ak = random_key_id()
        conn.execute("INSERT INTO access_keys(access_key_id,secret_access_key,tenant,created_at,active) VALUES(?,?,?,?,1)", (ak, sec, name, created))
        self.audit_tenant = name
        self.send_json(201, {"access_key": {"access_key_id": ak, "secret_access_key": sec, "created_at": created}})
        return 201

    def admin_list_keys(self, conn, name):
        if not conn.execute("SELECT 1 FROM tenants WHERE name=?", (name,)).fetchone():
            self.send_json(404, {"error": "NoSuchTenant"})
            return 404
        rows = conn.execute("SELECT access_key_id,created_at FROM access_keys WHERE tenant=? AND active=1 ORDER BY created_at", (name,)).fetchall()
        self.send_json(200, {"access_keys": [{"access_key_id": r["access_key_id"], "created_at": r["created_at"]} for r in rows]})
        return 200

    def admin_delete_key(self, conn, name, key_id):
        cur = conn.execute("UPDATE access_keys SET active=0 WHERE tenant=? AND access_key_id=? AND active=1", (name, key_id))
        if cur.rowcount == 0:
            self.send_json(404, {"error": "NotFound"})
            return 404
        self.audit_tenant = name
        self.audit_access_key = key_id
        self.send_body(204, b"", {}, "application/json")
        return 204

    def admin_stats(self, conn):
        tenants = conn.execute("SELECT * FROM tenants ORDER BY name").fetchall()
        per = []
        for t in tenants:
            buckets = bucket_count_for_tenant(conn, t["name"])
            obj = conn.execute(
                """SELECT COUNT(*) AS c, COALESCE(SUM(o.size),0) AS s FROM objects o
                   JOIN buckets b ON b.name=o.bucket
                   WHERE b.tenant=? AND o.is_latest=1 AND o.is_delete_marker=0""",
                (t["name"],),
            ).fetchone()
            per.append(
                {
                    "tenant": t["name"],
                    "buckets": buckets,
                    "live_objects": obj["c"],
                    "live_bytes": obj["s"] or 0,
                    "quota_bytes": t["quota_bytes"],
                    "quota_buckets": t["quota_buckets"],
                }
            )
        self.send_json(
            200,
            {
                "global": {
                    "tenants": len(tenants),
                    "buckets": sum(p["buckets"] for p in per),
                    "live_objects": sum(p["live_objects"] for p in per),
                    "live_bytes": sum(p["live_bytes"] for p in per),
                },
                "tenants": per,
            },
        )
        return 200

    def admin_audit(self, parsed):
        q = qd(parsed.query)
        limit = min(int(first(q, "limit", "100") or "100"), 1000)
        tenant = first(q, "tenant")
        events = []
        if os.path.exists(AUDIT_PATH):
            with open(AUDIT_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if tenant and ev.get("tenant") != tenant:
                    continue
                events.append(ev)
                if len(events) >= limit:
                    break
        self.send_json(200, {"events": events})
        return 200

    def handle_console(self, parsed):
        if parsed.path == "/console" or parsed.path == "/console/":
            return self.serve_file("/app/console/index.html", "text/html; charset=utf-8")
        if parsed.path == "/console/static/app.js":
            return self.serve_file("/app/console/static/app.js", "application/javascript; charset=utf-8")
        if parsed.path == "/console/static/styles.css":
            return self.serve_file("/app/console/static/styles.css", "text/css; charset=utf-8")
        if parsed.path.startswith("/console/api/"):
            return self.handle_console_api(parsed)
        self.send_body(404, b"not found", {}, "text/plain")
        return 404

    def serve_file(self, path, ctype):
        if not os.path.exists(path):
            self.send_body(404, b"not found", {}, "text/plain")
            return 404
        with open(path, "rb") as f:
            data = f.read()
        self.send_body(200, data, {"Cache-Control": "no-store"}, ctype)
        return 200

    def console_session(self, conn):
        cookies = parse_cookie(self.headers.get("Cookie"))
        key_id = cookie_access_key(cookies.get("halyard_console"))
        if not key_id:
            return None
        row = tenant_for_key(conn, key_id)
        return row

    def handle_console_api(self, parsed):
        conn = db()
        try:
            body = self.read_body() if self.command in ("POST", "PUT", "DELETE") else b""
            path = parsed.path[len("/console/api/") :]
            if path == "login" and self.command == "POST":
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    data = {}
                row = tenant_for_key(conn, data.get("access_key_id"))
                if not row or not hmac.compare_digest(row["secret_access_key"], data.get("secret_access_key") or ""):
                    self.send_json(401, {"error": "invalid_credentials"})
                    return 401
                headers = {"Set-Cookie": f"halyard_console={auth_cookie(row['access_key_id'])}; Path=/; HttpOnly; SameSite=Lax"}
                self.send_json(200, {"tenant": row["tenant"], "access_key_id": row["access_key_id"]}, headers)
                return 200
            if path == "logout" and self.command == "POST":
                self.send_json(200, {"ok": True}, {"Set-Cookie": "halyard_console=; Path=/; Max-Age=0; SameSite=Lax"})
                return 200
            row = self.console_session(conn)
            if not row:
                self.send_json(401, {"error": "unauthenticated"})
                return 401
            if path == "me" and self.command == "GET":
                self.send_json(200, {"tenant": row["tenant"], "access_key_id": row["access_key_id"]})
                return 200
            if path == "buckets" and self.command == "GET":
                rows = conn.execute("SELECT name,created_at FROM buckets WHERE tenant=? ORDER BY name", (row["tenant"],)).fetchall()
                self.send_json(200, {"buckets": [{"name": r["name"], "created_at": r["created_at"]} for r in rows]})
                return 200
            if path == "buckets" and self.command == "POST":
                data = json.loads(body.decode("utf-8") or "{}")
                name = data.get("name", "")
                if not validate_bucket_name(name):
                    self.send_json(400, {"error": "InvalidBucketName", "message": "Bucket names must be 3-63 lowercase letters, numbers, dots, or hyphens."})
                    return 400
                if bucket_row(conn, name):
                    self.send_json(409, {"error": "BucketAlreadyExists", "message": "Bucket already exists."})
                    return 409
                try:
                    check_bucket_quota(conn, row["tenant"])
                except S3Error as e:
                    self.send_json(e.status, {"error": e.code, "message": e.message})
                    return e.status
                conn.execute("INSERT INTO buckets(name,tenant,created_at) VALUES(?,?,?)", (name, row["tenant"], iso()))
                self.send_json(201, {"bucket": {"name": name}})
                return 201
            if path == "access-keys" and self.command == "GET":
                rows = conn.execute("SELECT access_key_id,created_at FROM access_keys WHERE tenant=? AND active=1 ORDER BY created_at", (row["tenant"],)).fetchall()
                self.send_json(200, {"access_keys": [{"access_key_id": r["access_key_id"], "created_at": r["created_at"]} for r in rows]})
                return 200
            if path == "access-keys" and self.command == "POST":
                ak, sec, created = random_key_id(), random_secret(), iso()
                while conn.execute("SELECT 1 FROM access_keys WHERE access_key_id=?", (ak,)).fetchone():
                    ak = random_key_id()
                conn.execute("INSERT INTO access_keys(access_key_id,secret_access_key,tenant,created_at,active) VALUES(?,?,?,?,1)", (ak, sec, row["tenant"], created))
                self.send_json(201, {"access_key": {"access_key_id": ak, "secret_access_key": sec, "created_at": created}})
                return 201
            m = re.fullmatch(r"access-keys/([^/]+)", path)
            if m and self.command == "DELETE":
                key_id = urllib.parse.unquote(m.group(1))
                if key_id == row["access_key_id"]:
                    self.send_json(409, {"error": "CannotDeleteCurrentAccessKey"})
                    return 409
                cur = conn.execute("UPDATE access_keys SET active=0 WHERE tenant=? AND access_key_id=? AND active=1", (row["tenant"], key_id))
                if cur.rowcount == 0:
                    self.send_json(404, {"error": "NotFound"})
                    return 404
                self.send_json(200, {"ok": True})
                return 200
            self.send_json(404, {"error": "not_found"})
            return 404
        finally:
            conn.close()


def lifecycle_worker():
    while True:
        time.sleep(LIFECYCLE_TICK)
        try:
            conn = db()
            buckets = conn.execute("SELECT * FROM buckets WHERE lifecycle_json IS NOT NULL").fetchall()
            for b in buckets:
                try:
                    rules = json.loads(b["lifecycle_json"] or "[]")
                except Exception:
                    rules = []
                for rule in rules:
                    if rule.get("Status") != "Enabled":
                        continue
                    prefix = rule.get("Prefix") or ""
                    cutoff = now() - dt.timedelta(seconds=int(rule.get("Days", 0)) * SECONDS_PER_DAY)
                    rows = conn.execute(
                        """SELECT * FROM objects WHERE bucket=? AND is_latest=1 AND is_delete_marker=0 AND key LIKE ?""",
                        (b["name"], prefix + "%"),
                    ).fetchall()
                    for obj in rows:
                        if parse_iso(obj["created_at"]) <= cutoff:
                            delete_object_semantics(conn, b["name"], obj["key"], None, b["versioning"])
            conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def notification_worker():
    while True:
        notify_event.wait(1)
        notify_event.clear()
        try:
            conn = db()
            rows = conn.execute("SELECT * FROM outbox WHERE next_attempt_at<=? ORDER BY id LIMIT 50", (time.time(),)).fetchall()
            for r in rows:
                ok = False
                try:
                    req = urllib.request.Request(
                        r["endpoint"],
                        data=r["payload_json"].encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        ok = 200 <= resp.status < 300
                except Exception:
                    ok = False
                if ok or int(r["attempts"]) + 1 >= 5:
                    conn.execute("DELETE FROM outbox WHERE id=?", (r["id"],))
                else:
                    attempts = int(r["attempts"]) + 1
                    conn.execute("UPDATE outbox SET attempts=?, next_attempt_at=? WHERE id=?", (attempts, time.time() + (2 ** (attempts - 1)), r["id"]))
            conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def main():
    init_db()
    threading.Thread(target=lifecycle_worker, daemon=True).start()
    threading.Thread(target=notification_worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), HalyardHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
