import json
import threading
import time
import os
import xml.etree.ElementTree as ET
from . import db
from .util import now_iso

try:
    import requests as _requests
except Exception:
    _requests = None

MAX_ATTEMPTS = 5
_started = False
_lock = threading.Lock()

def parse_notification_config(xml_text):
    """Returns list of configs: [{type, id, arn, events, prefix, suffix}]"""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        from .errors import S3Error
        raise S3Error('MalformedXML', 'Invalid notification XML')
    # strip namespace
    def localname(t):
        return t.split('}',1)[1] if '}' in t else t
    out = []
    for child in list(root):
        tag = localname(child.tag)
        if tag in ('CloudFunctionConfiguration', 'QueueConfiguration', 'TopicConfiguration'):
            cfg = {'type': tag, 'events': [], 'prefix': None, 'suffix': None, 'id': None, 'arn': None}
            for c in list(child):
                ct = localname(c.tag)
                if ct == 'Id':
                    cfg['id'] = c.text
                elif ct in ('CloudFunction', 'Queue', 'Topic'):
                    cfg['arn'] = c.text
                elif ct == 'Event':
                    cfg['events'].append(c.text)
                elif ct == 'Filter':
                    for f in c.iter():
                        ft = localname(f.tag)
                        if ft == 'FilterRule':
                            name = None
                            value = None
                            for ff in list(f):
                                fft = localname(ff.tag)
                                if fft == 'Name': name = (ff.text or '').lower()
                                if fft == 'Value': value = ff.text or ''
                            if name == 'prefix':
                                cfg['prefix'] = value
                            elif name == 'suffix':
                                cfg['suffix'] = value
            out.append(cfg)
    return out

def serialize_notification_config(configs):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
    for cfg in configs:
        t = cfg['type']
        endpoint_tag = {'CloudFunctionConfiguration':'CloudFunction','QueueConfiguration':'Queue','TopicConfiguration':'Topic'}[t]
        parts.append(f'<{t}>')
        if cfg.get('id'):
            parts.append(f'<Id>{cfg["id"]}</Id>')
        parts.append(f'<{endpoint_tag}>{cfg["arn"]}</{endpoint_tag}>')
        for e in cfg.get('events', []):
            parts.append(f'<Event>{e}</Event>')
        if cfg.get('prefix') or cfg.get('suffix'):
            parts.append('<Filter><S3Key>')
            if cfg.get('prefix') is not None:
                parts.append(f'<FilterRule><Name>prefix</Name><Value>{cfg["prefix"]}</Value></FilterRule>')
            if cfg.get('suffix') is not None:
                parts.append(f'<FilterRule><Name>suffix</Name><Value>{cfg["suffix"]}</Value></FilterRule>')
            parts.append('</S3Key></Filter>')
        parts.append(f'</{t}>')
    parts.append('</NotificationConfiguration>')
    return ''.join(parts)

def event_matches(cfg_events, event_name):
    for e in cfg_events:
        if e == event_name:
            return True
        if e.endswith(':*'):
            prefix = e[:-1]  # "s3:ObjectCreated:"
            if event_name.startswith(prefix):
                return True
    return False

def enqueue_event(bucket_name, configs, event_name, key, size=None, etag=None):
    if not configs:
        return
    for cfg in configs:
        if not event_matches(cfg.get('events', []), event_name):
            continue
        prefix = cfg.get('prefix')
        suffix = cfg.get('suffix')
        if prefix and not key.startswith(prefix):
            continue
        if suffix and not key.endswith(suffix):
            continue
        url = cfg.get('arn') or ''
        if not url.startswith('http://') and not url.startswith('https://'):
            continue
        record = {
            'eventVersion': '2.2',
            'eventSource': 'halyard:s3',
            'eventTime': now_iso(),
            'eventName': event_name,
            's3': {
                'bucket': {'name': bucket_name},
                'object': {'key': key, 'size': size, 'eTag': etag},
            }
        }
        payload = json.dumps({'Records': [record]})
        c = db.conn()
        c.execute('INSERT INTO notification_outbox(bucket,url,payload,attempts,next_attempt,created_at) VALUES(?,?,?,?,?,?)',
                  (bucket_name, url, payload, 0, time.time(), now_iso()))

def _drain_once():
    c = db.conn()
    rows = c.execute('SELECT * FROM notification_outbox WHERE next_attempt <= ? ORDER BY id LIMIT 100', (time.time(),)).fetchall()
    for row in rows:
        try:
            r = _requests.post(row['url'], data=row['payload'], headers={'Content-Type':'application/json'}, timeout=5)
            ok = 200 <= r.status_code < 300
        except Exception:
            ok = False
        if ok:
            c.execute('DELETE FROM notification_outbox WHERE id=?', (row['id'],))
        else:
            attempts = row['attempts'] + 1
            if attempts >= MAX_ATTEMPTS:
                c.execute('DELETE FROM notification_outbox WHERE id=?', (row['id'],))
            else:
                delay = 2 ** (attempts - 1)
                c.execute('UPDATE notification_outbox SET attempts=?, next_attempt=? WHERE id=?',
                          (attempts, time.time() + delay, row['id']))

def _worker_loop():
    while True:
        try:
            _drain_once()
        except Exception:
            pass
        time.sleep(1)

def start_worker():
    global _started
    with _lock:
        if _started:
            return
        if _requests is None:
            return
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        _started = True
