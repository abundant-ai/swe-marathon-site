"""Serialize DB rows to Mastodon API JSON shapes."""
import json
from typing import Optional

from . import db
from .util import BASE_URL, INSTANCE_DOMAIN, render_status_html


def account_dict(account: dict, viewer_id: Optional[int] = None) -> dict:
    if not account:
        return {}
    acct_str = account["acct"]
    if account.get("is_local") and "@" not in acct_str:
        acct = account["username"]
    else:
        acct = acct_str
    domain = account.get("domain") or INSTANCE_DOMAIN
    if account.get("is_local"):
        url = f"{BASE_URL}/@{account['username']}"
    else:
        url = f"https://{domain}/@{account['username']}"
    avatar = account.get("avatar") or f"{BASE_URL}/static/avatar.svg"
    header = account.get("header") or f"{BASE_URL}/static/header.svg"
    try:
        fields = json.loads(account.get("fields") or "[]")
    except Exception:
        fields = []
    return {
        "id": str(account["id"]),
        "username": account["username"],
        "acct": acct,
        "display_name": account.get("display_name") or account["username"],
        "locked": bool(account.get("locked")),
        "bot": bool(account.get("bot")),
        "discoverable": True,
        "group": False,
        "created_at": account["created_at"],
        "note": _note_html(account.get("note") or ""),
        "url": url,
        "uri": url,
        "avatar": avatar,
        "avatar_static": avatar,
        "header": header,
        "header_static": header,
        "followers_count": int(account.get("followers_count") or 0),
        "following_count": int(account.get("following_count") or 0),
        "statuses_count": int(account.get("statuses_count") or 0),
        "last_status_at": (account.get("last_status_at") or "")[:10] or None,
        "emojis": [],
        "fields": fields,
        "noindex": False,
        "roles": [{"id": "1", "name": "Admin", "color": "#563acc"}] if account.get("is_admin") else [],
        "source": {
            "privacy": "public",
            "sensitive": False,
            "language": "en",
            "note": account.get("note") or "",
            "fields": fields,
            "follow_requests_count": 0,
        } if (viewer_id and account["id"] == viewer_id) else None,
    }


def _note_html(note: str) -> str:
    if not note:
        return ""
    paras = [p.strip() for p in note.split("\n\n") if p.strip()]
    if not paras:
        # render single line breaks
        return "<p>" + note.replace("\n", "<br />") + "</p>"
    return "".join(f"<p>{p.replace(chr(10), '<br />')}</p>" for p in paras)


def relationship_dict(viewer_id: int, target_id: int) -> dict:
    follows = db.query_one(
        "SELECT 1 FROM follows WHERE account_id = ? AND target_id = ?", (viewer_id, target_id)
    )
    followed_by = db.query_one(
        "SELECT 1 FROM follows WHERE account_id = ? AND target_id = ?", (target_id, viewer_id)
    )
    blocking = db.query_one(
        "SELECT 1 FROM blocks WHERE account_id = ? AND target_id = ?", (viewer_id, target_id)
    )
    blocked_by = db.query_one(
        "SELECT 1 FROM blocks WHERE account_id = ? AND target_id = ?", (target_id, viewer_id)
    )
    muting = db.query_one(
        "SELECT 1 FROM mutes WHERE account_id = ? AND target_id = ?", (viewer_id, target_id)
    )
    return {
        "id": str(target_id),
        "following": bool(follows),
        "showing_reblogs": True,
        "notifying": False,
        "languages": [],
        "followed_by": bool(followed_by),
        "blocking": bool(blocking),
        "blocked_by": bool(blocked_by),
        "muting": bool(muting),
        "muting_notifications": False,
        "requested": False,
        "requested_by": False,
        "domain_blocking": False,
        "endorsed": False,
        "note": "",
    }


