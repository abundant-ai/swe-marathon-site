"""S3 data-plane handlers."""
import hashlib
import json
import os
import re
import shutil
import threading
import time
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from flask import Response, request, current_app, abort

from . import sigv4
from . import policy as policy_mod
from .audit import write_event
from .auth import authenticate_s3
from .db import get_conn
from .errors import S3Error, s3_error_xml, ERROR_STATUS, ERROR_MESSAGES
from .models import (
    get_bucket, get_tenant, list_buckets_for_tenant, count_buckets_for_tenant,
    create_bucket, delete_bucket, update_bucket_field, tenant_used_bytes,
)
from .s3xml import esc, iso_for_s3, http_iso_to_rfc1123
from .util import (
    now_iso, gen_request_id, gen_version_id, gen_upload_id,
    storage_path_for, part_storage_path, valid_bucket_name, safe_remove,
    safe_rmtree, DATA_DIR, md5_hex,
)

MIN_PART_SIZE = 5 * 1024 * 1024  # 5 MiB


# In-memory write lock per (bucket, key) for atomic put guarantees.
_object_locks = {}
_obj_locks_global = threading.Lock()


def get_obj_lock(bucket, key):
    k = (bucket, key)
    with _obj_locks_global:
        lk = _object_locks.get(k)
        if lk is None:
            lk = threading.Lock()
            _object_locks[k] = lk
        return lk


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def make_error_response(code, message=None, request_id=None, status=None, resource="", **extra):
    if message is None:
        message = ERROR_MESSAGES.get(code, code)
    if status is None:
        status = ERROR_STATUS.get(code, 400)
    if request_id is None:
        request_id = gen_request_id()
    body = s3_error_xml(code, message, request_id, resource=resource, **extra)
    resp = Response(body, status=status, mimetype="application/xml")
    resp.headers["x-amz-request-id"] = request_id
    return resp


def get_request_id():
    rid = getattr(request, "_halyard_request_id", None)
    if rid is None:
        rid = gen_request_id()
        request._halyard_request_id = rid
    return rid


def is_anonymous():
    qp = request.args
    if qp.get("X-Amz-Algorithm") == "AWS4-HMAC-SHA256":
        return False
    if request.headers.get("Authorization"):
        return False
    return True


def authorize(action, bucket_row, tenant, key=None, anonymous_method=None):
    """Check bucket-level access. Raises S3Error.

    bucket_row: dict from get_bucket
    tenant: authenticated tenant (None=anonymous)
    """
    if bucket_row is None:
        return  # caller handles
    owner = bucket_row["tenant"]
    if tenant is not None and tenant == owner:
        return  # owner has full access
    # Cross-tenant or anonymous - check policy
    pol = bucket_row.get("policy")
    # anonymous can only do GET/HEAD
    if tenant is None:
        if anonymous_method and anonymous_method not in ("GET", "HEAD"):
            raise S3Error("AccessDenied")
    if pol:
        decision = policy_mod.evaluate(pol, action, tenant, bucket_row["name"], key)
        if decision == "Allow":
            return
        if decision == "Deny":
            raise S3Error("AccessDenied")
    # No policy or no match - deny
    raise S3Error("AccessDenied")


def parse_amz_meta(headers):
    meta = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl.startswith("x-amz-meta-"):
            meta[kl[len("x-amz-meta-"):]] = v
    return meta


def parse_tag_query(s):
    """parse x-amz-tagging header style: k1=v1&k2=v2"""
    if not s:
        return {}
    out = {}
    for p in s.split("&"):
        if "=" in p:
            k, v = p.split("=", 1)
            out[unquote(k)] = unquote(v)
    return out


def get_audit_path():
    p = request.environ.get("RAW_URI", request.path)
    return p


# -----------------------------------------------------------------------------
# Bucket handlers
# -----------------------------------------------------------------------------

def handle_service_get(access_key_id, tenant):
    """GET / -> list buckets for tenant."""
    buckets = list_buckets_for_tenant(tenant)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             '<Owner>',
             f'<ID>{esc(tenant)}</ID>',
             f'<DisplayName>{esc(tenant)}</DisplayName>',
             '</Owner>',
             '<Buckets>']
    for b in buckets:
        parts.append('<Bucket>')
        parts.append(f'<Name>{esc(b["name"])}</Name>')
        parts.append(f'<CreationDate>{esc(iso_for_s3(b["created_at"]))}</CreationDate>')
        parts.append('</Bucket>')
    parts.append('</Buckets></ListAllMyBucketsResult>')
    return Response("".join(parts), mimetype="application/xml")


def handle_create_bucket(bucket, access_key_id, tenant):
    if not valid_bucket_name(bucket):
        raise S3Error("InvalidBucketName")
    existing = get_bucket(bucket)
    if existing:
        if existing["tenant"] == tenant:
            raise S3Error("BucketAlreadyOwnedByYou")
        raise S3Error("BucketAlreadyExists")
    # check quota
    t = get_tenant(tenant)
    if t and t.get("quota_buckets") is not None:
        if count_buckets_for_tenant(tenant) >= t["quota_buckets"]:
            raise S3Error("TooManyBuckets")
    create_bucket(bucket, tenant)
    resp = Response("", status=200)
    resp.headers["Location"] = f"/{bucket}"
    return resp


def handle_delete_bucket(bucket_row, access_key_id, tenant):
    bucket = bucket_row["name"]
    conn = get_conn()
    cur = conn.execute("SELECT COUNT(*) FROM objects WHERE bucket = ?", (bucket,))
    if cur.fetchone()[0] > 0:
        raise S3Error("BucketNotEmpty")
    cur = conn.execute("SELECT COUNT(*) FROM multipart_uploads WHERE bucket = ?", (bucket,))
    if cur.fetchone()[0] > 0:
        raise S3Error("BucketNotEmpty")
    delete_bucket(bucket)
    return Response("", status=204)


def handle_head_bucket(bucket_row):
    return Response("", status=200)


# Versioning
def handle_get_versioning(bucket_row):
    v = bucket_row.get("versioning", "Disabled")
    if v == "Disabled":
        body = '<?xml version="1.0" encoding="UTF-8"?><VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
    else:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Status>{esc(v)}</Status>'
            '</VersioningConfiguration>'
        )
    return Response(body, mimetype="application/xml")


def handle_put_versioning(bucket_row):
    body = request.get_data() or b""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    status_el = root.find("s3:Status", ns)
    if status_el is None:
        # try without ns
        status_el = root.find("Status")
    status = status_el.text if status_el is not None else None
    if status not in ("Enabled", "Suspended"):
        raise S3Error("MalformedXML")
    update_bucket_field(bucket_row["name"], "versioning", status)
    return Response("", status=200)


