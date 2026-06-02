import os
import sys
import signal
import json
from flask import Flask, request, Response, jsonify, send_from_directory
from werkzeug.serving import make_server
from werkzeug.middleware.proxy_fix import ProxyFix
import threading

from . import db, tenants, audit, s3, admin, console, lifecycle as lc, notifications, storage
from .errors import S3Error, s3_error_response
from .auth import verify_request
from .util import gen_request_id

app = Flask(__name__, static_folder=None)
app.url_map.strict_slashes = False

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
BASE_DIR = os.path.dirname(__file__)

# ---------- Health ----------
@app.route('/_health', methods=['GET'])
def health():
    return jsonify({'status':'ok'})

# ---------- Console static ----------
@app.route('/console', methods=['GET'])
@app.route('/console/', methods=['GET'])
def console_index():
    with open(os.path.join(BASE_DIR, 'console_index.html'), 'rb') as f:
        return Response(f.read(), mimetype='text/html')

@app.route('/console/static/<path:fname>', methods=['GET'])
def console_static(fname):
    return send_from_directory(STATIC_DIR, fname)

# ---------- Console API ----------
@app.route('/console/api/<path:subpath>', methods=['GET','POST','PUT','DELETE'])
def console_api(subpath):
    return console.handle(subpath, request.method)

# ---------- Admin API ----------
@app.route('/_admin/<path:subpath>', methods=['GET','POST','PUT','DELETE'])
def admin_api(subpath):
    return admin.handle(subpath, request.method)

# ---------- S3 dispatch ----------
def _do_audit_data_plane(method, path, bucket, key, status, request_id, auth_info):
    if method in ('GET','HEAD'):
        return
    audit.log_event(
        tenant=(auth_info['tenant'] if auth_info else None),
        access_key_id=(auth_info['access_key_id'] if auth_info else None),
        method=method, path=path, bucket=bucket, key=key,
        status=status, request_id=request_id)

def _handle_s3(bucket, key):
    method = request.method
    rid = gen_request_id()
    auth_info = None
    status = 200
    try:
        # CORS preflight is allowed without auth
        if method == 'OPTIONS':
            resp = Response('', status=200)
            resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin','*')
            resp.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,HEAD'
            resp.headers['Access-Control-Allow-Headers'] = '*'
            return resp
        # Try auth
        try:
            auth_info = verify_request(request)
        except S3Error as e:
            status = e.status
            _do_audit_data_plane(method, request.path, bucket, key, status, rid, None)
            return s3_error_response(e, request_id=rid)
        # Dispatch
        resp = _dispatch(bucket, key, auth_info)
        status = resp.status_code
        resp.headers['x-amz-request-id'] = rid
        resp.headers['x-amz-id-2'] = rid
        _do_audit_data_plane(method, request.path, bucket, key, status, rid, auth_info)
        return resp
    except S3Error as e:
        status = e.status
        _do_audit_data_plane(method, request.path, bucket, key, status, rid, auth_info)
        return s3_error_response(e, request_id=rid)
    except Exception as e:
        status = 500
        _do_audit_data_plane(method, request.path, bucket, key, status, rid, auth_info)
        return s3_error_response(S3Error('InternalError', str(e), status=500), request_id=rid)

