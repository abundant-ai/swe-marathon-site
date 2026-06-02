from . import db
from .util import now_iso, gen_access_key_id, gen_secret_key
from .errors import S3Error

DEFAULT_TENANT = 'default'
DEFAULT_ACCESS_KEY = 'AKIAIOSFODNN7EXAMPLE'
DEFAULT_SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'

def seed_default():
    c = db.conn()
    row = c.execute('SELECT 1 FROM tenants WHERE name=?', (DEFAULT_TENANT,)).fetchone()
    if not row:
        c.execute('INSERT INTO tenants(name,created_at,quota_bytes,quota_buckets) VALUES(?,?,?,?)',
                  (DEFAULT_TENANT, now_iso(), None, None))
    row = c.execute('SELECT 1 FROM access_keys WHERE access_key_id=?', (DEFAULT_ACCESS_KEY,)).fetchone()
    if not row:
        c.execute('INSERT INTO access_keys(access_key_id,tenant,secret_access_key,created_at,revoked) VALUES(?,?,?,?,0)',
                  (DEFAULT_ACCESS_KEY, DEFAULT_TENANT, DEFAULT_SECRET, now_iso()))

def create_tenant(name, quota_bytes=None, quota_buckets=None):
    c = db.conn()
    if c.execute('SELECT 1 FROM tenants WHERE name=?', (name,)).fetchone():
        raise S3Error('TenantAlreadyExists', 'Tenant exists', status=409)
    ts = now_iso()
    c.execute('INSERT INTO tenants(name,created_at,quota_bytes,quota_buckets) VALUES(?,?,?,?)',
              (name, ts, quota_bytes, quota_buckets))
    ak = gen_access_key_id()
    sk = gen_secret_key()
    c.execute('INSERT INTO access_keys(access_key_id,tenant,secret_access_key,created_at,revoked) VALUES(?,?,?,?,0)',
              (ak, name, sk, ts))
    return {
        'tenant': {'name': name, 'created_at': ts, 'quota_bytes': quota_bytes, 'quota_buckets': quota_buckets},
        'access_key': {'access_key_id': ak, 'secret_access_key': sk, 'created_at': ts},
    }

def get_tenant(name):
    c = db.conn()
    row = c.execute('SELECT * FROM tenants WHERE name=?', (name,)).fetchone()
    if not row:
        return None
    return {'name': row['name'], 'created_at': row['created_at'],
            'quota_bytes': row['quota_bytes'], 'quota_buckets': row['quota_buckets']}

def list_tenants():
    c = db.conn()
    rows = c.execute('SELECT * FROM tenants ORDER BY name').fetchall()
    return [{'name': r['name'], 'created_at': r['created_at'],
             'quota_bytes': r['quota_bytes'], 'quota_buckets': r['quota_buckets']} for r in rows]

def delete_tenant(name):
    if name == DEFAULT_TENANT:
        return 'CannotDeleteDefaultTenant'
    c = db.conn()
    if not c.execute('SELECT 1 FROM tenants WHERE name=?', (name,)).fetchone():
        return 'NoSuchTenant'
    if c.execute('SELECT 1 FROM buckets WHERE tenant=?', (name,)).fetchone():
        return 'TenantNotEmpty'
    c.execute('DELETE FROM access_keys WHERE tenant=?', (name,))
    c.execute('DELETE FROM tenants WHERE name=?', (name,))
    return None

def create_access_key(tenant):
    c = db.conn()
    if not c.execute('SELECT 1 FROM tenants WHERE name=?', (tenant,)).fetchone():
        return None
    ak = gen_access_key_id()
    sk = gen_secret_key()
    ts = now_iso()
    c.execute('INSERT INTO access_keys(access_key_id,tenant,secret_access_key,created_at,revoked) VALUES(?,?,?,?,0)',
              (ak, tenant, sk, ts))
    return {'access_key_id': ak, 'secret_access_key': sk, 'created_at': ts}

def list_access_keys(tenant):
    c = db.conn()
    rows = c.execute('SELECT access_key_id, created_at FROM access_keys WHERE tenant=? AND revoked=0 ORDER BY created_at', (tenant,)).fetchall()
    return [{'access_key_id': r['access_key_id'], 'created_at': r['created_at']} for r in rows]

def revoke_access_key(tenant, access_key_id):
    c = db.conn()
    row = c.execute('SELECT * FROM access_keys WHERE access_key_id=? AND tenant=?', (access_key_id, tenant)).fetchone()
    if not row:
        return False
    c.execute('UPDATE access_keys SET revoked=1 WHERE access_key_id=?', (access_key_id,))
    return True

def get_tenant_used_bytes(tenant):
    c = db.conn()
    row = c.execute('''SELECT COALESCE(SUM(o.size),0) AS s FROM objects o JOIN buckets b ON o.bucket=b.name
                       WHERE b.tenant=? AND o.delete_marker=0''', (tenant,)).fetchone()
    return int(row['s'] or 0)

def get_tenant_bucket_count(tenant):
    c = db.conn()
    row = c.execute('SELECT COUNT(*) AS n FROM buckets WHERE tenant=?', (tenant,)).fetchone()
    return int(row['n'] or 0)
