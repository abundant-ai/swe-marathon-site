import os
import json
from flask import request, Response, jsonify
from . import db, tenants, audit
from .util import valid_tenant_name, gen_request_id

ADMIN_TOKEN = os.environ.get('HALYARD_ADMIN_TOKEN', 'halyard-admin-dev-token')

def _check_auth():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    return auth[7:] == ADMIN_TOKEN

def _err(code, msg, status=400):
    return jsonify({'error': code, 'message': msg}), status

def _audit(method, path, status, request_id):
    audit.log_event(tenant=None, access_key_id=None, method=method, path=path,
                    bucket=None, key=None, status=status, request_id=request_id)

def handle(path, method):
    if not _check_auth():
        rid = gen_request_id()
        if method in ('POST','DELETE'):
            audit.log_event(tenant=None, access_key_id=None, method=method, path=request.path,
                            bucket=None, key=None, status=401, request_id=rid)
        return jsonify({'error':'unauthorized'}), 401
    rid = gen_request_id()
    parts = [p for p in path.split('/') if p]
    # /tenants ...
    try:
        if parts == ['tenants']:
            if method == 'GET':
                return jsonify({'tenants': tenants.list_tenants()})
            if method == 'POST':
                body = request.get_json(force=True, silent=True) or {}
                name = body.get('name', '')
                if not valid_tenant_name(name):
                    _audit(method, request.path, 400, rid)
                    return _err('InvalidTenantName', 'Invalid tenant name', 400)
                qb = body.get('quota_bytes')
                qbk = body.get('quota_buckets')
                for q in (qb, qbk):
                    if q is not None and (not isinstance(q, int) or q <= 0):
                        _audit(method, request.path, 400, rid)
                        return _err('InvalidQuota', 'Quotas must be positive integers or null', 400)
                if tenants.get_tenant(name):
                    _audit(method, request.path, 409, rid)
                    return _err('TenantAlreadyExists', 'Tenant exists', 409)
                res = tenants.create_tenant(name, qb, qbk)
                _audit(method, request.path, 201, rid)
                return jsonify(res), 201
        if len(parts) == 2 and parts[0] == 'tenants':
            name = parts[1]
            if method == 'GET':
                t = tenants.get_tenant(name)
                if not t:
                    return _err('NoSuchTenant','No such tenant', 404)
                return jsonify({'tenant': t})
            if method == 'DELETE':
                err = tenants.delete_tenant(name)
                if err == 'NoSuchTenant':
                    _audit(method, request.path, 404, rid)
                    return _err('NoSuchTenant','No such tenant', 404)
                if err == 'TenantNotEmpty':
                    _audit(method, request.path, 409, rid)
                    return _err('TenantNotEmpty','Tenant not empty', 409)
                if err == 'CannotDeleteDefaultTenant':
                    _audit(method, request.path, 403, rid)
                    return _err('CannotDeleteDefaultTenant','Cannot delete default tenant', 403)
                _audit(method, request.path, 204, rid)
                return Response('', status=204)
        if len(parts) == 3 and parts[0] == 'tenants' and parts[2] == 'access-keys':
            name = parts[1]
            if not tenants.get_tenant(name):
                if method in ('POST','DELETE'):
                    _audit(method, request.path, 404, rid)
                return _err('NoSuchTenant','No such tenant', 404)
            if method == 'GET':
                return jsonify({'access_keys': tenants.list_access_keys(name)})
            if method == 'POST':
                ak = tenants.create_access_key(name)
                _audit(method, request.path, 201, rid)
                return jsonify({'access_key': ak}), 201
        if len(parts) == 4 and parts[0] == 'tenants' and parts[2] == 'access-keys':
            name = parts[1]; akid = parts[3]
            if method == 'DELETE':
                if not tenants.get_tenant(name):
                    _audit(method, request.path, 404, rid)
                    return _err('NoSuchTenant','No such tenant', 404)
                ok = tenants.revoke_access_key(name, akid)
                if not ok:
                    _audit(method, request.path, 404, rid)
                    return _err('NoSuchAccessKey','No such key', 404)
                _audit(method, request.path, 204, rid)
                return Response('', status=204)
        if parts == ['stats'] and method == 'GET':
            return jsonify(_stats())
        if parts == ['audit'] and method == 'GET':
            try:
                limit = min(int(request.args.get('limit','100')), 1000)
            except Exception:
                limit = 100
            tn = request.args.get('tenant')
            return jsonify({'events': audit.read_events(limit=limit, tenant=tn)})
    except Exception as e:
        if method in ('POST','DELETE'):
            _audit(method, request.path, 500, rid)
        return _err('InternalError', str(e), 500)
    return _err('NotFound','Not found', 404)

def _stats():
    c = db.conn()
    glob = {
        'tenants': c.execute('SELECT COUNT(*) AS n FROM tenants').fetchone()['n'],
        'buckets': c.execute('SELECT COUNT(*) AS n FROM buckets').fetchone()['n'],
        'objects': c.execute('SELECT COUNT(*) AS n FROM objects WHERE delete_marker=0').fetchone()['n'],
        'bytes': c.execute('SELECT COALESCE(SUM(size),0) AS n FROM objects WHERE delete_marker=0').fetchone()['n'],
    }
    per = []
    for t in tenants.list_tenants():
        bk = c.execute('SELECT COUNT(*) AS n FROM buckets WHERE tenant=?', (t['name'],)).fetchone()['n']
        ob = c.execute('SELECT COUNT(*) AS n FROM objects o JOIN buckets b ON o.bucket=b.name WHERE b.tenant=? AND o.delete_marker=0', (t['name'],)).fetchone()['n']
        bs = c.execute('SELECT COALESCE(SUM(o.size),0) AS n FROM objects o JOIN buckets b ON o.bucket=b.name WHERE b.tenant=? AND o.delete_marker=0', (t['name'],)).fetchone()['n']
        per.append({'tenant': t['name'], 'buckets': bk, 'objects': ob, 'bytes': bs,
                    'quota_bytes': t['quota_bytes'], 'quota_buckets': t['quota_buckets']})
    return {'global': glob, 'tenants': per}
