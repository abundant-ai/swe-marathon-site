"""Posting (creating statuses, replies, reblogs, etc.)."""
import json
from typing import Optional

from . import auth, db
from .util import extract_mentions, extract_tags, now_iso


VISIBILITIES = {"public", "unlisted", "private", "direct"}


def _ensure_tags(names: list[str]) -> list[int]:
    ids = []
    for name in names:
        row = db.query_one("SELECT id FROM tags WHERE name = ?", (name,))
        if row:
            ids.append(row["id"])
            continue
        cur = db.execute("INSERT INTO tags (name, created_at) VALUES (?, ?)", (name, now_iso()))
        ids.append(cur.lastrowid)
    return ids


def _resolve_mention_acct(acct: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM accounts WHERE acct = ? OR (username = ? AND is_local = 1)", (acct, acct))
    return dict(row) if row else None


def create_status(account_id: int, text: str, visibility: str = "public",
                  in_reply_to_id: Optional[int] = None,
                  spoiler_text: str = "", sensitive: bool = False,
                  language: str = "en", media_ids: list[int] = None,
                  poll: dict = None, app_id: Optional[int] = None,
                  idempotency_key: Optional[str] = None) -> dict:
    """Create a new status, returns the row dict."""
    media_ids = media_ids or []
    if visibility not in VISIBILITIES:
        visibility = "public"

    if idempotency_key:
        prior = db.query_one(
            "SELECT * FROM statuses WHERE account_id = ? AND idempotency_key = ? AND deleted = 0",
            (account_id, idempotency_key),
        )
        if prior:
            return dict(prior)

    parent_account_id = None
    if in_reply_to_id:
        parent = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (in_reply_to_id,))
        if parent:
            parent_account_id = parent["account_id"]

    cur = db.execute(
        """INSERT INTO statuses (account_id, content, text, spoiler_text, visibility, sensitive,
                                  in_reply_to_id, in_reply_to_account_id, language, created_at,
                                  application_id, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, "", text or "", spoiler_text or "", visibility, 1 if sensitive else 0,
         in_reply_to_id, parent_account_id, language, now_iso(), app_id, idempotency_key),
    )
    sid = cur.lastrowid

    # mentions
    mention_accts = extract_mentions(text or "")
    seen = set()
    for ma in mention_accts:
        if ma in seen:
            continue
        seen.add(ma)
        acct_row = _resolve_mention_acct(ma)
        if not acct_row:
            continue
        try:
            db.execute("INSERT INTO status_mentions (status_id, account_id) VALUES (?, ?)",
                       (sid, acct_row["id"]))
        except Exception:
            pass

    # tags
    tag_names = extract_tags(text or "")
    if tag_names:
        tag_ids = _ensure_tags(tag_names)
        for tid in tag_ids:
            try:
                db.execute("INSERT INTO status_tags (status_id, tag_id) VALUES (?, ?)", (sid, tid))
            except Exception:
                pass

    # media
    for i, mid in enumerate(media_ids):
        db.execute(
            "UPDATE media_attachments SET status_id = ? WHERE id = ? AND account_id = ? AND status_id IS NULL",
            (sid, int(mid), account_id),
        )
        try:
            db.execute("INSERT INTO status_media (status_id, media_id, position) VALUES (?, ?, ?)",
                       (sid, int(mid), i))
        except Exception:
            pass

    # poll
    if poll and isinstance(poll, dict) and poll.get("options"):
        opts = []
        for opt in poll["options"]:
            opts.append({"title": str(opt), "votes_count": 0})
        cur2 = db.execute(
            "INSERT INTO polls (status_id, expires_at, multiple, options, created_at) VALUES (?, ?, ?, ?, ?)",
            (sid, poll.get("expires_at"), 1 if poll.get("multiple") else 0, json.dumps(opts), now_iso()),
        )
        pid = cur2.lastrowid
        db.execute("UPDATE statuses SET poll_id = ? WHERE id = ?", (pid, sid))

    # account stats
    db.execute("UPDATE accounts SET statuses_count = statuses_count + 1, last_status_at = ? WHERE id = ?",
               (now_iso(), account_id))

    # parent reply count + notification
    if in_reply_to_id and parent_account_id and parent_account_id != account_id:
        db.execute("UPDATE statuses SET replies_count = replies_count + 1 WHERE id = ?", (in_reply_to_id,))
        db.execute(
            "INSERT INTO notifications (account_id, from_account_id, type, status_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (parent_account_id, account_id, "mention", sid, now_iso()),
        )
    elif in_reply_to_id:
        db.execute("UPDATE statuses SET replies_count = replies_count + 1 WHERE id = ?", (in_reply_to_id,))

    # mention notifications (excluding the in-reply target which already got one)
    for ma in seen:
        acct_row = _resolve_mention_acct(ma)
        if not acct_row or acct_row["id"] == account_id:
            continue
        if acct_row["id"] == parent_account_id:
            continue
        if not acct_row.get("is_local"):
            continue
        db.execute(
            "INSERT INTO notifications (account_id, from_account_id, type, status_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (acct_row["id"], account_id, "mention", sid, now_iso()),
        )

    row = db.query_one("SELECT * FROM statuses WHERE id = ?", (sid,))
    return dict(row)


def delete_status(account_id: int, status_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM statuses WHERE id = ? AND account_id = ? AND deleted = 0",
                       (status_id, account_id))
    if not row:
        return None
    db.execute("UPDATE statuses SET deleted = 1 WHERE id = ?", (status_id,))
    db.execute("UPDATE accounts SET statuses_count = MAX(0, statuses_count - 1) WHERE id = ?", (account_id,))
    if row["in_reply_to_id"]:
        db.execute("UPDATE statuses SET replies_count = MAX(0, replies_count - 1) WHERE id = ?",
                   (row["in_reply_to_id"],))
    return dict(row)


def edit_status(account_id: int, status_id: int, text: str, spoiler_text: str = "",
                sensitive: bool = False, language: str = "en") -> Optional[dict]:
    row = db.query_one("SELECT * FROM statuses WHERE id = ? AND account_id = ? AND deleted = 0",
                       (status_id, account_id))
    if not row:
        return None
    # snapshot history
    db.execute(
        "INSERT INTO status_history (status_id, content, spoiler_text, sensitive, created_at) VALUES (?, ?, ?, ?, ?)",
        (status_id, row["text"] or "", row["spoiler_text"] or "", row["sensitive"], row["edited_at"] or row["created_at"]),
    )
    db.execute(
        "UPDATE statuses SET text = ?, spoiler_text = ?, sensitive = ?, language = ?, edited_at = ? WHERE id = ?",
        (text, spoiler_text, 1 if sensitive else 0, language, now_iso(), status_id),
    )
    # rebuild tags + mentions
    db.execute("DELETE FROM status_tags WHERE status_id = ?", (status_id,))
    db.execute("DELETE FROM status_mentions WHERE status_id = ?", (status_id,))
    tag_names = extract_tags(text or "")
    if tag_names:
        for tid in _ensure_tags(tag_names):
            try:
                db.execute("INSERT INTO status_tags (status_id, tag_id) VALUES (?, ?)", (status_id, tid))
            except Exception:
                pass
    for ma in set(extract_mentions(text or "")):
        a = _resolve_mention_acct(ma)
        if a:
            try:
                db.execute("INSERT INTO status_mentions (status_id, account_id) VALUES (?, ?)", (status_id, a["id"]))
            except Exception:
                pass
    return dict(db.query_one("SELECT * FROM statuses WHERE id = ?", (status_id,)))


def reblog(account_id: int, target_id: int, visibility: str = "public") -> Optional[dict]:
    target = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (target_id,))
    if not target:
        return None
    existing = db.query_one(
        "SELECT * FROM statuses WHERE account_id = ? AND reblog_of_id = ? AND deleted = 0",
        (account_id, target_id),
    )
    if existing:
        return dict(existing)
    cur = db.execute(
        """INSERT INTO statuses (account_id, content, text, visibility, reblog_of_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (account_id, "", "", visibility, target_id, now_iso()),
    )
    db.execute("UPDATE statuses SET reblogs_count = reblogs_count + 1 WHERE id = ?", (target_id,))
    if target["account_id"] != account_id:
        a = db.query_one("SELECT is_local FROM accounts WHERE id = ?", (target["account_id"],))
        if a and a["is_local"]:
            db.execute(
                "INSERT INTO notifications (account_id, from_account_id, type, status_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (target["account_id"], account_id, "reblog", target_id, now_iso()),
            )
    row = db.query_one("SELECT * FROM statuses WHERE id = ?", (cur.lastrowid,))
    return dict(row)


def unreblog(account_id: int, target_id: int) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM statuses WHERE account_id = ? AND reblog_of_id = ? AND deleted = 0",
        (account_id, target_id),
    )
    if not row:
        target = db.query_one("SELECT * FROM statuses WHERE id = ?", (target_id,))
        return dict(target) if target else None
    db.execute("UPDATE statuses SET deleted = 1 WHERE id = ?", (row["id"],))
    db.execute("UPDATE statuses SET reblogs_count = MAX(0, reblogs_count - 1) WHERE id = ?", (target_id,))
    target = db.query_one("SELECT * FROM statuses WHERE id = ?", (target_id,))
    return dict(target) if target else None


