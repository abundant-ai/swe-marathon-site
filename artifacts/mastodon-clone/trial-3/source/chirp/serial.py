import json
from .db import qone, qall
from .util import render_content, INSTANCE

def _avatar(acct_row):
    a = acct_row["avatar"]
    if not a:
        return f"/static/avatars/default.png"
    return a

def account(row, viewer_id=None):
    if row is None: return None
    acct = row["acct"]
    username = row["username"]
    domain = row["domain"]
    full_acct = acct if domain else username
    url = row["url"] or (f"https://{INSTANCE}/@{username}" if not domain else f"https://{domain}/@{username}")
    fields = []
    try:
        fields = json.loads(row["fields_json"] or "[]")
    except Exception:
        fields = []
    avatar = _avatar(row)
    header = row["header"] or "/static/headers/default.png"
    return {
        "id": row["id"],
        "username": username,
        "acct": full_acct,
        "display_name": row["display_name"] or username,
        "locked": bool(row["locked"]),
        "bot": bool(row["bot"]),
        "discoverable": True,
        "group": False,
        "created_at": row["created_at"],
        "note": render_content(row["note"] or ""),
        "url": url,
        "uri": url,
        "avatar": avatar,
        "avatar_static": avatar,
        "header": header,
        "header_static": header,
        "followers_count": row["followers_count"],
        "following_count": row["following_count"],
        "statuses_count": row["statuses_count"],
        "last_status_at": None,
        "emojis": [],
        "fields": fields,
    }

def account_by_id(aid, viewer_id=None):
    r = qone("SELECT * FROM accounts WHERE id=?", aid)
    return account(r, viewer_id)

def mention_dict(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "acct": row["acct"] if row["domain"] else row["username"],
        "url": row["url"] or f"https://{INSTANCE}/@{row['username']}",
    }

def tag_dict(name):
    return {"name": name, "url": f"https://{INSTANCE}/tags/{name}", "history": []}

def media_dict(row):
    meta = {}
    try: meta = json.loads(row["meta"] or "{}")
    except Exception: meta = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "url": row["url"],
        "preview_url": row["preview_url"] or row["url"],
        "remote_url": None,
        "text_url": None,
        "description": row["description"],
        "meta": meta,
        "blurhash": row["blurhash"],
    }

def poll_dict(poll_id, viewer_id=None):
    p = qone("SELECT * FROM polls WHERE id=?", poll_id)
    if not p: return None
    opts = qall("SELECT * FROM poll_options WHERE poll_id=? ORDER BY idx", poll_id)
    voted = False
    own_votes = []
    if viewer_id:
        votes = qall("SELECT idx FROM poll_votes WHERE poll_id=? AND account_id=?", poll_id, viewer_id)
        own_votes = [v["idx"] for v in votes]
        voted = bool(own_votes)
    import datetime as _dt
    expired = False
    if p["expires_at"]:
        try:
            expired = _dt.datetime.fromisoformat(p["expires_at"].replace("Z","")) < _dt.datetime.utcnow()
        except Exception: pass
    return {
        "id": p["id"],
        "expires_at": p["expires_at"],
        "expired": expired,
        "multiple": bool(p["multiple"]),
        "votes_count": p["votes_count"],
        "voters_count": p["voters_count"],
        "options": [{"title": o["title"], "votes_count": o["votes_count"]} for o in opts],
        "voted": voted,
        "own_votes": own_votes,
        "emojis": [],
    }

