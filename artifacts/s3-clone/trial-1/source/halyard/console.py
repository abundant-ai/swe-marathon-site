"""Console API."""
import os

from flask import Blueprint, request, jsonify, Response, send_from_directory

from .db import get_conn
from .models import (
    lookup_access_key, get_tenant,
    list_buckets_for_tenant, count_buckets_for_tenant, get_bucket, create_bucket, delete_bucket,
    list_access_keys, create_access_key, delete_access_key,
)
from .util import gen_session_token, now_iso, valid_bucket_name


bp = Blueprint("console", __name__)


def _get_session():
    """Return (token, tenant, access_key_id) or None."""
    tok = request.cookies.get("halyard_console")
    if not tok:
        return None
    conn = get_conn()
    cur = conn.execute("SELECT * FROM console_sessions WHERE token=?", (tok,))
    row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def require_session():
    s = _get_session()
    if s is None:
        return None, (jsonify({"error": "unauthenticated"}), 401)
    return s, None


@bp.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    ak = body.get("access_key_id")
    sk = body.get("secret_access_key")
    if not ak or not sk:
        return jsonify({"error": "InvalidArgument"}), 400
    res = lookup_access_key(ak)
    if not res:
        return jsonify({"error": "InvalidCredentials"}), 401
    tenant, secret = res
    if secret != sk:
        return jsonify({"error": "InvalidCredentials"}), 401
    token = gen_session_token()
    conn = get_conn()
    conn.execute(
        "INSERT INTO console_sessions (token, access_key_id, tenant, created_at) VALUES (?,?,?,?)",
        (token, ak, tenant, now_iso()),
    )
    resp = jsonify({"tenant": tenant, "access_key_id": ak})
    resp.set_cookie("halyard_console", token, httponly=True, samesite="Lax", path="/")
    return resp


@bp.route("/api/logout", methods=["POST"])
def logout():
    tok = request.cookies.get("halyard_console")
    if tok:
        conn = get_conn()
        conn.execute("DELETE FROM console_sessions WHERE token=?", (tok,))
    resp = jsonify({"ok": True})
    resp.delete_cookie("halyard_console", path="/")
    return resp


@bp.route("/api/me", methods=["GET"])
def me():
    s, err = require_session()
    if err:
        return err
    return jsonify({"tenant": s["tenant"], "access_key_id": s["access_key_id"]})


@bp.route("/api/buckets", methods=["GET"])
def buckets_list():
    s, err = require_session()
    if err:
        return err
    bs = list_buckets_for_tenant(s["tenant"])
    return jsonify({"buckets": [{"name": b["name"], "created_at": b["created_at"]} for b in bs]})


@bp.route("/api/buckets", methods=["POST"])
def buckets_create():
    s, err = require_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    if not valid_bucket_name(name):
        return jsonify({"error": "InvalidBucketName", "message": "Invalid bucket name"}), 400
    existing = get_bucket(name)
    if existing:
        if existing["tenant"] == s["tenant"]:
            return jsonify({"error": "BucketAlreadyOwnedByYou"}), 409
        return jsonify({"error": "BucketAlreadyExists"}), 409
    t = get_tenant(s["tenant"])
    if t and t.get("quota_buckets") is not None:
        if count_buckets_for_tenant(s["tenant"]) >= t["quota_buckets"]:
            return jsonify({"error": "TooManyBuckets"}), 403
    create_bucket(name, s["tenant"])
    return jsonify({"name": name, "created_at": now_iso()}), 201


@bp.route("/api/buckets/<name>", methods=["DELETE"])
def buckets_delete(name):
    s, err = require_session()
    if err:
        return err
    b = get_bucket(name)
    if not b:
        return jsonify({"error": "NoSuchBucket"}), 404
    if b["tenant"] != s["tenant"]:
        return jsonify({"error": "AccessDenied"}), 403
    conn = get_conn()
    cur = conn.execute("SELECT COUNT(*) FROM objects WHERE bucket=?", (name,))
    if cur.fetchone()[0] > 0:
        return jsonify({"error": "BucketNotEmpty"}), 409
    delete_bucket(name)
    return Response("", status=204)


@bp.route("/api/access-keys", methods=["GET"])
def access_keys_list_route():
    s, err = require_session()
    if err:
        return err
    return jsonify({"access_keys": list_access_keys(s["tenant"])})


@bp.route("/api/access-keys", methods=["POST"])
def access_keys_create_route():
    s, err = require_session()
    if err:
        return err
    ak = create_access_key(s["tenant"])
    return jsonify({"access_key": ak}), 201


@bp.route("/api/access-keys/<id>", methods=["DELETE"])
def access_keys_delete_route(id):
    s, err = require_session()
    if err:
        return err
    if id == s["access_key_id"]:
        return jsonify({"error": "CannotDeleteCurrentAccessKey"}), 409
    if not delete_access_key(s["tenant"], id):
        return jsonify({"error": "NoSuchAccessKey"}), 404
    return Response("", status=204)


# Static UI
@bp.route("/", defaults={"path": ""})
@bp.route("/<path:path>")
def index(path):
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if path and os.path.isfile(os.path.join(static_dir, path)):
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, "index.html")


@bp.route("/static/<path:path>")
def static_files(path):
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return send_from_directory(static_dir, path)
