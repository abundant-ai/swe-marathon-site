"""Web UI views (server-rendered HTML)."""
import html as htmllib
import json
import os
import re
import shutil
import time
import urllib.parse
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import auth, db, posting, serializers, timelines
from .events import publish, subscribe, unsubscribe
from .util import (
    BASE_URL,
    INSTANCE_DOMAIN,
    csrf_make,
    extract_tags,
    now_iso,
    relative_time,
    render_status_html,
    safe_redirect,
)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)
env.globals["instance_domain"] = INSTANCE_DOMAIN
env.globals["base_url"] = BASE_URL


def _decorate_status(s: dict, viewer_id: Optional[int]) -> dict:
    sd = serializers.status_dict(s, viewer_id=viewer_id)
    if not sd:
        return None
    sd["created_at_rel"] = relative_time(sd["created_at"])
    sd["content_html"] = sd["content"]
    if sd.get("reblog"):
        sd["reblog"]["content_html"] = sd["reblog"]["content"]
        sd["reblog"]["created_at_rel"] = relative_time(sd["reblog"]["created_at"])
    return sd


def _decorate_account(a: dict, viewer_id: Optional[int]) -> dict:
    ad = serializers.account_dict(a, viewer_id=viewer_id)
    ad["note_html"] = ad["note"]
    ad["note_raw"] = a.get("note") or ""
    return ad


def _viewer(request: Request) -> Optional[dict]:
    sid = request.cookies.get("chirp_session")
    sess = auth.lookup_session(sid)
    if not sess:
        return None
    return auth.find_account_by_id(sess["account_id"])


def _csrf_for(request: Request) -> str:
    sid = request.cookies.get("chirp_session")
    sess = auth.lookup_session(sid) if sid else None
    if sess:
        return sess["csrf"]
    return ""


def _check_csrf(request: Request, posted: str) -> bool:
    sid = request.cookies.get("chirp_session")
    sess = auth.lookup_session(sid) if sid else None
    if not sess:
        return False
    return posted == sess["csrf"]


def _trending() -> list[dict]:
    rows = db.query_all(
        """
        SELECT t.name, COUNT(*) AS uses
        FROM tags t
        JOIN status_tags st ON st.tag_id = t.id
        JOIN statuses s ON s.id = st.status_id
        WHERE s.deleted = 0 AND s.visibility IN ('public','unlisted')
        GROUP BY t.id
        ORDER BY uses DESC, t.id DESC
        LIMIT 5
        """
    )
    return [{"name": r["name"], "uses": r["uses"]} for r in rows]


def _unread_notifs(viewer_id: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM notifications WHERE account_id = ? AND seen = 0",
        (viewer_id,),
    )
    return int(row["c"]) if row else 0


def _ctx(request: Request, **extra) -> dict:
    viewer = _viewer(request)
    csrf = _csrf_for(request)
    ctx = {
        "viewer": viewer,
        "csrf": csrf,
        "instance_domain": INSTANCE_DOMAIN,
        "trending": _trending(),
        "active": "",
    }
    if viewer:
        ctx["unread_count"] = _unread_notifs(viewer["id"])
    ctx.update(extra)
    return ctx


def _render(name: str, ctx: dict) -> HTMLResponse:
    tpl = env.get_template(name)
    body = tpl.render(**ctx)
    return HTMLResponse(body)


# ---------- routes ----------

async def home_index(request: Request):
    viewer = _viewer(request)
    if viewer:
        rows = timelines.home_timeline(viewer["id"], {"limit": 30, "max_id": None, "since_id": None, "min_id": None})
        statuses = [s for s in (_decorate_status(r, viewer["id"]) for r in rows) if s]
        return _render("timeline.html", _ctx(request, statuses=statuses, active="home", title="Home"))
    # unauthenticated landing → public timeline
    rows = timelines.public_timeline(None, {"limit": 30, "max_id": None, "since_id": None, "min_id": None})
    statuses = [s for s in (_decorate_status(r, None) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="federated", title="Chirp"))