def _dispatch(bucket, key, auth_info):
    method = request.method
    args = request.args
    if not bucket:
        # Service-level: list buckets (GET /)
        if method == 'GET':
            if not auth_info:
                raise S3Error('AccessDenied','Access Denied')
            return s3.list_buckets(auth_info)
        raise S3Error('MethodNotAllowed','Method not allowed')
    if key is None:
        # Bucket-level
        if method == 'PUT':
            if 'versioning' in args: return s3.put_versioning(bucket, auth_info)
            if 'cors' in args: return s3.put_cors(bucket, auth_info)
            if 'policy' in args: return s3.put_policy(bucket, auth_info)
            if 'lifecycle' in args: return s3.put_lifecycle(bucket, auth_info)
            if 'notification' in args: return s3.put_notification(bucket, auth_info)
            if not auth_info: raise S3Error('AccessDenied','Access Denied')
            return s3.create_bucket(bucket, auth_info)
        if method == 'DELETE':
            if 'cors' in args: return s3.delete_cors(bucket, auth_info)
            if 'policy' in args: return s3.delete_policy(bucket, auth_info)
            if 'lifecycle' in args: return s3.delete_lifecycle(bucket, auth_info)
            return s3.delete_bucket(bucket, auth_info)
        if method == 'GET':
            if 'versioning' in args: return s3.get_versioning(bucket, auth_info)
            if 'cors' in args: return s3.get_cors(bucket, auth_info)
            if 'policy' in args: return s3.get_policy(bucket, auth_info)
            if 'lifecycle' in args: return s3.get_lifecycle(bucket, auth_info)
            if 'notification' in args: return s3.get_notification(bucket, auth_info)
            if 'tagging' in args: return s3.get_bucket_tagging(bucket, auth_info)
            if 'uploads' in args: return s3.list_multipart_uploads(bucket, auth_info)
            if 'versions' in args: return s3.list_object_versions(bucket, auth_info)
            if 'location' in args:
                return Response('<?xml version="1.0" encoding="UTF-8"?><LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></LocationConstraint>', mimetype='application/xml')
            return s3.list_objects_v2(bucket, auth_info)
        if method == 'HEAD':
            return s3.head_bucket(bucket, auth_info)
        if method == 'POST':
            if 'delete' in args:
                return s3.multi_delete(bucket, auth_info)
            raise S3Error('MethodNotAllowed','Method not allowed')
        raise S3Error('MethodNotAllowed','Method not allowed')
    # Object-level
    if method == 'PUT':
        if 'tagging' in args: return s3.put_object_tagging(bucket, key, auth_info)
        if request.headers.get('x-amz-copy-source'):
            return s3.copy_object(bucket, key, auth_info)
        if 'partNumber' in args and 'uploadId' in args:
            return s3.upload_part(bucket, key, auth_info)
        return s3.put_object(bucket, key, auth_info)
    if method == 'GET':
        if 'tagging' in args: return s3.get_object_tagging(bucket, key, auth_info)
        if 'uploadId' in args: return s3.list_parts(bucket, key, auth_info)
        return s3.get_object(bucket, key, auth_info, head=False)
    if method == 'HEAD':
        return s3.get_object(bucket, key, auth_info, head=True)
    if method == 'DELETE':
        if 'tagging' in args: return s3.delete_object_tagging(bucket, key, auth_info)
        if 'uploadId' in args: return s3.abort_multipart(bucket, key, auth_info)
        return s3.delete_object(bucket, key, auth_info)
    if method == 'POST':
        if 'uploads' in args: return s3.create_multipart(bucket, key, auth_info)
        if 'uploadId' in args: return s3.complete_multipart(bucket, key, auth_info)
        raise S3Error('MethodNotAllowed','Method not allowed')
    raise S3Error('MethodNotAllowed','Method not allowed')

# Catch-all S3 routes - register last so they don't shadow others
@app.route('/', methods=['GET','HEAD','OPTIONS'])
def s3_root():
    return _handle_s3(None, None)

@app.route('/<bucket>', methods=['GET','PUT','POST','DELETE','HEAD','OPTIONS'])
def s3_bucket(bucket):
    if bucket in ('console', '_admin', '_health'):
        return Response('Not Found', status=404)
    return _handle_s3(bucket, None)

@app.route('/<bucket>/<path:key>', methods=['GET','PUT','POST','DELETE','HEAD','OPTIONS'])
def s3_object(bucket, key):
    if bucket in ('console', '_admin'):
        return Response('Not Found', status=404)
    return _handle_s3(bucket, key)

def _setup():
    db.init_db()
    tenants.seed_default()
    storage._ensure_dirs(); notifications.start_worker()
    lc.start_worker()

_setup()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    host = '0.0.0.0'
    # Use waitress if available, otherwise werkzeug
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=64)
    except ImportError:
        # Multi-threaded werkzeug server
        srv = make_server(host, port, app, threaded=True)
        srv.serve_forever()