# Bucket policy
def handle_get_policy(bucket_row):
    p = bucket_row.get("policy")
    if not p:
        raise S3Error("NoSuchBucketPolicy")
    return Response(p, mimetype="application/json")


def handle_put_policy(bucket_row):
    body = request.get_data() or b""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise S3Error("MalformedPolicy")
    try:
        policy_mod.validate_policy(text)
    except ValueError as e:
        raise S3Error("MalformedPolicy", str(e))
    update_bucket_field(bucket_row["name"], "policy", text)
    return Response("", status=204)


def handle_delete_policy(bucket_row):
    update_bucket_field(bucket_row["name"], "policy", None)
    return Response("", status=204)


# CORS
def handle_get_cors(bucket_row):
    cors = bucket_row.get("cors")
    if not cors:
        raise S3Error("NoSuchCORSConfiguration")
    return Response(cors, mimetype="application/xml")


def handle_put_cors(bucket_row):
    body = request.get_data() or b""
    try:
        ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    update_bucket_field(bucket_row["name"], "cors", body.decode("utf-8"))
    return Response("", status=200)


def handle_delete_cors(bucket_row):
    update_bucket_field(bucket_row["name"], "cors", None)
    return Response("", status=204)


# Lifecycle
def handle_get_lifecycle(bucket_row):
    lc = bucket_row.get("lifecycle")
    if not lc:
        raise S3Error("NoSuchLifecycleConfiguration")
    return Response(lc, mimetype="application/xml")


def handle_put_lifecycle(bucket_row):
    body = request.get_data() or b""
    try:
        ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    update_bucket_field(bucket_row["name"], "lifecycle", body.decode("utf-8"))
    return Response("", status=200)


def handle_delete_lifecycle(bucket_row):
    update_bucket_field(bucket_row["name"], "lifecycle", None)
    return Response("", status=204)


# Notification
def handle_get_notification(bucket_row):
    n = bucket_row.get("notification")
    if not n:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
        )
        return Response(body, mimetype="application/xml")
    return Response(n, mimetype="application/xml")


def handle_put_notification(bucket_row):
    body = request.get_data() or b""
    try:
        ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    update_bucket_field(bucket_row["name"], "notification", body.decode("utf-8"))
    return Response("", status=200)


# Bucket location
def handle_get_location(bucket_row):
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>'
    )
    return Response(body, mimetype="application/xml")


# Bucket tagging (no-op support, accept and store-less for now)
def handle_get_tagging(bucket_row, key=None, version_id=None):
    if key:
        # object tagging
        obj = _get_object(bucket_row["name"], key, version_id)
        if not obj:
            raise S3Error("NoSuchKey")
        tags_json = obj.get("tagging") or "{}"
        try:
            tags = json.loads(tags_json)
        except Exception:
            tags = {}
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                 '<TagSet>']
        for k, v in tags.items():
            parts.append(f'<Tag><Key>{esc(k)}</Key><Value>{esc(v)}</Value></Tag>')
        parts.append('</TagSet></Tagging>')
        return Response("".join(parts), mimetype="application/xml")
    # bucket tagging - return empty
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><TagSet/></Tagging>')
    return Response(body, mimetype="application/xml")


def handle_put_tagging(bucket_row, key=None, version_id=None):
    body = request.get_data() or b""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    tags = _parse_tag_xml(root)
    if key:
        obj = _get_object(bucket_row["name"], key, version_id)
        if not obj:
            raise S3Error("NoSuchKey")
        conn = get_conn()
        conn.execute(
            "UPDATE objects SET tagging = ? WHERE bucket = ? AND key = ? AND version_id = ?",
            (json.dumps(tags), bucket_row["name"], key, obj["version_id"]),
        )
    return Response("", status=200)


def handle_delete_tagging(bucket_row, key=None, version_id=None):
    if key:
        obj = _get_object(bucket_row["name"], key, version_id)
        if not obj:
            raise S3Error("NoSuchKey")
        conn = get_conn()
        conn.execute(
            "UPDATE objects SET tagging = NULL WHERE bucket = ? AND key = ? AND version_id = ?",
            (bucket_row["name"], key, obj["version_id"]),
        )
    return Response("", status=204)


def _parse_tag_xml(root):
    tags = {}
    for ts in root.iter():
        if ts.tag.endswith("Tag"):
            k = None
            v = None
            for child in ts:
                if child.tag.endswith("Key"):
                    k = child.text or ""
                elif child.tag.endswith("Value"):
                    v = child.text or ""
            if k is not None:
                tags[k] = v or ""
    return tags


# -----------------------------------------------------------------------------
# Object operations
# -----------------------------------------------------------------------------

def _get_object(bucket, key, version_id=None):
    conn = get_conn()
    if version_id:
        cur = conn.execute(
            "SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?",
            (bucket, key, version_id),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM objects WHERE bucket=? AND key=? AND is_latest=1",
            (bucket, key),
        )
    row = cur.fetchone()
    return dict(row) if row else None