def favourite(account_id: int, status_id: int) -> Optional[dict]:
    target = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (status_id,))
    if not target:
        return None
    try:
        db.execute("INSERT INTO favourites (account_id, status_id, created_at) VALUES (?, ?, ?)",
                   (account_id, status_id, now_iso()))
        db.execute("UPDATE statuses SET favourites_count = favourites_count + 1 WHERE id = ?", (status_id,))
        if target["account_id"] != account_id:
            a = db.query_one("SELECT is_local FROM accounts WHERE id = ?", (target["account_id"],))
            if a and a["is_local"]:
                db.execute(
                    "INSERT INTO notifications (account_id, from_account_id, type, status_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (target["account_id"], account_id, "favourite", status_id, now_iso()),
                )
    except Exception:
        pass
    return dict(db.query_one("SELECT * FROM statuses WHERE id = ?", (status_id,)))


def unfavourite(account_id: int, status_id: int) -> Optional[dict]:
    cur = db.execute("DELETE FROM favourites WHERE account_id = ? AND status_id = ?", (account_id, status_id))
    if cur.rowcount > 0:
        db.execute("UPDATE statuses SET favourites_count = MAX(0, favourites_count - 1) WHERE id = ?", (status_id,))
    target = db.query_one("SELECT * FROM statuses WHERE id = ?", (status_id,))
    return dict(target) if target else None


