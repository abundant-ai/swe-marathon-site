"""Data access layer."""
import json
import os

from .db import get_conn
from .util import (
    now_iso, gen_access_key_id, gen_secret_key, valid_tenant_name,
    DATA_DIR
)


def seed_default_tenant():
    conn = get_conn()
    cur = conn.execute("SELECT name FROM tenants WHERE name = 'default'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO tenants (name, created_at, quota_bytes, quota_buckets) VALUES (?, ?, NULL, NULL)",
            ("default", now_iso()),
        )
    cur = conn.execute("SELECT access_key_id FROM access_keys WHERE access_key_id = 'AKIAIOSFODNN7EXAMPLE'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO access_keys (access_key_id, secret_access_key, tenant, created_at) VALUES (?, ?, ?, ?)",
            ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "default", now_iso()),
        )


def get_tenant(name):
    conn = get_conn()
    cur = conn.execute("SELECT * FROM tenants WHERE name = ?", (name,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_tenants():
    conn = get_conn()
    cur = conn.execute("SELECT * FROM tenants ORDER BY name")
    return [dict(r) for r in cur.fetchall()]


def create_tenant(name, quota_bytes=None, quota_buckets=None):
    conn = get_conn()
    created_at = now_iso()
    conn.execute(
        "INSERT INTO tenants (name, created_at, quota_bytes, quota_buckets) VALUES (?, ?, ?, ?)",
        (name, created_at, quota_bytes, quota_buckets),
    )
    ak = gen_access_key_id()
    sk = gen_secret_key()
    conn.execute(
        "INSERT INTO access_keys (access_key_id, secret_access_key, tenant, created_at) VALUES (?, ?, ?, ?)",
        (ak, sk, name, created_at),
    )
    return {
        "tenant": {"name": name, "created_at": created_at, "quota_bytes": quota_bytes, "quota_buckets": quota_buckets},
        "access_key": {"access_key_id": ak, "secret_access_key": sk, "created_at": created_at},
    }


def delete_tenant(name):
    conn = get_conn()
    # check buckets
    cur = conn.execute("SELECT COUNT(*) FROM buckets WHERE tenant = ?", (name,))
    if cur.fetchone()[0] > 0:
        return "TenantNotEmpty"
    conn.execute("DELETE FROM access_keys WHERE tenant = ?", (name,))
    conn.execute("DELETE FROM tenants WHERE name = ?", (name,))
    return None


def list_access_keys(tenant):
    conn = get_conn()
    cur = conn.execute("SELECT access_key_id, created_at FROM access_keys WHERE tenant = ? ORDER BY created_at", (tenant,))
    return [dict(r) for r in cur.fetchall()]


def create_access_key(tenant):
    conn = get_conn()
    ak = gen_access_key_id()
    sk = gen_secret_key()
    created_at = now_iso()
    conn.execute(
        "INSERT INTO access_keys (access_key_id, secret_access_key, tenant, created_at) VALUES (?, ?, ?, ?)",
        (ak, sk, tenant, created_at),
    )
    return {"access_key_id": ak, "secret_access_key": sk, "created_at": created_at}


def delete_access_key(tenant, access_key_id):
    conn = get_conn()
    cur = conn.execute("SELECT 1 FROM access_keys WHERE tenant = ? AND access_key_id = ?", (tenant, access_key_id))
    if not cur.fetchone():
        return False
    conn.execute("DELETE FROM access_keys WHERE tenant = ? AND access_key_id = ?", (tenant, access_key_id))
    return True


def lookup_access_key(access_key_id):
    """Return (tenant_name, secret) or None."""
    conn = get_conn()
    cur = conn.execute("SELECT tenant, secret_access_key FROM access_keys WHERE access_key_id = ?", (access_key_id,))
    row = cur.fetchone()
    if not row:
        return None
    return row["tenant"], row["secret_access_key"]


def get_bucket(name):
    conn = get_conn()
    cur = conn.execute("SELECT * FROM buckets WHERE name = ?", (name,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_buckets_for_tenant(tenant):
    conn = get_conn()
    cur = conn.execute("SELECT * FROM buckets WHERE tenant = ? ORDER BY created_at, name", (tenant,))
    return [dict(r) for r in cur.fetchall()]


def count_buckets_for_tenant(tenant):
    conn = get_conn()
    cur = conn.execute("SELECT COUNT(*) FROM buckets WHERE tenant = ?", (tenant,))
    return cur.fetchone()[0]


def create_bucket(name, tenant):
    conn = get_conn()
    conn.execute(
        "INSERT INTO buckets (name, tenant, created_at, versioning) VALUES (?, ?, ?, 'Disabled')",
        (name, tenant, now_iso()),
    )


def delete_bucket(name):
    conn = get_conn()
    conn.execute("DELETE FROM buckets WHERE name = ?", (name,))


def update_bucket_field(name, field, value):
    assert field in {"versioning", "cors", "lifecycle", "policy", "notification"}
    conn = get_conn()
    conn.execute(f"UPDATE buckets SET {field} = ? WHERE name = ?", (value, name))


def tenant_used_bytes(tenant):
    conn = get_conn()
    cur = conn.execute(
        "SELECT COALESCE(SUM(o.size), 0) FROM objects o JOIN buckets b ON o.bucket = b.name "
        "WHERE b.tenant = ? AND o.is_delete_marker = 0",
        (tenant,),
    )
    return cur.fetchone()[0]


def tenant_live_object_count(tenant):
    conn = get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) FROM objects o JOIN buckets b ON o.bucket = b.name "
        "WHERE b.tenant = ? AND o.is_latest = 1 AND o.is_delete_marker = 0",
        (tenant,),
    )
    return cur.fetchone()[0]