def handle_put_object(bucket_row, key, access_key_id, tenant):
    bucket = bucket_row["name"]
    body = request.get_data(cache=False)
    if body is None:
        body = b""
    size = len(body)

    # Check tenant quota
    t = get_tenant(bucket_row["tenant"])
    if t and t.get("quota_bytes") is not None:
        used = tenant_used_bytes(bucket_row["tenant"])
        if used + size > t["quota_bytes"]:
            raise S3Error("QuotaExceeded")

    # Conditional put
    inm = request.headers.get("If-None-Match")
    if inm:
        existing = _get_object(bucket, key)
        if existing and not existing.get("is_delete_marker"):
            if inm == "*" or inm.strip('"') == existing["etag"]:
                raise S3Error("PreconditionFailed")

    # Optional Content-MD5
    md5_header = request.headers.get("Content-MD5")
    digest = md5_hex(body)
    if md5_header:
        import base64
        try:
            expected = base64.b64decode(md5_header).hex()
            if expected != digest:
                raise S3Error("InvalidArgument", "Content-MD5 mismatch")
        except Exception:
            raise S3Error("InvalidArgument", "Bad Content-MD5")

    version_id = gen_version_id()
    storage_path = storage_path_for(version_id)
    # Atomic write
    tmp = storage_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, storage_path)

    content_type = request.headers.get("Content-Type", "application/octet-stream")
    content_disposition = request.headers.get("Content-Disposition")
    content_encoding = request.headers.get("Content-Encoding")
    cache_control = request.headers.get("Cache-Control")
    metadata = parse_amz_meta(request.headers)
    tag_hdr = request.headers.get("x-amz-tagging") or ""
    tags = parse_tag_query(tag_hdr) if tag_hdr else {}

    versioning = bucket_row.get("versioning", "Disabled")
    versioned = versioning == "Enabled"

    lock = get_obj_lock(bucket, key)
    with lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not versioned:
                # remove any existing object versions (and their files)
                cur = conn.execute(
                    "SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                    (bucket, key),
                )
                old = cur.fetchall()
                conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
                for o in old:
                    if o["storage_path"]:
                        safe_remove(o["storage_path"])
                fixed_vid = "null"
                conn.execute(
                    "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, content_type, content_disposition, content_encoding, cache_control, metadata, tagging, storage_path, created_at, last_modified) "
                    "VALUES (?,?,?,1,0,?,?,?,?,?,?,?,?,?,?,?)",
                    (bucket, key, fixed_vid, size, digest, content_type,
                     content_disposition, content_encoding, cache_control,
                     json.dumps(metadata), json.dumps(tags) if tags else None,
                     storage_path, now_iso(), now_iso()),
                )
                final_vid = fixed_vid
            else:
                # mark old latest as not latest
                conn.execute(
                    "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                    (bucket, key),
                )
                conn.execute(
                    "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, content_type, content_disposition, content_encoding, cache_control, metadata, tagging, storage_path, created_at, last_modified) "
                    "VALUES (?,?,?,1,0,?,?,?,?,?,?,?,?,?,?,?)",
                    (bucket, key, version_id, size, digest, content_type,
                     content_disposition, content_encoding, cache_control,
                     json.dumps(metadata), json.dumps(tags) if tags else None,
                     storage_path, now_iso(), now_iso()),
                )
                final_vid = version_id
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            safe_remove(storage_path)
            raise

    # notification
    _enqueue_notification(bucket_row, "s3:ObjectCreated:Put", key, size, digest)

    resp = Response("", status=200)
    resp.headers["ETag"] = f'"{digest}"'
    if versioned:
        resp.headers["x-amz-version-id"] = final_vid
    return resp


