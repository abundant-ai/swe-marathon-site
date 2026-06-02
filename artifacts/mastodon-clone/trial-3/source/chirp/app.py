"""Chirp main Starlette app."""
import os, json, time, asyncio, secrets, hashlib, base64, mimetypes, datetime
from urllib.parse import urlencode, urlparse, parse_qs
from starlette.applications import Starlette
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.responses import (
    JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse,
    Response, StreamingResponse, FileResponse,
)
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, util, serial, actions
from .util import now_iso, snowflake, new_token, INSTANCE, render_content
from .db import q, qone, qall, conn

DOMAIN = INSTANCE
DATA_DIR = os.environ.get("CHIRP_DATA", "/app/data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
os.makedirs(MEDIA_DIR, exist_ok=True)

# ---------- Templates ----------
TPL = Environment(
    loader=FileSystemLoader("/app/templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

def _rel_time(s):
    if not s: return ""
    try:
        if isinstance(s, str):
            t = datetime.datetime.fromisoformat(s.replace("Z",""))
        else: t = s
    except Exception: return s
    delta = datetime.datetime.utcnow() - t
    sec = int(delta.total_seconds())
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec//60}m"
    if sec < 86400: return f"{sec//3600}h"
    if sec < 30*86400: return f"{sec//86400}d"
    return t.strftime("%Y-%m-%d")

TPL.filters["reltime"] = _rel_time
TPL.filters["render"] = render_content

def render_template(name, **ctx):
    ctx.setdefault("DOMAIN", DOMAIN)
    ctx.setdefault("INSTANCE_TITLE", "Chirp")
    return TPL.get_template(name).render(**ctx)

def html_response(name, request=None, status_code=200, **ctx):
    user = ctx.get("user") or (request and getattr(request.state, "user", None))
    ctx["user"] = user
    if request is not None:
        ctx["csrf"] = getattr(request.state, "csrf", "")
        ctx["path"] = request.url.path
    body = render_template(name, **ctx)
    return HTMLResponse(body, status_code=status_code)

# ---------- Auth helpers ----------
SESSION_COOKIE = "chirp_sid"
CSRF_COOKIE = "chirp_csrf"

def get_bearer(request: Request):
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None

def token_account(token):
    if not token: return None, None
    t = qone("SELECT * FROM oauth_tokens WHERE token=? AND revoked=0", token)
    if not t: return None, None
    if not t["account_id"]: return None, t
    a = qone("SELECT * FROM accounts WHERE id=?", t["account_id"])
    return a, t

def session_account(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid: return None, None
    s = qone("SELECT * FROM sessions WHERE sid=?", sid)
    if not s: return None, None
    a = qone("SELECT * FROM accounts WHERE id=?", s["account_id"])
    return a, s

def has_scope(token_row, scope):
    if not token_row: return False
    granted = (token_row["scopes"] or "").split()
    if scope in granted: return True
    # write implies write:*
    if ":" in scope:
        base = scope.split(":")[0]
        if base in granted: return True
    return False

async def require_user(request: Request, scope="read"):
    tok = get_bearer(request)
    if tok:
        acc, t = token_account(tok)
        if acc and has_scope(t, scope):
            request.state.user = acc
            request.state.token = t
            return acc
    acc, sess = session_account(request)
    if acc:
        request.state.user = acc
        request.state.session = sess
        return acc
    return None

def api_error(status, error, description=None):
    body = {"error": error}
    if description: body["error_description"] = description
    return JSONResponse(body, status_code=status)

# ---------- Security middleware ----------
class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # populate request.state.user (best effort)
        request.state.user = None
        request.state.token = None
        request.state.session = None
        request.state.csrf = ""
        tok = get_bearer(request)
        if tok:
            a, t = token_account(tok)
            if a:
                request.state.user = a
                request.state.token = t
        if request.state.user is None:
            a, s = session_account(request)
            if a:
                request.state.user = a
                request.state.session = s
                request.state.csrf = s["csrf"]
        try:
            response = await call_next(request)
        except HTTPException as e:
            response = JSONResponse({"error": e.detail}, status_code=e.status_code) if request.url.path.startswith("/api") else HTMLResponse(
                render_template("error.html", code=e.status_code, message=e.detail, user=request.state.user), status_code=e.status_code)
        # Common headers
        path = request.url.path
        if path.startswith("/api") or path.startswith("/oauth") or path.startswith("/.well-known"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["X-Frame-Options"] = "DENY"
        return response

async def oauth_apps(request: Request):
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    name = (data.get("client_name") or "").strip()
    redirect = (data.get("redirect_uris") or "urn:ietf:wg:oauth:2.0:oob").strip()
    scopes = (data.get("scopes") or "read").strip()
    website = data.get("website")
    if not name: return api_error(422, "invalid_request", "client_name required")
    aid = snowflake(); cid = new_token(24); csec = new_token(32)
    q("INSERT INTO oauth_apps(id, client_id, client_secret, name, redirect_uri, scopes, website, created_at) VALUES(?,?,?,?,?,?,?,?)",
      aid, cid, csec, name, redirect, scopes, website, now_iso())
    return JSONResponse({"id": aid, "name": name, "website": website, "redirect_uri": redirect,
                         "client_id": cid, "client_secret": csec, "vapid_key": ""})

async def oauth_verify_credentials(request: Request):
    tok = get_bearer(request)
    a, t = token_account(tok)
    if not t: return api_error(401, "invalid_token")
    app_row = qone("SELECT * FROM oauth_apps WHERE id=?", t["app_id"])
    if not app_row: return api_error(401, "invalid_token")
    return JSONResponse({"id": app_row["id"], "name": app_row["name"], "website": app_row["website"], "vapid_key": ""})

async def oauth_authorize(request: Request):
    user = await require_user(request)
    params = dict(request.query_params)
    if request.method == "POST":
        form = await request.form(); params = dict(form)
        sess = getattr(request.state, "session", None)
        if not sess or params.get("_csrf") != sess["csrf"]:
            return api_error(403, "csrf_failed")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob"
    scope = params.get("scope") or "read"
    state = params.get("state") or ""
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    app_row = qone("SELECT * FROM oauth_apps WHERE client_id=?", client_id) if client_id else None
    if not app_row: return HTMLResponse("unknown client", status_code=400)
    if not user:
        return html_response("login.html", request=request, next_url=str(request.url))
    if request.method == "GET":
        return html_response("authorize.html", request=request, app=app_row, scope=scope,
                             redirect_uri=redirect_uri, client_id=client_id, state=state,
                             code_challenge=code_challenge or "", code_challenge_method=code_challenge_method or "")
    code = new_token(32)
    q("INSERT INTO oauth_codes(code, app_id, account_id, scopes, redirect_uri, code_challenge, code_challenge_method, created_at) VALUES(?,?,?,?,?,?,?,?)",
      code, app_row["id"], user["id"], scope, redirect_uri, code_challenge, code_challenge_method, int(time.time()))
    if redirect_uri == "urn:ietf:wg:oauth:2.0:oob":
        return html_response("oob.html", request=request, code=code)
    sep = "&" if "?" in redirect_uri else "?"
    qs = urlencode({"code": code, **({"state": state} if state else {})})
    return RedirectResponse(redirect_uri + sep + qs, status_code=302)

def _verify_pkce(code_row, verifier):
    if not code_row["code_challenge"]: return True
    if not verifier: return False
    method = (code_row["code_challenge_method"] or "plain").upper()
    if method == "PLAIN": return verifier == code_row["code_challenge"]
    if method == "S256":
        h = hashlib.sha256(verifier.encode()).digest()
        b = base64.urlsafe_b64encode(h).rstrip(b"=").decode()
        return b == code_row["code_challenge"]
    return False

async def oauth_token(request: Request):
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    grant = data.get("grant_type")
    cid = data.get("client_id"); csec = data.get("client_secret")
    app_row = qone("SELECT * FROM oauth_apps WHERE client_id=?", cid) if cid else None
    if not app_row: return api_error(401, "invalid_client")
    if grant == "client_credentials":
        scopes = data.get("scope") or "read"
        if app_row["client_secret"] != csec: return api_error(401, "invalid_client")
        tok = new_token(40)
        q("INSERT INTO oauth_tokens(token, app_id, account_id, scopes, created_at) VALUES(?,?,?,?,?)",
          tok, app_row["id"], None, scopes, int(time.time()))
        return JSONResponse({"access_token": tok, "token_type": "Bearer", "scope": scopes, "created_at": int(time.time())})
    if grant == "authorization_code":
        code = data.get("code"); verifier = data.get("code_verifier")
        cr = qone("SELECT * FROM oauth_codes WHERE code=?", code) if code else None
        if not cr or cr["used"] or cr["app_id"] != app_row["id"]:
            return api_error(400, "invalid_grant")
        if cr["redirect_uri"] != (data.get("redirect_uri") or cr["redirect_uri"]):
            return api_error(400, "invalid_grant")
        if not cr["code_challenge"] and app_row["client_secret"] != csec:
            return api_error(401, "invalid_client")
        if not _verify_pkce(cr, verifier):
            return api_error(400, "invalid_grant", "PKCE failed")
        q("UPDATE oauth_codes SET used=1 WHERE code=?", code)
        tok = new_token(40)
        q("INSERT INTO oauth_tokens(token, app_id, account_id, scopes, created_at) VALUES(?,?,?,?,?)",
          tok, app_row["id"], cr["account_id"], cr["scopes"], int(time.time()))
        return JSONResponse({"access_token": tok, "token_type": "Bearer", "scope": cr["scopes"], "created_at": int(time.time())})
    if grant == "password":
        username = data.get("username"); password = data.get("password"); scopes = data.get("scope") or "read"
        if app_row["client_secret"] != csec: return api_error(401, "invalid_client")
        from .util import pwverify
        u = qone("SELECT * FROM accounts WHERE (username=? OR email=?) AND is_local=1", username, username)
        if not u or not pwverify(password or "", u["password_hash"] or ""):
            return api_error(400, "invalid_grant")
        tok = new_token(40)
        q("INSERT INTO oauth_tokens(token, app_id, account_id, scopes, created_at) VALUES(?,?,?,?,?)",
          tok, app_row["id"], u["id"], scopes, int(time.time()))
        return JSONResponse({"access_token": tok, "token_type": "Bearer", "scope": scopes, "created_at": int(time.time())})
    return api_error(400, "unsupported_grant_type")

async def oauth_revoke(request: Request):
    try:
        form = await request.form(); data = dict(form)
    except Exception: data = {}
    tok = data.get("token")
    if tok:
        q("UPDATE oauth_tokens SET revoked=1 WHERE token=?", tok)
    return JSONResponse({})

async def oauth_introspect(request: Request):
    auth_tok = get_bearer(request)
    if not auth_tok:
        return api_error(401, "invalid_token")
    at = qone("SELECT * FROM oauth_tokens WHERE token=?", auth_tok)
    if not at or at["revoked"]:
        return api_error(401, "invalid_token")
    try:
        form = await request.form(); data = dict(form)
    except Exception: data = {}
    tok = data.get("token")
    if not tok:
        return JSONResponse({"active": False})
    t = qone("SELECT * FROM oauth_tokens WHERE token=?", tok)
    if not t or t["revoked"]:
        return JSONResponse({"active": False})
    out = {"active": True, "scope": t["scopes"], "token_type": "Bearer", "iat": t["created_at"]}
    app_row = qone("SELECT * FROM oauth_apps WHERE id=?", t["app_id"])
    if app_row: out["client_id"] = app_row["client_id"]
    if t["account_id"]:
        a = qone("SELECT * FROM accounts WHERE id=?", t["account_id"])
        if a:
            out["username"] = a["username"]
            out["sub"] = a["id"]
    return JSONResponse(out)

# ---------- API: instance/accounts ----------
async def api_instance(request: Request):
    return JSONResponse({
        "uri": DOMAIN, "title": "Chirp", "short_description": "A small Chirp instance.",
        "description": "Chirp speaks Mastodon v1.", "email": "admin@"+DOMAIN,
        "version": "4.2.0 (compatible; Chirp 1.0)", "languages": ["en"],
        "registrations": True, "approval_required": False, "invites_enabled": False,
        "urls": {"streaming_api": f"wss://{DOMAIN}"},
        "stats": {
            "user_count": qone("SELECT COUNT(*) c FROM accounts WHERE is_local=1")["c"],
            "status_count": qone("SELECT COUNT(*) c FROM statuses WHERE deleted=0 AND is_local=1")["c"],
            "domain_count": 1,
        },
        "thumbnail": "/static/thumbnail.png",
        "contact_account": None,
        "rules": [],
        "configuration": {
            "statuses": {"max_characters": 500, "max_media_attachments": 4, "characters_reserved_per_url": 23},
            "media_attachments": {"supported_mime_types": ["image/jpeg","image/png","image/gif"], "image_size_limit": 10485760, "image_matrix_limit": 16777216, "video_size_limit": 41943040, "video_frame_rate_limit": 60, "video_matrix_limit": 2304000},
            "polls": {"max_options": 4, "max_characters_per_option": 50, "min_expiration": 300, "max_expiration": 2629746},
            "accounts": {"max_featured_tags": 10},
        },
    })

async def api_instance_v2(request: Request):
    base = (await api_instance(request))
    return base

async def api_apps_verify(request: Request):
    return await oauth_verify_credentials(request)

async def api_verify_credentials(request: Request):
    user = await require_user(request, "read:accounts")
    if not user:
        # also accept plain read scope
        user = await require_user(request, "read")
    if not user: return api_error(401, "unauthorized")
    out = serial.account(user, user["id"])
    out["source"] = {"privacy": "public", "sensitive": False, "language": "en", "note": user["note"], "fields": json.loads(user["fields_json"] or "[]")}
    out["role"] = {"id": "admin" if user["is_admin"] else "user", "name": "admin" if user["is_admin"] else "user", "permissions": "0"}
    return JSONResponse(out)

async def api_account(request: Request):
    aid = request.path_params["id"]
    a = qone("SELECT * FROM accounts WHERE id=?", aid)
    if not a: return api_error(404, "not_found")
    return JSONResponse(serial.account(a))

async def api_account_lookup(request: Request):
    acct = request.query_params.get("acct", "").lstrip("@")
    if "@" in acct:
        u, d = acct.split("@", 1)
        a = qone("SELECT * FROM accounts WHERE username=? AND domain=?", u, d)
    else:
        a = qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", acct)
    if not a: return api_error(404, "not_found")
    return JSONResponse(serial.account(a))

async def api_account_search(request: Request):
    q_ = request.query_params.get("q","").lstrip("@")
    limit = min(int(request.query_params.get("limit", 40) or 40), 80)
    rows = qall("SELECT * FROM accounts WHERE username LIKE ? OR display_name LIKE ? ORDER BY followers_count DESC LIMIT ?", f"%{q_}%", f"%{q_}%", limit)
    return JSONResponse([serial.account(r) for r in rows])

async def api_relationships(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    ids = request.query_params.getlist("id[]") or request.query_params.getlist("id")
    out = [serial.relationship(user["id"], i) for i in ids]
    return JSONResponse(out)

async def api_account_statuses(request: Request):
    aid = request.path_params["id"]
    limit = min(int(request.query_params.get("limit", 20) or 20), 40)
    max_id = request.query_params.get("max_id")
    only_media = request.query_params.get("only_media") in ("1","true")
    exclude_replies = request.query_params.get("exclude_replies") in ("1","true")
    pinned = request.query_params.get("pinned") in ("1","true")
    where = ["account_id=?", "deleted=0"]; args = [aid]
    if max_id:
        where.append("id < ?"); args.append(max_id)
    if exclude_replies:
        where.append("in_reply_to_id IS NULL")
    if pinned:
        where.append("0=1")
    sql = "SELECT * FROM statuses WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = qall(sql, *args)
    viewer = request.state.user["id"] if request.state.user else None
    return JSONResponse([serial.status(r, viewer) for r in rows if not r["deleted"]])

# ---------- API: statuses ----------
async def api_statuses_post(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, "write"):
        return api_error(403, "forbidden", "requires write scope")
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
            mids = form.getlist("media_ids[]")
            if mids: data["media_ids"] = mids
            opts = form.getlist("poll[options][]")
            if opts:
                data["poll"] = {
                    "options": opts,
                    "expires_in": form.get("poll[expires_in]") or 86400,
                    "multiple": form.get("poll[multiple]") in ("1","true","on"),
                    "hide_totals": form.get("poll[hide_totals]") in ("1","true","on"),
                }
    except Exception: data = {}
    content = (data.get("status") or "").strip()
    visibility = data.get("visibility") or "public"
    if not util.visibility_ok(visibility): visibility = "public"
    spoiler = data.get("spoiler_text") or ""
    sensitive = bool(data.get("sensitive")) or bool(spoiler)
    in_reply_to = data.get("in_reply_to_id")
    media_ids = data.get("media_ids") or []
    if isinstance(media_ids, str): media_ids = [media_ids]
    poll = data.get("poll")
    idem = request.headers.get("idempotency-key")
    if not content and not media_ids and not poll:
        return api_error(422, "invalid_request", "empty status")
    if len(content) > 500:
        return api_error(422, "invalid_request", "too long")
    sid = actions.create_status(user["id"], content=content, visibility=visibility,
                                in_reply_to_id=in_reply_to, spoiler_text=spoiler,
                                sensitive=sensitive, media_ids=media_ids,
                                idempotency_key=idem, poll=poll if isinstance(poll, dict) else None)
    s = serial.status_by_id(sid, user["id"])
    return JSONResponse(s)

def can_view_status(row, viewer_id):
    if row["visibility"] in ("public", "unlisted"): return True
    if not viewer_id: return False
    if row["account_id"] == viewer_id: return True
    if row["visibility"] == "private":
        if qone("SELECT 1 FROM follows WHERE follower_id=? AND target_id=?", viewer_id, row["account_id"]): return True
        return bool(qone("SELECT 1 FROM status_mentions WHERE status_id=? AND account_id=?", row["id"], viewer_id))
    if row["visibility"] == "direct":
        return bool(qone("SELECT 1 FROM status_mentions WHERE status_id=? AND account_id=?", row["id"], viewer_id))
    return False

async def api_status_get(request: Request):
    sid = request.path_params["id"]
    viewer = request.state.user["id"] if request.state.user else None
    row = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not row: return api_error(404, "not_found")
    if not can_view_status(row, viewer): return api_error(404, "not_found")
    last = row["edited_at"] or row["created_at"]
    etag = '"' + hashlib.sha1((sid + last + str(row["favourites_count"]) + str(row["reblogs_count"])).encode()).hexdigest() + '"'
    inm = request.headers.get("if-none-match")
    if inm and inm == etag:
        return Response(status_code=304, headers={"ETag": etag, "Last-Modified": last})
    s = serial.status(row, viewer)
    resp = JSONResponse(s)
    resp.headers["ETag"] = etag
    resp.headers["Last-Modified"] = last
    resp.headers["Cache-Control"] = "private, max-age=0"
    return resp

async def api_status_delete(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    sid = request.path_params["id"]
    s = serial.status_by_id(sid, user["id"])
    if not s: return api_error(404, "not_found")
    ok = actions.delete_status(sid, user["id"])
    if not ok: return api_error(403, "forbidden")
    return JSONResponse(s)

async def api_status_edit(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    sid = request.path_params["id"]
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    new_id = actions.edit_status(sid, user["id"], content=data.get("status"),
                                 spoiler_text=data.get("spoiler_text"),
                                 sensitive=data.get("sensitive"))
    if not new_id: return api_error(403, "forbidden")
    return JSONResponse(serial.status_by_id(new_id, user["id"]))

async def api_status_history(request: Request):
    sid = request.path_params["id"]
    s = qone("SELECT * FROM statuses WHERE id=?", sid)
    if not s: return api_error(404, "not_found")
    edits = qall("SELECT * FROM status_edits WHERE status_id=? ORDER BY id ASC", sid)
    out = []
    acc = qone("SELECT * FROM accounts WHERE id=?", s["account_id"])
    for e in edits:
        out.append({
            "content": render_content(e["content"]),
            "spoiler_text": e["spoiler_text"],
            "sensitive": bool(e["sensitive"]),
            "created_at": e["created_at"],
            "account": serial.account(acc),
            "poll": None, "media_attachments": [], "emojis": [],
        })
    out.append({
        "content": render_content(s["content"]),
        "spoiler_text": s["spoiler_text"], "sensitive": bool(s["sensitive"]),
        "created_at": s["edited_at"] or s["created_at"],
        "account": serial.account(acc), "poll": None, "media_attachments": [], "emojis": [],
    })
    return JSONResponse(out)

async def api_status_source(request: Request):
    sid = request.path_params["id"]
    s = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not s: return api_error(404, "not_found")
    return JSONResponse({"id": sid, "text": s["content"], "spoiler_text": s["spoiler_text"]})

async def api_status_context(request: Request):
    sid = request.path_params["id"]
    s = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not s: return api_error(404, "not_found")
    viewer = request.state.user["id"] if request.state.user else None
    ancestors = []
    cur = s
    while cur and cur["in_reply_to_id"]:
        p = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", cur["in_reply_to_id"])
        if not p: break
        ancestors.append(serial.status(p, viewer))
        cur = p
    ancestors.reverse()
    desc = []
    def walk(pid):
        kids = qall("SELECT * FROM statuses WHERE in_reply_to_id=? AND deleted=0 ORDER BY id ASC", pid)
        for k in kids:
            desc.append(serial.status(k, viewer))
            walk(k["id"])
    walk(sid)
    return JSONResponse({"ancestors": ancestors, "descendants": desc})

async def _status_action(request, fn, scope="write"):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, scope):
        return api_error(403, "forbidden", "requires " + scope + " scope")
    sid = request.path_params["id"]
    if not qone("SELECT 1 FROM statuses WHERE id=? AND deleted=0", sid):
        return api_error(404, "not_found")
    fn(user["id"], sid)
    return JSONResponse(serial.status_by_id(sid, user["id"]))

async def api_favourite(request): return await _status_action(request, actions.favourite)
async def api_unfavourite(request): return await _status_action(request, actions.unfavourite)
async def api_bookmark(request): return await _status_action(request, actions.bookmark)
async def api_unbookmark(request): return await _status_action(request, actions.unbookmark)
async def api_reblog(request): return await _status_action(request, lambda a,s: actions.reblog(a,s))
async def api_unreblog(request): return await _status_action(request, actions.unreblog)
async def api_pin(request): return await _status_action(request, lambda a,s: None)
async def api_unpin(request): return await _status_action(request, lambda a,s: None)

async def api_status_favourited_by(request: Request):
    sid = request.path_params["id"]
    rows = qall("SELECT a.* FROM favourites f JOIN accounts a ON a.id=f.account_id WHERE f.status_id=? ORDER BY f.created_at DESC", sid)
    return JSONResponse([serial.account(r) for r in rows])

async def api_status_reblogged_by(request: Request):
    sid = request.path_params["id"]
    rows = qall("SELECT a.* FROM statuses s JOIN accounts a ON a.id=s.account_id WHERE s.reblog_of_id=? AND s.deleted=0", sid)
    return JSONResponse([serial.account(r) for r in rows])

# ---------- Timelines ----------
def _pager_link(request, items, key="id"):
    return None  # we set Link header in returned response

def _set_link_header(resp, request, items, ident=lambda x: x["id"]):
    if not items: return resp
    base = str(request.url).split("?")[0]
    qs = dict(request.query_params)
    older = ident(items[-1])
    newer = ident(items[0])
    qs_o = qs.copy(); qs_o["max_id"] = older
    qs_n = qs.copy(); qs_n["min_id"] = newer
    link = f'<{base}?{urlencode(qs_o)}>; rel="next", <{base}?{urlencode(qs_n)}>; rel="prev"'
    resp.headers["Link"] = link
    return resp

def _excluded_ids(viewer_id):
    if not viewer_id: return set()
    s = set()
    for r in qall("SELECT target_id FROM blocks WHERE account_id=?", viewer_id): s.add(r["target_id"])
    for r in qall("SELECT account_id FROM blocks WHERE target_id=?", viewer_id): s.add(r["account_id"])
    for r in qall("SELECT target_id FROM mutes WHERE account_id=?", viewer_id): s.add(r["target_id"])
    return s

def _timeline_query(scope, viewer_id, **opts):
    where = ["s.deleted=0"]
    args = []
    if scope == "home":
        if not viewer_id: return [], []
        where.append("s.visibility IN ('public','unlisted','private')")
        where.append("(s.account_id=? OR s.account_id IN (SELECT target_id FROM follows WHERE follower_id=?))")
        args.extend([viewer_id, viewer_id])
    elif scope == "local":
        where.append("s.is_local=1")
        where.append("s.visibility IN ('public','unlisted')")
        where.append("s.in_reply_to_id IS NULL")
        where.append("s.reblog_of_id IS NULL")
    elif scope == "public":
        where.append("s.visibility = 'public'")
        where.append("s.in_reply_to_id IS NULL")
        where.append("s.reblog_of_id IS NULL")
    elif scope == "tag":
        where.append("s.id IN (SELECT status_id FROM status_tags WHERE tag=?)")
        args.append(opts["tag"].lower())
        where.append("s.visibility IN ('public','unlisted')")
    if opts.get("max_id"):
        where.append("s.id < ?"); args.append(opts["max_id"])
    if opts.get("since_id"):
        where.append("s.id > ?"); args.append(opts["since_id"])
    if opts.get("min_id"):
        where.append("s.id > ?"); args.append(opts["min_id"])
    sql = "SELECT s.* FROM statuses s WHERE " + " AND ".join(where) + " ORDER BY s.id DESC LIMIT ?"
    args.append(min(int(opts.get("limit", 20) or 20), 40))
    return sql, args

def _timeline_rows(scope, viewer_id, **opts):
    sql, args = _timeline_query(scope, viewer_id, **opts)
    if not sql: return []
    rows = qall(sql, *args)
    excl = _excluded_ids(viewer_id)
    return [r for r in rows if r["account_id"] not in excl]

async def api_tl_home(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    rows = _timeline_rows("home", user["id"], **dict(request.query_params))
    out = [serial.status(r, user["id"]) for r in rows]
    return _set_link_header(JSONResponse([s for s in out if s]), request, rows)

async def api_tl_public(request: Request):
    viewer = request.state.user["id"] if request.state.user else None
    only_local = request.query_params.get("local") in ("1","true")
    scope = "local" if only_local else "public"
    rows = _timeline_rows(scope, viewer, **dict(request.query_params))
    out = [serial.status(r, viewer) for r in rows]
    return _set_link_header(JSONResponse([s for s in out if s]), request, rows)

async def api_tl_tag(request: Request):
    tag = request.path_params["hashtag"].lower()
    viewer = request.state.user["id"] if request.state.user else None
    rows = _timeline_rows("tag", viewer, tag=tag, **dict(request.query_params))
    out = [serial.status(r, viewer) for r in rows]
    return JSONResponse([s for s in out if s])

async def api_tl_list(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    lid = request.path_params["id"]
    l = qone("SELECT * FROM lists WHERE id=? AND account_id=?", lid, user["id"])
    if not l: return api_error(404, "not_found")
    accs = [r["account_id"] for r in qall("SELECT account_id FROM list_accounts WHERE list_id=?", lid)]
    if not accs: return JSONResponse([])
    placeholders = ",".join(["?"]*len(accs))
    rows = qall(f"SELECT * FROM statuses WHERE account_id IN ({placeholders}) AND deleted=0 ORDER BY id DESC LIMIT 40", *accs)
    return JSONResponse([serial.status(r, user["id"]) for r in rows])

# ---------- Follow / Block / Mute ----------
async def api_follow(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.follow(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_unfollow(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.unfollow(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_block(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.block(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_unblock(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.unblock(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_mute(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.mute(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_unmute(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    aid = request.path_params["id"]
    actions.unmute(user["id"], aid)
    return JSONResponse(serial.relationship(user["id"], aid))

async def api_followers(request: Request):
    aid = request.path_params["id"]
    rows = qall("SELECT a.* FROM follows f JOIN accounts a ON a.id=f.follower_id WHERE f.target_id=? ORDER BY f.created_at DESC LIMIT 40", aid)
    return JSONResponse([serial.account(r) for r in rows])

async def api_following(request: Request):
    aid = request.path_params["id"]
    rows = qall("SELECT a.* FROM follows f JOIN accounts a ON a.id=f.target_id WHERE f.follower_id=? ORDER BY f.created_at DESC LIMIT 40", aid)
    return JSONResponse([serial.account(r) for r in rows])

async def api_blocks(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    rows = qall("SELECT a.* FROM blocks b JOIN accounts a ON a.id=b.target_id WHERE b.account_id=?", user["id"])
    return JSONResponse([serial.account(r) for r in rows])

async def api_mutes(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    rows = qall("SELECT a.* FROM mutes m JOIN accounts a ON a.id=m.target_id WHERE m.account_id=?", user["id"])
    return JSONResponse([serial.account(r) for r in rows])

async def api_favourites(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    rows = qall("SELECT s.* FROM favourites f JOIN statuses s ON s.id=f.status_id WHERE f.account_id=? AND s.deleted=0 ORDER BY f.created_at DESC LIMIT 40", user["id"])
    return JSONResponse([serial.status(r, user["id"]) for r in rows])

async def api_bookmarks(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    rows = qall("SELECT s.* FROM bookmarks b JOIN statuses s ON s.id=b.status_id WHERE b.account_id=? AND s.deleted=0 ORDER BY b.created_at DESC LIMIT 40", user["id"])
    return JSONResponse([serial.status(r, user["id"]) for r in rows])

# ---------- Notifications ----------
async def api_notifications(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    types = request.query_params.getlist("types[]")
    excl = request.query_params.getlist("exclude_types[]")
    max_id = request.query_params.get("max_id")
    limit = min(int(request.query_params.get("limit", 20) or 20), 40)
    where = ["account_id=?"]; args = [user["id"]]
    if types:
        where.append("type IN ("+",".join("?"*len(types))+")"); args.extend(types)
    if excl:
        where.append("type NOT IN ("+",".join("?"*len(excl))+")"); args.extend(excl)
    if max_id:
        where.append("id < ?"); args.append(max_id)
    sql = "SELECT * FROM notifications WHERE "+" AND ".join(where)+" ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = qall(sql, *args)
    return JSONResponse([serial.notification(r, user["id"]) for r in rows])

async def api_notif_clear(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    q("DELETE FROM notifications WHERE account_id=?", user["id"])
    return JSONResponse({})

async def api_notif_get(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    nid = request.path_params["id"]
    r = qone("SELECT * FROM notifications WHERE id=? AND account_id=?", nid, user["id"])
    if not r: return api_error(404, "not_found")
    return JSONResponse(serial.notification(r, user["id"]))

# ---------- Lists ----------
async def api_lists(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    if request.method == "POST":
        try:
            if "application/json" in request.headers.get("content-type",""):
                data = await request.json()
            else:
                form = await request.form(); data = dict(form)
        except Exception: data = {}
        title = (data.get("title") or "").strip()
        if not title: return api_error(422, "invalid_request")
        lid = snowflake()
        q("INSERT INTO lists(id, account_id, title, replies_policy, created_at) VALUES(?,?,?,?,?)",
          lid, user["id"], title, data.get("replies_policy") or "list", now_iso())
        l = qone("SELECT * FROM lists WHERE id=?", lid)
        return JSONResponse({"id": l["id"], "title": l["title"], "replies_policy": l["replies_policy"], "exclusive": False})
    rows = qall("SELECT * FROM lists WHERE account_id=?", user["id"])
    return JSONResponse([{"id": r["id"], "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": False} for r in rows])

async def api_list_get(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    lid = request.path_params["id"]
    r = qone("SELECT * FROM lists WHERE id=? AND account_id=?", lid, user["id"])
    if not r: return api_error(404, "not_found")
    return JSONResponse({"id": r["id"], "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": False})

async def api_list_update(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    lid = request.path_params["id"]
    r = qone("SELECT * FROM lists WHERE id=? AND account_id=?", lid, user["id"])
    if not r: return api_error(404, "not_found")
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    if data.get("title"): q("UPDATE lists SET title=? WHERE id=?", data["title"], lid)
    r = qone("SELECT * FROM lists WHERE id=?", lid)
    return JSONResponse({"id": r["id"], "title": r["title"], "replies_policy": r["replies_policy"], "exclusive": False})

async def api_list_delete(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    lid = request.path_params["id"]
    q("DELETE FROM lists WHERE id=? AND account_id=?", lid, user["id"])
    q("DELETE FROM list_accounts WHERE list_id=?", lid)
    return JSONResponse({})

async def api_list_accounts(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    lid = request.path_params["id"]
    if request.method == "GET":
        rows = qall("SELECT a.* FROM list_accounts la JOIN accounts a ON a.id=la.account_id WHERE la.list_id=?", lid)
        return JSONResponse([serial.account(r) for r in rows])
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
            ids = form.getlist("account_ids[]")
            if ids: data["account_ids"] = ids
    except Exception: data = {}
    ids = data.get("account_ids") or []
    if isinstance(ids, str): ids = [ids]
    if request.method == "POST":
        for i in ids:
            q("INSERT OR IGNORE INTO list_accounts(list_id, account_id) VALUES(?,?)", lid, i)
    elif request.method == "DELETE":
        for i in ids:
            q("DELETE FROM list_accounts WHERE list_id=? AND account_id=?", lid, i)
    return JSONResponse({})

# ---------- Search ----------
async def api_search(request: Request):
    qstr = request.query_params.get("q","").strip()
    typ = request.query_params.get("type")
    out = {"accounts": [], "statuses": [], "hashtags": []}
    if not qstr: return JSONResponse(out)
    if typ in (None, "accounts"):
        rows = qall("SELECT * FROM accounts WHERE username LIKE ? OR display_name LIKE ? LIMIT 20", f"%{qstr.lstrip('@')}%", f"%{qstr}%")
        out["accounts"] = [serial.account(r) for r in rows]
    if typ in (None, "statuses"):
        rows = qall("SELECT * FROM statuses WHERE deleted=0 AND content LIKE ? ORDER BY id DESC LIMIT 20", f"%{qstr}%")
        out["statuses"] = [serial.status(r) for r in rows if not r["deleted"]]
    if typ in (None, "hashtags"):
        rows = qall("SELECT name FROM hashtags WHERE name LIKE ? LIMIT 20", f"%{qstr.lstrip('#').lower()}%")
        out["hashtags"] = [serial.tag_dict(r["name"]) for r in rows]
    return JSONResponse(out)

async def api_trends_tags(request: Request):
    rows = qall("SELECT t.tag, COUNT(*) c FROM status_tags t JOIN statuses s ON s.id=t.status_id WHERE s.deleted=0 GROUP BY t.tag ORDER BY c DESC LIMIT 10")
    return JSONResponse([serial.tag_dict(r["tag"]) for r in rows])

# ---------- Media ----------
async def api_media_post(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    form = await request.form()
    f = form.get("file")
    desc = form.get("description") or ""
    if not f or not hasattr(f, "filename"): return api_error(422, "invalid_request")
    mid = snowflake()
    raw = await f.read()
    safe = f.filename.replace("/","_")
    ext = os.path.splitext(safe)[1] or ".bin"
    fn = mid + ext
    fp = os.path.join(MEDIA_DIR, fn)
    with open(fp, "wb") as out: out.write(raw)
    typ = "image"
    if (f.content_type or "").startswith("video"): typ = "video"
    url = f"/media/{fn}"
    q("INSERT INTO media(id, account_id, type, url, preview_url, description, created_at, file_path) VALUES(?,?,?,?,?,?,?,?)",
      mid, user["id"], typ, url, url, desc, now_iso(), fp)
    r = qone("SELECT * FROM media WHERE id=?", mid)
    return JSONResponse(serial.media_dict(r))

async def api_media_update(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    mid = request.path_params["id"]
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    if "description" in data:
        q("UPDATE media SET description=? WHERE id=? AND account_id=?", data["description"], mid, user["id"])
    r = qone("SELECT * FROM media WHERE id=?", mid)
    if not r: return api_error(404, "not_found")
    return JSONResponse(serial.media_dict(r))

async def media_file(request: Request):
    fn = request.path_params["name"]
    fp = os.path.join(MEDIA_DIR, fn)
    if not os.path.isfile(fp): raise HTTPException(404)
    return FileResponse(fp)

# ---------- Polls ----------
async def api_poll_get(request: Request):
    pid = request.path_params["id"]
    viewer = request.state.user["id"] if request.state.user else None
    p = serial.poll_dict(pid, viewer)
    if not p: return api_error(404, "not_found")
    return JSONResponse(p)

async def api_poll_vote(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    pid = request.path_params["id"]
    p = qone("SELECT * FROM polls WHERE id=?", pid)
    if not p: return api_error(404, "not_found")
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
            ch = form.getlist("choices[]")
            if ch: data["choices"] = ch
    except Exception: data = {}
    choices = data.get("choices") or []
    if isinstance(choices, (str, int)): choices = [choices]
    choices = [int(c) for c in choices]
    if not choices: return api_error(422, "invalid_request")
    if not p["multiple"] and len(choices) > 1: choices = choices[:1]
    if qone("SELECT 1 FROM poll_votes WHERE poll_id=? AND account_id=?", pid, user["id"]):
        return api_error(422, "already_voted")
    for c in choices:
        q("INSERT OR IGNORE INTO poll_votes(poll_id, account_id, idx, created_at) VALUES(?,?,?,?)", pid, user["id"], c, now_iso())
        q("UPDATE poll_options SET votes_count=votes_count+1 WHERE poll_id=? AND idx=?", pid, c)
        q("UPDATE polls SET votes_count=votes_count+1 WHERE id=?", pid)
    q("UPDATE polls SET voters_count=voters_count+1 WHERE id=?", pid)
    return JSONResponse(serial.poll_dict(pid, user["id"]))

# ---------- Reports ----------
async def api_reports(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
            sids = form.getlist("status_ids[]")
            if sids: data["status_ids"] = sids
    except Exception: data = {}
    target = data.get("account_id")
    if not target: return api_error(422, "invalid_request")
    rid = snowflake()
    sids = data.get("status_ids") or []
    if isinstance(sids, str): sids = [sids]
    q("INSERT INTO reports(id, account_id, target_id, status_ids, comment, category, created_at) VALUES(?,?,?,?,?,?,?)",
      rid, user["id"], target, json.dumps(sids), data.get("comment") or "", data.get("category") or "other", now_iso())
    actions.audit(user["id"], "report", target, {"comment": data.get("comment"), "status_ids": sids})
    return JSONResponse({"id": rid, "action_taken": False, "category": data.get("category") or "other",
                         "comment": data.get("comment") or "", "forwarded": False, "created_at": now_iso(),
                         "status_ids": sids, "rules_ids": [], "target_account": serial.account_by_id(target)})

# ---------- Admin ----------
def _require_admin(request):
    user = request.state.user
    if not user or not user["is_admin"]: return None
    if request.state.token and not has_scope(request.state.token, "admin"):
        return None
    return user

async def admin_queues(request: Request):
    if not _require_admin(request): return api_error(403, "forbidden")
    rows = qall("SELECT queue, status, COUNT(*) c FROM jobs GROUP BY queue, status")
    out = {}
    for r in rows:
        out.setdefault(r["queue"], {})[r["status"]] = r["c"]
    return JSONResponse({"queues": out})

async def admin_audit(request: Request):
    if not _require_admin(request): return api_error(403, "forbidden")
    rows = qall("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")
    return JSONResponse([{"id": r["id"], "actor_id": r["actor_id"], "action": r["action"],
                          "target": r["target"], "meta": json.loads(r["meta"] or "{}"),
                          "created_at": r["created_at"]} for r in rows])

async def admin_reports(request: Request):
    if not _require_admin(request): return api_error(403, "forbidden")
    rows = qall("SELECT * FROM reports ORDER BY id DESC")
    return JSONResponse([{"id": r["id"], "account_id": r["account_id"], "target_id": r["target_id"],
                          "comment": r["comment"], "category": r["category"],
                          "status_ids": json.loads(r["status_ids"] or "[]"),
                          "created_at": r["created_at"], "action_taken": bool(r["action_taken"])} for r in rows])

# ---------- Streaming SSE ----------
async def streaming_sse(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    stream = request.query_params.get("stream", "user")
    queue = asyncio.Queue(maxsize=64)
    actions.SSE_LISTENERS.setdefault(user["id"], []).append(queue)
    async def gen():
        try:
            yield "event: ping\ndata: ok\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    ev, data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {ev}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: keepalive\n\n"
        finally:
            try: actions.SSE_LISTENERS[user["id"]].remove(queue)
            except Exception: pass
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

# ---------- Account update credentials ----------
async def api_update_credentials(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    if request.state.token and not has_scope(request.state.token, 'write'):
        return api_error(403, 'forbidden', 'requires write scope')
    form = await request.form(); data = dict(form)
    upd = []
    args = []
    if "display_name" in data:
        upd.append("display_name=?"); args.append(data["display_name"])
    if "note" in data:
        upd.append("note=?"); args.append(data["note"])
    if "locked" in data:
        upd.append("locked=?"); args.append(1 if data["locked"] in ("1","true","on") else 0)
    fields = []
    for i in range(4):
        n = data.get(f"fields_attributes[{i}][name]")
        v = data.get(f"fields_attributes[{i}][value]")
        if n or v:
            fields.append({"name": n or "", "value": v or "", "verified_at": None})
    if fields:
        upd.append("fields_json=?"); args.append(json.dumps(fields))
    av = form.get("avatar")
    if av and hasattr(av, "filename") and av.filename:
        raw = await av.read()
        ext = os.path.splitext(av.filename)[1] or ".png"
        fn = f"avatar_{user['id']}{ext}"
        fp = os.path.join(MEDIA_DIR, fn)
        with open(fp, "wb") as f: f.write(raw)
        upd.append("avatar=?"); args.append(f"/media/{fn}")
    hd = form.get("header")
    if hd and hasattr(hd, "filename") and hd.filename:
        raw = await hd.read()
        ext = os.path.splitext(hd.filename)[1] or ".png"
        fn = f"header_{user['id']}{ext}"
        fp = os.path.join(MEDIA_DIR, fn)
        with open(fp, "wb") as f: f.write(raw)
        upd.append("header=?"); args.append(f"/media/{fn}")
    if upd:
        args.append(user["id"])
        q(f"UPDATE accounts SET {', '.join(upd)} WHERE id=?", *args)
    a = qone("SELECT * FROM accounts WHERE id=?", user["id"])
    return JSONResponse(serial.account(a))

# ---------- Markers ----------
async def api_markers_get(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    tls = request.query_params.getlist("timeline[]") or ["home","notifications"]
    out = {}
    for tl in tls:
        m = qone("SELECT * FROM markers WHERE account_id=? AND timeline=?", user["id"], tl)
        if m:
            out[tl] = {"last_read_id": m["last_read_id"], "version": m["version"], "updated_at": m["updated_at"]}
    return JSONResponse(out)

async def api_markers_post(request: Request):
    user = request.state.user
    if not user: return api_error(401, "unauthorized")
    try:
        if "application/json" in request.headers.get("content-type",""):
            data = await request.json()
        else:
            form = await request.form(); data = dict(form)
    except Exception: data = {}
    out = {}
    for tl, val in data.items():
        if not isinstance(val, dict): continue
        lri = val.get("last_read_id")
        if not lri: continue
        existing = qone("SELECT * FROM markers WHERE account_id=? AND timeline=?", user["id"], tl)
        v = (existing["version"] + 1) if existing else 1
        q("INSERT OR REPLACE INTO markers(account_id, timeline, last_read_id, version, updated_at) VALUES(?,?,?,?,?)",
          user["id"], tl, lri, v, now_iso())
        out[tl] = {"last_read_id": lri, "version": v, "updated_at": now_iso()}
    return JSONResponse(out)

# ---------- Web UI ----------
def _new_session(account_id):
    sid = new_token(32); csrf = new_token(24)
    q("INSERT INTO sessions(sid, account_id, csrf, created_at) VALUES(?,?,?,?)",
      sid, account_id, csrf, int(time.time()))
    return sid, csrf

def _set_session_cookie(resp, sid):
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax", secure=False, path="/", max_age=86400*30)
    return resp

async def web_index(request: Request):
    user = request.state.user
    rows = _timeline_rows("public", user["id"] if user else None, limit=20)
    statuses = [serial.status(r, user["id"] if user else None) for r in rows]
    statuses = [s for s in statuses if s]
    trending = qall("SELECT t.tag, COUNT(*) c FROM status_tags t JOIN statuses s ON s.id=t.status_id WHERE s.deleted=0 GROUP BY t.tag ORDER BY c DESC LIMIT 6")
    return html_response("index.html", request=request, statuses=statuses, trending=trending, tab="public")

async def web_public(request: Request):
    return await web_index(request)

async def web_home(request: Request):
    user = request.state.user
    if not user:
        return RedirectResponse("/login", status_code=302)
    rows = _timeline_rows("home", user["id"], limit=20)
    statuses = [serial.status(r, user["id"]) for r in rows]
    statuses = [s for s in statuses if s]
    trending = qall("SELECT t.tag, COUNT(*) c FROM status_tags t JOIN statuses s ON s.id=t.status_id WHERE s.deleted=0 GROUP BY t.tag ORDER BY c DESC LIMIT 6")
    return html_response("home.html", request=request, statuses=statuses, trending=trending, tab="home")

async def web_local(request: Request):
    user = request.state.user
    rows = _timeline_rows("local", user["id"] if user else None, limit=20)
    statuses = [serial.status(r, user["id"] if user else None) for r in rows]
    statuses = [s for s in statuses if s]
    trending = qall("SELECT t.tag, COUNT(*) c FROM status_tags t JOIN statuses s ON s.id=t.status_id WHERE s.deleted=0 GROUP BY t.tag ORDER BY c DESC LIMIT 6")
    return html_response("home.html", request=request, statuses=statuses, trending=trending, tab="local")

async def web_federated(request: Request):
    user = request.state.user
    rows = _timeline_rows("public", user["id"] if user else None, limit=20)
    statuses = [serial.status(r, user["id"] if user else None) for r in rows]
    statuses = [s for s in statuses if s]
    trending = qall("SELECT t.tag, COUNT(*) c FROM status_tags t JOIN statuses s ON s.id=t.status_id WHERE s.deleted=0 GROUP BY t.tag ORDER BY c DESC LIMIT 6")
    return html_response("home.html", request=request, statuses=statuses, trending=trending, tab="federated")

async def web_tag(request: Request):
    tag = request.path_params["hashtag"].lower()
    viewer = request.state.user["id"] if request.state.user else None
    rows = _timeline_rows("tag", viewer, tag=tag, limit=30)
    statuses = [serial.status(r, viewer) for r in rows]
    statuses = [s for s in statuses if s]
    return html_response("tag.html", request=request, statuses=statuses, tag=tag)

async def web_login(request: Request):
    if request.method == "GET":
        return html_response("login.html", request=request, next_url=request.query_params.get("next","/home"))
    form = await request.form()
    username = form.get("username","").strip()
    password = form.get("password","")
    nxt = form.get("next") or "/home"
    from .util import pwverify
    a = qone("SELECT * FROM accounts WHERE (username=? OR email=?) AND is_local=1", username, username)
    if not a or not pwverify(password, a["password_hash"] or ""):
        return html_response("login.html", request=request, error="Invalid credentials", next_url=nxt, status_code=401)
    sid, csrf = _new_session(a["id"])
    resp = RedirectResponse(nxt, status_code=302)
    return _set_session_cookie(resp, sid)

async def web_logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid: q("DELETE FROM sessions WHERE sid=?", sid)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

async def web_signup(request: Request):
    if request.method == "GET":
        return html_response("signup.html", request=request)
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    email = (form.get("email") or "").strip()
    if not username or not password or len(password) < 6:
        return html_response("signup.html", request=request, error="Invalid input", status_code=422)
    if not username.replace("_","").isalnum():
        return html_response("signup.html", request=request, error="Invalid username", status_code=422)
    if qone("SELECT 1 FROM accounts WHERE username=? AND domain IS NULL", username):
        return html_response("signup.html", request=request, error="Username taken", status_code=409)
    aid = actions.create_account(username, password=password, email=email)
    sid, _ = _new_session(aid)
    resp = RedirectResponse("/home", status_code=302)
    return _set_session_cookie(resp, sid)

async def web_profile(request: Request):
    username = request.path_params["username"]
    a = qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", username)
    if not a: raise HTTPException(404, "Account not found")
    rows = qall("SELECT * FROM statuses WHERE account_id=? AND deleted=0 ORDER BY id DESC LIMIT 20", a["id"])
    viewer = request.state.user["id"] if request.state.user else None
    statuses = [serial.status(r, viewer) for r in rows]
    statuses = [s for s in statuses if s]
    rel = serial.relationship(viewer, a["id"]) if viewer else None
    return html_response("profile.html", request=request, profile=serial.account(a, viewer),
                         statuses=statuses, rel=rel, raw=a)

async def web_users_legacy(request: Request):
    return RedirectResponse(f"/@{request.path_params['username']}", status_code=301)

async def web_status_page(request: Request):
    sid = request.path_params["id"]
    s = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", sid)
    if not s: raise HTTPException(404, "Status not found")
    viewer = request.state.user["id"] if request.state.user else None
    status_obj = serial.status(s, viewer)
    ancestors = []
    cur = s
    while cur and cur["in_reply_to_id"]:
        p = qone("SELECT * FROM statuses WHERE id=? AND deleted=0", cur["in_reply_to_id"])
        if not p: break
        ancestors.append(serial.status(p, viewer))
        cur = p
    ancestors.reverse()
    desc = []
    def walk(pid):
        for k in qall("SELECT * FROM statuses WHERE in_reply_to_id=? AND deleted=0 ORDER BY id ASC", pid):
            desc.append(serial.status(k, viewer)); walk(k["id"])
    walk(sid)
    return html_response("status.html", request=request, status=status_obj, ancestors=ancestors, descendants=desc)

async def web_notifications(request: Request):
    user = request.state.user
    if not user: return RedirectResponse("/login", status_code=302)
    rows = qall("SELECT * FROM notifications WHERE account_id=? ORDER BY id DESC LIMIT 50", user["id"])
    notifs = [serial.notification(r, user["id"]) for r in rows]
    q("UPDATE notifications SET read=1 WHERE account_id=?", user["id"])
    return html_response("notifications.html", request=request, notifications=notifs)

async def web_compose(request: Request):
    user = request.state.user
    if not user: return RedirectResponse("/login", status_code=302)
    form = await request.form()
    sess = request.state.session
    if not sess or form.get("_csrf") != sess["csrf"]:
        return HTMLResponse("CSRF failed", status_code=403)
    content = (form.get("status") or "").strip()
    visibility = form.get("visibility") or "public"
    spoiler = form.get("spoiler_text") or ""
    in_reply = form.get("in_reply_to_id")
    if not content: return RedirectResponse("/home", status_code=302)
    actions.create_status(user["id"], content=content, visibility=visibility,
                          in_reply_to_id=in_reply, spoiler_text=spoiler,
                          sensitive=bool(spoiler))
    nxt = form.get("next") or "/home"
    return RedirectResponse(nxt, status_code=302)

async def web_health(request: Request):
    return JSONResponse({"status": "ok"})

async def web_404(request: Request):
    return html_response("error.html", request=request, code=404, message="Not found", status_code=404)

async def web_settings(request: Request):
    user = request.state.user
    if not user: return RedirectResponse("/login", status_code=302)
    if request.method == "GET":
        return html_response("settings.html", request=request)
    sess = request.state.session
    form = await request.form()
    if not sess or form.get("_csrf") != sess["csrf"]:
        return HTMLResponse("CSRF failed", status_code=403)
    # Reuse api_update_credentials logic via direct DB
    upd = []; args = []
    if form.get("display_name"): upd.append("display_name=?"); args.append(form["display_name"])
    if "note" in form: upd.append("note=?"); args.append(form.get("note") or "")
    fields = []
    for i in range(4):
        n = form.get(f"fields_attributes[{i}][name]")
        v = form.get(f"fields_attributes[{i}][value]")
        if n or v: fields.append({"name": n or "", "value": v or "", "verified_at": None})
    if fields:
        upd.append("fields_json=?"); args.append(json.dumps(fields))
    av = form.get("avatar")
    if av and hasattr(av, "filename") and av.filename:
        raw = await av.read()
        ext = os.path.splitext(av.filename)[1] or ".png"
        fn = f"avatar_{user['id']}{ext}"
        fp = os.path.join(MEDIA_DIR, fn)
        with open(fp, "wb") as f: f.write(raw)
        upd.append("avatar=?"); args.append(f"/media/{fn}")
    hd = form.get("header")
    if hd and hasattr(hd, "filename") and hd.filename:
        raw = await hd.read()
        ext = os.path.splitext(hd.filename)[1] or ".png"
        fn = f"header_{user['id']}{ext}"
        fp = os.path.join(MEDIA_DIR, fn)
        with open(fp, "wb") as f: f.write(raw)
        upd.append("header=?"); args.append(f"/media/{fn}")
    if upd:
        args.append(user["id"])
        q(f"UPDATE accounts SET {', '.join(upd)} WHERE id=?", *args)
    return RedirectResponse(f"/@{user['username']}", status_code=302)


async def webfinger(request: Request):
    res = request.query_params.get("resource","")
    if res.startswith("acct:"):
        acct = res[5:]
        u = acct.split("@",1)[0]
        a = qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", u)
        if a:
            return JSONResponse({"subject": f"acct:{u}@{DOMAIN}",
                "links": [{"rel": "http://webfinger.net/rel/profile-page", "type":"text/html", "href": f"https://{DOMAIN}/@{u}"},
                          {"rel": "self", "type": "application/activity+json", "href": f"https://{DOMAIN}/users/{u}"}]})
    return api_error(404, "not_found")

async def nodeinfo_root(request: Request):
    return JSONResponse({"links":[{"rel":"http://nodeinfo.diaspora.software/ns/schema/2.0","href": f"https://{DOMAIN}/nodeinfo/2.0"}]})

async def nodeinfo_2(request: Request):
    users = qone("SELECT COUNT(*) c FROM accounts WHERE is_local=1")["c"]
    statuses = qone("SELECT COUNT(*) c FROM statuses WHERE deleted=0 AND is_local=1")["c"]
    return JSONResponse({"version": "2.0", "software": {"name": "chirp", "version": "1.0"},
        "protocols": ["activitypub"], "services": {"inbound": [], "outbound": []},
        "openRegistrations": True, "usage": {"users": {"total": users}, "localPosts": statuses}, "metadata": {}})

routes = [
    Route("/_health", web_health),
    Route("/.well-known/webfinger", webfinger),
    Route("/.well-known/nodeinfo", nodeinfo_root),
    Route("/nodeinfo/2.0", nodeinfo_2),
    Route("/oauth/authorize", oauth_authorize, methods=["GET","POST"]),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Route("/oauth/revoke", oauth_revoke, methods=["POST"]),
    Route("/oauth/introspect", oauth_introspect, methods=["POST"]),
    Route("/api/v1/instance", api_instance),
    Route("/api/v2/instance", api_instance_v2),
    Route("/api/v1/apps", oauth_apps, methods=["POST"]),
    Route("/api/v1/apps/verify_credentials", api_apps_verify),
    Route("/api/v1/accounts/verify_credentials", api_verify_credentials),
    Route("/api/v1/accounts/update_credentials", api_update_credentials, methods=["PATCH","POST"]),
    Route("/api/v1/accounts/lookup", api_account_lookup),
    Route("/api/v1/accounts/search", api_account_search),
    Route("/api/v1/accounts/relationships", api_relationships),
    Route("/api/v1/accounts/{id}", api_account),
    Route("/api/v1/accounts/{id}/statuses", api_account_statuses),
    Route("/api/v1/accounts/{id}/followers", api_followers),
    Route("/api/v1/accounts/{id}/following", api_following),
    Route("/api/v1/accounts/{id}/follow", api_follow, methods=["POST"]),
    Route("/api/v1/accounts/{id}/unfollow", api_unfollow, methods=["POST"]),
    Route("/api/v1/accounts/{id}/block", api_block, methods=["POST"]),
    Route("/api/v1/accounts/{id}/unblock", api_unblock, methods=["POST"]),
    Route("/api/v1/accounts/{id}/mute", api_mute, methods=["POST"]),
    Route("/api/v1/accounts/{id}/unmute", api_unmute, methods=["POST"]),
    Route("/api/v1/blocks", api_blocks),
    Route("/api/v1/mutes", api_mutes),
    Route("/api/v1/favourites", api_favourites),
    Route("/api/v1/bookmarks", api_bookmarks),
    Route("/api/v1/statuses", api_statuses_post, methods=["POST"]),
    Route("/api/v1/statuses/{id}", api_status_get, methods=["GET"]),
    Route("/api/v1/statuses/{id}", api_status_delete, methods=["DELETE"]),
    Route("/api/v1/statuses/{id}", api_status_edit, methods=["PUT"]),
    Route("/api/v1/statuses/{id}/history", api_status_history),
    Route("/api/v1/statuses/{id}/source", api_status_source),
    Route("/api/v1/statuses/{id}/context", api_status_context),
    Route("/api/v1/statuses/{id}/favourite", api_favourite, methods=["POST"]),
    Route("/api/v1/statuses/{id}/unfavourite", api_unfavourite, methods=["POST"]),
    Route("/api/v1/statuses/{id}/reblog", api_reblog, methods=["POST"]),
    Route("/api/v1/statuses/{id}/unreblog", api_unreblog, methods=["POST"]),
    Route("/api/v1/statuses/{id}/bookmark", api_bookmark, methods=["POST"]),
    Route("/api/v1/statuses/{id}/unbookmark", api_unbookmark, methods=["POST"]),
    Route("/api/v1/statuses/{id}/pin", api_pin, methods=["POST"]),
    Route("/api/v1/statuses/{id}/unpin", api_unpin, methods=["POST"]),
    Route("/api/v1/statuses/{id}/favourited_by", api_status_favourited_by),
    Route("/api/v1/statuses/{id}/reblogged_by", api_status_reblogged_by),
    Route("/api/v1/timelines/home", api_tl_home),
    Route("/api/v1/timelines/public", api_tl_public),
    Route("/api/v1/timelines/tag/{hashtag}", api_tl_tag),
    Route("/api/v1/timelines/list/{id}", api_tl_list),
    Route("/api/v1/notifications", api_notifications),
    Route("/api/v1/notifications/clear", api_notif_clear, methods=["POST"]),
    Route("/api/v1/notifications/{id}", api_notif_get),
    Route("/api/v1/lists", api_lists, methods=["GET","POST"]),
    Route("/api/v1/lists/{id}", api_list_get, methods=["GET"]),
    Route("/api/v1/lists/{id}", api_list_update, methods=["PUT"]),
    Route("/api/v1/lists/{id}", api_list_delete, methods=["DELETE"]),
    Route("/api/v1/lists/{id}/accounts", api_list_accounts, methods=["GET","POST","DELETE"]),
    Route("/api/v1/search", api_search),
    Route("/api/v2/search", api_search),
    Route("/api/v1/trends", api_trends_tags),
    Route("/api/v1/trends/tags", api_trends_tags),
    Route("/api/v1/media", api_media_post, methods=["POST"]),
    Route("/api/v2/media", api_media_post, methods=["POST"]),
    Route("/api/v1/media/{id}", api_media_update, methods=["PUT","GET"]),
    Route("/media/{name}", media_file),
    Route("/api/v1/polls/{id}", api_poll_get),
    Route("/api/v1/polls/{id}/votes", api_poll_vote, methods=["POST"]),
    Route("/api/v1/reports", api_reports, methods=["POST"]),
    Route("/api/v1/markers", api_markers_get, methods=["GET"]),
    Route("/api/v1/markers", api_markers_post, methods=["POST"]),
    Route("/api/v1/streaming", streaming_sse),
    Route("/api/v1/streaming/user", streaming_sse),
    Route("/_admin/queues", admin_queues),
    Route("/_admin/audit", admin_audit),
    Route("/_admin/reports", admin_reports),
    Route("/", web_index),
    Route("/public", web_public),
    Route("/home", web_home),
    Route("/local", web_local),
    Route("/federated", web_federated),
    Route("/settings", web_settings, methods=["GET","POST"]),
    Route("/login", web_login, methods=["GET","POST"]),
    Route("/logout", web_logout, methods=["GET","POST"]),
    Route("/signup", web_signup, methods=["GET","POST"]),
    Route("/notifications", web_notifications),
    Route("/compose", web_compose, methods=["POST"]),
    Route("/tags/{hashtag}", web_tag),
    Route("/users/{username}", web_users_legacy),
    Route("/@{username}", web_profile),
    Route("/statuses/{id}", web_status_page),
    Route("/@{username}/{id}", web_status_page),
    Mount("/static", app=StaticFiles(directory="/app/static"), name="static"),
]

async def not_found(request: Request, exc):
    if request.url.path.startswith("/api") or request.url.path.startswith("/oauth"):
        return api_error(404, "not_found")
    return html_response("error.html", request=request, code=404, message="Not found", status_code=404)

async def server_error(request: Request, exc):
    if request.url.path.startswith("/api") or request.url.path.startswith("/oauth"):
        return api_error(500, "internal_error")
    return html_response("error.html", request=request, code=500, message="Server error", status_code=500)

db.init()
app = Starlette(
    debug=False,
    routes=routes,
    middleware=[Middleware(SecurityMiddleware)],
    exception_handlers={404: not_found, 500: server_error, HTTPException: lambda r,e: not_found(r,e) if e.status_code==404 else api_error(e.status_code, str(e.detail)) if r.url.path.startswith("/api") else html_response("error.html", request=r, code=e.status_code, message=str(e.detail), status_code=e.status_code)},
)
