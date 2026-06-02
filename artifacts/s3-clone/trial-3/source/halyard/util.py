import hashlib
import os
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
import re

def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f"{datetime.now(timezone.utc).microsecond//1000:03d}Z"

def now_http():
    # RFC 1123
    return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')

def gen_request_id():
    return uuid.uuid4().hex.upper()

def gen_access_key_id():
    return 'AKIA' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))

def gen_secret_key():
    alphabet = string.ascii_letters + string.digits + '/+'
    return ''.join(secrets.choice(alphabet) for _ in range(40))

def gen_version_id():
    return uuid.uuid4().hex

def gen_upload_id():
    return uuid.uuid4().hex + uuid.uuid4().hex

def md5_hex(data):
    return hashlib.md5(data).hexdigest()

VALID_BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
VALID_TENANT_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$')

def valid_bucket_name(name):
    if not name or len(name) < 3 or len(name) > 63:
        return False
    if not VALID_BUCKET_RE.match(name):
        return False
    if '..' in name:
        return False
    if name.startswith('-') or name.endswith('-'):
        return False
    # No IP-like
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', name):
        return False
    return True

def valid_tenant_name(name):
    return bool(name and VALID_TENANT_RE.match(name))