def status(row, viewer_id=None, include_reblog=True):
    if row is None: return None
    if row["deleted"]:
        return None
    acc = qone("SELECT * FROM accounts WHERE id=?", row["account_id"])
    reblog = None
    if include_reblog and row["reblog_of_id"]:
        rb = qone("SELECT * FROM statuses WHERE id=?", row["reblog_of_id"])
        if rb: reblog = status(rb, viewer_id, include_reblog=False)
    media = qall("SELECT * FROM media WHERE status_id=?", row["id"])
    tags = qall("SELECT tag FROM status_tags WHERE status_id=?", row["id"])
    mentions = []
    for m in qall("SELECT account_id FROM status_mentions WHERE status_id=?", row["id"]):
        ar = qone("SELECT * FROM accounts WHERE id=?", m["account_id"])
        if ar: mentions.append(mention_dict(ar))
    favourited = False; reblogged = False; bookmarked = False; muted = False
    if viewer_id:
        favourited = bool(qone("SELECT 1 FROM favourites WHERE account_id=? AND status_id=?", viewer_id, row["id"]))
        reblogged = bool(qone("SELECT 1 FROM statuses WHERE account_id=? AND reblog_of_id=? AND deleted=0", viewer_id, row["id"]))
        bookmarked = bool(qone("SELECT 1 FROM bookmarks WHERE account_id=? AND status_id=?", viewer_id, row["id"]))
    out = {
        "id": row["id"],
        "created_at": row["created_at"],
        "in_reply_to_id": row["in_reply_to_id"],
        "in_reply_to_account_id": row["in_reply_to_account_id"],
        "sensitive": bool(row["sensitive"]),
        "spoiler_text": row["spoiler_text"] or "",
        "visibility": row["visibility"],
        "language": row["language"],
        "uri": row["uri"] or f"https://{INSTANCE}/users/{acc['username']}/statuses/{row['id']}",
        "url": row["url"] or f"https://{INSTANCE}/@{acc['username']}/{row['id']}",
        "replies_count": row["replies_count"],
        "reblogs_count": row["reblogs_count"],
        "favourites_count": row["favourites_count"],
        "edited_at": row["edited_at"],
        "favourited": favourited,
        "reblogged": reblogged,
        "muted": muted,
        "bookmarked": bookmarked,
        "pinned": False,
        "content": render_content(row["content"]),
        "text": row["content"],
        "reblog": reblog,
        "application": None,
        "account": account(acc, viewer_id),
        "media_attachments": [media_dict(m) for m in media],
        "mentions": mentions,
        "tags": [tag_dict(t["tag"]) for t in tags],
        "emojis": [],
        "card": None,
        "poll": poll_dict(row["poll_id"], viewer_id) if row["poll_id"] else None,
    }
    return out

def status_by_id(sid, viewer_id=None):
    r = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    return status(r, viewer_id)

def relationship(viewer_id, target_id):
    f = bool(qone("SELECT 1 FROM follows WHERE follower_id=? AND target_id=?", viewer_id, target_id))
    fb = bool(qone("SELECT 1 FROM follows WHERE follower_id=? AND target_id=?", target_id, viewer_id))
    bl = bool(qone("SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", viewer_id, target_id))
    bby = bool(qone("SELECT 1 FROM blocks WHERE account_id=? AND target_id=?", target_id, viewer_id))
    mu = bool(qone("SELECT 1 FROM mutes WHERE account_id=? AND target_id=?", viewer_id, target_id))
    req = bool(qone("SELECT 1 FROM follow_requests WHERE follower_id=? AND target_id=?", viewer_id, target_id))
    return {
        "id": target_id,
        "following": f,
        "showing_reblogs": True,
        "notifying": False,
        "languages": [],
        "followed_by": fb,
        "blocking": bl,
        "blocked_by": bby,
        "muting": mu,
        "muting_notifications": False,
        "requested": req,
        "requested_by": False,
        "domain_blocking": False,
        "endorsed": False,
        "note": "",
    }

def notification(row, viewer_id=None):
    fr = qone("SELECT * FROM accounts WHERE id=?", row["from_account_id"])
    out = {
        "id": row["id"],
        "type": row["type"],
        "created_at": row["created_at"],
        "account": account(fr, viewer_id),
    }
    if row["status_id"]:
        s = status_by_id(row["status_id"], viewer_id)
        if s: out["status"] = s
    return out
