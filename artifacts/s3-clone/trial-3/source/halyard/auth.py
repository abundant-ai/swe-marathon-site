import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone, timedelta
from .errors import S3Error
from . import db

SIGNED_PAYLOAD_UNSIGNED = 'UNSIGNED-PAYLOAD'
SIGNED_PAYLOAD_STREAMING = 'STREAMING-AWS4-HMAC-SHA256-PAYLOAD'

def _signing_key(secret, datestamp, region, service):
    kDate = hmac.new(('AWS4' + secret).encode('utf-8'), datestamp.encode('utf-8'), hashlib.sha256).digest()
    kRegion = hmac.new(kDate, region.encode('utf-8'), hashlib.sha256).digest()
    kService = hmac.new(kRegion, service.encode('utf-8'), hashlib.sha256).digest()
    return hmac.new(kService, b'aws4_request', hashlib.sha256).digest()

def _uri_encode(s, encode_slash=True):
    safe = '-_.~'
    if not encode_slash:
        safe += '/'
    return urllib.parse.quote(s, safe=safe)

def _canonical_query_string(query_string, exclude_signature=False):
    if not query_string:
        return ''
    pairs = []
    for k, v in urllib.parse.parse_qsl(query_string, keep_blank_values=True):
        if exclude_signature and k == 'X-Amz-Signature':
            continue
        pairs.append((_uri_encode(k), _uri_encode(v)))
    pairs.sort()
    return '&'.join(f'{k}={v}' for k, v in pairs)

def _canonical_headers(headers, signed_headers_list):
    out = []
    for h in signed_headers_list:
        v = headers.get(h, '')
        v = ' '.join(v.split()) if v else ''
        out.append(f'{h}:{v}\n')
    return ''.join(out)

def _get_header(env, name):
    if name == 'host':
        return env.get('HTTP_HOST', '')
    if name == 'content-type':
        return env.get('CONTENT_TYPE', '')
    if name == 'content-length':
        return env.get('CONTENT_LENGTH', '')
    key = 'HTTP_' + name.upper().replace('-', '_')
    return env.get(key, '')

def get_secret_for_key(access_key_id):
    c = db.conn()
    row = c.execute('SELECT * FROM access_keys WHERE access_key_id=? AND revoked=0', (access_key_id,)).fetchone()
    if not row:
        return None
    return {'tenant': row['tenant'], 'secret': row['secret_access_key'], 'access_key_id': row['access_key_id']}

