"""Main Flask application."""
import os
import threading

from flask import Flask, request, Response, jsonify, abort, redirect

from . import s3 as s3mod
from .admin import bp as admin_bp
from .audit import write_event
from .auth import authenticate_s3
from .console import bp as console_bp
from .db import init_db, get_conn
from .errors import S3Error
from .models import seed_default_tenant, get_bucket, get_tenant
from .util import gen_request_id, valid_bucket_name


app = Flask(__name__)
app.url_map.strict_slashes = False


# Initialize DB and seed default tenant on app startup
_init_lock = threading.Lock()
_initialized = False


def ensure_init():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        init_db()
        seed_default_tenant()
        # spawn lifecycle / outbox workers
        from .workers import start_workers
        start_workers()
        _initialized = True


@app.before_request
def _before():
    ensure_init()
    request._halyard_request_id = gen_request_id()


@app.errorhandler(S3Error)
def handle_s3error(e):
    rid = getattr(request, "_halyard_request_id", None) or gen_request_id()
    resp = s3mod.make_error_response(e.code, e.message, rid, e.status, **e.extra)
    return resp


# Register blueprints
app.register_blueprint(admin_bp, url_prefix="/_admin")
app.register_blueprint(console_bp, url_prefix="/console")


@app.route("/_health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# Catch-all S3 routes
@app.route("/", defaults={"path": ""}, methods=["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"])
def s3_dispatch(path):
    return _dispatch_s3(path)


def _dispatch_s3(path):
    """Path-style S3: /<bucket> or /<bucket>/<key>."""
    method = request.method
    rid = request._halyard_request_id

    # OPTIONS preflight
    if method == "OPTIONS":
        return _handle_cors_preflight(path)

    # Parse path
    if not path or path == "":
        # GET / -> list buckets (requires auth)
        bucket = ""
        key = None
    else:
        if "/" in path:
            bucket, key = path.split("/", 1)
        else:
            bucket = path
            key = None

    audit_path = request.environ.get("RAW_URI", request.path)
    audit_bucket = bucket or None
    audit_key = key

    # Authenticate (always try)
    access_key_id = None
    tenant = None
    try:
        try:
            access_key_id, tenant = authenticate_s3()
        except S3Error as e:
            # Auth failure
            if method in ("PUT", "POST", "DELETE"):
                write_event(None, None, method, audit_path, audit_bucket, audit_key, e.status, rid)
            raise

        if access_key_id is None:
            # Anonymous - only allowed if bucket policy permits, and only GET/HEAD
            if method not in ("GET", "HEAD"):
                if method in ("PUT", "POST", "DELETE"):
                    write_event(None, None, method, audit_path, audit_bucket, audit_key, 403, rid)
                raise S3Error("AccessDenied")

        # GET / list-buckets
        if not bucket:
            if method != "GET":
                raise S3Error("MethodNotAllowed")
            if access_key_id is None:
                raise S3Error("AccessDenied")
            return s3mod.handle_service_get(access_key_id, tenant)

        # PUT bucket (CreateBucket)
        if method == "PUT" and key is None and not request.args:
            if access_key_id is None:
                raise S3Error("AccessDenied")
            try:
                resp = s3mod.handle_create_bucket(bucket, access_key_id, tenant)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp

        # Look up bucket
        bucket_row = get_bucket(bucket)
        if bucket_row is None:
            if method == "PUT" and key is None:
                # CreateBucket sub-resources need bucket
                pass
            raise S3Error("NoSuchBucket")

        # bucket-level requests (key is None)
        if key is None:
            return _dispatch_bucket(bucket_row, method, access_key_id, tenant, audit_path, audit_bucket, audit_key, rid)

        # object-level
        return _dispatch_object(bucket_row, key, method, access_key_id, tenant, audit_path, audit_bucket, audit_key, rid)

    except S3Error:
        raise


def _dispatch_bucket(bucket_row, method, access_key_id, tenant, audit_path, audit_bucket, audit_key, rid):
    args = request.args

    # Bucket-level operations
    is_owner = (tenant is not None and bucket_row["tenant"] == tenant)

    if method == "DELETE":
        if not is_owner:
            raise S3Error("AccessDenied")
        if "policy" in args:
            resp = s3mod.handle_delete_policy(bucket_row)
        elif "cors" in args:
            resp = s3mod.handle_delete_cors(bucket_row)
        elif "lifecycle" in args:
            resp = s3mod.handle_delete_lifecycle(bucket_row)
        elif "tagging" in args:
            resp = s3mod.handle_delete_tagging(bucket_row)
        else:
            try:
                resp = s3mod.handle_delete_bucket(bucket_row, access_key_id, tenant)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
        write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
        return resp

    if method == "PUT":
        if not is_owner:
            raise S3Error("AccessDenied")
        try:
            if "policy" in args:
                resp = s3mod.handle_put_policy(bucket_row)
            elif "cors" in args:
                resp = s3mod.handle_put_cors(bucket_row)
            elif "lifecycle" in args:
                resp = s3mod.handle_put_lifecycle(bucket_row)
            elif "versioning" in args:
                resp = s3mod.handle_put_versioning(bucket_row)
            elif "notification" in args:
                resp = s3mod.handle_put_notification(bucket_row)
            elif "tagging" in args:
                resp = s3mod.handle_put_tagging(bucket_row)
            else:
                # CreateBucket-style PUT against existing bucket
                if bucket_row["tenant"] == tenant:
                    raise S3Error("BucketAlreadyOwnedByYou")
                raise S3Error("BucketAlreadyExists")
        except S3Error as e:
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
            raise
        write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
        return resp

    if method == "POST":
        # ?delete - multi-object delete
        if "delete" in args:
            # Need ListBucket+DeleteObject; for simplicity owner-only for cross-tenant unless allowed
            if not is_owner:
                # check policy
                from . import policy as policy_mod
                pol = bucket_row.get("policy")
                if not pol or policy_mod.evaluate(pol, "s3:DeleteObject", tenant, bucket_row["name"], None) != "Allow":
                    raise S3Error("AccessDenied")
            try:
                resp = s3mod.handle_multi_delete(bucket_row, access_key_id, tenant)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp
        raise S3Error("MethodNotAllowed")

    if method in ("GET", "HEAD"):
        if "policy" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_get_policy(bucket_row)
        if "cors" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_get_cors(bucket_row)
        if "lifecycle" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_get_lifecycle(bucket_row)
        if "versioning" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_get_versioning(bucket_row)
        if "notification" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_get_notification(bucket_row)
        if "location" in args:
            return s3mod.handle_get_location(bucket_row)
        if "tagging" in args:
            return s3mod.handle_get_tagging(bucket_row)
        if "uploads" in args:
            if not is_owner:
                raise S3Error("AccessDenied")
            return s3mod.handle_list_multipart_uploads(bucket_row)
        if "versions" in args:
            if not is_owner:
                # check ListBucket policy
                from . import policy as policy_mod
                pol = bucket_row.get("policy")
                if not pol or policy_mod.evaluate(pol, "s3:ListBucket", tenant, bucket_row["name"], None) != "Allow":
                    raise S3Error("AccessDenied")
            return s3mod.handle_list_object_versions(bucket_row)

        # listing
        if method == "HEAD":
            if not is_owner:
                from . import policy as policy_mod
                pol = bucket_row.get("policy")
                if not pol or policy_mod.evaluate(pol, "s3:ListBucket", tenant, bucket_row["name"], None) != "Allow":
                    raise S3Error("AccessDenied")
            return s3mod.handle_head_bucket(bucket_row)

        # GET listing
        if not is_owner:
            from . import policy as policy_mod
            pol = bucket_row.get("policy")
            if not pol or policy_mod.evaluate(pol, "s3:ListBucket", tenant, bucket_row["name"], None) != "Allow":
                raise S3Error("AccessDenied")
        if args.get("list-type") == "2":
            return s3mod.handle_list_objects_v2(bucket_row)
        return s3mod.handle_list_objects_v1(bucket_row)

    raise S3Error("MethodNotAllowed")


def _dispatch_object(bucket_row, key, method, access_key_id, tenant, audit_path, audit_bucket, audit_key, rid):
    args = request.args
    is_owner = (tenant is not None and bucket_row["tenant"] == tenant)

    # Authorize for non-owners via policy
    def check_anon_or_policy(action):
        if is_owner:
            return
        if access_key_id is None and method not in ("GET", "HEAD"):
            raise S3Error("AccessDenied")
        from . import policy as policy_mod
        pol = bucket_row.get("policy")
        if pol:
            decision = policy_mod.evaluate(pol, action, tenant, bucket_row["name"], key)
            if decision == "Allow":
                return
            if decision == "Deny":
                raise S3Error("AccessDenied")
        raise S3Error("AccessDenied")

    if method == "GET":
        if "uploadId" in args:
            check_anon_or_policy("s3:ListBucket")
            return s3mod.handle_list_parts(bucket_row, key)
        if "tagging" in args:
            check_anon_or_policy("s3:GetObject")
            return s3mod.handle_get_tagging(bucket_row, key, args.get("versionId"))
        check_anon_or_policy("s3:GetObject")
        return s3mod.handle_get_object(bucket_row, key, access_key_id, tenant)

    if method == "HEAD":
        check_anon_or_policy("s3:GetObject")
        return s3mod.handle_head_object(bucket_row, key, access_key_id, tenant)

    if method == "PUT":
        if "uploadId" in args and "partNumber" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_upload_part(bucket_row, key)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp

        if "tagging" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_put_tagging(bucket_row, key, args.get("versionId"))
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp

        # CopyObject
        if request.headers.get("x-amz-copy-source"):
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_copy_object(bucket_row, key, access_key_id, tenant)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp

        check_anon_or_policy("s3:PutObject")
        try:
            resp = s3mod.handle_put_object(bucket_row, key, access_key_id, tenant)
        except S3Error as e:
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
            raise
        write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
        return resp

    if method == "POST":
        if "uploads" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_create_multipart(bucket_row, key)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp
        if "uploadId" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_complete_multipart(bucket_row, key)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp
        raise S3Error("MethodNotAllowed")

    if method == "DELETE":
        if "uploadId" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_abort_multipart(bucket_row, key)
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp
        if "tagging" in args:
            check_anon_or_policy("s3:PutObject")
            try:
                resp = s3mod.handle_delete_tagging(bucket_row, key, args.get("versionId"))
            except S3Error as e:
                write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
                raise
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
            return resp
        check_anon_or_policy("s3:DeleteObject" if False else "s3:PutObject")
        try:
            resp = s3mod.handle_delete_object(bucket_row, key, access_key_id, tenant)
        except S3Error as e:
            write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, e.status, rid)
            raise
        write_event(tenant, access_key_id, method, audit_path, audit_bucket, audit_key, resp.status_code, rid)
        return resp

    raise S3Error("MethodNotAllowed")


