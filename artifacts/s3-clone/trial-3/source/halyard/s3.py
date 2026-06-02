import hashlib
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sx
from flask import Response, request, stream_with_context
from . import db, storage, auth, audit, policy as policy_mod, notifications, lifecycle
from .util import now_iso, gen_version_id, gen_upload_id, gen_request_id, valid_bucket_name, md5_hex
from .errors import S3Error, s3_error_response

MAX_BUCKETS_PER_TENANT = 1000
MIN_PART_SIZE = 5 * 1024 * 1024
MAX_OBJECT_SIZE = 5 * 1024 * 1024 * 1024 * 5  # 5 TiB

# ---------- Helpers ----------

def _xml(body, status=200, extra_headers=None):
    if not body.startswith('<?xml'):
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' + body
    resp = Response(body, status=status, mimetype='application/xml')
    if extra_headers:
        for k, v in extra_headers.items():
            resp.headers[k] = v
    return resp

def _esc(s):
    if s is None:
        return ''
    return sx.escape(str(s))

def _get_bucket_row(name):
    c = db.conn()
    return c.execute('SELECT * FROM buckets WHERE name=?', (name,)).fetchone()

def _bucket_owner(name):
    row = _get_bucket_row(name)
    if not row:
        return None
    return row['tenant']

def _check_bucket_access(bucket_name, auth_info, action, key=None, allow_anonymous=True, method='GET'):
    """Returns (bucket_row, requester_tenant) or raises S3Error."""
    row = _get_bucket_row(bucket_name)
    if not row:
        raise S3Error('NoSuchBucket', 'The specified bucket does not exist', BucketName=bucket_name)
    owner = row['tenant']
    if auth_info:
        requester = auth_info['tenant']
        if requester == owner:
            return row, requester
        # Cross-tenant: evaluate policy
        pol = None
        if row['policy']:
            try:
                pol = json.loads(row['policy'])
            except Exception:
                pol = None
        decision = policy_mod.evaluate(pol, action, bucket_name, key, requester)
        if decision == 'Allow':
            return row, requester
        raise S3Error('AccessDenied', 'Access Denied')
    else:
        if not allow_anonymous:
            raise S3Error('AccessDenied', 'Access Denied')
        if method not in ('GET', 'HEAD'):
            raise S3Error('AccessDenied', 'Access Denied')
        pol = None
        if row['policy']:
            try:
                pol = json.loads(row['policy'])
            except Exception:
                pol = None
        decision = policy_mod.evaluate(pol, action, bucket_name, key, None)
        if decision == 'Allow':
            return row, None
        raise S3Error('AccessDenied', 'Access Denied')

def _parse_meta_headers(req):
    meta = {}
    for k, v in req.headers.items():
        if k.lower().startswith('x-amz-meta-'):
            meta[k[11:].lower()] = v
    return meta

def _meta_to_headers(resp, metadata_json):
    if not metadata_json:
        return
    try:
        meta = json.loads(metadata_json)
    except Exception:
        return
    for k, v in meta.items():
        resp.headers['x-amz-meta-' + k] = v

def _parse_tagging_xml(xml_text):
    if not xml_text or not xml_text.strip():
        return {}
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        raise S3Error('MalformedXML', 'Invalid tagging XML')
    def localname(t): return t.split('}',1)[1] if '}' in t else t
    out = {}
    for tag in root.iter():
        if localname(tag.tag) == 'Tag':
            k = v = None
            for c in list(tag):
                if localname(c.tag) == 'Key': k = c.text
                if localname(c.tag) == 'Value': v = c.text
            if k is not None:
                out[k] = v or ''
    return out

def _serialize_tagging(tags):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><TagSet>']
    for k, v in tags.items():
        parts.append(f'<Tag><Key>{_esc(k)}</Key><Value>{_esc(v)}</Value></Tag>')
    parts.append('</TagSet></Tagging>')
    return ''.join(parts)