def media_attachment_dict(media: dict) -> dict:
    try:
        meta = json.loads(media.get("meta") or "{}")
    except Exception:
        meta = {}
    return {
        "id": str(media["id"]),
        "type": media["type"],
        "url": media["url"],
        "preview_url": media["preview_url"],
        "remote_url": None,
        "preview_remote_url": None,
        "text_url": None,
        "meta": meta,
        "description": media.get("description") or "",
        "blurhash": media.get("blurhash") or "",
    }


def tag_dict(name: str) -> dict:
    return {
        "name": name,
        "url": f"{BASE_URL}/tags/{name}",
        "history": [],
        "following": False,
    }


def status_dict(status: dict, viewer_id: Optional[int] = None, depth: int = 0) -> Optional[dict]:
    if not status or status.get("deleted"):
        return None
    account = db.query_one("SELECT * FROM accounts WHERE id = ?", (status["account_id"],))
    if not account:
        return None
    media_rows = db.query_all(
        "SELECT m.* FROM media_attachments m JOIN status_media sm ON sm.media_id = m.id WHERE sm.status_id = ? ORDER BY sm.position",
        (status["id"],),
    )
    media = [media_attachment_dict(dict(r)) for r in media_rows]
    tag_rows = db.query_all(
        "SELECT t.name FROM tags t JOIN status_tags st ON st.tag_id = t.id WHERE st.status_id = ?",
        (status["id"],),
    )
    tags = [tag_dict(r["name"]) for r in tag_rows]
    mention_rows = db.query_all(
        "SELECT a.id, a.username, a.acct FROM accounts a JOIN status_mentions sm ON sm.account_id = a.id WHERE sm.status_id = ?",
        (status["id"],),
    )
    mentions = [
        {
            "id": str(m["id"]),
            "username": m["username"],
            "acct": m["acct"],
            "url": f"{BASE_URL}/@{m['username']}",
        }
        for m in mention_rows
    ]
    favourited = False
    bookmarked = False
    reblogged = False
    pinned = False
    muted = False
    if viewer_id:
        favourited = bool(db.query_one(
            "SELECT 1 FROM favourites WHERE account_id = ? AND status_id = ?", (viewer_id, status["id"])
        ))
        bookmarked = bool(db.query_one(
            "SELECT 1 FROM bookmarks WHERE account_id = ? AND status_id = ?", (viewer_id, status["id"])
        ))
        reblogged = bool(db.query_one(
            "SELECT 1 FROM statuses WHERE account_id = ? AND reblog_of_id = ? AND deleted = 0", (viewer_id, status["id"])
        ))
        pinned = bool(db.query_one(
            "SELECT 1 FROM pins WHERE account_id = ? AND status_id = ?", (viewer_id, status["id"])
        ))
    reblog_obj = None
    if status.get("reblog_of_id") and depth == 0:
        sub = db.query_one("SELECT * FROM statuses WHERE id = ?", (status["reblog_of_id"],))
        if sub:
            reblog_obj = status_dict(dict(sub), viewer_id=viewer_id, depth=depth + 1)
    poll_obj = None
    if status.get("poll_id"):
        poll_row = db.query_one("SELECT * FROM polls WHERE id = ?", (status["poll_id"],))
        if poll_row:
            poll_obj = poll_dict(dict(poll_row), viewer_id)
    url = f"{BASE_URL}/@{account['username']}/{status['id']}"
    return {
        "id": str(status["id"]),
        "created_at": status["created_at"],
        "in_reply_to_id": str(status["in_reply_to_id"]) if status["in_reply_to_id"] else None,
        "in_reply_to_account_id": str(status["in_reply_to_account_id"]) if status["in_reply_to_account_id"] else None,
        "sensitive": bool(status["sensitive"]),
        "spoiler_text": status["spoiler_text"] or "",
        "visibility": status["visibility"],
        "language": status["language"] or "en",
        "uri": url,
        "url": url,
        "replies_count": int(status.get("replies_count") or 0),
        "reblogs_count": int(status.get("reblogs_count") or 0),
        "favourites_count": int(status.get("favourites_count") or 0),
        "edited_at": status.get("edited_at"),
        "favourited": favourited,
        "reblogged": reblogged,
        "muted": muted,
        "bookmarked": bookmarked,
        "pinned": pinned,
        "content": render_status_html(status["text"] or "", mentions=mentions, tags=tags) if not status.get("reblog_of_id") else "",
        "filtered": [],
        "reblog": reblog_obj,
        "application": None,
        "media_attachments": media,
        "mentions": mentions,
        "tags": [{"name": t["name"], "url": t["url"]} for t in tags],
        "emojis": [],
        "card": None,
        "poll": poll_obj,
        "account": account_dict(dict(account)),
    }


