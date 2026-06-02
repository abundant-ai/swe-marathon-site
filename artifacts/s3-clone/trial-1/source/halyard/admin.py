"""Admin API."""
import os

from flask import Blueprint, request, jsonify, Response

from .audit import write_event, list_events
from .db import get_conn
from .models import (
    list_tenants, get_tenant, create_tenant, delete_tenant,
    list_access_keys, create_access_key, delete_access_key,
    tenant_used_bytes, count_buckets_for_tenant, tenant_live_object_count,
)
from .util import now_iso, gen_request_id, valid_tenant_name


bp = Blueprint("admin", __name__)


def admin_token():
    return os.environ.get("HALYARD_ADMIN_TOKEN", "halyard-admin-dev-token")


def require_admin():
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + admin_token()
    if auth != expected:
        return jsonify({"error": "unauthorized"}), 401
    return None


def err(code, message, status=400):
    return jsonify({"error": code, "message": message}), status


def tenant_dict(t):
    return {
        "name": t["name"],
        "created_at": t["created_at"],
        "quota_bytes": t.get("quota_bytes"),
        "quota_buckets": t.get("quota_buckets"),
    }


@bp.before_request
def _audit_admin():
    request._admin_audit_path = request.path
    request._admin_request_id = gen_request_id()


@bp.after_request
def _admin_after(resp):
    if request.method in ("POST", "DELETE", "PUT"):
        try:
            write_event(None, None, request.method, request.path, None, None, resp.status_code, getattr(request, "_admin_request_id", gen_request_id()))
        except Exception:
            pass
    resp.headers["x-amz-request-id"] = getattr(request, "_admin_request_id", gen_request_id())
    return resp


@bp.route("/tenants", methods=["GET"])
def tenants_list():
    r = require_admin()
    if r:
        return r
    return jsonify({"tenants": [tenant_dict(t) for t in list_tenants()]})


@bp.route("/tenants", methods=["POST"])
def tenants_create():
    r = require_admin()
    if r:
        return r
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    if not name or not valid_tenant_name(name):
        return err("InvalidArgument", "Invalid tenant name")
    qb = body.get("quota_bytes")
    qbk = body.get("quota_buckets")
    if qb is not None and (not isinstance(qb, int) or qb <= 0):
        return err("InvalidArgument", "quota_bytes must be positive int or null")
    if qbk is not None and (not isinstance(qbk, int) or qbk <= 0):
        return err("InvalidArgument", "quota_buckets must be positive int or null")
    if get_tenant(name):
        return err("TenantAlreadyExists", "Tenant exists", 409)
    res = create_tenant(name, qb, qbk)
    return jsonify({"tenant": tenant_dict(res["tenant"]), "access_key": res["access_key"]}), 201


@bp.route("/tenants/<name>", methods=["GET"])
def tenants_get(name):
    r = require_admin()
    if r:
        return r
    t = get_tenant(name)
    if not t:
        return err("NoSuchTenant", "Tenant not found", 404)
    return jsonify({"tenant": tenant_dict(t)})


@bp.route("/tenants/<name>", methods=["DELETE"])
def tenants_delete(name):
    r = require_admin()
    if r:
        return r
    if name == "default":
        return err("CannotDeleteDefaultTenant", "Cannot delete default tenant", 403)
    t = get_tenant(name)
    if not t:
        return err("NoSuchTenant", "Tenant not found", 404)
    res = delete_tenant(name)
    if res == "TenantNotEmpty":
        return err("TenantNotEmpty", "Tenant has buckets", 409)
    return Response("", status=204)


@bp.route("/tenants/<name>/access-keys", methods=["POST"])
def access_keys_create(name):
    r = require_admin()
    if r:
        return r
    t = get_tenant(name)
    if not t:
        return err("NoSuchTenant", "Tenant not found", 404)
    ak = create_access_key(name)
    return jsonify({"access_key": ak}), 201


@bp.route("/tenants/<name>/access-keys", methods=["GET"])
def access_keys_list(name):
    r = require_admin()
    if r:
        return r
    t = get_tenant(name)
    if not t:
        return err("NoSuchTenant", "Tenant not found", 404)
    return jsonify({"access_keys": list_access_keys(name)})


@bp.route("/tenants/<name>/access-keys/<access_key_id>", methods=["DELETE"])
def access_keys_delete(name, access_key_id):
    r = require_admin()
    if r:
        return r
    t = get_tenant(name)
    if not t:
        return err("NoSuchTenant", "Tenant not found", 404)
    if not delete_access_key(name, access_key_id):
        return err("NoSuchAccessKey", "Access key not found", 404)
    return Response("", status=204)


@bp.route("/stats", methods=["GET"])
def stats():
    r = require_admin()
    if r:
        return r
    conn = get_conn()
    t_total = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    b_total = conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
    o_total = conn.execute("SELECT COUNT(*) FROM objects WHERE is_latest=1 AND is_delete_marker=0").fetchone()[0]
    bytes_total = conn.execute("SELECT COALESCE(SUM(size),0) FROM objects WHERE is_delete_marker=0").fetchone()[0]
    per_tenant = []
    for t in list_tenants():
        per_tenant.append({
            "name": t["name"],
            "buckets": count_buckets_for_tenant(t["name"]),
            "objects": tenant_live_object_count(t["name"]),
            "bytes": tenant_used_bytes(t["name"]),
            "quota_bytes": t.get("quota_bytes"),
            "quota_buckets": t.get("quota_buckets"),
        })
    return jsonify({
        "global": {
            "tenants": t_total,
            "buckets": b_total,
            "objects": o_total,
            "bytes": bytes_total,
        },
        "tenants": per_tenant,
    })


@bp.route("/audit", methods=["GET"])
def audit():
    r = require_admin()
    if r:
        return r
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    tenant = request.args.get("tenant")
    return jsonify({"events": list_events(limit, tenant)})
