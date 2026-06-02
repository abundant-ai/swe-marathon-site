"""Timeline queries and visibility helpers."""
from typing import Optional

from . import db


def visible_to(status: dict, viewer_id: Optional[int]) -> bool:
    if status.get("deleted"):
        return False
    vis = status.get("visibility")
    if vis == "public" or vis == "unlisted":
        return True
    if viewer_id is None:
        return False
    if status["account_id"] == viewer_id:
        return True
    if vis == "private":
        return bool(db.query_one(
            "SELECT 1 FROM follows WHERE account_id = ? AND target_id = ?",
            (viewer_id, status["account_id"]),
        ))
    if vis == "direct":
        return bool(db.query_one(
            "SELECT 1 FROM status_mentions WHERE status_id = ? AND account_id = ?",
            (status["id"], viewer_id),
        ))
    return False


def filter_visible(rows: list[dict], viewer_id: Optional[int]) -> list[dict]:
    return [r for r in rows if visible_to(r, viewer_id)]


def parse_paging(qs: dict) -> dict:
    return {
        "max_id": qs.get("max_id"),
        "since_id": qs.get("since_id"),
        "min_id": qs.get("min_id"),
        "limit": min(int(qs.get("limit", 20) or 20), 40),
    }


def paged_clause(paging: dict, id_col: str = "id") -> tuple[str, list]:
    where = []
    params = []
    if paging.get("max_id"):
        where.append(f"{id_col} < ?")
        params.append(int(paging["max_id"]))
    if paging.get("since_id"):
        where.append(f"{id_col} > ?")
        params.append(int(paging["since_id"]))
    if paging.get("min_id"):
        where.append(f"{id_col} > ?")
        params.append(int(paging["min_id"]))
    return (" AND ".join(where), params)


def home_timeline(viewer_id: int, paging: dict) -> list[dict]:
    """Statuses from the viewer + accounts they follow."""
    extra, params = paged_clause(paging, id_col="s.id")
    where_extra = (" AND " + extra) if extra else ""
    sql = f"""
        SELECT s.* FROM statuses s
        WHERE s.deleted = 0
          AND (s.account_id = ?
               OR s.account_id IN (SELECT target_id FROM follows WHERE account_id = ?))
          AND s.visibility IN ('public', 'unlisted', 'private')
          {where_extra}
        ORDER BY s.id DESC
        LIMIT ?
    """
    rows = db.query_all(sql, [viewer_id, viewer_id, *params, paging["limit"]])
    return [dict(r) for r in rows]


def public_timeline(viewer_id: Optional[int], paging: dict, local: bool = False, remote: bool = False) -> list[dict]:
    extra, params = paged_clause(paging, id_col="s.id")
    where_extra = (" AND " + extra) if extra else ""
    locality = ""
    if local:
        locality = " AND a.is_local = 1"
    if remote:
        locality = " AND a.is_local = 0"
    sql = f"""
        SELECT s.* FROM statuses s
        JOIN accounts a ON a.id = s.account_id
        WHERE s.deleted = 0 AND s.visibility = 'public'
          AND s.in_reply_to_id IS NULL
          {locality}
          {where_extra}
        ORDER BY s.id DESC
        LIMIT ?
    """
    rows = db.query_all(sql, [*params, paging["limit"]])
    return [dict(r) for r in rows]


def hashtag_timeline(name: str, paging: dict) -> list[dict]:
    extra, params = paged_clause(paging, id_col="s.id")
    where_extra = (" AND " + extra) if extra else ""
    sql = f"""
        SELECT s.* FROM statuses s
        JOIN status_tags st ON st.status_id = s.id
        JOIN tags t ON t.id = st.tag_id
        WHERE s.deleted = 0 AND s.visibility IN ('public','unlisted')
          AND LOWER(t.name) = ?
          {where_extra}
        ORDER BY s.id DESC
        LIMIT ?
    """
    rows = db.query_all(sql, [name.lower(), *params, paging["limit"]])
    return [dict(r) for r in rows]


def list_timeline(list_id: int, viewer_id: int, paging: dict) -> list[dict]:
    extra, params = paged_clause(paging, id_col="s.id")
    where_extra = (" AND " + extra) if extra else ""
    sql = f"""
        SELECT s.* FROM statuses s
        WHERE s.deleted = 0
          AND s.account_id IN (SELECT account_id FROM list_accounts WHERE list_id = ?)
          AND s.visibility IN ('public','unlisted','private')
          {where_extra}
        ORDER BY s.id DESC
        LIMIT ?
    """
    rows = db.query_all(sql, [list_id, *params, paging["limit"]])
    return [dict(r) for r in rows]


def account_statuses(target_id: int, viewer_id: Optional[int], paging: dict,
                     only_media: bool = False, exclude_replies: bool = False,
                     exclude_reblogs: bool = False, pinned: bool = False) -> list[dict]:
    extra, params = paged_clause(paging, id_col="s.id")
    where_extra = (" AND " + extra) if extra else ""
    extras = []
    if only_media:
        extras.append("AND EXISTS (SELECT 1 FROM status_media sm WHERE sm.status_id = s.id)")
    if exclude_replies:
        extras.append("AND s.in_reply_to_id IS NULL")
    if exclude_reblogs:
        extras.append("AND s.reblog_of_id IS NULL")
    if pinned:
        sql = f"""
            SELECT s.* FROM statuses s
            JOIN pins p ON p.status_id = s.id
            WHERE s.deleted = 0 AND p.account_id = ?
              {where_extra}
            ORDER BY s.id DESC LIMIT ?
        """
        rows = db.query_all(sql, [target_id, *params, paging["limit"]])
    else:
        sql = f"""
            SELECT s.* FROM statuses s
            WHERE s.deleted = 0 AND s.account_id = ?
              {' '.join(extras)}
              {where_extra}
            ORDER BY s.id DESC LIMIT ?
        """
        rows = db.query_all(sql, [target_id, *params, paging["limit"]])
    out = [dict(r) for r in rows]
    return [r for r in out if visible_to(r, viewer_id)]