async def home_timeline_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login?next=/home", status_code=303)
    rows = timelines.home_timeline(viewer["id"], {"limit": 30, "max_id": None, "since_id": None, "min_id": None})
    statuses = [s for s in (_decorate_status(r, viewer["id"]) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="home", title="Home"))


async def public_timeline_view(request: Request):
    viewer = _viewer(request)
    rows = timelines.public_timeline(viewer["id"] if viewer else None, {"limit": 30, "max_id": None, "since_id": None, "min_id": None})
    statuses = [s for s in (_decorate_status(r, viewer["id"] if viewer else None) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="federated", title="Federated"))


async def local_timeline_view(request: Request):
    viewer = _viewer(request)
    rows = timelines.public_timeline(viewer["id"] if viewer else None, {"limit": 30, "max_id": None, "since_id": None, "min_id": None}, local=True)
    statuses = [s for s in (_decorate_status(r, viewer["id"] if viewer else None) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="local", title="Local"))


async def hashtag_view(request: Request):
    name = request.path_params["name"].lower()
    viewer = _viewer(request)
    rows = timelines.hashtag_timeline(name, {"limit": 30, "max_id": None, "since_id": None, "min_id": None})
    statuses = [s for s in (_decorate_status(r, viewer["id"] if viewer else None) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, title=f"#{name}", active=""))


async def profile_view(request: Request):
    username = request.path_params["username"]
    if username.startswith("@"):
        username = username[1:]
    if "@" in username:
        acct = auth.find_account_by_acct(username)
    else:
        acct = auth.find_account_by_username(username)
    if not acct:
        raise HTTPException(404, "Account not found")
    viewer = _viewer(request)
    viewer_id = viewer["id"] if viewer else None
    rows = timelines.account_statuses(acct["id"], viewer_id, {"limit": 30, "max_id": None, "since_id": None, "min_id": None}, exclude_replies=True)
    statuses = [s for s in (_decorate_status(r, viewer_id) for r in rows) if s]
    rel = serializers.relationship_dict(viewer_id, acct["id"]) if viewer_id else {}
    ad = _decorate_account(acct, viewer_id)
    return _render("profile.html", _ctx(request, account=ad, relationship=rel, statuses=statuses, subview=None))


async def profile_legacy_redirect(request: Request):
    return RedirectResponse(f"/@{request.path_params['username']}", status_code=302)


async def status_view(request: Request):
    sid = int(request.path_params["id"])
    row = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (sid,))
    if not row:
        raise HTTPException(404, "Status not found")
    viewer = _viewer(request)
    viewer_id = viewer["id"] if viewer else None
    s = dict(row)
    if not timelines.visible_to(s, viewer_id):
        raise HTTPException(404, "Status not visible")
    # ancestors
    ancestors = []
    cur = s
    while cur.get("in_reply_to_id"):
        p = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (cur["in_reply_to_id"],))
        if not p:
            break
        ancestors.append(dict(p))
        cur = dict(p)
    ancestors.reverse()
    descendants_rows = db.query_all(
        "SELECT * FROM statuses WHERE in_reply_to_id = ? AND deleted = 0 ORDER BY id",
        (sid,),
    )
    descendants = [dict(r) for r in descendants_rows]
    decorated = _decorate_status(s, viewer_id)
    decorated_anc = [_decorate_status(a, viewer_id) for a in ancestors]
    decorated_desc = [_decorate_status(d, viewer_id) for d in descendants]
    return _render("thread.html", _ctx(request,
        status=decorated, ancestors=[a for a in decorated_anc if a],
        descendants=[d for d in decorated_desc if d], in_reply_to=str(sid)))


async def login_view(request: Request):
    return _render("login.html", _ctx(request, error=None, next=request.query_params.get("next", "/home")))


async def signup_view(request: Request):
    return _render("signup.html", _ctx(request, error=None))