def _parse_tagging_query(s):
    if not s:
        return {}
    out = {}
    for pair in s.split('&'):
        if '=' in pair:
            k, v = pair.split('=', 1)
            out[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
        elif pair:
            out[urllib.parse.unquote_plus(pair)] = ''
    return out

def _get_notif_configs(bucket_row):
    if not bucket_row['notification']:
        return []
    try:
        return notifications.parse_notification_config(bucket_row['notification'])
    except Exception:
        return []

def _emit_event(bucket_name, event, key, size=None, etag=None):
    row = _get_bucket_row(bucket_name)
    if not row:
        return
    cfgs = _get_notif_configs(row)
    if cfgs:
        try:
            notifications.enqueue_event(bucket_name, cfgs, event, key, size=size, etag=etag)
        except Exception:
            pass

# ---------- Bucket CRUD ----------

def list_buckets(auth_info):
    c = db.conn()
    rows = c.execute('SELECT * FROM buckets WHERE tenant=? ORDER BY name', (auth_info['tenant'],)).fetchall()
    parts = ['<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Owner><ID>{auth_info["tenant"]}</ID><DisplayName>{auth_info["tenant"]}</DisplayName></Owner>',
             '<Buckets>']
    for r in rows:
        parts.append(f'<Bucket><Name>{_esc(r["name"])}</Name><CreationDate>{r["created_at"]}</CreationDate></Bucket>')
    parts.append('</Buckets></ListAllMyBucketsResult>')
    return _xml(''.join(parts))

def create_bucket(bucket_name, auth_info):
    if not valid_bucket_name(bucket_name):
        raise S3Error('InvalidBucketName', 'Invalid bucket name', BucketName=bucket_name)
    c = db.conn()
    existing = c.execute('SELECT * FROM buckets WHERE name=?', (bucket_name,)).fetchone()
    if existing:
        if existing['tenant'] == auth_info['tenant']:
            raise S3Error('BucketAlreadyOwnedByYou', 'Already own', BucketName=bucket_name)
        raise S3Error('BucketAlreadyExists', 'Not available', BucketName=bucket_name)
    from . import tenants as tenants_mod
    t = tenants_mod.get_tenant(auth_info['tenant'])
    if t and t.get('quota_buckets') is not None:
        cur = tenants_mod.get_tenant_bucket_count(auth_info['tenant'])
        if cur >= t['quota_buckets']:
            raise S3Error('TooManyBuckets', 'Too many buckets', BucketName=bucket_name)
    c.execute('INSERT INTO buckets(name,tenant,created_at,versioning) VALUES(?,?,?,?)',
              (bucket_name, auth_info['tenant'], now_iso(), 'Unversioned'))
    resp = Response('', status=200)
    resp.headers['Location'] = f'/{bucket_name}'
    return resp

def delete_bucket(bucket_name, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row:
        raise S3Error('NoSuchBucket', 'No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied', 'Access Denied')
    c = db.conn()
    cnt = c.execute('SELECT COUNT(*) AS n FROM objects WHERE bucket=?', (bucket_name,)).fetchone()['n']
    if cnt > 0:
        raise S3Error('BucketNotEmpty', 'Not empty', BucketName=bucket_name)
    c.execute('DELETE FROM buckets WHERE name=?', (bucket_name,))
    return Response('', status=204)

def head_bucket(bucket_name, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row:
        raise S3Error('NoSuchBucket', 'No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied', 'Access Denied')
    return Response('', status=200)

# ---------- Versioning ----------
def _owner_only(bucket_name, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row: raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    return row

def get_versioning(bucket_name, auth_info):
    row = _owner_only(bucket_name, auth_info)
    if row['versioning'] == 'Unversioned':
        return _xml('<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>')
    return _xml(f'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Status>{row["versioning"]}</Status></VersioningConfiguration>')

def put_versioning(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    body = request.get_data()
    try:
        rt = ET.fromstring(body)
    except Exception:
        raise S3Error('MalformedXML', 'Invalid XML')
    def localname(t): return t.split('}',1)[1] if '}' in t else t
    status = 'Suspended'
    for c in list(rt):
        if localname(c.tag) == 'Status':
            status = c.text
    if status not in ('Enabled', 'Suspended'):
        raise S3Error('MalformedXML', 'Invalid versioning status')
    db.conn().execute('UPDATE buckets SET versioning=? WHERE name=?', (status, bucket_name))
    return Response('', status=200)

# ---------- CORS ----------
def get_cors(bucket_name, auth_info):
    row = _owner_only(bucket_name, auth_info)
    if not row['cors']:
        raise S3Error('NoSuchCORSConfiguration','No CORS', BucketName=bucket_name)
    return _xml(row['cors'])

def put_cors(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    body = request.get_data().decode('utf-8', errors='replace')
    try: ET.fromstring(body)
    except Exception: raise S3Error('MalformedXML','Invalid XML')
    db.conn().execute('UPDATE buckets SET cors=? WHERE name=?', (body, bucket_name))
    return Response('', status=200)

def delete_cors(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    db.conn().execute('UPDATE buckets SET cors=NULL WHERE name=?', (bucket_name,))
    return Response('', status=204)

# ---------- Policy ----------
def get_policy(bucket_name, auth_info):
    row = _owner_only(bucket_name, auth_info)
    if not row['policy']:
        raise S3Error('NoSuchBucketPolicy','No policy', BucketName=bucket_name)
    return Response(row['policy'], status=200, mimetype='application/json')

def put_policy(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    body = request.get_data().decode('utf-8', errors='replace')
    pol = policy_mod.parse_policy(body)
    db.conn().execute('UPDATE buckets SET policy=? WHERE name=?', (json.dumps(pol), bucket_name))
    return Response('', status=204)

def delete_policy(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    db.conn().execute('UPDATE buckets SET policy=NULL WHERE name=?', (bucket_name,))
    return Response('', status=204)

# ---------- Lifecycle ----------
def get_lifecycle(bucket_name, auth_info):
    row = _owner_only(bucket_name, auth_info)
    if not row['lifecycle']:
        raise S3Error('NoSuchLifecycleConfiguration','No lifecycle', BucketName=bucket_name)
    return _xml(row['lifecycle'])

def put_lifecycle(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    body = request.get_data().decode('utf-8', errors='replace')
    lifecycle.parse_lifecycle(body)
    db.conn().execute('UPDATE buckets SET lifecycle=? WHERE name=?', (body, bucket_name))
    return Response('', status=200)

def delete_lifecycle(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    db.conn().execute('UPDATE buckets SET lifecycle=NULL WHERE name=?', (bucket_name,))
    return Response('', status=204)

# ---------- Notification ----------
def get_notification(bucket_name, auth_info):
    row = _owner_only(bucket_name, auth_info)
    if not row['notification']:
        return _xml('<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>')
    return _xml(row['notification'])

def put_notification(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    body = request.get_data().decode('utf-8', errors='replace')
    notifications.parse_notification_config(body)
    db.conn().execute('UPDATE buckets SET notification=? WHERE name=?', (body, bucket_name))
    return Response('', status=200)

# ---------- Bucket tagging (treated as no-op compatibility) ----------
def get_bucket_tagging(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    raise S3Error('NoSuchTagSet', 'No tag set', BucketName=bucket_name)

# ---------- ListObjectsV2 ----------
def list_objects_v2(bucket_name, auth_info):
    row, _ = _check_bucket_access(bucket_name, auth_info, 's3:ListBucket', allow_anonymous=True, method='GET')
    args = request.args
    prefix = args.get('prefix', '')
    delimiter = args.get('delimiter', '')
    cont = args.get('continuation-token', '')
    start_after = args.get('start-after', '')
    try:
        max_keys = min(int(args.get('max-keys', '1000')), 1000)
    except Exception:
        max_keys = 1000
    encoding_type = args.get('encoding-type')
    fetch_owner = args.get('fetch-owner', 'false').lower() == 'true'

    c = db.conn()
    rows = c.execute('SELECT * FROM objects WHERE bucket=? AND is_latest=1 AND delete_marker=0 ORDER BY key',
                     (bucket_name,)).fetchall()

    after = cont if cont else start_after
    common_prefixes = set()
    contents = []
    for r in rows:
        if prefix and not r['key'].startswith(prefix):
            continue
        if after and r['key'] <= after:
            continue
        if delimiter:
            rest = r['key'][len(prefix):]
            idx = rest.find(delimiter)
            if idx >= 0:
                cp = prefix + rest[:idx+len(delimiter)]
                common_prefixes.add(cp)
                continue
        contents.append(r)
        if len(contents) + len(common_prefixes) >= max_keys:
            break
    truncated = (len(contents) + len(common_prefixes)) >= max_keys and (len(rows) > 0)
    next_token = ''
    if truncated and contents:
        next_token = contents[-1]['key']
    elif truncated and common_prefixes:
        next_token = sorted(common_prefixes)[-1]

    def enc(k):
        if encoding_type == 'url':
            return urllib.parse.quote(k, safe='')
        return k

    parts = ['<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Name>{_esc(bucket_name)}</Name>',
             f'<Prefix>{_esc(enc(prefix))}</Prefix>',
             f'<KeyCount>{len(contents)+len(common_prefixes)}</KeyCount>',
             f'<MaxKeys>{max_keys}</MaxKeys>',
             f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>']
    if delimiter:
        parts.append(f'<Delimiter>{_esc(enc(delimiter))}</Delimiter>')
    if encoding_type:
        parts.append(f'<EncodingType>{encoding_type}</EncodingType>')
    if cont:
        parts.append(f'<ContinuationToken>{_esc(cont)}</ContinuationToken>')
    if start_after:
        parts.append(f'<StartAfter>{_esc(enc(start_after))}</StartAfter>')
    if truncated:
        parts.append(f'<NextContinuationToken>{_esc(next_token)}</NextContinuationToken>')
    for r in contents:
        parts.append('<Contents>')
        parts.append(f'<Key>{_esc(enc(r["key"]))}</Key>')
        parts.append(f'<LastModified>{r["created_at"]}</LastModified>')
        parts.append(f'<ETag>"{r["etag"]}"</ETag>')
        parts.append(f'<Size>{r["size"]}</Size>')
        parts.append('<StorageClass>STANDARD</StorageClass>')
        if fetch_owner:
            parts.append(f'<Owner><ID>{row["tenant"]}</ID><DisplayName>{row["tenant"]}</DisplayName></Owner>')
        parts.append('</Contents>')
    for cp in sorted(common_prefixes):
        parts.append(f'<CommonPrefixes><Prefix>{_esc(enc(cp))}</Prefix></CommonPrefixes>')
    parts.append('</ListBucketResult>')
    return _xml(''.join(parts))

# ---------- ListObjectVersions ----------
def list_object_versions(bucket_name, auth_info):
    row, _ = _check_bucket_access(bucket_name, auth_info, 's3:ListBucket', allow_anonymous=True)
    args = request.args
    prefix = args.get('prefix', '')
    delimiter = args.get('delimiter', '')
    try:
        max_keys = min(int(args.get('max-keys', '1000')), 1000)
    except Exception:
        max_keys = 1000
    c = db.conn()
    rows = c.execute('SELECT * FROM objects WHERE bucket=? ORDER BY key, created_at DESC',
                     (bucket_name,)).fetchall()
    parts = ['<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Name>{_esc(bucket_name)}</Name>',
             f'<Prefix>{_esc(prefix)}</Prefix>',
             f'<MaxKeys>{max_keys}</MaxKeys>',
             '<IsTruncated>false</IsTruncated>']
    for r in rows:
        if prefix and not r['key'].startswith(prefix):
            continue
        latest = 'true' if r['is_latest'] else 'false'
        if r['delete_marker']:
            parts.append('<DeleteMarker>')
            parts.append(f'<Key>{_esc(r["key"])}</Key>')
            parts.append(f'<VersionId>{r["version_id"]}</VersionId>')
            parts.append(f'<IsLatest>{latest}</IsLatest>')
            parts.append(f'<LastModified>{r["created_at"]}</LastModified>')
            parts.append(f'<Owner><ID>{row["tenant"]}</ID><DisplayName>{row["tenant"]}</DisplayName></Owner>')
            parts.append('</DeleteMarker>')
        else:
            parts.append('<Version>')
            parts.append(f'<Key>{_esc(r["key"])}</Key>')
            parts.append(f'<VersionId>{r["version_id"]}</VersionId>')
            parts.append(f'<IsLatest>{latest}</IsLatest>')
            parts.append(f'<LastModified>{r["created_at"]}</LastModified>')
            parts.append(f'<ETag>"{r["etag"]}"</ETag>')
            parts.append(f'<Size>{r["size"]}</Size>')
            parts.append('<StorageClass>STANDARD</StorageClass>')
            parts.append(f'<Owner><ID>{row["tenant"]}</ID><DisplayName>{row["tenant"]}</DisplayName></Owner>')
            parts.append('</Version>')
    parts.append('</ListVersionsResult>')
    return _xml(''.join(parts))

# ---------- Object PUT ----------
def put_object(bucket_name, key, auth_info):
    row, requester = _check_bucket_access(bucket_name, auth_info, 's3:PutObject', key=key, allow_anonymous=False, method='PUT')
    from . import tenants as tenants_mod
    owner = row['tenant']
    t = tenants_mod.get_tenant(owner)
    data = request.get_data(cache=False)
    size = len(data)
    if t and t.get('quota_bytes') is not None:
        used = tenants_mod.get_tenant_used_bytes(owner)
        if used + size > t['quota_bytes']:
            raise S3Error('QuotaExceeded', 'Quota exceeded', BucketName=bucket_name)
    inm = request.headers.get('If-None-Match', '')
    if inm == '*':
        c = db.conn()
        ex = c.execute('SELECT 1 FROM objects WHERE bucket=? AND key=? AND is_latest=1 AND delete_marker=0', (bucket_name, key)).fetchone()
        if ex:
            raise S3Error('PreconditionFailed', 'Precondition failed')
    digest_md5 = hashlib.md5(data).hexdigest()
    digest_sha = hashlib.sha256(data).hexdigest()
    import os as _os, tempfile as _tf
    sub = _os.path.join(storage.BLOB_DIR, digest_sha[:2], digest_sha[2:4])
    _os.makedirs(sub, exist_ok=True)
    blob_path = _os.path.join(sub, digest_sha)
    if not _os.path.exists(blob_path):
        fd, tmp = _tf.mkstemp(dir=storage.TMP_DIR)
        with _os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); _os.fsync(f.fileno())
        try: _os.replace(tmp, blob_path)
        except Exception:
            try: _os.unlink(tmp)
            except: pass
    metadata = _parse_meta_headers(request)
    content_type = request.headers.get('Content-Type', 'application/octet-stream')
    tagging = {}
    if request.headers.get('x-amz-tagging'):
        tagging = _parse_tagging_query(request.headers.get('x-amz-tagging'))
    version_id = gen_version_id() if row['versioning'] == 'Enabled' else 'null'
    c = db.conn()
    if row['versioning'] != 'Enabled':
        c.execute('DELETE FROM objects WHERE bucket=? AND key=?', (bucket_name, key))
    else:
        c.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (bucket_name, key))
    c.execute('''INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,etag,content_type,metadata,tagging,storage_path,created_at)
                 VALUES(?,?,?,1,0,?,?,?,?,?,?,?)''',
              (bucket_name, key, version_id, size, digest_md5, content_type,
               json.dumps(metadata), json.dumps(tagging), blob_path, now_iso()))
    resp = Response('', status=200)
    resp.headers['ETag'] = f'"{digest_md5}"'
    if row['versioning'] == 'Enabled':
        resp.headers['x-amz-version-id'] = version_id
    _emit_event(bucket_name, 's3:ObjectCreated:Put', key, size=size, etag=f'"{digest_md5}"')
    return resp

# ---------- Object GET/HEAD ----------
def _get_object_row(bucket_name, key, version_id=None):
    c = db.conn()
    if version_id and version_id != 'null':
        return c.execute('SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?',
                         (bucket_name, key, version_id)).fetchone()
    return c.execute('SELECT * FROM objects WHERE bucket=? AND key=? AND is_latest=1',
                     (bucket_name, key)).fetchone()

def _check_conditional_get(req, etag):
    inm = req.headers.get('If-None-Match')
    im = req.headers.get('If-Match')
    quoted = f'"{etag}"'
    if im and im != quoted and im != etag and im != '*':
        raise S3Error('PreconditionFailed', 'If-Match failed')
    if inm and (inm == quoted or inm == etag or inm == '*'):
        r = Response('', status=304)
        r.headers['ETag'] = quoted
        return r
    return None

def _parse_range(header, size):
    if not header or not header.startswith('bytes='):
        return None
    spec = header[6:].split(',')[0].strip()
    try:
        if spec.startswith('-'):
            n = int(spec[1:])
            if n == 0: return 'invalid'
            start = max(0, size - n); end = size - 1
        elif spec.endswith('-'):
            start = int(spec[:-1]); end = size - 1
        else:
            a, b = spec.split('-', 1)
            start = int(a); end = int(b)
    except Exception:
        return 'invalid'
    if size == 0:
        return 'invalid'
    if start >= size or start < 0 or end < start:
        return 'invalid'
    if end >= size: end = size - 1
    return (start, end)

def get_object(bucket_name, key, auth_info, head=False):
    row, _ = _check_bucket_access(bucket_name, auth_info, 's3:GetObject', key=key, allow_anonymous=True, method='HEAD' if head else 'GET')
    version_id = request.args.get('versionId')
    o = _get_object_row(bucket_name, key, version_id)
    if not o:
        if version_id:
            raise S3Error('NoSuchVersion', 'No such version', Key=key)
        raise S3Error('NoSuchKey', 'No such key', Key=key)
    if o['delete_marker']:
        if version_id:
            resp = Response('', status=405)
            resp.headers['x-amz-delete-marker'] = 'true'
            return resp
        raise S3Error('NoSuchKey', 'No such key', Key=key)
    cond = _check_conditional_get(request, o['etag'])
    if cond is not None:
        return cond
    rng = _parse_range(request.headers.get('Range'), o['size'])
    if rng == 'invalid':
        raise S3Error('InvalidRange', 'Range not satisfiable', ActualObjectSize=str(o['size']))
    headers = {
        'ETag': f'"{o["etag"]}"',
        'Content-Type': o['content_type'] or 'application/octet-stream',
        'Last-Modified': o['created_at'],
        'Accept-Ranges': 'bytes',
    }
    if o['version_id'] != 'null':
        headers['x-amz-version-id'] = o['version_id']
    if rng:
        start, end = rng
        length = end - start + 1
        headers['Content-Range'] = f'bytes {start}-{end}/{o["size"]}'
        headers['Content-Length'] = str(length)
        if head:
            resp = Response('', status=206, headers=headers)
        else:
            def gen():
                with open(o['storage_path'], 'rb') as f:
                    f.seek(start); remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk: break
                        remaining -= len(chunk)
                        yield chunk
            resp = Response(stream_with_context(gen()), status=206, headers=headers)
    else:
        headers['Content-Length'] = str(o['size'])
        if head:
            resp = Response('', status=200, headers=headers)
        else:
            if o['size'] == 0 or not o['storage_path']:
                resp = Response(b'', status=200, headers=headers)
            else:
                def gen():
                    with open(o['storage_path'], 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk: break
                            yield chunk
                resp = Response(stream_with_context(gen()), status=200, headers=headers)
    _meta_to_headers(resp, o['metadata'])
    return resp

# ---------- Object DELETE ----------
def delete_object(bucket_name, key, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row:
        raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        if not auth_info: raise S3Error('AccessDenied','Access Denied')
        raise S3Error('AccessDenied','Access Denied')
    version_id = request.args.get('versionId')
    c = db.conn()
    if version_id:
        o = c.execute('SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, version_id)).fetchone()
        if not o:
            return Response('', status=204)
        was_latest = bool(o['is_latest'])
        was_dm = bool(o['delete_marker'])
        c.execute('DELETE FROM objects WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, version_id))
        if was_latest:
            other = c.execute('SELECT * FROM objects WHERE bucket=? AND key=? ORDER BY created_at DESC LIMIT 1', (bucket_name, key)).fetchone()
            if other:
                c.execute('UPDATE objects SET is_latest=1 WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, other['version_id']))
        resp = Response('', status=204)
        resp.headers['x-amz-version-id'] = version_id
        if was_dm:
            resp.headers['x-amz-delete-marker'] = 'true'
        _emit_event(bucket_name, 's3:ObjectRemoved:Delete', key)
        return resp
    if row['versioning'] == 'Enabled':
        c.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (bucket_name, key))
        vid = gen_version_id()
        c.execute('''INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,created_at)
                     VALUES(?,?,?,1,1,0,?)''', (bucket_name, key, vid, now_iso()))
        resp = Response('', status=204)
        resp.headers['x-amz-delete-marker'] = 'true'
        resp.headers['x-amz-version-id'] = vid
        _emit_event(bucket_name, 's3:ObjectRemoved:Delete', key)
        return resp
    c.execute('DELETE FROM objects WHERE bucket=? AND key=?', (bucket_name, key))
    _emit_event(bucket_name, 's3:ObjectRemoved:Delete', key)
    return Response('', status=204)

# ---------- Multi-object delete ----------
def multi_delete(bucket_name, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row:
        raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    body = request.get_data()
    try:
        rt = ET.fromstring(body)
    except Exception:
        raise S3Error('MalformedXML', 'Invalid XML')
    def localname(t): return t.split('}',1)[1] if '}' in t else t
    quiet = False
    objects = []
    for c in list(rt):
        ln = localname(c.tag)
        if ln == 'Quiet':
            quiet = (c.text or '').lower() == 'true'
        elif ln == 'Object':
            obj = {'key': None, 'version_id': None}
            for sc in list(c):
                sn = localname(sc.tag)
                if sn == 'Key': obj['key'] = sc.text
                elif sn == 'VersionId': obj['version_id'] = sc.text
            if obj['key'] is not None:
                objects.append(obj)
    deleted = []
    errors = []
    cn = db.conn()
    for o in objects:
        try:
            key = o['key']; vid = o['version_id']
            if vid:
                row2 = cn.execute('SELECT * FROM objects WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, vid)).fetchone()
                if row2:
                    was_latest = bool(row2['is_latest'])
                    cn.execute('DELETE FROM objects WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, vid))
                    if was_latest:
                        other = cn.execute('SELECT * FROM objects WHERE bucket=? AND key=? ORDER BY created_at DESC LIMIT 1', (bucket_name, key)).fetchone()
                        if other:
                            cn.execute('UPDATE objects SET is_latest=1 WHERE bucket=? AND key=? AND version_id=?', (bucket_name, key, other['version_id']))
                deleted.append({'key': key, 'version_id': vid})
            else:
                if row['versioning'] == 'Enabled':
                    cn.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (bucket_name, key))
                    nv = gen_version_id()
                    cn.execute('INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,created_at) VALUES(?,?,?,1,1,0,?)',
                               (bucket_name, key, nv, now_iso()))
                    deleted.append({'key': key, 'delete_marker': True, 'delete_marker_version_id': nv})
                else:
                    cn.execute('DELETE FROM objects WHERE bucket=? AND key=?', (bucket_name, key))
                    deleted.append({'key': key})
            _emit_event(bucket_name, 's3:ObjectRemoved:Delete', key)
        except Exception as e:
            errors.append({'key': o['key'], 'code': 'InternalError', 'message': str(e)})
    parts = ['<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
    if not quiet:
        for d in deleted:
            parts.append('<Deleted>')
            parts.append(f'<Key>{_esc(d["key"])}</Key>')
            if d.get('version_id'):
                parts.append(f'<VersionId>{d["version_id"]}</VersionId>')
            if d.get('delete_marker'):
                parts.append('<DeleteMarker>true</DeleteMarker>')
                parts.append(f'<DeleteMarkerVersionId>{d["delete_marker_version_id"]}</DeleteMarkerVersionId>')
            parts.append('</Deleted>')
    for e in errors:
        parts.append(f'<Error><Key>{_esc(e["key"])}</Key><Code>{e["code"]}</Code><Message>{_esc(e["message"])}</Message></Error>')
    parts.append('</DeleteResult>')
    return _xml(''.join(parts))

# ---------- CopyObject ----------
def copy_object(bucket_name, key, auth_info):
    src = request.headers.get('x-amz-copy-source', '')
    src = urllib.parse.unquote(src)
    if src.startswith('/'):
        src = src[1:]
    src_version = None
    if '?versionId=' in src:
        src, src_version = src.split('?versionId=', 1)
    if '/' not in src:
        raise S3Error('InvalidArgument', 'Invalid copy source')
    src_bucket, src_key = src.split('/', 1)
    src_row = _get_bucket_row(src_bucket)
    if not src_row:
        raise S3Error('NoSuchBucket', 'No such bucket', BucketName=src_bucket)
    # Source access
    if src_row['tenant'] != auth_info['tenant']:
        pol = json.loads(src_row['policy']) if src_row['policy'] else None
        decision = policy_mod.evaluate(pol, 's3:GetObject', src_bucket, src_key, auth_info['tenant'])
        if decision != 'Allow':
            raise S3Error('AccessDenied', 'Access Denied')
    # Destination access
    dest_row, _ = _check_bucket_access(bucket_name, auth_info, 's3:PutObject', key=key, allow_anonymous=False, method='PUT')
    src_obj = _get_object_row(src_bucket, src_key, src_version)
    if not src_obj or src_obj['delete_marker']:
        raise S3Error('NoSuchKey', 'No such key', Key=src_key)
    # Quota
    from . import tenants as tenants_mod
    t = tenants_mod.get_tenant(dest_row['tenant'])
    if t and t.get('quota_bytes') is not None:
        used = tenants_mod.get_tenant_used_bytes(dest_row['tenant'])
        if used + src_obj['size'] > t['quota_bytes']:
            raise S3Error('QuotaExceeded', 'Quota exceeded', BucketName=bucket_name)
    # Metadata directive
    md_directive = request.headers.get('x-amz-metadata-directive', 'COPY').upper()
    tag_directive = request.headers.get('x-amz-tagging-directive', 'COPY').upper()
    if md_directive == 'REPLACE':
        metadata = _parse_meta_headers(request)
        content_type = request.headers.get('Content-Type', src_obj['content_type'] or 'application/octet-stream')
    else:
        metadata = json.loads(src_obj['metadata']) if src_obj['metadata'] else {}
        content_type = src_obj['content_type'] or 'application/octet-stream'
    if tag_directive == 'REPLACE':
        tagging = _parse_tagging_query(request.headers.get('x-amz-tagging', ''))
    else:
        tagging = json.loads(src_obj['tagging']) if src_obj['tagging'] else {}
    version_id = gen_version_id() if dest_row['versioning'] == 'Enabled' else 'null'
    c = db.conn()
    if dest_row['versioning'] != 'Enabled':
        c.execute('DELETE FROM objects WHERE bucket=? AND key=?', (bucket_name, key))
    else:
        c.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (bucket_name, key))
    c.execute('''INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,etag,content_type,metadata,tagging,storage_path,created_at)
                 VALUES(?,?,?,1,0,?,?,?,?,?,?,?)''',
              (bucket_name, key, version_id, src_obj['size'], src_obj['etag'], content_type,
               json.dumps(metadata), json.dumps(tagging), src_obj['storage_path'], now_iso()))
    body = f'<CopyObjectResult><ETag>"{src_obj["etag"]}"</ETag><LastModified>{now_iso()}</LastModified></CopyObjectResult>'
    _emit_event(bucket_name, 's3:ObjectCreated:Put', key, size=src_obj['size'], etag=f'"{src_obj["etag"]}"')
    resp = _xml(body)
    if dest_row['versioning'] == 'Enabled':
        resp.headers['x-amz-version-id'] = version_id
    return resp

# ---------- Object tagging ----------
def get_object_tagging(bucket_name, key, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row: raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    o = _get_object_row(bucket_name, key)
    if not o: raise S3Error('NoSuchKey','No such key', Key=key)
    tags = json.loads(o['tagging']) if o['tagging'] else {}
    return _xml(_serialize_tagging(tags))

def put_object_tagging(bucket_name, key, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row: raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    o = _get_object_row(bucket_name, key)
    if not o: raise S3Error('NoSuchKey','No such key', Key=key)
    tags = _parse_tagging_xml(request.get_data().decode('utf-8', errors='replace'))
    db.conn().execute('UPDATE objects SET tagging=? WHERE bucket=? AND key=? AND version_id=?',
                      (json.dumps(tags), bucket_name, key, o['version_id']))
    return Response('', status=200)

def delete_object_tagging(bucket_name, key, auth_info):
    row = _get_bucket_row(bucket_name)
    if not row: raise S3Error('NoSuchBucket','No such bucket', BucketName=bucket_name)
    if not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    o = _get_object_row(bucket_name, key)
    if not o: raise S3Error('NoSuchKey','No such key', Key=key)
    db.conn().execute('UPDATE objects SET tagging=? WHERE bucket=? AND key=? AND version_id=?',
                      ('{}', bucket_name, key, o['version_id']))
    return Response('', status=204)

# ---------- Multipart create/upload/list/abort ----------
def create_multipart(bucket_name, key, auth_info):
    row, _ = _check_bucket_access(bucket_name, auth_info, 's3:PutObject', key=key, allow_anonymous=False, method='POST')
    upload_id = gen_upload_id()
    metadata = _parse_meta_headers(request)
    content_type = request.headers.get('Content-Type', 'application/octet-stream')
    tagging = {}
    if request.headers.get('x-amz-tagging'):
        tagging = _parse_tagging_query(request.headers.get('x-amz-tagging'))
    db.conn().execute('INSERT INTO multipart(upload_id,bucket,key,initiated_at,metadata,content_type,tagging) VALUES(?,?,?,?,?,?,?)',
                      (upload_id, bucket_name, key, now_iso(), json.dumps(metadata), content_type, json.dumps(tagging)))
    body = ('<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Bucket>{_esc(bucket_name)}</Bucket><Key>{_esc(key)}</Key>'
            f'<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>')
    return _xml(body)

def upload_part(bucket_name, key, auth_info):
    upload_id = request.args.get('uploadId')
    part_number = int(request.args.get('partNumber', '0'))
    if part_number < 1 or part_number > 10000:
        raise S3Error('InvalidArgument', 'Part number out of range')
    c = db.conn()
    mp = c.execute('SELECT * FROM multipart WHERE upload_id=? AND bucket=? AND key=?', (upload_id, bucket_name, key)).fetchone()
    if not mp:
        raise S3Error('NoSuchUpload', 'No such upload', UploadId=upload_id)
    row = _get_bucket_row(bucket_name)
    if not row or not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    data = request.get_data(cache=False)
    size = len(data)
    digest_md5 = hashlib.md5(data).hexdigest()
    digest_sha = hashlib.sha256(data).hexdigest()
    import os as _os, tempfile as _tf
    sub = _os.path.join(storage.BLOB_DIR, digest_sha[:2], digest_sha[2:4])
    _os.makedirs(sub, exist_ok=True)
    path = _os.path.join(sub, digest_sha)
    if not _os.path.exists(path):
        fd, tmp = _tf.mkstemp(dir=storage.TMP_DIR)
        with _os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); _os.fsync(f.fileno())
        try: _os.replace(tmp, path)
        except Exception:
            try: _os.unlink(tmp)
            except: pass
    c.execute('INSERT OR REPLACE INTO multipart_parts(upload_id,part_number,etag,size,storage_path,uploaded_at) VALUES(?,?,?,?,?,?)',
              (upload_id, part_number, digest_md5, size, path, now_iso()))
    resp = Response('', status=200)
    resp.headers['ETag'] = f'"{digest_md5}"'
    return resp

def abort_multipart(bucket_name, key, auth_info):
    upload_id = request.args.get('uploadId')
    c = db.conn()
    mp = c.execute('SELECT * FROM multipart WHERE upload_id=? AND bucket=? AND key=?', (upload_id, bucket_name, key)).fetchone()
    if not mp:
        raise S3Error('NoSuchUpload','No such upload', UploadId=upload_id)
    row = _get_bucket_row(bucket_name)
    if not row or not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    c.execute('DELETE FROM multipart_parts WHERE upload_id=?', (upload_id,))
    c.execute('DELETE FROM multipart WHERE upload_id=?', (upload_id,))
    return Response('', status=204)

def list_parts(bucket_name, key, auth_info):
    upload_id = request.args.get('uploadId')
    c = db.conn()
    mp = c.execute('SELECT * FROM multipart WHERE upload_id=? AND bucket=? AND key=?', (upload_id, bucket_name, key)).fetchone()
    if not mp:
        raise S3Error('NoSuchUpload','No such upload', UploadId=upload_id)
    row = _get_bucket_row(bucket_name)
    if not row or not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    parts_rows = c.execute('SELECT * FROM multipart_parts WHERE upload_id=? ORDER BY part_number', (upload_id,)).fetchall()
    parts = ['<ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Bucket>{_esc(bucket_name)}</Bucket><Key>{_esc(key)}</Key>',
             f'<UploadId>{upload_id}</UploadId>',
             '<StorageClass>STANDARD</StorageClass>',
             '<IsTruncated>false</IsTruncated>']
    for p in parts_rows:
        parts.append(f'<Part><PartNumber>{p["part_number"]}</PartNumber><LastModified>{p["uploaded_at"]}</LastModified><ETag>"{p["etag"]}"</ETag><Size>{p["size"]}</Size></Part>')
    parts.append('</ListPartsResult>')
    return _xml(''.join(parts))

def list_multipart_uploads(bucket_name, auth_info):
    _owner_only(bucket_name, auth_info)
    c = db.conn()
    rows = c.execute('SELECT * FROM multipart WHERE bucket=? ORDER BY initiated_at', (bucket_name,)).fetchall()
    parts = ['<ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
             f'<Bucket>{_esc(bucket_name)}</Bucket>',
             '<IsTruncated>false</IsTruncated>']
    for r in rows:
        parts.append(f'<Upload><Key>{_esc(r["key"])}</Key><UploadId>{r["upload_id"]}</UploadId><Initiated>{r["initiated_at"]}</Initiated><StorageClass>STANDARD</StorageClass></Upload>')
    parts.append('</ListMultipartUploadsResult>')
    return _xml(''.join(parts))

def complete_multipart(bucket_name, key, auth_info):
    upload_id = request.args.get('uploadId')
    c = db.conn()
    mp = c.execute('SELECT * FROM multipart WHERE upload_id=? AND bucket=? AND key=?', (upload_id, bucket_name, key)).fetchone()
    if not mp:
        raise S3Error('NoSuchUpload','No such upload', UploadId=upload_id)
    row = _get_bucket_row(bucket_name)
    if not row or not auth_info or row['tenant'] != auth_info['tenant']:
        raise S3Error('AccessDenied','Access Denied')
    body = request.get_data()
    try:
        rt = ET.fromstring(body)
    except Exception:
        raise S3Error('MalformedXML','Invalid XML')
    def localname(t): return t.split('}',1)[1] if '}' in t else t
    requested = []
    for ce in list(rt):
        if localname(ce.tag) == 'Part':
            pn = None; et = None
            for sc in list(ce):
                if localname(sc.tag) == 'PartNumber': pn = int(sc.text)
                if localname(sc.tag) == 'ETag': et = (sc.text or '').strip().strip('"')
            requested.append((pn, et))
    if not requested:
        raise S3Error('MalformedXML','No parts')
    last_pn = 0
    for pn, _et in requested:
        if pn is None or pn <= last_pn:
            raise S3Error('InvalidPartOrder','Parts must be in ascending order')
        last_pn = pn
    all_parts = {p['part_number']: p for p in c.execute('SELECT * FROM multipart_parts WHERE upload_id=?', (upload_id,)).fetchall()}
    for pn, et in requested:
        p = all_parts.get(pn)
        if not p or p['etag'] != et:
            raise S3Error('InvalidPart','Invalid part', UploadId=upload_id, PartNumber=str(pn))
    n = len(requested)
    for i, (pn, _et) in enumerate(requested):
        if i < n - 1 and all_parts[pn]['size'] < MIN_PART_SIZE:
            raise S3Error('EntityTooSmall','Part smaller than minimum')
    total_size = sum(all_parts[pn]['size'] for pn, _ in requested)
    from . import tenants as tenants_mod
    t = tenants_mod.get_tenant(row['tenant'])
    if t and t.get('quota_bytes') is not None:
        used = tenants_mod.get_tenant_used_bytes(row['tenant'])
        if used + total_size > t['quota_bytes']:
            raise S3Error('QuotaExceeded','Quota exceeded', BucketName=bucket_name)
    import os as _os, tempfile as _tf
    fd, tmp = _tf.mkstemp(dir=storage.TMP_DIR)
    sha = hashlib.sha256()
    md_concat = hashlib.md5()
    with _os.fdopen(fd, 'wb') as out:
        for pn, _et in requested:
            p = all_parts[pn]
            md_concat.update(bytes.fromhex(p['etag']))
            with open(p['storage_path'], 'rb') as f:
                while True:
                    ch = f.read(65536)
                    if not ch: break
                    out.write(ch); sha.update(ch)
        out.flush(); _os.fsync(out.fileno())
    digest_sha = sha.hexdigest()
    sub = _os.path.join(storage.BLOB_DIR, digest_sha[:2], digest_sha[2:4])
    _os.makedirs(sub, exist_ok=True)
    final_path = _os.path.join(sub, digest_sha)
    if _os.path.exists(final_path):
        try: _os.unlink(tmp)
        except: pass
    else:
        _os.replace(tmp, final_path)
    final_etag = f'{md_concat.hexdigest()}-{n}'
    metadata = mp['metadata'] or '{}'
    content_type = mp['content_type'] or 'application/octet-stream'
    tagging = mp['tagging'] or '{}'
    version_id = gen_version_id() if row['versioning'] == 'Enabled' else 'null'
    if row['versioning'] != 'Enabled':
        c.execute('DELETE FROM objects WHERE bucket=? AND key=?', (bucket_name, key))
    else:
        c.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (bucket_name, key))
    c.execute('''INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,etag,content_type,metadata,tagging,storage_path,created_at)
                 VALUES(?,?,?,1,0,?,?,?,?,?,?,?)''',
              (bucket_name, key, version_id, total_size, final_etag, content_type,
               metadata, tagging, final_path, now_iso()))
    c.execute('DELETE FROM multipart_parts WHERE upload_id=?', (upload_id,))
    c.execute('DELETE FROM multipart WHERE upload_id=?', (upload_id,))
    location = f'/{bucket_name}/{key}'
    body_xml = (f'<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f'<Location>{_esc(location)}</Location>'
                f'<Bucket>{_esc(bucket_name)}</Bucket>'
                f'<Key>{_esc(key)}</Key>'
                f'<ETag>"{final_etag}"</ETag>'
                f'</CompleteMultipartUploadResult>')
    _emit_event(bucket_name, 's3:ObjectCreated:CompleteMultipartUpload', key, size=total_size, etag=f'"{final_etag}"')
    resp = _xml(body_xml)
    if row['versioning'] == 'Enabled':
        resp.headers['x-amz-version-id'] = version_id
    return resp
