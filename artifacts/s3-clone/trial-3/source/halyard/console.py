import secrets
import json
from flask import request, Response, jsonify, make_response
from . import db, tenants, audit
from .util import now_iso, valid_bucket_name, gen_request_id

COOKIE_NAME = 'halyard_console'

def _new_session(access_key_id, tenant):
    sid = secrets.token_hex(32)
    db.conn().execute('INSERT INTO console_sessions(session_id,access_key_id,tenant,created_at) VALUES(?,?,?,?)',
                      (sid, access_key_id, tenant, now_iso()))
    return sid

def _get_session():
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    row = db.conn().execute('SELECT * FROM console_sessions WHERE session_id=?', (sid,)).fetchone()
    if not row:
        return None
    # Verify the access key still exists
    ak = db.conn().execute('SELECT * FROM access_keys WHERE access_key_id=? AND revoked=0', (row['access_key_id'],)).fetchone()
    if not ak:
        return None
    return {'access_key_id': row['access_key_id'], 'tenant': row['tenant'], 'session_id': sid}

def _delete_session(sid):
    db.conn().execute('DELETE FROM console_sessions WHERE session_id=?', (sid,))

def handle(path, method):
    parts = [p for p in path.split('/') if p]
    # /login
    if parts == ['login'] and method == 'POST':
        body = request.get_json(force=True, silent=True) or {}
        ak = body.get('access_key_id', '')
        sk = body.get('secret_access_key', '')
        row = db.conn().execute('SELECT * FROM access_keys WHERE access_key_id=? AND revoked=0', (ak,)).fetchone()
        if not row or row['secret_access_key'] != sk:
            return jsonify({'error': 'invalid_credentials'}), 401
        sid = _new_session(ak, row['tenant'])
        resp = make_response(jsonify({'tenant': row['tenant'], 'access_key_id': ak}))
        resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite='Lax', path='/')
        return resp
    if parts == ['logout'] and method == 'POST':
        sid = request.cookies.get(COOKIE_NAME)
        if sid: _delete_session(sid)
        resp = make_response(jsonify({'ok': True}))
        resp.set_cookie(COOKIE_NAME, '', expires=0, path='/')
        return resp
    if parts == ['me'] and method == 'GET':
        s = _get_session()
        if not s:
            return jsonify({'error': 'unauthenticated'}), 401
        return jsonify({'tenant': s['tenant'], 'access_key_id': s['access_key_id']})
    s = _get_session()
    if not s:
        return jsonify({'error': 'unauthenticated'}), 401
    if parts == ['buckets']:
        if method == 'GET':
            rows = db.conn().execute('SELECT name, created_at FROM buckets WHERE tenant=? ORDER BY name', (s['tenant'],)).fetchall()
            return jsonify({'buckets': [{'name': r['name'], 'created_at': r['created_at']} for r in rows]})
        if method == 'POST':
            body = request.get_json(force=True, silent=True) or {}
            name = body.get('name', '')
            if not valid_bucket_name(name):
                return jsonify({'error': 'InvalidBucketName', 'message': 'Invalid bucket name'}), 400
            ex = db.conn().execute('SELECT * FROM buckets WHERE name=?', (name,)).fetchone()
            if ex:
                if ex['tenant'] == s['tenant']:
                    return jsonify({'error': 'BucketAlreadyOwnedByYou', 'message': 'You already own this bucket'}), 409
                return jsonify({'error': 'BucketAlreadyExists', 'message': 'Bucket already exists'}), 409
            t = tenants.get_tenant(s['tenant'])
            if t and t.get('quota_buckets') is not None:
                cur = tenants.get_tenant_bucket_count(s['tenant'])
                if cur >= t['quota_buckets']:
                    return jsonify({'error': 'TooManyBuckets', 'message': 'Quota exceeded'}), 403
            db.conn().execute('INSERT INTO buckets(name,tenant,created_at,versioning) VALUES(?,?,?,?)',
                              (name, s['tenant'], now_iso(), 'Unversioned'))
            return jsonify({'bucket': {'name': name, 'created_at': now_iso()}}), 201
    if parts == ['access-keys']:
        if method == 'GET':
            return jsonify({'access_keys': tenants.list_access_keys(s['tenant'])})
        if method == 'POST':
            ak = tenants.create_access_key(s['tenant'])
            return jsonify({'access_key': ak}), 201
    if len(parts) == 2 and parts[0] == 'access-keys' and method == 'DELETE':
        akid = parts[1]
        if akid == s['access_key_id']:
            return jsonify({'error': 'CannotDeleteCurrentAccessKey', 'message': 'Cannot delete current access key'}), 409
        ok = tenants.revoke_access_key(s['tenant'], akid)
        if not ok:
            return jsonify({'error': 'NoSuchAccessKey', 'message': 'No such key'}), 404
        return Response('', status=204)
    return jsonify({'error': 'NotFound'}), 404