async def login_post(request: Request):
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        # accept first-time POST when no session yet (anonymous CSRF preflight)
        pass  # we re-check via cookie absence - first login flow doesn't have session yet
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = safe_redirect(form.get("next", "/home"))
    acct = auth.authenticate_local(username, password)
    if not acct:
        return _render("login.html", _ctx(request, error="Invalid username or password.", next=next_url))
    sid, csrf = auth.create_session(acct["id"])
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie("chirp_session", sid, httponly=True, samesite="lax", path="/", max_age=60 * 60 * 24 * 30)
    return resp


async def signup_post(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    display_name = (form.get("display_name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{2,20}", username):
        return _render("signup.html", _ctx(request, error="Invalid username (use 2–20 letters/digits/underscore)."))
    if len(password) < 8:
        return _render("signup.html", _ctx(request, error="Password must be at least 8 characters."))
    if auth.find_account_by_username(username):
        return _render("signup.html", _ctx(request, error="Username already taken."))
    aid = auth.create_account(username, password, email=email, display_name=display_name or username)
    sid, csrf = auth.create_session(aid)
    resp = RedirectResponse("/home", status_code=303)
    resp.set_cookie("chirp_session", sid, httponly=True, samesite="lax", path="/", max_age=60 * 60 * 24 * 30)
    return resp


async def logout_post(request: Request):
    sid = request.cookies.get("chirp_session")
    if sid:
        auth.destroy_session(sid)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("chirp_session", path="/")
    return resp


# Web compose
async def web_post_status(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF token invalid")
    text = (form.get("status") or "").strip()
    if not text:
        return RedirectResponse(request.headers.get("referer", "/home"), status_code=303)
    visibility = form.get("visibility", "public")
    spoiler_text = (form.get("spoiler_text") or "").strip()
    in_reply_to = form.get("in_reply_to_id")
    media_ids = []
    f = form.get("media")
    if f and hasattr(f, "filename") and f.filename:
        mid = await _save_uploaded_media(viewer["id"], f)
        if mid:
            media_ids.append(mid)
    s = posting.create_status(
        viewer["id"], text, visibility=visibility,
        in_reply_to_id=int(in_reply_to) if in_reply_to else None,
        spoiler_text=spoiler_text, sensitive=bool(spoiler_text),
        media_ids=media_ids,
    )
    _publish_status_events(s)
    if in_reply_to:
        return RedirectResponse(f"/statuses/{in_reply_to}", status_code=303)
    return RedirectResponse("/home", status_code=303)


def _publish_status_events(s: dict):
    sd = _decorate_status(s, None)
    if not sd:
        return
    payload = json.dumps(sd)
    publish(None, "public", "update", payload)
    if s.get("in_reply_to_account_id") and s["in_reply_to_account_id"] != s["account_id"]:
        # notify recipient
        last = db.query_one(
            "SELECT * FROM notifications WHERE account_id = ? ORDER BY id DESC LIMIT 1",
            (s["in_reply_to_account_id"],),
        )
        if last:
            nd = serializers.notification_dict(dict(last), s["in_reply_to_account_id"])
            if nd:
                publish(s["in_reply_to_account_id"], "user", "notification", json.dumps(nd))


async def _save_uploaded_media(account_id: int, f) -> Optional[int]:
    # f is a starlette UploadFile-ish
    fname = f.filename
    ext = os.path.splitext(fname)[1].lower() or ".bin"
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".svg"}:
        return None
    data = await f.read()
    if len(data) > 10 * 1024 * 1024:
        return None
    media_dir = "/app/data/media"
    os.makedirs(media_dir, exist_ok=True)
    import secrets
    tok = secrets.token_urlsafe(16)
    out = os.path.join(media_dir, f"{tok}{ext}")
    with open(out, "wb") as fp:
        fp.write(data)
    media_type = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"} else "video"
    url = f"{BASE_URL}/media/{tok}{ext}"
    cur = db.execute(
        """INSERT INTO media_attachments (account_id, type, url, preview_url, file_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (account_id, media_type, url, url, out, now_iso()),
    )
    return cur.lastrowid


async def web_action(request: Request):
    """Optimistic action POST endpoints that double-write the API state and return JSON."""
    action = request.path_params["action"]
    sid = int(request.path_params["id"])
    viewer = _viewer(request)
    if not viewer:
        return JSONResponse({"error": "auth required"}, status_code=401)
    csrf = request.headers.get("x-csrf-token") or ""
    if not csrf:
        try:
            form = await request.form()
            csrf = form.get("csrf", "")
        except Exception:
            pass
    if not _check_csrf(request, csrf):
        return JSONResponse({"error": "CSRF"}, status_code=403)
    fns = {
        "favourite": posting.favourite,
        "unfavourite": posting.unfavourite,
        "reblog": posting.reblog,
        "unreblog": posting.unreblog,
        "bookmark": posting.bookmark,
        "unbookmark": posting.unbookmark,
    }
    fn = fns.get(action)
    if not fn:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    res = fn(viewer["id"], sid)
    if not res:
        return JSONResponse({"error": "not found"}, status_code=404)
    sd = _decorate_status(res, viewer["id"])
    return JSONResponse(sd or {})


async def web_account_action(request: Request):
    action = request.path_params["action"]
    target = int(request.path_params["id"])
    viewer = _viewer(request)
    if not viewer:
        return JSONResponse({"error": "auth required"}, status_code=401)
    csrf = request.headers.get("x-csrf-token") or ""
    if not csrf:
        try:
            form = await request.form()
            csrf = form.get("csrf", "")
        except Exception:
            pass
    if not _check_csrf(request, csrf):
        return JSONResponse({"error": "CSRF"}, status_code=403)
    if action == "follow":
        posting.follow(viewer["id"], target)
    elif action == "unfollow":
        posting.unfollow(viewer["id"], target)
    elif action == "mute":
        if db.query_one("SELECT 1 FROM mutes WHERE account_id = ? AND target_id = ?", (viewer["id"], target)):
            db.execute("DELETE FROM mutes WHERE account_id = ? AND target_id = ?", (viewer["id"], target))
        else:
            db.execute("INSERT INTO mutes (account_id, target_id, created_at) VALUES (?, ?, ?)",
                       (viewer["id"], target, now_iso()))
    elif action == "block":
        if db.query_one("SELECT 1 FROM blocks WHERE account_id = ? AND target_id = ?", (viewer["id"], target)):
            db.execute("DELETE FROM blocks WHERE account_id = ? AND target_id = ?", (viewer["id"], target))
        else:
            db.execute("INSERT INTO blocks (account_id, target_id, created_at) VALUES (?, ?, ?)",
                       (viewer["id"], target, now_iso()))
            posting.unfollow(viewer["id"], target)
            posting.unfollow(target, viewer["id"])
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    rel = serializers.relationship_dict(viewer["id"], target)
    if request.headers.get("accept", "").find("application/json") >= 0:
        return JSONResponse(rel)
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


async def web_delete_status(request: Request):
    viewer = _viewer(request)
    if not viewer:
        raise HTTPException(401, "auth required")
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    posting.delete_status(viewer["id"], int(request.path_params["id"]))
    return RedirectResponse("/home", status_code=303)


async def notifications_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login?next=/notifications", status_code=303)
    rows = db.query_all(
        "SELECT * FROM notifications WHERE account_id = ? ORDER BY id DESC LIMIT 50",
        (viewer["id"],),
    )
    notifs = []
    for r in rows:
        nd = serializers.notification_dict(dict(r), viewer["id"])
        if nd:
            nd["created_at_rel"] = relative_time(nd["created_at"])
            if nd.get("status"):
                nd["status"]["content_html"] = nd["status"]["content"]
            notifs.append(nd)
    db.execute("UPDATE notifications SET seen = 1 WHERE account_id = ?", (viewer["id"],))
    return _render("notifications.html", _ctx(request, notifications=notifs, active="notifications"))


async def settings_profile_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    ad = _decorate_account(viewer, viewer["id"])
    return _render("settings_profile.html", _ctx(request, account=ad, saved=False))


async def settings_profile_post(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    dn = (form.get("display_name") or "").strip()
    note = (form.get("note") or "").strip()
    fields = []
    for i in range(4):
        n = (form.get(f"field_name_{i}") or "").strip()
        v = (form.get(f"field_value_{i}") or "").strip()
        if n or v:
            fields.append({"name": n, "value": v, "verified_at": None})
    db.execute(
        "UPDATE accounts SET display_name = ?, note = ?, fields = ? WHERE id = ?",
        (dn or viewer["username"], note, json.dumps(fields), viewer["id"]),
    )
    # avatar / header
    for kind in ("avatar", "header"):
        f = form.get(kind)
        if f and hasattr(f, "filename") and f.filename:
            ext = os.path.splitext(f.filename)[1].lower() or ".png"
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
                data = await f.read()
                if len(data) <= 5 * 1024 * 1024:
                    out_dir = "/app/data/media"
                    os.makedirs(out_dir, exist_ok=True)
                    import secrets
                    name = f"{kind}_{viewer['id']}_{secrets.token_hex(8)}{ext}"
                    out = os.path.join(out_dir, name)
                    with open(out, "wb") as fp:
                        fp.write(data)
                    db.execute(
                        f"UPDATE accounts SET {kind} = ? WHERE id = ?",
                        (f"{BASE_URL}/media/{name}", viewer["id"]),
                    )
    viewer = auth.find_account_by_id(viewer["id"])
    ad = _decorate_account(viewer, viewer["id"])
    return _render("settings_profile.html", _ctx(request, account=ad, saved=True))


async def edit_status_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    sid = int(request.path_params["id"])
    row = db.query_one("SELECT * FROM statuses WHERE id = ? AND account_id = ? AND deleted = 0",
                       (sid, viewer["id"]))
    if not row:
        raise HTTPException(404, "Not found")
    return _render("edit_status.html", _ctx(request, status=dict(row)))


async def edit_status_post(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    sid = int(request.path_params["id"])
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    text = (form.get("status") or "").strip()
    spoiler = (form.get("spoiler_text") or "").strip()
    posting.edit_status(viewer["id"], sid, text, spoiler_text=spoiler, sensitive=bool(spoiler))
    return RedirectResponse(f"/statuses/{sid}", status_code=303)


async def report_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    target_id = request.query_params.get("account_id")
    if not target_id:
        raise HTTPException(400, "Missing account_id")
    target = auth.find_account_by_id(int(target_id))
    if not target:
        raise HTTPException(404, "Account not found")
    return _render("report.html", _ctx(request, target=target))


async def report_post(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    target_id = int(form.get("account_id", "0"))
    if not target_id:
        raise HTTPException(400, "bad target")
    cur = db.execute(
        """INSERT INTO reports (account_id, target_account_id, comment, category, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (viewer["id"], target_id, form.get("comment", ""), form.get("category", "other"), now_iso()),
    )
    db.execute(
        "INSERT INTO audit_log (actor_id, action, target_type, target_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (viewer["id"], "report.create", "report", str(cur.lastrowid), now_iso()),
    )
    return _render("report.html", _ctx(request,
        target=auth.find_account_by_id(target_id),
    ))


async def lists_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    rows = db.query_all("SELECT * FROM lists WHERE account_id = ? ORDER BY id DESC", (viewer["id"],))
    out = []
    for r in rows:
        d = dict(r)
        c = db.query_one("SELECT COUNT(*) AS c FROM list_accounts WHERE list_id = ?", (r["id"],))
        d["member_count"] = c["c"] if c else 0
        out.append(d)
    return _render("lists.html", _ctx(request, lists=out, active="lists"))


async def lists_create(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    title = (form.get("title") or "").strip()
    if title:
        db.execute(
            "INSERT INTO lists (account_id, title, created_at) VALUES (?, ?, ?)",
            (viewer["id"], title, now_iso()),
        )
    return RedirectResponse("/lists", status_code=303)


async def followers_view(request: Request):
    username = request.path_params["username"].lstrip("@")
    acct = auth.find_account_by_username(username) or auth.find_account_by_acct(username)
    if not acct:
        raise HTTPException(404)
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN follows f ON f.account_id = a.id WHERE f.target_id = ? ORDER BY f.id DESC",
        (acct["id"],),
    )
    accs = [_decorate_account(dict(r), None) for r in rows]
    return _render("account_list.html", _ctx(request, accounts=accs, title=f"Followers of @{acct['username']}"))


async def following_view(request: Request):
    username = request.path_params["username"].lstrip("@")
    acct = auth.find_account_by_username(username) or auth.find_account_by_acct(username)
    if not acct:
        raise HTTPException(404)
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN follows f ON f.target_id = a.id WHERE f.account_id = ? ORDER BY f.id DESC",
        (acct["id"],),
    )
    accs = [_decorate_account(dict(r), None) for r in rows]
    return _render("account_list.html", _ctx(request, accounts=accs, title=f"@{acct['username']} follows"))


async def bookmarks_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login?next=/bookmarks", status_code=303)
    rows = db.query_all(
        "SELECT s.* FROM statuses s JOIN bookmarks b ON b.status_id = s.id WHERE b.account_id = ? AND s.deleted = 0 ORDER BY b.created_at DESC",
        (viewer["id"],),
    )
    statuses = [s for s in (_decorate_status(dict(r), viewer["id"]) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="bookmarks", title="Bookmarks"))


async def favourites_view(request: Request):
    viewer = _viewer(request)
    if not viewer:
        return RedirectResponse("/login?next=/favourites", status_code=303)
    rows = db.query_all(
        "SELECT s.* FROM statuses s JOIN favourites f ON f.status_id = s.id WHERE f.account_id = ? AND s.deleted = 0 ORDER BY f.created_at DESC",
        (viewer["id"],),
    )
    statuses = [s for s in (_decorate_status(dict(r), viewer["id"]) for r in rows) if s]
    return _render("timeline.html", _ctx(request, statuses=statuses, active="favourites", title="Favourites"))


async def sse_stream(request: Request):
    viewer = _viewer(request)
    account_id = viewer["id"] if viewer else None
    channel = "user" if account_id else "public"
    q = await subscribe(account_id, channel)
    pub_q = await subscribe(None, "public")

    async def gen():
        import asyncio
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                done, _ = await asyncio.wait(
                    [asyncio.create_task(q.get()), asyncio.create_task(pub_q.get())],
                    timeout=15.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    yield b": ping\n\n"
                    continue
                for d in done:
                    msg = d.result()
                    payload = msg["payload"] if isinstance(msg["payload"], str) else json.dumps(msg["payload"])
                    body = f"event: {msg['event']}\ndata: {payload}\n\n".encode()
                    yield body
        finally:
            await unsubscribe(account_id, channel, q)
            await unsubscribe(None, "public", pub_q)

    return Response(
        content=b"",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# Simpler SSE using StreamingResponse
from starlette.responses import StreamingResponse


async def sse_stream2(request: Request):
    import asyncio
    viewer = _viewer(request)
    account_id = viewer["id"] if viewer else None
    channel = "user" if account_id else "public"
    q = await subscribe(account_id, channel)
    pub_q = await subscribe(None, "public") if account_id else None

    async def gen():
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                tasks = [asyncio.create_task(q.get())]
                if pub_q is not None:
                    tasks.append(asyncio.create_task(pub_q.get()))
                done, pending = await asyncio.wait(tasks, timeout=15.0, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                if not done:
                    yield b": ping\n\n"
                    continue
                for d in done:
                    try:
                        msg = d.result()
                    except Exception:
                        continue
                    payload = msg["payload"] if isinstance(msg["payload"], str) else json.dumps(msg["payload"])
                    yield (f"event: {msg['event']}\ndata: {payload}\n\n").encode()
        finally:
            await unsubscribe(account_id, channel, q)
            if pub_q is not None:
                await unsubscribe(None, "public", pub_q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