def poll_dict(poll: dict, viewer_id: Optional[int]) -> dict:
    try:
        opts = json.loads(poll["options"])
    except Exception:
        opts = []
    try:
        voters = json.loads(poll.get("voters") or "{}")
    except Exception:
        voters = {}
    voted = False
    own_votes = []
    if viewer_id and str(viewer_id) in voters:
        voted = True
        own_votes = voters[str(viewer_id)]
    return {
        "id": str(poll["id"]),
        "expires_at": poll.get("expires_at"),
        "expired": False,
        "multiple": bool(poll.get("multiple")),
        "votes_count": sum(o.get("votes_count", 0) for o in opts),
        "voters_count": int(poll.get("voters_count") or 0),
        "voted": voted,
        "own_votes": own_votes,
        "options": [{"title": o["title"], "votes_count": o.get("votes_count", 0)} for o in opts],
        "emojis": [],
    }


def notification_dict(notif: dict, viewer_id: int) -> Optional[dict]:
    fa = db.query_one("SELECT * FROM accounts WHERE id = ?", (notif["from_account_id"],))
    if not fa:
        return None
    status = None
    if notif.get("status_id"):
        srow = db.query_one("SELECT * FROM statuses WHERE id = ?", (notif["status_id"],))
        if srow:
            status = status_dict(dict(srow), viewer_id=viewer_id)
    return {
        "id": str(notif["id"]),
        "type": notif["type"],
        "created_at": notif["created_at"],
        "account": account_dict(dict(fa)),
        "status": status,
    }


def conversation_dict(conv: dict, viewer_id: int) -> dict:
    last = None
    if conv.get("last_status_id"):
        srow = db.query_one("SELECT * FROM statuses WHERE id = ?", (conv["last_status_id"],))
        if srow:
            last = status_dict(dict(srow), viewer_id=viewer_id)
    return {
        "id": str(conv["id"]),
        "unread": bool(conv.get("unread")),
        "accounts": [],
        "last_status": last,
    }


def list_dict(lst: dict) -> dict:
    return {
        "id": str(lst["id"]),
        "title": lst["title"],
        "replies_policy": lst.get("replies_policy") or "list",
        "exclusive": bool(lst.get("exclusive")),
    }


def app_dict(app: dict, full: bool = False) -> dict:
    out = {
        "id": str(app["id"]),
        "name": app["name"],
        "website": app.get("website"),
        "redirect_uri": (app.get("redirect_uris") or "").split()[0] if app.get("redirect_uris") else "",
        "vapid_key": app.get("vapid_key") or "",
    }
    if full:
        out["client_id"] = app["client_id"]
        out["client_secret"] = app["client_secret"]
    return out


def report_dict(rep: dict) -> dict:
    try:
        sids = json.loads(rep.get("status_ids") or "[]")
    except Exception:
        sids = []
    return {
        "id": str(rep["id"]),
        "action_taken": bool(rep.get("action_taken")),
        "action_taken_at": None,
        "category": rep.get("category") or "other",
        "comment": rep.get("comment") or "",
        "forwarded": bool(rep.get("forwarded")),
        "created_at": rep["created_at"],
        "status_ids": [str(s) for s in sids],
        "rules_ids": [],
        "target_account": account_dict(dict(db.query_one("SELECT * FROM accounts WHERE id = ?", (rep["target_account_id"],)))),
    }
