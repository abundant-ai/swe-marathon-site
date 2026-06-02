import os
import threading
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from . import db
from .util import now_iso, gen_version_id

TICK = float(os.environ.get('S3CLONE_LIFECYCLE_TICK_SECONDS', '2'))
SECONDS_PER_DAY = float(os.environ.get('S3CLONE_LIFECYCLE_SECONDS_PER_DAY', '86400'))
_started = False
_lock = threading.Lock()

def parse_lifecycle(xml_text):
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        from .errors import S3Error
        raise S3Error('MalformedXML', 'Invalid lifecycle XML')
    def localname(t): return t.split('}',1)[1] if '}' in t else t
    rules = []
    for child in list(root):
        if localname(child.tag) != 'Rule':
            continue
        rule = {'id': None, 'status': 'Enabled', 'prefix': '', 'expiration_days': None}
        for c in list(child):
            t = localname(c.tag)
            if t == 'ID':
                rule['id'] = c.text
            elif t == 'Status':
                rule['status'] = c.text
            elif t == 'Prefix':
                rule['prefix'] = c.text or ''
            elif t == 'Filter':
                for f in list(c):
                    ft = localname(f.tag)
                    if ft == 'Prefix':
                        rule['prefix'] = f.text or ''
            elif t == 'Expiration':
                for e in list(c):
                    if localname(e.tag) == 'Days':
                        rule['expiration_days'] = int(e.text)
        rules.append(rule)
    return rules

def serialize_lifecycle(rules):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<LifecycleConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
    for r in rules:
        parts.append('<Rule>')
        if r.get('id'):
            parts.append(f'<ID>{r["id"]}</ID>')
        parts.append(f'<Status>{r.get("status","Enabled")}</Status>')
        parts.append(f'<Filter><Prefix>{r.get("prefix","")}</Prefix></Filter>')
        if r.get('expiration_days') is not None:
            parts.append(f'<Expiration><Days>{r["expiration_days"]}</Days></Expiration>')
        parts.append('</Rule>')
    parts.append('</LifecycleConfiguration>')
    return ''.join(parts)

def _parse_iso(s):
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)

def _expire_pass():
    c = db.conn()
    now = datetime.now(timezone.utc)
    rows = c.execute('SELECT name, lifecycle, versioning FROM buckets WHERE lifecycle IS NOT NULL').fetchall()
    for b in rows:
        try:
            rules = parse_lifecycle(b['lifecycle'])
        except Exception:
            continue
        versioning = b['versioning']
        for rule in rules:
            if rule.get('status') != 'Enabled':
                continue
            days = rule.get('expiration_days')
            if days is None:
                continue
            cutoff_seconds = days * SECONDS_PER_DAY
            prefix = rule.get('prefix') or ''
            objs = c.execute('SELECT * FROM objects WHERE bucket=? AND is_latest=1 AND delete_marker=0',
                             (b['name'],)).fetchall()
            for o in objs:
                if not o['key'].startswith(prefix):
                    continue
                try:
                    age = (now - _parse_iso(o['created_at'])).total_seconds()
                except Exception:
                    continue
                if age < cutoff_seconds:
                    continue
                if versioning == 'Enabled':
                    c.execute('UPDATE objects SET is_latest=0 WHERE bucket=? AND key=?', (b['name'], o['key']))
                    vid = gen_version_id()
                    c.execute('''INSERT INTO objects(bucket,key,version_id,is_latest,delete_marker,size,created_at)
                                 VALUES(?,?,?,1,1,0,?)''',
                              (b['name'], o['key'], vid, now_iso()))
                else:
                    c.execute('DELETE FROM objects WHERE bucket=? AND key=?', (b['name'], o['key']))

def _loop():
    while True:
        try:
            _expire_pass()
        except Exception:
            pass
        time.sleep(TICK)

def start_worker():
    global _started
    with _lock:
        if _started:
            return
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        _started = True