def handle_get_object(bucket_row, key, access_key_id, tenant, head=False):
    bucket = bucket_row["name"]
    version_id = request.args.get("versionId")
    obj = _get_object(bucket, key, version_id)
    if obj is None or obj.get("is_delete_marker"):
        raise S3Error("NoSuchKey")

    # conditional GET
    inm = request.headers.get("If-None-Match")
    ims = request.headers.get("If-Modified-Since")
    im = request.headers.get("If-Match")
    ius = request.headers.get("If-Unmodified-Since")

    etag = obj["etag"]
    last_mod_iso = obj["last_modified"]

    if im:
        if im.strip('"') != etag:
            raise S3Error("PreconditionFailed")
    if inm and (inm == "*" or inm.strip('"') == etag):
        resp = Response("", status=304)
        resp.headers["ETag"] = f'"{etag}"'
        return resp
    if ius:
        # if last_mod > ius -> 412
        try:
            from email.utils import parsedate_to_datetime
            t1 = parsedate_to_datetime(ius)
            from datetime import datetime
            if "T" in last_mod_iso and last_mod_iso.endswith("Z"):
                t2 = datetime.strptime(last_mod_iso, "%Y-%m-%dT%H:%M:%SZ")
            else:
                t2 = parsedate_to_datetime(last_mod_iso)
            t1_naive = t1.replace(tzinfo=None) if t1.tzinfo else t1
            if t2 > t1_naive:
                raise S3Error("PreconditionFailed")
        except Exception:
            pass
    if ims:
        try:
            from email.utils import parsedate_to_datetime
            t1 = parsedate_to_datetime(ims)
            from datetime import datetime
            if "T" in last_mod_iso and last_mod_iso.endswith("Z"):
                t2 = datetime.strptime(last_mod_iso, "%Y-%m-%dT%H:%M:%SZ")
            else:
                t2 = parsedate_to_datetime(last_mod_iso)
            t1_naive = t1.replace(tzinfo=None) if t1.tzinfo else t1
            if t2 <= t1_naive:
                resp = Response("", status=304)
                resp.headers["ETag"] = f'"{etag}"'
                return resp
        except Exception:
            pass

    size = obj["size"]
    storage_path = obj["storage_path"]
    range_hdr = request.headers.get("Range")
    start = 0
    end = size - 1
    is_partial = False
    if range_hdr:
        m = re.match(r"^bytes=(\d*)-(\d*)$", range_hdr.strip())
        if not m:
            raise S3Error("InvalidRange")
        s = m.group(1)
        e = m.group(2)
        if s == "" and e == "":
            raise S3Error("InvalidRange")
        if s == "":
            # suffix
            n = int(e)
            if n == 0:
                raise S3Error("InvalidRange")
            start = max(0, size - n)
            end = size - 1
        elif e == "":
            start = int(s)
            end = size - 1
        else:
            start = int(s)
            end = int(e)
            if end >= size:
                end = size - 1
        if start >= size or start > end:
            raise S3Error("InvalidRange")
        is_partial = True

    length = end - start + 1
    headers = {}
    headers["Content-Type"] = obj.get("content_type") or "application/octet-stream"
    headers["ETag"] = f'"{etag}"'
    headers["Last-Modified"] = http_iso_to_rfc1123(last_mod_iso)
    headers["Accept-Ranges"] = "bytes"
    headers["Content-Length"] = str(length)
    if obj.get("content_disposition"):
        headers["Content-Disposition"] = obj["content_disposition"]
    if obj.get("content_encoding"):
        headers["Content-Encoding"] = obj["content_encoding"]
    if obj.get("cache_control"):
        headers["Cache-Control"] = obj["cache_control"]
    # user metadata
    try:
        meta = json.loads(obj.get("metadata") or "{}")
    except Exception:
        meta = {}
    for k, v in meta.items():
        headers[f"x-amz-meta-{k}"] = v
    if obj.get("tagging"):
        try:
            tagcount = len(json.loads(obj["tagging"]))
            if tagcount:
                headers["x-amz-tagging-count"] = str(tagcount)
        except Exception:
            pass

    versioning = bucket_row.get("versioning", "Disabled")
    if versioning != "Disabled":
        headers["x-amz-version-id"] = obj["version_id"]

    if head:
        # For HEAD responses, body is empty but Content-Length must reflect the object size.
        # Use direct_passthrough to prevent Werkzeug from recomputing Content-Length.
        resp = Response(status=200, direct_passthrough=True)
        for hk, hv in headers.items():
            resp.headers[hk] = hv
        resp.headers["Content-Length"] = str(length)
        return resp

    if is_partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        with open(storage_path, "rb") as f:
            f.seek(start)
            data = f.read(length)
        resp = Response(data, status=206, headers=headers)
        return resp

    def gen():
        with open(storage_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return Response(gen(), status=200, headers=headers)


def handle_delete_object(bucket_row, key, access_key_id, tenant):
    bucket = bucket_row["name"]
    version_id = request.args.get("versionId")
    versioning = bucket_row.get("versioning", "Disabled")
    versioned = versioning == "Enabled"
    suspended = versioning == "Suspended"

    lock = get_obj_lock(bucket, key)
    with lock:
        conn = get_conn()
        if version_id:
            # delete specific version
            cur = conn.execute(
                "SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?",
                (bucket, key, version_id),
            )
            row = cur.fetchone()
            if not row:
                # fine, idempotent
                resp = Response("", status=204)
                resp.headers["x-amz-version-id"] = version_id
                if row and row["is_delete_marker"]:
                    resp.headers["x-amz-delete-marker"] = "true"
                return resp
            was_latest = row["is_latest"]
            sp = row["storage_path"]
            is_dm = row["is_delete_marker"]
            conn.execute("DELETE FROM objects WHERE bucket=? AND key=? AND version_id=?",
                         (bucket, key, version_id))
            if sp and not is_dm:
                safe_remove(sp)
            if was_latest:
                # promote newest remaining version to latest
                cur = conn.execute(
                    "SELECT version_id FROM objects WHERE bucket=? AND key=? ORDER BY created_at DESC LIMIT 1",
                    (bucket, key),
                )
                r = cur.fetchone()
                if r:
                    conn.execute(
                        "UPDATE objects SET is_latest=1 WHERE bucket=? AND key=? AND version_id=?",
                        (bucket, key, r["version_id"]),
                    )
            resp = Response("", status=204)
            resp.headers["x-amz-version-id"] = version_id
            if is_dm:
                resp.headers["x-amz-delete-marker"] = "true"
            return resp

        # No version specified
        if versioned:
            # insert delete marker
            dm_vid = gen_version_id()
            conn.execute(
                "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                (bucket, key),
            )
            conn.execute(
                "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, storage_path, created_at, last_modified) "
                "VALUES (?,?,?,1,1,0,'',NULL,?,?)",
                (bucket, key, dm_vid, now_iso(), now_iso()),
            )
            resp = Response("", status=204)
            resp.headers["x-amz-version-id"] = dm_vid
            resp.headers["x-amz-delete-marker"] = "true"
            _enqueue_notification(bucket_row, "s3:ObjectRemoved:Delete", key, 0, "")
            return resp
        else:
            cur = conn.execute(
                "SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                (bucket, key),
            )
            rows = cur.fetchall()
            if rows:
                conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
                for r in rows:
                    if r["storage_path"]:
                        safe_remove(r["storage_path"])
                _enqueue_notification(bucket_row, "s3:ObjectRemoved:Delete", key, 0, "")
            resp = Response("", status=204)
            return resp


def handle_head_object(bucket_row, key, access_key_id, tenant):
    return handle_get_object(bucket_row, key, access_key_id, tenant, head=True)


# CopyObject
def handle_copy_object(bucket_row, key, access_key_id, tenant):
    """PUT /<bucket>/<key> with x-amz-copy-source header"""
    src_hdr = request.headers.get("x-amz-copy-source")
    if not src_hdr:
        return None
    src_hdr = unquote(src_hdr)
    if src_hdr.startswith("/"):
        src_hdr = src_hdr[1:]
    if "?versionId=" in src_hdr:
        src_path, src_vid = src_hdr.split("?versionId=", 1)
    elif "?" in src_hdr:
        src_path = src_hdr.split("?", 1)[0]
        src_vid = None
    else:
        src_path = src_hdr
        src_vid = None
    if "/" not in src_path:
        raise S3Error("InvalidArgument", "Bad copy source")
    src_bucket, src_key = src_path.split("/", 1)

    src_bucket_row = get_bucket(src_bucket)
    if not src_bucket_row:
        raise S3Error("NoSuchBucket")
    # cross-tenant access check
    if src_bucket_row["tenant"] != tenant:
        # check policy
        pol = src_bucket_row.get("policy")
        if pol:
            decision = policy_mod.evaluate(pol, "s3:GetObject", tenant, src_bucket, src_key)
            if decision != "Allow":
                raise S3Error("AccessDenied")
        else:
            raise S3Error("AccessDenied")

    src_obj = _get_object(src_bucket, src_key, src_vid)
    if not src_obj or src_obj.get("is_delete_marker"):
        raise S3Error("NoSuchKey")

    # check quota
    t = get_tenant(bucket_row["tenant"])
    if t and t.get("quota_bytes") is not None:
        used = tenant_used_bytes(bucket_row["tenant"])
        if used + src_obj["size"] > t["quota_bytes"]:
            raise S3Error("QuotaExceeded")

    # Read source
    new_vid = gen_version_id()
    new_path = storage_path_for(new_vid)
    # copy file
    shutil.copyfile(src_obj["storage_path"], new_path)
    size = src_obj["size"]
    etag = src_obj["etag"]

    # metadata directive
    md = request.headers.get("x-amz-metadata-directive", "COPY").upper()
    if md == "REPLACE":
        metadata = parse_amz_meta(request.headers)
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        content_disposition = request.headers.get("Content-Disposition")
        content_encoding = request.headers.get("Content-Encoding")
        cache_control = request.headers.get("Cache-Control")
    else:
        try:
            metadata = json.loads(src_obj.get("metadata") or "{}")
        except Exception:
            metadata = {}
        content_type = src_obj.get("content_type") or "application/octet-stream"
        content_disposition = src_obj.get("content_disposition")
        content_encoding = src_obj.get("content_encoding")
        cache_control = src_obj.get("cache_control")

    td = request.headers.get("x-amz-tagging-directive", "COPY").upper()
    if td == "REPLACE":
        tags = parse_tag_query(request.headers.get("x-amz-tagging") or "")
    else:
        try:
            tags = json.loads(src_obj.get("tagging") or "{}")
        except Exception:
            tags = {}

    bucket = bucket_row["name"]
    versioning = bucket_row.get("versioning", "Disabled")
    versioned = versioning == "Enabled"

    lock = get_obj_lock(bucket, key)
    with lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not versioned:
                cur = conn.execute(
                    "SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                    (bucket, key),
                )
                old = cur.fetchall()
                conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
                for o in old:
                    if o["storage_path"]:
                        safe_remove(o["storage_path"])
                final_vid = "null"
            else:
                conn.execute(
                    "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                    (bucket, key),
                )
                final_vid = new_vid

            conn.execute(
                "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, content_type, content_disposition, content_encoding, cache_control, metadata, tagging, storage_path, created_at, last_modified) "
                "VALUES (?,?,?,1,0,?,?,?,?,?,?,?,?,?,?,?)",
                (bucket, key, final_vid, size, etag, content_type,
                 content_disposition, content_encoding, cache_control,
                 json.dumps(metadata), json.dumps(tags) if tags else None,
                 new_path, now_iso(), now_iso()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            safe_remove(new_path)
            raise

    _enqueue_notification(bucket_row, "s3:ObjectCreated:Put", key, size, etag)

    last_mod = iso_for_s3(now_iso())
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<CopyObjectResult>'
        f'<LastModified>{esc(last_mod)}</LastModified>'
        f'<ETag>"{esc(etag)}"</ETag>'
        '</CopyObjectResult>'
    )
    resp = Response(body, status=200, mimetype="application/xml")
    if versioned:
        resp.headers["x-amz-version-id"] = final_vid
    return resp


# -----------------------------------------------------------------------------
# ListObjectsV2 / V1
# -----------------------------------------------------------------------------

def handle_list_objects_v2(bucket_row):
    bucket = bucket_row["name"]
    args = request.args
    prefix = args.get("prefix", "")
    delimiter = args.get("delimiter")
    encoding_type = args.get("encoding-type")
    max_keys = int(args.get("max-keys", "1000"))
    if max_keys > 1000:
        max_keys = 1000
    if max_keys < 0:
        max_keys = 0
    cont_token = args.get("continuation-token")
    start_after = args.get("start-after")
    fetch_owner = args.get("fetch-owner") in ("true", "1")

    conn = get_conn()
    cur = conn.execute(
        "SELECT key, size, etag, last_modified FROM objects "
        "WHERE bucket=? AND is_latest=1 AND is_delete_marker=0 ORDER BY key",
        (bucket,),
    )
    all_objs = [dict(r) for r in cur.fetchall()]
    if prefix:
        all_objs = [o for o in all_objs if o["key"].startswith(prefix)]

    if cont_token:
        # continuation-token resumes AT the saved key (inclusive)
        all_objs = [o for o in all_objs if o["key"] >= cont_token]
    elif start_after:
        all_objs = [o for o in all_objs if o["key"] > start_after]

    contents = []
    common_prefixes = []
    seen_prefix = set()
    truncated = False
    next_token = None
    count = 0

    for o in all_objs:
        key = o["key"]
        if delimiter:
            rest = key[len(prefix):]
            if delimiter in rest:
                idx = rest.find(delimiter)
                cp = prefix + rest[:idx + len(delimiter)]
                if cp not in seen_prefix:
                    seen_prefix.add(cp)
                    if count >= max_keys:
                        truncated = True
                        next_token = key
                        break
                    common_prefixes.append(cp)
                    count += 1
                continue
        if count >= max_keys:
            truncated = True
            next_token = key
            break
        contents.append(o)
        count += 1

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Name>{esc(bucket)}</Name>',
             f'<Prefix>{esc(prefix)}</Prefix>',
             f'<KeyCount>{len(contents) + len(common_prefixes)}</KeyCount>',
             f'<MaxKeys>{max_keys}</MaxKeys>',
             f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>']
    if delimiter:
        parts.append(f'<Delimiter>{esc(delimiter)}</Delimiter>')
    if encoding_type:
        parts.append(f'<EncodingType>{esc(encoding_type)}</EncodingType>')
    if cont_token:
        parts.append(f'<ContinuationToken>{esc(cont_token)}</ContinuationToken>')
    if start_after:
        parts.append(f'<StartAfter>{esc(start_after)}</StartAfter>')
    if truncated and next_token:
        parts.append(f'<NextContinuationToken>{esc(next_token)}</NextContinuationToken>')

    for o in contents:
        parts.append('<Contents>')
        k = o["key"]
        if encoding_type == "url":
            k = quote(k, safe="")
        parts.append(f'<Key>{esc(k)}</Key>')
        parts.append(f'<LastModified>{esc(iso_for_s3(o["last_modified"]))}</LastModified>')
        parts.append(f'<ETag>"{esc(o["etag"])}"</ETag>')
        parts.append(f'<Size>{o["size"]}</Size>')
        parts.append('<StorageClass>STANDARD</StorageClass>')
        parts.append('</Contents>')
    for cp in common_prefixes:
        if encoding_type == "url":
            cp = quote(cp, safe="")
        parts.append(f'<CommonPrefixes><Prefix>{esc(cp)}</Prefix></CommonPrefixes>')
    parts.append('</ListBucketResult>')
    return Response("".join(parts), mimetype="application/xml")


def handle_list_objects_v1(bucket_row):
    """V1 listing - supports marker, prefix, delimiter, max-keys."""
    bucket = bucket_row["name"]
    args = request.args
    prefix = args.get("prefix", "")
    delimiter = args.get("delimiter")
    marker = args.get("marker")
    max_keys = int(args.get("max-keys", "1000"))
    if max_keys > 1000:
        max_keys = 1000

    conn = get_conn()
    cur = conn.execute(
        "SELECT key, size, etag, last_modified FROM objects WHERE bucket=? AND is_latest=1 AND is_delete_marker=0 ORDER BY key",
        (bucket,),
    )
    all_objs = [dict(r) for r in cur.fetchall()]
    if prefix:
        all_objs = [o for o in all_objs if o["key"].startswith(prefix)]
    if marker:
        all_objs = [o for o in all_objs if o["key"] > marker]

    contents = []
    common_prefixes = []
    seen_prefix = set()
    truncated = False
    next_marker = None
    count = 0
    for o in all_objs:
        key = o["key"]
        if delimiter:
            rest = key[len(prefix):]
            if delimiter in rest:
                idx = rest.find(delimiter)
                cp = prefix + rest[:idx + len(delimiter)]
                if cp not in seen_prefix:
                    seen_prefix.add(cp)
                    if count >= max_keys:
                        truncated = True
                        break
                    common_prefixes.append(cp)
                    count += 1
                continue
        if count >= max_keys:
            truncated = True
            break
        contents.append(o)
        next_marker = key
        count += 1

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Name>{esc(bucket)}</Name>',
             f'<Prefix>{esc(prefix)}</Prefix>']
    if marker:
        parts.append(f'<Marker>{esc(marker)}</Marker>')
    parts.append(f'<MaxKeys>{max_keys}</MaxKeys>')
    parts.append(f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>')
    if delimiter:
        parts.append(f'<Delimiter>{esc(delimiter)}</Delimiter>')
    if truncated and next_marker and delimiter:
        parts.append(f'<NextMarker>{esc(next_marker)}</NextMarker>')
    for o in contents:
        parts.append('<Contents>')
        parts.append(f'<Key>{esc(o["key"])}</Key>')
        parts.append(f'<LastModified>{esc(iso_for_s3(o["last_modified"]))}</LastModified>')
        parts.append(f'<ETag>"{esc(o["etag"])}"</ETag>')
        parts.append(f'<Size>{o["size"]}</Size>')
        parts.append('<StorageClass>STANDARD</StorageClass>')
        parts.append('</Contents>')
    for cp in common_prefixes:
        parts.append(f'<CommonPrefixes><Prefix>{esc(cp)}</Prefix></CommonPrefixes>')
    parts.append('</ListBucketResult>')
    return Response("".join(parts), mimetype="application/xml")


def handle_list_object_versions(bucket_row):
    bucket = bucket_row["name"]
    args = request.args
    prefix = args.get("prefix", "")
    delimiter = args.get("delimiter")
    max_keys = int(args.get("max-keys", "1000"))
    if max_keys > 1000:
        max_keys = 1000

    conn = get_conn()
    cur = conn.execute(
        "SELECT * FROM objects WHERE bucket=? ORDER BY key, created_at DESC",
        (bucket,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if prefix:
        rows = [o for o in rows if o["key"].startswith(prefix)]

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Name>{esc(bucket)}</Name>',
             f'<Prefix>{esc(prefix)}</Prefix>',
             f'<MaxKeys>{max_keys}</MaxKeys>',
             '<IsTruncated>false</IsTruncated>']
    for r in rows[:max_keys]:
        if r["is_delete_marker"]:
            parts.append('<DeleteMarker>')
            parts.append(f'<Key>{esc(r["key"])}</Key>')
            parts.append(f'<VersionId>{esc(r["version_id"])}</VersionId>')
            parts.append(f'<IsLatest>{"true" if r["is_latest"] else "false"}</IsLatest>')
            parts.append(f'<LastModified>{esc(iso_for_s3(r["last_modified"]))}</LastModified>')
            parts.append('</DeleteMarker>')
        else:
            parts.append('<Version>')
            parts.append(f'<Key>{esc(r["key"])}</Key>')
            parts.append(f'<VersionId>{esc(r["version_id"])}</VersionId>')
            parts.append(f'<IsLatest>{"true" if r["is_latest"] else "false"}</IsLatest>')
            parts.append(f'<LastModified>{esc(iso_for_s3(r["last_modified"]))}</LastModified>')
            parts.append(f'<ETag>"{esc(r["etag"])}"</ETag>')
            parts.append(f'<Size>{r["size"]}</Size>')
            parts.append('<StorageClass>STANDARD</StorageClass>')
            parts.append('</Version>')
    parts.append('</ListVersionsResult>')
    return Response("".join(parts), mimetype="application/xml")


# -----------------------------------------------------------------------------
# Multipart
# -----------------------------------------------------------------------------

def handle_create_multipart(bucket_row, key):
    upload_id = gen_upload_id()
    content_type = request.headers.get("Content-Type", "application/octet-stream")
    content_disposition = request.headers.get("Content-Disposition")
    content_encoding = request.headers.get("Content-Encoding")
    cache_control = request.headers.get("Cache-Control")
    metadata = parse_amz_meta(request.headers)
    tags = parse_tag_query(request.headers.get("x-amz-tagging") or "")
    conn = get_conn()
    conn.execute(
        "INSERT INTO multipart_uploads (upload_id, bucket, key, initiated_at, content_type, content_disposition, content_encoding, cache_control, metadata, tagging) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (upload_id, bucket_row["name"], key, now_iso(), content_type, content_disposition,
         content_encoding, cache_control, json.dumps(metadata), json.dumps(tags) if tags else None),
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f'<Bucket>{esc(bucket_row["name"])}</Bucket>'
        f'<Key>{esc(key)}</Key>'
        f'<UploadId>{esc(upload_id)}</UploadId>'
        '</InitiateMultipartUploadResult>'
    )
    return Response(body, mimetype="application/xml")


def handle_upload_part(bucket_row, key):
    upload_id = request.args.get("uploadId")
    part_str = request.args.get("partNumber")
    if not upload_id or not part_str:
        raise S3Error("InvalidArgument")
    try:
        part_num = int(part_str)
    except ValueError:
        raise S3Error("InvalidArgument")
    if part_num < 1 or part_num > 10000:
        raise S3Error("InvalidArgument")

    conn = get_conn()
    cur = conn.execute("SELECT * FROM multipart_uploads WHERE upload_id=? AND bucket=? AND key=?",
                       (upload_id, bucket_row["name"], key))
    if not cur.fetchone():
        raise S3Error("NoSuchUpload")

    body = request.get_data() or b""
    digest = md5_hex(body)
    path = part_storage_path(upload_id, part_num)
    with open(path, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    conn.execute(
        "INSERT OR REPLACE INTO multipart_parts (upload_id, part_number, size, etag, storage_path, last_modified) "
        "VALUES (?,?,?,?,?,?)",
        (upload_id, part_num, len(body), digest, path, now_iso()),
    )
    resp = Response("", status=200)
    resp.headers["ETag"] = f'"{digest}"'
    return resp


def handle_complete_multipart(bucket_row, key):
    upload_id = request.args.get("uploadId")
    if not upload_id:
        raise S3Error("InvalidArgument")
    bucket = bucket_row["name"]
    conn = get_conn()
    cur = conn.execute("SELECT * FROM multipart_uploads WHERE upload_id=? AND bucket=? AND key=?",
                       (upload_id, bucket, key))
    mpu = cur.fetchone()
    if not mpu:
        raise S3Error("NoSuchUpload")
    mpu = dict(mpu)

    body = request.get_data() or b""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")

    requested_parts = []
    for part_el in root.iter():
        if part_el.tag.endswith("Part"):
            pn = None
            etag = None
            for c in part_el:
                if c.tag.endswith("PartNumber"):
                    pn = int(c.text or "0")
                elif c.tag.endswith("ETag"):
                    etag = (c.text or "").strip().strip('"')
            if pn:
                requested_parts.append((pn, etag))
    if not requested_parts:
        raise S3Error("MalformedXML")

    # check ascending order
    for i in range(1, len(requested_parts)):
        if requested_parts[i][0] <= requested_parts[i-1][0]:
            raise S3Error("InvalidPartOrder")

    cur = conn.execute(
        "SELECT * FROM multipart_parts WHERE upload_id=? ORDER BY part_number",
        (upload_id,),
    )
    stored_parts = {r["part_number"]: dict(r) for r in cur.fetchall()}

    # validate
    selected = []
    for pn, etag in requested_parts:
        sp = stored_parts.get(pn)
        if not sp:
            raise S3Error("InvalidPart")
        if etag and etag != sp["etag"]:
            raise S3Error("InvalidPart")
        selected.append(sp)

    # Validate sizes (non-last parts must be >= 5 MiB)
    for i, sp in enumerate(selected[:-1]):
        if sp["size"] < MIN_PART_SIZE:
            raise S3Error("EntityTooSmall")

    # Check tenant quota
    total_size = sum(p["size"] for p in selected)
    t = get_tenant(bucket_row["tenant"])
    if t and t.get("quota_bytes") is not None:
        used = tenant_used_bytes(bucket_row["tenant"])
        if used + total_size > t["quota_bytes"]:
            raise S3Error("QuotaExceeded")

    # Compute final ETag: md5(concat(raw_part_md5_bytes))-N
    md5_concat = b""
    for sp in selected:
        md5_concat += bytes.fromhex(sp["etag"])
    combined = hashlib.md5(md5_concat).hexdigest()
    final_etag = f"{combined}-{len(selected)}"

    # Concatenate parts to a new file
    new_vid = gen_version_id()
    new_path = storage_path_for(new_vid)
    tmp = new_path + ".tmp"
    with open(tmp, "wb") as out:
        for sp in selected:
            with open(sp["storage_path"], "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.rename(tmp, new_path)

    versioning = bucket_row.get("versioning", "Disabled")
    versioned = versioning == "Enabled"

    try:
        meta = json.loads(mpu.get("metadata") or "{}")
    except Exception:
        meta = {}

    lock = get_obj_lock(bucket, key)
    with lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not versioned:
                cur = conn.execute(
                    "SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                    (bucket, key),
                )
                old = cur.fetchall()
                conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
                for o in old:
                    if o["storage_path"]:
                        safe_remove(o["storage_path"])
                final_vid = "null"
            else:
                conn.execute(
                    "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                    (bucket, key),
                )
                final_vid = new_vid

            conn.execute(
                "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, content_type, content_disposition, content_encoding, cache_control, metadata, tagging, storage_path, created_at, last_modified) "
                "VALUES (?,?,?,1,0,?,?,?,?,?,?,?,?,?,?,?)",
                (bucket, key, final_vid, total_size, final_etag,
                 mpu.get("content_type"), mpu.get("content_disposition"),
                 mpu.get("content_encoding"), mpu.get("cache_control"),
                 json.dumps(meta), mpu.get("tagging"),
                 new_path, now_iso(), now_iso()),
            )
            conn.execute("DELETE FROM multipart_uploads WHERE upload_id=?", (upload_id,))
            conn.execute("DELETE FROM multipart_parts WHERE upload_id=?", (upload_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            safe_remove(new_path)
            raise

    # cleanup part files
    for sp in selected:
        safe_remove(sp["storage_path"])
    safe_rmtree(os.path.join(DATA_DIR, "parts", upload_id))

    _enqueue_notification(bucket_row, "s3:ObjectCreated:CompleteMultipartUpload", key, total_size, final_etag)

    location = f"/{bucket}/{quote(key, safe='/')}"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f'<Location>{esc(location)}</Location>'
        f'<Bucket>{esc(bucket)}</Bucket>'
        f'<Key>{esc(key)}</Key>'
        f'<ETag>"{esc(final_etag)}"</ETag>'
        '</CompleteMultipartUploadResult>'
    )
    resp = Response(body, mimetype="application/xml")
    if versioned:
        resp.headers["x-amz-version-id"] = final_vid
    return resp


def handle_abort_multipart(bucket_row, key):
    upload_id = request.args.get("uploadId")
    if not upload_id:
        raise S3Error("InvalidArgument")
    bucket = bucket_row["name"]
    conn = get_conn()
    cur = conn.execute("SELECT * FROM multipart_uploads WHERE upload_id=? AND bucket=? AND key=?",
                       (upload_id, bucket, key))
    if not cur.fetchone():
        raise S3Error("NoSuchUpload")
    cur = conn.execute("SELECT storage_path FROM multipart_parts WHERE upload_id=?", (upload_id,))
    for r in cur.fetchall():
        safe_remove(r["storage_path"])
    conn.execute("DELETE FROM multipart_parts WHERE upload_id=?", (upload_id,))
    conn.execute("DELETE FROM multipart_uploads WHERE upload_id=?", (upload_id,))
    safe_rmtree(os.path.join(DATA_DIR, "parts", upload_id))
    return Response("", status=204)


def handle_list_multipart_uploads(bucket_row):
    bucket = bucket_row["name"]
    conn = get_conn()
    cur = conn.execute(
        "SELECT upload_id, key, initiated_at FROM multipart_uploads WHERE bucket=? ORDER BY key, initiated_at",
        (bucket,),
    )
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Bucket>{esc(bucket)}</Bucket>',
             '<KeyMarker></KeyMarker><UploadIdMarker></UploadIdMarker>',
             '<NextKeyMarker></NextKeyMarker><NextUploadIdMarker></NextUploadIdMarker>',
             '<MaxUploads>1000</MaxUploads>',
             '<IsTruncated>false</IsTruncated>']
    for r in cur.fetchall():
        parts.append('<Upload>')
        parts.append(f'<Key>{esc(r["key"])}</Key>')
        parts.append(f'<UploadId>{esc(r["upload_id"])}</UploadId>')
        parts.append(f'<Initiated>{esc(iso_for_s3(r["initiated_at"]))}</Initiated>')
        parts.append('<StorageClass>STANDARD</StorageClass>')
        parts.append('</Upload>')
    parts.append('</ListMultipartUploadsResult>')
    return Response("".join(parts), mimetype="application/xml")


def handle_list_parts(bucket_row, key):
    upload_id = request.args.get("uploadId")
    if not upload_id:
        raise S3Error("InvalidArgument")
    bucket = bucket_row["name"]
    conn = get_conn()
    cur = conn.execute("SELECT 1 FROM multipart_uploads WHERE upload_id=? AND bucket=? AND key=?",
                       (upload_id, bucket, key))
    if not cur.fetchone():
        raise S3Error("NoSuchUpload")
    cur = conn.execute(
        "SELECT part_number, size, etag, last_modified FROM multipart_parts WHERE upload_id=? ORDER BY part_number",
        (upload_id,),
    )
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Bucket>{esc(bucket)}</Bucket>',
             f'<Key>{esc(key)}</Key>',
             f'<UploadId>{esc(upload_id)}</UploadId>',
             '<MaxParts>1000</MaxParts>',
             '<IsTruncated>false</IsTruncated>']
    for r in cur.fetchall():
        parts.append('<Part>')
        parts.append(f'<PartNumber>{r["part_number"]}</PartNumber>')
        parts.append(f'<LastModified>{esc(iso_for_s3(r["last_modified"]))}</LastModified>')
        parts.append(f'<ETag>"{esc(r["etag"])}"</ETag>')
        parts.append(f'<Size>{r["size"]}</Size>')
        parts.append('</Part>')
    parts.append('</ListPartsResult>')
    return Response("".join(parts), mimetype="application/xml")


# -----------------------------------------------------------------------------
# Multi-object delete (POST /<bucket>?delete)
# -----------------------------------------------------------------------------

def handle_multi_delete(bucket_row, access_key_id, tenant):
    bucket = bucket_row["name"]
    body = request.get_data() or b""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise S3Error("MalformedXML")
    quiet = False
    keys = []
    for child in root:
        if child.tag.endswith("Quiet"):
            quiet = (child.text or "").lower() == "true"
        elif child.tag.endswith("Object"):
            k = None
            v = None
            for c in child:
                if c.tag.endswith("Key"):
                    k = c.text or ""
                elif c.tag.endswith("VersionId"):
                    v = c.text
            if k is not None:
                keys.append((k, v))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']

    versioning = bucket_row.get("versioning", "Disabled")
    versioned = versioning == "Enabled"
    conn = get_conn()
    for key, vid in keys:
        try:
            lock = get_obj_lock(bucket, key)
            with lock:
                if vid:
                    cur = conn.execute("SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?",
                                       (bucket, key, vid))
                    row = cur.fetchone()
                    if row:
                        was_latest = row["is_latest"]
                        sp = row["storage_path"]
                        is_dm = row["is_delete_marker"]
                        conn.execute("DELETE FROM objects WHERE bucket=? AND key=? AND version_id=?",
                                     (bucket, key, vid))
                        if sp and not is_dm:
                            safe_remove(sp)
                        if was_latest:
                            cur = conn.execute(
                                "SELECT version_id FROM objects WHERE bucket=? AND key=? ORDER BY created_at DESC LIMIT 1",
                                (bucket, key),
                            )
                            r = cur.fetchone()
                            if r:
                                conn.execute(
                                    "UPDATE objects SET is_latest=1 WHERE bucket=? AND key=? AND version_id=?",
                                    (bucket, key, r["version_id"]),
                                )
                else:
                    if versioned:
                        dm_vid = gen_version_id()
                        conn.execute(
                            "UPDATE objects SET is_latest=0 WHERE bucket=? AND key=? AND is_latest=1",
                            (bucket, key),
                        )
                        conn.execute(
                            "INSERT INTO objects (bucket, key, version_id, is_latest, is_delete_marker, size, etag, storage_path, created_at, last_modified) "
                            "VALUES (?,?,?,1,1,0,'',NULL,?,?)",
                            (bucket, key, dm_vid, now_iso(), now_iso()),
                        )
                        if not quiet:
                            parts.append(f'<Deleted><Key>{esc(key)}</Key><DeleteMarker>true</DeleteMarker><DeleteMarkerVersionId>{esc(dm_vid)}</DeleteMarkerVersionId></Deleted>')
                            continue
                    else:
                        cur = conn.execute("SELECT version_id, storage_path FROM objects WHERE bucket=? AND key=?",
                                           (bucket, key))
                        rows = cur.fetchall()
                        conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
                        for r in rows:
                            if r["storage_path"]:
                                safe_remove(r["storage_path"])
            if not quiet:
                if vid:
                    parts.append(f'<Deleted><Key>{esc(key)}</Key><VersionId>{esc(vid)}</VersionId></Deleted>')
                else:
                    parts.append(f'<Deleted><Key>{esc(key)}</Key></Deleted>')
        except Exception as e:
            parts.append(f'<Error><Key>{esc(key)}</Key><Code>InternalError</Code><Message>{esc(str(e))}</Message></Error>')

    parts.append('</DeleteResult>')
    return Response("".join(parts), mimetype="application/xml")


# -----------------------------------------------------------------------------
# Notifications enqueue
# -----------------------------------------------------------------------------

def _enqueue_notification(bucket_row, event_name, key, size, etag):
    notif = bucket_row.get("notification")
    if not notif:
        return
    try:
        root = ET.fromstring(notif)
    except ET.ParseError:
        return
    configs = []
    for child in root:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag in ("CloudFunctionConfiguration", "QueueConfiguration", "TopicConfiguration"):
            cfg = {"events": [], "endpoint": None, "id": None, "prefix": None, "suffix": None}
            for c in child:
                ct = c.tag.split("}")[-1] if "}" in c.tag else c.tag
                if ct == "Id":
                    cfg["id"] = c.text
                elif ct == "Event":
                    cfg["events"].append(c.text)
                elif ct in ("Topic", "Queue", "CloudFunction"):
                    cfg["endpoint"] = c.text
                elif ct == "Filter":
                    for f in c.iter():
                        ft = f.tag.split("}")[-1] if "}" in f.tag else f.tag
                        if ft == "FilterRule":
                            name = None
                            value = None
                            for fc in f:
                                fct = fc.tag.split("}")[-1] if "}" in fc.tag else fc.tag
                                if fct == "Name":
                                    name = fc.text
                                elif fct == "Value":
                                    value = fc.text
                            if name == "prefix":
                                cfg["prefix"] = value
                            elif name == "suffix":
                                cfg["suffix"] = value
            configs.append(cfg)

    for cfg in configs:
        if not cfg["endpoint"]:
            continue
        # filter by event
        ok = False
        for e in cfg["events"]:
            if e == event_name:
                ok = True
                break
            if e.endswith(":*"):
                fam = e[:-2]
                if event_name.startswith(fam):
                    ok = True
                    break
        if not ok:
            continue
        if cfg.get("prefix") and not key.startswith(cfg["prefix"]):
            continue
        if cfg.get("suffix") and not key.endswith(cfg["suffix"]):
            continue

        record = {
            "Records": [{
                "eventVersion": "2.1",
                "eventSource": "halyard:s3",
                "eventTime": now_iso(),
                "eventName": event_name,
                "s3": {
                    "bucket": {"name": bucket_row["name"]},
                    "object": {"key": key, "size": size, "eTag": etag},
                },
            }]
        }
        conn = get_conn()
        conn.execute(
            "INSERT INTO notification_outbox (bucket, endpoint, payload, attempts, next_attempt, created_at) VALUES (?,?,?,0,0,?)",
            (bucket_row["name"], cfg["endpoint"], json.dumps(record), now_iso()),
        )