def bookmark(account_id: int, status_id: int) -> Optional[dict]:
    target = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (status_id,))
    if not target:
        return None
    try:
        db.execute("INSERT INTO bookmarks (account_id, status_id, created_at) VALUES (?, ?, ?)",
                   (account_id, status_id, now_iso()))
    except Exception:
        pass
    return dict(target)


def unbookmark(account_id: int, status_id: int) -> Optional[dict]:
    db.execute("DELETE FROM bookmarks WHERE account_id = ? AND status_id = ?", (account_id, status_id))
    target = db.query_one("SELECT * FROM statuses WHERE id = ?", (status_id,))
    return dict(target) if target else None


def follow(account_id: int, target_id: int) -> bool:
    if account_id == target_id:
        return False
    if not db.query_one("SELECT 1 FROM accounts WHERE id = ?", (target_id,)):
        return False
    try:
        db.execute("INSERT INTO follows (account_id, target_id, created_at) VALUES (?, ?, ?)",
                   (account_id, target_id, now_iso()))
        db.execute("UPDATE accounts SET followers_count = followers_count + 1 WHERE id = ?", (target_id,))
        db.execute("UPDATE accounts SET following_count = following_count + 1 WHERE id = ?", (account_id,))
        a = db.query_one("SELECT is_local FROM accounts WHERE id = ?", (target_id,))
        if a and a["is_local"]:
            db.execute(
                "INSERT INTO notifications (account_id, from_account_id, type, created_at) VALUES (?, ?, ?, ?)",
                (target_id, account_id, "follow", now_iso()),
            )
    except Exception:
        pass
    return True


def unfollow(account_id: int, target_id: int) -> bool:
    cur = db.execute("DELETE FROM follows WHERE account_id = ? AND target_id = ?", (account_id, target_id))
    if cur.rowcount > 0:
        db.execute("UPDATE accounts SET followers_count = MAX(0, followers_count - 1) WHERE id = ?", (target_id,))
        db.execute("UPDATE accounts SET following_count = MAX(0, following_count - 1) WHERE id = ?", (account_id,))
    return cur.rowcount > 0