def _handle_cors_preflight(path):
    """CORS preflight - check the bucket CORS configuration."""
    if "/" in path:
        bucket = path.split("/", 1)[0]
    else:
        bucket = path
    if not bucket:
        return Response("", status=200)
    bucket_row = get_bucket(bucket)
    if not bucket_row:
        raise S3Error("NoSuchBucket")
    cors_xml = bucket_row.get("cors")
    if not cors_xml:
        raise S3Error("CORSForbidden", "CORS not configured", status=403)
    origin = request.headers.get("Origin", "")
    method = request.headers.get("Access-Control-Request-Method", "")
    headers = request.headers.get("Access-Control-Request-Headers", "")
    # Parse cors and find matching rule
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(cors_xml)
    except ET.ParseError:
        raise S3Error("CORSForbidden")
    allowed_origins = []
    allowed_methods = []
    exposed_headers = []
    allowed_headers = []
    max_age = None
    for rule in root.iter():
        tag = rule.tag.split("}")[-1] if "}" in rule.tag else rule.tag
        if tag == "CORSRule":
            r_origins = []
            r_methods = []
            r_allowed_h = []
            r_exposed = []
            r_max = None
            for c in rule:
                ct = c.tag.split("}")[-1] if "}" in c.tag else c.tag
                if ct == "AllowedOrigin":
                    r_origins.append(c.text or "")
                elif ct == "AllowedMethod":
                    r_methods.append(c.text or "")
                elif ct == "AllowedHeader":
                    r_allowed_h.append(c.text or "")
                elif ct == "ExposeHeader":
                    r_exposed.append(c.text or "")
                elif ct == "MaxAgeSeconds":
                    r_max = c.text
            # match origin and method
            origin_match = any(o == "*" or o == origin or
                               (o.startswith("http") and "*" in o and _match_wildcard(o, origin))
                               for o in r_origins)
            method_match = method in r_methods
            if origin_match and method_match:
                allowed_origins = r_origins
                allowed_methods = r_methods
                allowed_headers = r_allowed_h
                exposed_headers = r_exposed
                max_age = r_max
                break

    if not allowed_methods:
        raise S3Error("CORSForbidden", "Origin/method not allowed", status=403)

    headers_resp = {
        "Access-Control-Allow-Origin": "*" if "*" in allowed_origins else origin,
        "Access-Control-Allow-Methods": ",".join(allowed_methods),
    }
    if allowed_headers:
        if "*" in allowed_headers:
            headers_resp["Access-Control-Allow-Headers"] = headers or "*"
        else:
            headers_resp["Access-Control-Allow-Headers"] = ",".join(allowed_headers)
    if exposed_headers:
        headers_resp["Access-Control-Expose-Headers"] = ",".join(exposed_headers)
    if max_age:
        headers_resp["Access-Control-Max-Age"] = max_age
    return Response("", status=200, headers=headers_resp)


def _match_wildcard(pattern, value):
    import re
    p = re.escape(pattern).replace(r"\*", ".*")
    return re.match("^" + p + "$", value) is not None


@app.after_request
def _add_cors_origin(resp):
    origin = request.headers.get("Origin")
    if origin and request.method != "OPTIONS":
        try:
            path = request.path.lstrip("/")
            if path:
                bucket = path.split("/", 1)[0]
                bucket_row = get_bucket(bucket) if bucket else None
                if bucket_row and bucket_row.get("cors"):
                    from xml.etree import ElementTree as ET
                    try:
                        root = ET.fromstring(bucket_row["cors"])
                        method = request.method
                        for rule in root.iter():
                            tag = rule.tag.split("}")[-1] if "}" in rule.tag else rule.tag
                            if tag == "CORSRule":
                                origins = [c.text for c in rule if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == "AllowedOrigin"]
                                methods = [c.text for c in rule if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == "AllowedMethod"]
                                if method in methods and (origin in origins or "*" in origins):
                                    resp.headers["Access-Control-Allow-Origin"] = "*" if "*" in origins else origin
                                    resp.headers["Vary"] = "Origin"
                                    break
                    except Exception:
                        pass
        except Exception:
            pass
    return resp