def _check_skew(amz_date_str, max_skew=900):
    try:
        dt = datetime.strptime(amz_date_str, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        raise S3Error('AccessDenied', 'Invalid X-Amz-Date')
    now = datetime.now(timezone.utc)
    if abs((now - dt).total_seconds()) > max_skew:
        raise S3Error('AccessDenied', 'Request expired')

def _payload_hash(request, query_signed):
    if query_signed:
        return 'UNSIGNED-PAYLOAD'
    h = request.headers.get('x-amz-content-sha256')
    if not h:
        data = request.get_data(cache=True)
        return hashlib.sha256(data).hexdigest()
    return h

def _build_canonical(request, signed_headers, payload_hash, canonical_qs):
    method = request.method
    raw_path = request.environ.get('RAW_URI') or request.environ.get('REQUEST_URI') or request.path
    if '?' in raw_path:
        raw_path = raw_path.split('?', 1)[0]
    if not raw_path:
        raw_path = '/'
    canonical_uri = _uri_encode(urllib.parse.unquote(raw_path), encode_slash=False)
    headers = {}
    env = request.environ
    for h in signed_headers:
        headers[h] = _get_header(env, h)
    canonical_headers = _canonical_headers(headers, signed_headers)
    signed_headers_str = ';'.join(signed_headers)
    return '\n'.join([method, canonical_uri, canonical_qs, canonical_headers, signed_headers_str, payload_hash])

def _string_to_sign(amz_date, credential_scope, canonical_request):
    return '\n'.join(['AWS4-HMAC-SHA256', amz_date, credential_scope,
                      hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()])

def verify_header_sigv4(request):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('AWS4-HMAC-SHA256 '):
        raise S3Error('AccessDenied', 'Missing or invalid Authorization')
    parts = auth[len('AWS4-HMAC-SHA256 '):].split(',')
    fields = {}
    for p in parts:
        if '=' in p:
            k, v = p.strip().split('=', 1)
            fields[k.strip()] = v.strip()
    cred = fields.get('Credential', '')
    signed_headers = fields.get('SignedHeaders', '').split(';')
    signature = fields.get('Signature', '')
    if not cred or not signed_headers or not signature:
        raise S3Error('AccessDenied', 'Malformed authorization header')
    cred_parts = cred.split('/')
    if len(cred_parts) != 5:
        raise S3Error('AccessDenied', 'Malformed credential')
    access_key_id, datestamp, region, service, _term = cred_parts
    amz_date = request.headers.get('x-amz-date') or request.headers.get('X-Amz-Date')
    if not amz_date:
        raise S3Error('AccessDenied', 'Missing X-Amz-Date')
    _check_skew(amz_date)
    info = get_secret_for_key(access_key_id)
    if not info:
        raise S3Error('InvalidAccessKeyId', 'The AWS Access Key Id you provided does not exist in our records.')
    payload_hash = _payload_hash(request, query_signed=False)
    qs = request.environ.get('QUERY_STRING', '')
    canonical_qs = _canonical_query_string(qs)
    canonical_request = _build_canonical(request, signed_headers, payload_hash, canonical_qs)
    credential_scope = f'{datestamp}/{region}/{service}/aws4_request'
    sts = _string_to_sign(amz_date, credential_scope, canonical_request)
    key = _signing_key(info['secret'], datestamp, region, service)
    expected = hmac.new(key, sts.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise S3Error('SignatureDoesNotMatch', 'The request signature we calculated does not match the signature you provided.')
    return info

def verify_query_sigv4(request):
    args = request.args
    if args.get('X-Amz-Algorithm') != 'AWS4-HMAC-SHA256':
        return None
    cred = args.get('X-Amz-Credential', '')
    signed_headers = args.get('X-Amz-SignedHeaders', '').split(';')
    signature = args.get('X-Amz-Signature', '')
    amz_date = args.get('X-Amz-Date', '')
    expires = args.get('X-Amz-Expires', '3600')
    if not (cred and signed_headers and signature and amz_date):
        raise S3Error('AccessDenied', 'Missing presign params')
    cred_parts = cred.split('/')
    if len(cred_parts) != 5:
        raise S3Error('AccessDenied', 'Malformed credential')
    access_key_id, datestamp, region, service, _ = cred_parts
    try:
        dt = datetime.strptime(amz_date, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        raise S3Error('AccessDenied', 'Invalid X-Amz-Date')
    exp_secs = int(expires)
    now = datetime.now(timezone.utc)
    if now > dt + timedelta(seconds=exp_secs + 900):
        raise S3Error('AccessDenied', 'Request has expired')
    if now < dt - timedelta(seconds=900):
        raise S3Error('AccessDenied', 'Request not yet valid')
    info = get_secret_for_key(access_key_id)
    if not info:
        raise S3Error('InvalidAccessKeyId', 'The AWS Access Key Id you provided does not exist in our records.')
    qs = request.environ.get('QUERY_STRING', '')
    canonical_qs = _canonical_query_string(qs, exclude_signature=True)
    canonical_request = _build_canonical(request, signed_headers, 'UNSIGNED-PAYLOAD', canonical_qs)
    credential_scope = f'{datestamp}/{region}/{service}/aws4_request'
    sts = _string_to_sign(amz_date, credential_scope, canonical_request)
    key = _signing_key(info['secret'], datestamp, region, service)
    expected = hmac.new(key, sts.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise S3Error('SignatureDoesNotMatch', 'The request signature we calculated does not match the signature you provided.')
    return info

def verify_request(request):
    """Returns auth info dict or None for anonymous."""
    if request.args.get('X-Amz-Algorithm'):
        return verify_query_sigv4(request)
    if request.headers.get('Authorization', '').startswith('AWS4-HMAC-SHA256'):
        return verify_header_sigv4(request)
    return None
