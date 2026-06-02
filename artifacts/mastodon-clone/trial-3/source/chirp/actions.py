import json, os, time, datetime
from .db import conn, q, qone, qall
from .util import snowflake, now_iso, extract_tags, extract_mentions, INSTANCE

SSE_LISTENERS = {}

def _sse_emit(account_id, event, data):
    import json as _j
    qs = SSE_LISTENERS.get(account_id) or []
    for q_ in list(qs):
        try: q_.put_nowait((event, _j.dumps(data)))
        except Exception: pass

def create_account(username, *, password=None, email=None, is_admin=0, display_name=None, note="", domain=None, account_id=None, avatar="", header=""):
    aid = account_id or snowflake()
    acct = username if not domain else f"{username}@{domain}"
    is_local = 1 if not domain else 0
    url = f"https://{INSTANCE}/@{username}" if is_local else f"https://{domain}/@{username}"
    from .util import pwhash
    pw = pwhash(password) if password else None
    q("INSERT INTO accounts(id, username, domain, acct, display_name, note, created_at, password_hash, email, is_admin, is_local, url, avatar, header) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      aid, username, domain, acct, display_name or username, note, now_iso(), pw, email, is_admin, is_local, url, avatar, header)
    return aid

def account_by_username(username, domain=None):
    if domain:
        return qone("SELECT * FROM accounts WHERE username=? AND domain=?", username, domain)
    return qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", username)

def notify(account_id, from_id, type_, status_id=None):
    if account_id == from_id: return
    if qone("SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", account_id, from_id): return
    nid = snowflake()
    q("INSERT INTO notifications(id, account_id, from_account_id, type, status_id, created_at) VALUES(?,?,?,?,?,?)",
      nid, account_id, from_id, type_, status_id, now_iso())
    n = qone("SELECT * FROM notifications WHERE id=?", nid)
    try:
        from .serial import notification
        _sse_emit(account_id, "notification", notification(n))
    except Exception: pass

def _expires_after(seconds):
    return (datetime.datetime.utcnow() + datetime.timedelta(seconds=int(seconds))).replace(microsecond=0).isoformat() + "Z"

def create_status(account_id, *, content="", visibility="public", in_reply_to_id=None,
                  spoiler_text="", sensitive=False, language=None, media_ids=None,
                  application_id=None, idempotency_key=None, poll=None):
    if idempotency_key:
        existing = qone("SELECT * FROM statuses WHERE account_id=? AND idempotency_key=? AND deleted=0", account_id, idempotency_key)
        if existing: return existing["id"]
    sid = snowflake()
    irt_acc = None
    if in_reply_to_id:
        parent = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", in_reply_to_id)
        if parent: irt_acc = parent["account_id"]
    acc = qone("SELECT * FROM accounts WHERE id=?", account_id)
    url = f"https://{INSTANCE}/@{acc['username']}/{sid}"
    uri = f"https://{INSTANCE}/users/{acc['username']}/statuses/{sid}"
    q("INSERT INTO statuses(id, account_id, content, spoiler_text, visibility, sensitive, in_reply_to_id, in_reply_to_account_id, language, created_at, uri, url, application_id, idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      sid, account_id, content, spoiler_text or "", visibility, 1 if sensitive else 0,
      in_reply_to_id, irt_acc, language, now_iso(), uri, url, application_id, idempotency_key)
    q("UPDATE accounts SET statuses_count=statuses_count+1 WHERE id=?", account_id)
    for t in extract_tags(content):
        q("INSERT OR IGNORE INTO hashtags(name, created_at) VALUES(?,?)", t, now_iso())
        q("INSERT OR IGNORE INTO status_tags(status_id, tag) VALUES(?,?)", sid, t)
    for user, dom in extract_mentions(content):
        target = account_by_username(user, dom) if dom else account_by_username(user)
        if target:
            q("INSERT OR IGNORE INTO status_mentions(status_id, account_id) VALUES(?,?)", sid, target["id"])
            notify(target["id"], account_id, "mention", sid)
    if media_ids:
        for mid in media_ids:
            q("UPDATE media SET status_id=? WHERE id=? AND account_id=? AND status_id IS NULL", sid, mid, account_id)
    if poll and poll.get("options"):
        pid = snowflake()
        exp = poll.get("expires_at") or _expires_after(poll.get("expires_in") or 86400)
        q("INSERT INTO polls(id, status_id, account_id, expires_at, multiple, hide_totals, created_at) VALUES(?,?,?,?,?,?,?)",
          pid, sid, account_id, exp, 1 if poll.get("multiple") else 0, 1 if poll.get("hide_totals") else 0, now_iso())
        for i, opt in enumerate(poll["options"]):
            q("INSERT INTO poll_options(poll_id, idx, title) VALUES(?,?,?)", pid, i, opt)
        q("UPDATE statuses SET poll_id=? WHERE id=?", pid, sid)
    if in_reply_to_id:
        q("UPDATE statuses SET replies_count=replies_count+1 WHERE id=?", in_reply_to_id)
        if irt_acc and irt_acc != account_id:
            notify(irt_acc, account_id, "mention", sid)
    try:
        from .serial import status as ser_status
        s_obj = ser_status(qone("SELECT * FROM statuses WHERE id=?", sid))
        if visibility in ("public","unlisted"):
            for r in qall("SELECT id FROM accounts WHERE is_local=1"):
                _sse_emit(r["id"], "update", s_obj)
        for r in qall("SELECT follower_id FROM follows WHERE target_id=?", account_id):
            _sse_emit(r["follower_id"], "update", s_obj)
        _sse_emit(account_id, "update", s_obj)
    except Exception: pass
    return sid

def delete_status(sid, account_id):
    s = qone("SELECT * FROM statuses WHERE id=?", sid)
    if not s or s["account_id"] != account_id: return False
    q("UPDATE statuses SET deleted=1 WHERE id=?", sid)
    q("UPDATE accounts SET statuses_count=MAX(0,statuses_count-1) WHERE id=?", account_id)
    if s["in_reply_to_id"]:
        q("UPDATE statuses SET replies_count=MAX(0,replies_count-1) WHERE id=?", s["in_reply_to_id"])
    if s["reblog_of_id"]:
        q("UPDATE statuses SET reblogs_count=MAX(0,reblogs_count-1) WHERE id=?", s["reblog_of_id"])
    return True

def edit_status(sid, account_id, *, content=None, spoiler_text=None, sensitive=None):
    s = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not s or s["account_id"] != account_id: return None
    q("INSERT INTO status_edits(status_id, content, spoiler_text, sensitive, created_at) VALUES(?,?,?,?,?)",
      sid, s["content"], s["spoiler_text"], s["sensitive"], s["edited_at"] or s["created_at"])
    new_content = s["content"] if content is None else content
    new_spoiler = s["spoiler_text"] if spoiler_text is None else spoiler_text
    new_sens = s["sensitive"] if sensitive is None else (1 if sensitive else 0)
    q("UPDATE statuses SET content=?, spoiler_text=?, sensitive=?, edited_at=? WHERE id=?",
      new_content, new_spoiler, new_sens, now_iso(), sid)
    return sid

def favourite(account_id, sid):
    if qone("SELECT 1 FROM favourites WHERE account_id=? AND status_id=?", account_id, sid): return
    q("INSERT INTO favourites(account_id, status_id, created_at) VALUES(?,?,?)", account_id, sid, now_iso())
    q("UPDATE statuses SET favourites_count=favourites_count+1 WHERE id=?", sid)
    s = qone("SELECT account_id FROM statuses WHERE id=?", sid)
    if s: notify(s["account_id"], account_id, "favourite", sid)

def unfavourite(account_id, sid):
    if not qone("SELECT 1 FROM favourites WHERE account_id=? AND status_id=?", account_id, sid): return
    q("DELETE FROM favourites WHERE account_id=? AND status_id=?", account_id, sid)
    q("UPDATE statuses SET favourites_count=MAX(0,favourites_count-1) WHERE id=?", sid)

def bookmark(account_id, sid):
    if qone("SELECT 1 FROM bookmarks WHERE account_id=? AND status_id=?", account_id, sid): return
    q("INSERT INTO bookmarks(account_id, status_id, created_at) VALUES(?,?,?)", account_id, sid, now_iso())

def unbookmark(account_id, sid):
    q("DELETE FROM bookmarks WHERE account_id=? AND status_id=?", account_id, sid)

def reblog(account_id, sid, visibility="public"):
    existing = qone("SELECT id FROM statuses WHERE account_id=? AND reblog_of_id=? AND deleted=0", account_id, sid)
    if existing: return existing["id"]
    parent = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not parent: return None
    rid = snowflake()
    acc = qone("SELECT * FROM accounts WHERE id=?", account_id)
    url = f"https://{INSTANCE}/@{acc['username']}/{rid}"
    q("INSERT INTO statuses(id, account_id, content, visibility, reblog_of_id, created_at, uri, url) VALUES(?,?,?,?,?,?,?,?)",
      rid, account_id, "", visibility, sid, now_iso(), url, url)
    q("UPDATE statuses SET reblogs_count=reblogs_count+1 WHERE id=?", sid)
    notify(parent["account_id"], account_id, "reblog", sid)
    return rid

def unreblog(account_id, sid):
    existing = qone("SELECT id FROM statuses WHERE account_id=? AND reblog_of_id=? AND deleted=0", account_id, sid)
    if not existing: return
    q("UPDATE statuses SET deleted=1 WHERE id=?", existing["id"])
    q("UPDATE statuses SET reblogs_count=MAX(0,reblogs_count-1) WHERE id=?", sid)

def follow(follower_id, target_id):
    if follower_id == target_id: return False
    if qone("SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", target_id, follower_id): return False
    if qone("SELECT 1 FROM follows WHERE follower_id=? AND target_id=?", follower_id, target_id): return True
    target = qone("SELECT * FROM accounts WHERE id=?", target_id)
    if not target: return False
    if target["locked"]:
        q("INSERT OR IGNORE INTO follow_requests(follower_id, target_id, created_at) VALUES(?,?,?)", follower_id, target_id, now_iso())
        notify(target_id, follower_id, "follow_request")
        return True
    q("INSERT INTO follows(follower_id, target_id, created_at) VALUES(?,?,?)", follower_id, target_id, now_iso())
    q("UPDATE accounts SET following_count=following_count+1 WHERE id=?", follower_id)
    q("UPDATE accounts SET followers_count=followers_count+1 WHERE id=?", target_id)
    notify(target_id, follower_id, "follow")
    return True

def unfollow(follower_id, target_id):
    if not qone("SELECT 1 FROM follows WHERE follower_id=? AND target_id=?", follower_id, target_id): return
    q("DELETE FROM follows WHERE follower_id=? AND target_id=?", follower_id, target_id)
    q("UPDATE accounts SET following_count=MAX(0,following_count-1) WHERE id=?", follower_id)
    q("UPDATE accounts SET followers_count=MAX(0,followers_count-1) WHERE id=?", target_id)

def block(account_id, target_id):
    q("INSERT OR IGNORE INTO blocks(account_id, target_id, created_at) VALUES(?,?,?)", account_id, target_id, now_iso())
    unfollow(account_id, target_id); unfollow(target_id, account_id)

def unblock(account_id, target_id):
    q("DELETE FROM blocks WHERE account_id=? AND target_id=?", account_id, target_id)

def mute(account_id, target_id):
    q("INSERT OR IGNORE INTO mutes(account_id, target_id, created_at) VALUES(?,?,?)", account_id, target_id, now_iso())

def unmute(account_id, target_id):
    q("DELETE FROM mutes WHERE account_id=? AND target_id=?", account_id, target_id)

def audit(actor_id, action, target=None, meta=None):
    q("INSERT INTO audit_log(actor_id, action, target, meta, created_at) VALUES(?,?,?,?,?)",
      actor_id, action, target, json.dumps(meta or {}), now_iso())
