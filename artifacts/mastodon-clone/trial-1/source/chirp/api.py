"""Mastodon-compatible REST API endpoints."""
import hashlib
import json
import os
import secrets
import time
from typing import Optional

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import auth, db, posting, serializers, timelines, events
from .util import (
    BASE_URL,
    INSTANCE_DOMAIN,
    env_int,
    extract_tags,
    gen_token,
    link_header,
    now_iso,
    parse_iso,
    parse_qs_first,
)


# ---- helpers ----

def json_response(data, status=200, headers=None) -> JSONResponse:
    h = {"Cache-Control": "no-store"}
    if headers:
        h.update(headers)
    return JSONResponse(data, status_code=status, headers=h)


def error(msg: str, status=400) -> JSONResponse:
    return json_response({"error": msg}, status=status)


def require_token(request: Request, scope: Optional[str] = None) -> dict:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    tok = h.split(" ", 1)[1].strip()
    info = auth.lookup_token(tok)
    if not info:
        raise HTTPException(401, "Invalid token")
    if scope and not auth.scope_satisfies(info["scopes"], scope):
        raise HTTPException(403, f"Required scope: {scope}")
    return info


def maybe_token(request: Request) -> Optional[dict]:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return auth.lookup_token(h.split(" ", 1)[1].strip())


def _set_nested(out: dict, key: str, value):
    """Handle Rails-style bracketed form keys like home[last_read_id]."""
    if "[" not in key:
        out[key] = value
        return
    head, _, rest = key.partition("[")
    parts = [head]
    cur = rest
    while cur:
        i = cur.find("]")
        if i < 0:
            parts.append(cur)
            break
        parts.append(cur[:i])
        cur = cur[i + 1:]
        if cur.startswith("["):
            cur = cur[1:]
    target = out
    for p in parts[:-1]:
        nxt = target.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            target[p] = nxt
        target = nxt
    last = parts[-1]
    if last == "":  # array, e.g. account_ids[]
        existing = target.get(parts[-2])
        if not isinstance(existing, list):
            existing = []
            target[parts[-2]] = existing
        existing.append(value)
    else:
        target[last] = value


async def get_form_or_json(request: Request) -> dict:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            return await request.json()
        except Exception:
            return {}
    if ctype.startswith("multipart/form-data") or ctype == "application/x-www-form-urlencoded":
        form = await request.form()
        out = {}
        for k in form:
            vals = form.getlist(k)
            if k.endswith("[]"):
                # array key
                out.setdefault(k[:-2], []).extend(vals)
            elif "[" in k:
                _set_nested(out, k, vals[0] if len(vals) == 1 else list(vals))
            elif len(vals) > 1:
                out[k] = list(vals)
            else:
                out[k] = vals[0]
        return out
    try:
        return await request.json()
    except Exception:
        return {}


# ---- /_health ----

async def health(request: Request):
    return json_response({"status": "ok"})


# ---- well-known + nodeinfo ----

async def well_known_webfinger(request: Request):
    res = request.query_params.get("resource", "")
    if not res.startswith("acct:"):
        return error("Invalid resource", 400)
    acct = res[5:]
    if "@" in acct:
        username, domain = acct.split("@", 1)
        if domain != INSTANCE_DOMAIN:
            return error("Not local", 404)
    else:
        username = acct
    a = auth.find_account_by_username(username)
    if not a:
        return error("Account not found", 404)
    out = {
        "subject": f"acct:{username}@{INSTANCE_DOMAIN}",
        "aliases": [f"{BASE_URL}/@{username}"],
        "links": [
            {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html", "href": f"{BASE_URL}/@{username}"},
        ],
    }
    return json_response(out)


async def well_known_host_meta(request: Request):
    body = f'<?xml version="1.0" encoding="UTF-8"?>\n<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0"><Link rel="lrdd" template="{BASE_URL}/.well-known/webfinger?resource={{uri}}"/></XRD>'
    return Response(body, media_type="application/xrd+xml")


async def nodeinfo_well_known(request: Request):
    return json_response({
        "links": [
            {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0", "href": f"{BASE_URL}/nodeinfo/2.0"}
        ]
    })


async def nodeinfo_v2(request: Request):
    user_count_row = db.query_one("SELECT COUNT(*) AS c FROM accounts WHERE is_local = 1")
    status_count_row = db.query_one("SELECT COUNT(*) AS c FROM statuses WHERE deleted = 0")
    return json_response({
        "version": "2.0",
        "software": {"name": "chirp", "version": "0.1.0"},
        "protocols": ["activitypub"],
        "services": {"outbound": [], "inbound": []},
        "openRegistrations": True,
        "usage": {
            "users": {"total": int(user_count_row["c"]) if user_count_row else 0},
            "localPosts": int(status_count_row["c"]) if status_count_row else 0,
        },
        "metadata": {"nodeName": "Chirp"},
    })


# ---- instance ----

async def instance_v1(request: Request):
    user_count = db.query_one("SELECT COUNT(*) AS c FROM accounts WHERE is_local = 1")["c"]
    status_count = db.query_one("SELECT COUNT(*) AS c FROM statuses WHERE deleted = 0")["c"]
    return json_response({
        "uri": INSTANCE_DOMAIN,
        "title": "Chirp",
        "short_description": "A small Chirp instance.",
        "description": "Chirp — a small self-hosted social server.",
        "email": "admin@" + INSTANCE_DOMAIN,
        "version": "4.2.0+chirp-0.1",
        "urls": {"streaming_api": f"{BASE_URL}".replace("http", "ws", 1)},
        "stats": {
            "user_count": int(user_count),
            "status_count": int(status_count),
            "domain_count": 1,
        },
        "thumbnail": f"{BASE_URL}/static/header.svg",
        "languages": ["en"],
        "registrations": True,
        "approval_required": False,
        "invites_enabled": False,
        "configuration": {
            "statuses": {"max_characters": 500, "max_media_attachments": 4, "characters_reserved_per_url": 23},
            "media_attachments": {"supported_mime_types": ["image/jpeg", "image/png", "image/gif", "image/webp"], "image_size_limit": 10485760, "image_matrix_limit": 16777216, "video_size_limit": 41943040, "video_frame_rate_limit": 60, "video_matrix_limit": 2304000},
            "polls": {"max_options": 4, "max_characters_per_option": 50, "min_expiration": 300, "max_expiration": 2629746},
        },
        "contact_account": None,
    })


async def instance_v2(request: Request):
    user_count = db.query_one("SELECT COUNT(*) AS c FROM accounts WHERE is_local = 1")["c"]
    status_count = db.query_one("SELECT COUNT(*) AS c FROM statuses WHERE deleted = 0")["c"]
    return json_response({
        "domain": INSTANCE_DOMAIN,
        "title": "Chirp",
        "version": "4.2.0+chirp-0.1",
        "source_url": "",
        "description": "Chirp — a small self-hosted social server.",
        "usage": {"users": {"active_month": int(user_count)}},
        "thumbnail": {"url": f"{BASE_URL}/static/header.svg"},
        "languages": ["en"],
        "configuration": {
            "urls": {"streaming": f"{BASE_URL}".replace("http", "ws", 1)},
            "statuses": {"max_characters": 500, "max_media_attachments": 4, "characters_reserved_per_url": 23},
            "media_attachments": {"supported_mime_types": ["image/jpeg", "image/png", "image/gif", "image/webp"]},
            "polls": {"max_options": 4, "max_characters_per_option": 50},
            "translation": {"enabled": False},
        },
        "registrations": {"enabled": True, "approval_required": False},
        "contact": {"email": "admin@" + INSTANCE_DOMAIN, "account": None},
        "rules": [],
    })


async def instance_peers(request: Request):
    return json_response([])


async def instance_activity(request: Request):
    return json_response([])


# ---- apps ----

async def apps_create(request: Request):
    data = await get_form_or_json(request)
    name = data.get("client_name") or "Chirp App"
    redirect_uris = data.get("redirect_uris") or "urn:ietf:wg:oauth:2.0:oob"
    scopes = data.get("scopes") or "read"
    website = data.get("website")
    app = auth.create_app(name, redirect_uris, scopes, website)
    return json_response(serializers.app_dict(app, full=True))


async def apps_verify(request: Request):
    info = require_token(request)
    if info.get("app_id"):
        app = db.query_one("SELECT * FROM apps WHERE id = ?", (info["app_id"],))
        if app:
            return json_response(serializers.app_dict(dict(app)))
    return json_response({"name": "chirp", "website": None, "vapid_key": ""})


# ---- OAuth ----

async def oauth_authorize_get(request: Request):
    from .web import _ctx, _render, _viewer
    qp = dict(request.query_params)
    client_id = qp.get("client_id", "")
    redirect_uri = qp.get("redirect_uri", "")
    scope = qp.get("scope", "read")
    response_type = qp.get("response_type", "code")
    if not client_id or not redirect_uri:
        return error("missing parameters", 400)
    app = auth.find_app_by_client(client_id)
    if not app:
        return error("invalid client", 400)
    viewer = _viewer(request)
    if not viewer:
        # bounce to login with next preserving query
        from urllib.parse import urlencode
        next_url = "/oauth/authorize?" + urlencode(qp)
        return RedirectResponse(f"/login?next={next_url}", status_code=303)
    return _render("oauth_authorize.html", _ctx(request,
        app=app, scope_list=auth.normalize_scopes(scope),
        next_url=request.url.path + "?" + str(request.url.query),
        form=qp, error=None,
    ))


async def oauth_authorize_post(request: Request):
    from .web import _check_csrf, _viewer
    form = await request.form()
    if not _check_csrf(request, form.get("csrf", "")):
        return error("CSRF", 403)
    viewer = _viewer(request)
    if not viewer:
        return error("Login required", 401)
    if form.get("approve") != "1":
        return RedirectResponse("/", status_code=303)
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    scope = form.get("scope", "read")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "") or None
    code_challenge_method = form.get("code_challenge_method", "") or None
    app = auth.find_app_by_client(client_id)
    if not app:
        return error("invalid client", 400)
    code = auth.issue_authorization_code(
        app["id"], viewer["id"], scope, redirect_uri, code_challenge, code_challenge_method,
    )
    if redirect_uri == "urn:ietf:wg:oauth:2.0:oob":
        from .web import _render, _ctx
        return _render("oauth_authorize.html", _ctx(request,
            app=app, scope_list=auth.normalize_scopes(scope),
            next_url="", form={"redirect_uri": redirect_uri},
            error=f"Your authorization code: {code}",
        ))
    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}code={code}"
    if state:
        target += f"&state={state}"
    return RedirectResponse(target, status_code=303)


async def oauth_token(request: Request):
    data = await get_form_or_json(request)
    grant = data.get("grant_type", "")
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")
    app = auth.find_app_by_client(client_id) if client_id else None
    if grant == "authorization_code":
        if not app:
            return error("invalid_client", 401)
        code = data.get("code", "")
        redirect_uri = data.get("redirect_uri", "")
        code_verifier = data.get("code_verifier")
        consumed = auth.consume_authorization_code(code, redirect_uri, code_verifier)
        if not consumed:
            return error("invalid_grant", 400)
        if not code_verifier and app["client_secret"] != client_secret:
            return error("invalid_client", 401)
        scopes = auth.normalize_scopes(consumed["scopes"])
        tok = auth.issue_token(app["id"], consumed["account_id"], scopes, grant_type="authorization_code")
        return json_response({
            "access_token": tok,
            "token_type": "Bearer",
            "scope": " ".join(scopes),
            "created_at": int(time.time()),
        })
    if grant == "client_credentials":
        if not app or app["client_secret"] != client_secret:
            return error("invalid_client", 401)
        scopes = auth.normalize_scopes(data.get("scope", "read"))
        tok = auth.issue_token(app["id"], None, scopes, grant_type="client_credentials")
        return json_response({
            "access_token": tok,
            "token_type": "Bearer",
            "scope": " ".join(scopes),
            "created_at": int(time.time()),
        })
    if grant == "password":
        if not app or app["client_secret"] != client_secret:
            return error("invalid_client", 401)
        username = data.get("username", "")
        password = data.get("password", "")
        acct = auth.authenticate_local(username, password)
        if not acct:
            return error("invalid_grant", 400)
        scopes = auth.normalize_scopes(data.get("scope", "read"))
        tok = auth.issue_token(app["id"], acct["id"], scopes, grant_type="password")
        return json_response({
            "access_token": tok,
            "token_type": "Bearer",
            "scope": " ".join(scopes),
            "created_at": int(time.time()),
        })
    return error("unsupported_grant_type", 400)


async def oauth_revoke(request: Request):
    data = await get_form_or_json(request)
    tok = data.get("token", "")
    auth.revoke_token(tok)
    return json_response({})


async def oauth_introspect(request: Request):
    """RFC 7662 introspection. Authentication via client credentials in body or basic auth."""
    data = await get_form_or_json(request)
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not client_id:
        # basic
        ah = request.headers.get("authorization", "")
        if ah.lower().startswith("basic "):
            import base64
            try:
                raw = base64.b64decode(ah.split(" ", 1)[1]).decode()
                if ":" in raw:
                    client_id, client_secret = raw.split(":", 1)
            except Exception:
                pass
    if not client_id:
        return error("invalid_client", 401)
    app = auth.find_app_by_client(client_id)
    if not app or app["client_secret"] != client_secret:
        return error("invalid_client", 401)
    tok = data.get("token", "")
    info = auth.lookup_token(tok)
    if not info:
        return json_response({"active": False})
    out = {
        "active": True,
        "scope": " ".join(info["scopes"]),
        "client_id": app["client_id"],
        "token_type": "Bearer",
        "exp": None,
        "iat": int(parse_iso(info["created_at"])),
    }
    if info.get("account_id"):
        a = auth.find_account_by_id(info["account_id"])
        if a:
            out["sub"] = str(a["id"])
            out["username"] = a["username"]
    return json_response(out)


# ---- Accounts ----

async def accounts_verify_credentials(request: Request):
    info = require_token(request, "read")
    if not info.get("account_id"):
        return error("user token required", 401)
    a = auth.find_account_by_id(info["account_id"])
    return json_response(serializers.account_dict(a, viewer_id=a["id"]))


async def accounts_update_credentials(request: Request):
    info = require_token(request, "write")
    if not info.get("account_id"):
        return error("user token required", 401)
    data = await get_form_or_json(request)
    a = auth.find_account_by_id(info["account_id"])
    fields = []
    for i in range(4):
        n = data.get(f"fields_attributes[{i}][name]")
        v = data.get(f"fields_attributes[{i}][value]")
        if n is not None or v is not None:
            fields.append({"name": n or "", "value": v or "", "verified_at": None})
    fields_json = json.dumps(fields) if fields else a.get("fields") or "[]"
    db.execute(
        "UPDATE accounts SET display_name = COALESCE(?, display_name), note = COALESCE(?, note), fields = ? WHERE id = ?",
        (data.get("display_name"), data.get("note"), fields_json, info["account_id"]),
    )
    a = auth.find_account_by_id(info["account_id"])
    return json_response(serializers.account_dict(a, viewer_id=a["id"]))


async def accounts_get(request: Request):
    aid = int(request.path_params["id"])
    a = auth.find_account_by_id(aid)
    if not a:
        return error("not found", 404)
    info = maybe_token(request)
    return json_response(serializers.account_dict(a, viewer_id=info["account_id"] if info else None))


async def accounts_lookup(request: Request):
    acct = request.query_params.get("acct", "")
    a = auth.find_account_by_acct(acct) or auth.find_account_by_username(acct.lstrip("@"))
    if not a:
        return error("not found", 404)
    return json_response(serializers.account_dict(a))


async def accounts_search(request: Request):
    q = request.query_params.get("q", "").lstrip("@").strip()
    limit = min(int(request.query_params.get("limit", 10) or 10), 40)
    if not q:
        return json_response([])
    rows = db.query_all(
        "SELECT * FROM accounts WHERE username LIKE ? OR display_name LIKE ? OR acct LIKE ? LIMIT ?",
        (f"%{q}%", f"%{q}%", f"%{q}%", limit),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def accounts_relationships(request: Request):
    info = require_token(request, "read")
    ids_qs = request.query_params.getlist("id[]") or request.query_params.getlist("id")
    out = []
    for sid in ids_qs:
        try:
            tid = int(sid)
        except ValueError:
            continue
        out.append(serializers.relationship_dict(info["account_id"], tid))
    return json_response(out)


async def accounts_statuses(request: Request):
    aid = int(request.path_params["id"])
    info = maybe_token(request)
    paging = timelines.parse_paging(dict(request.query_params))
    rows = timelines.account_statuses(
        aid, info["account_id"] if info else None, paging,
        only_media=request.query_params.get("only_media") in ("true", "1"),
        exclude_replies=request.query_params.get("exclude_replies") in ("true", "1"),
        exclude_reblogs=request.query_params.get("exclude_reblogs") in ("true", "1"),
        pinned=request.query_params.get("pinned") in ("true", "1"),
    )
    out = [serializers.status_dict(r, info["account_id"] if info else None) for r in rows]
    return json_response([s for s in out if s])


async def accounts_followers(request: Request):
    aid = int(request.path_params["id"])
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN follows f ON f.account_id = a.id WHERE f.target_id = ? ORDER BY f.id DESC LIMIT 40",
        (aid,),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def accounts_following(request: Request):
    aid = int(request.path_params["id"])
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN follows f ON f.target_id = a.id WHERE f.account_id = ? ORDER BY f.id DESC LIMIT 40",
        (aid,),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def accounts_follow(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    posting.follow(info["account_id"], aid)
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def accounts_unfollow(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    posting.unfollow(info["account_id"], aid)
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def accounts_block(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    if not db.query_one("SELECT 1 FROM blocks WHERE account_id = ? AND target_id = ?", (info["account_id"], aid)):
        try:
            db.execute("INSERT INTO blocks (account_id, target_id, created_at) VALUES (?, ?, ?)",
                       (info["account_id"], aid, now_iso()))
        except Exception:
            pass
        posting.unfollow(info["account_id"], aid)
        posting.unfollow(aid, info["account_id"])
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def accounts_unblock(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    db.execute("DELETE FROM blocks WHERE account_id = ? AND target_id = ?", (info["account_id"], aid))
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def accounts_mute(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    if not db.query_one("SELECT 1 FROM mutes WHERE account_id = ? AND target_id = ?", (info["account_id"], aid)):
        try:
            db.execute("INSERT INTO mutes (account_id, target_id, created_at) VALUES (?, ?, ?)",
                       (info["account_id"], aid, now_iso()))
        except Exception:
            pass
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def accounts_unmute(request: Request):
    info = require_token(request, "follow")
    aid = int(request.path_params["id"])
    db.execute("DELETE FROM mutes WHERE account_id = ? AND target_id = ?", (info["account_id"], aid))
    return json_response(serializers.relationship_dict(info["account_id"], aid))


async def blocks_list(request: Request):
    info = require_token(request, "follow")
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN blocks b ON b.target_id = a.id WHERE b.account_id = ? ORDER BY b.created_at DESC LIMIT 40",
        (info["account_id"],),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def mutes_list(request: Request):
    info = require_token(request, "follow")
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN mutes m ON m.target_id = a.id WHERE m.account_id = ? ORDER BY m.created_at DESC LIMIT 40",
        (info["account_id"],),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


# ---- Statuses ----

async def statuses_create(request: Request):
    info = require_token(request, "write")
    if not info.get("account_id"):
        return error("user token required", 401)
    data = await get_form_or_json(request)
    text = data.get("status") or ""
    visibility = data.get("visibility", "public")
    in_reply_to = data.get("in_reply_to_id")
    spoiler = data.get("spoiler_text") or ""
    sensitive = bool(data.get("sensitive"))
    language = data.get("language") or "en"
    media_ids = data.get("media_ids") or []
    if isinstance(media_ids, str):
        media_ids = [media_ids]
    media_ids = [int(x) for x in media_ids if str(x).isdigit()]
    poll = data.get("poll")
    idem = request.headers.get("idempotency-key")
    s = posting.create_status(
        info["account_id"], text, visibility=visibility,
        in_reply_to_id=int(in_reply_to) if in_reply_to else None,
        spoiler_text=spoiler, sensitive=sensitive, language=language,
        media_ids=media_ids, poll=poll if isinstance(poll, dict) else None,
        app_id=info.get("app_id"), idempotency_key=idem,
    )
    sd = serializers.status_dict(s, info["account_id"])
    if sd:
        events.publish(None, "public", "update", json.dumps(sd))
    return json_response(sd)


async def statuses_get(request: Request):
    sid = int(request.path_params["id"])
    info = maybe_token(request)
    row = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (sid,))
    if not row:
        return error("not found", 404)
    s = dict(row)
    if not timelines.visible_to(s, info["account_id"] if info else None):
        return error("not found", 404)
    sd = serializers.status_dict(s, info["account_id"] if info else None)
    body = json.dumps(sd, separators=(",", ":")).encode()
    etag = hashlib.sha256(body).hexdigest()[:16]
    if request.headers.get("if-none-match") == f'"{etag}"':
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})
    return JSONResponse(sd, headers={"ETag": f'"{etag}"', "Cache-Control": "no-cache"})


async def statuses_delete(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.delete_status(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    sd = serializers.status_dict(s, info["account_id"])
    if sd:
        sd["text"] = s.get("text", "")
    return json_response(sd or {})


async def statuses_edit(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    data = await get_form_or_json(request)
    s = posting.edit_status(
        info["account_id"], sid,
        data.get("status", ""),
        spoiler_text=data.get("spoiler_text", ""),
        sensitive=bool(data.get("sensitive")),
        language=data.get("language", "en"),
    )
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_history(request: Request):
    sid = int(request.path_params["id"])
    rows = db.query_all("SELECT * FROM status_history WHERE status_id = ? ORDER BY id ASC", (sid,))
    main = db.query_one("SELECT * FROM statuses WHERE id = ?", (sid,))
    if not main:
        return error("not found", 404)
    out = []
    for r in rows:
        out.append({
            "content": r["content"],
            "spoiler_text": r["spoiler_text"] or "",
            "sensitive": bool(r["sensitive"]),
            "created_at": r["created_at"],
            "account": serializers.account_dict(dict(db.query_one("SELECT * FROM accounts WHERE id = ?", (main["account_id"],)))),
            "media_attachments": [],
            "emojis": [],
        })
    out.append({
        "content": main["text"] or "",
        "spoiler_text": main["spoiler_text"] or "",
        "sensitive": bool(main["sensitive"]),
        "created_at": main["edited_at"] or main["created_at"],
        "account": serializers.account_dict(dict(db.query_one("SELECT * FROM accounts WHERE id = ?", (main["account_id"],)))),
        "media_attachments": [],
        "emojis": [],
    })
    return json_response(out)


async def statuses_source(request: Request):
    sid = int(request.path_params["id"])
    info = require_token(request, "read")
    s = db.query_one("SELECT * FROM statuses WHERE id = ? AND account_id = ?", (sid, info["account_id"]))
    if not s:
        return error("not found", 404)
    return json_response({
        "id": str(s["id"]),
        "text": s["text"] or "",
        "spoiler_text": s["spoiler_text"] or "",
    })


async def statuses_context(request: Request):
    sid = int(request.path_params["id"])
    info = maybe_token(request)
    viewer_id = info["account_id"] if info else None
    main = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (sid,))
    if not main:
        return error("not found", 404)
    ancestors = []
    cur = dict(main)
    while cur.get("in_reply_to_id"):
        p = db.query_one("SELECT * FROM statuses WHERE id = ? AND deleted = 0", (cur["in_reply_to_id"],))
        if not p:
            break
        ancestors.append(dict(p))
        cur = dict(p)
    ancestors.reverse()
    desc = db.query_all("SELECT * FROM statuses WHERE in_reply_to_id = ? AND deleted = 0 ORDER BY id", (sid,))
    return json_response({
        "ancestors": [serializers.status_dict(a, viewer_id) for a in ancestors if a],
        "descendants": [serializers.status_dict(dict(d), viewer_id) for d in desc],
    })


async def statuses_favourite(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.favourite(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_unfavourite(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.unfavourite(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_reblog(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    rb = posting.reblog(info["account_id"], sid)
    if not rb:
        return error("not found", 404)
    return json_response(serializers.status_dict(rb, info["account_id"]))


async def statuses_unreblog(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.unreblog(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_bookmark(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.bookmark(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_unbookmark(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = posting.unbookmark(info["account_id"], sid)
    if not s:
        return error("not found", 404)
    return json_response(serializers.status_dict(s, info["account_id"]))


async def statuses_pin(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    s = db.query_one("SELECT * FROM statuses WHERE id = ? AND account_id = ?", (sid, info["account_id"]))
    if not s:
        return error("not found", 404)
    try:
        db.execute("INSERT INTO pins (account_id, status_id, created_at) VALUES (?, ?, ?)",
                   (info["account_id"], sid, now_iso()))
    except Exception:
        pass
    return json_response(serializers.status_dict(dict(s), info["account_id"]))


async def statuses_unpin(request: Request):
    info = require_token(request, "write")
    sid = int(request.path_params["id"])
    db.execute("DELETE FROM pins WHERE account_id = ? AND status_id = ?", (info["account_id"], sid))
    s = db.query_one("SELECT * FROM statuses WHERE id = ?", (sid,))
    return json_response(serializers.status_dict(dict(s), info["account_id"]) if s else {})


async def statuses_favourited_by(request: Request):
    sid = int(request.path_params["id"])
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN favourites f ON f.account_id = a.id WHERE f.status_id = ?", (sid,)
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def statuses_reblogged_by(request: Request):
    sid = int(request.path_params["id"])
    rows = db.query_all(
        "SELECT DISTINCT a.* FROM accounts a JOIN statuses s ON s.account_id = a.id WHERE s.reblog_of_id = ? AND s.deleted = 0",
        (sid,),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


# ---- timelines ----

def _paged_link_header(request: Request, results: list[dict], path: str) -> Optional[str]:
    if not results:
        return None
    ids = [int(s["id"]) for s in results]
    next_url = f"{BASE_URL}{path}?max_id={min(ids)}"
    prev_url = f"{BASE_URL}{path}?min_id={max(ids)}"
    return link_header(prev_url, next_url)


async def timeline_home(request: Request):
    info = require_token(request, "read")
    paging = timelines.parse_paging(dict(request.query_params))
    rows = timelines.home_timeline(info["account_id"], paging)
    out = [s for s in (serializers.status_dict(r, info["account_id"]) for r in rows) if s]
    headers = {}
    lh = _paged_link_header(request, out, "/api/v1/timelines/home")
    if lh: headers["Link"] = lh
    return json_response(out, headers=headers)


async def timeline_public(request: Request):
    info = maybe_token(request)
    qp = dict(request.query_params)
    paging = timelines.parse_paging(qp)
    local = qp.get("local") in ("1", "true")
    remote = qp.get("remote") in ("1", "true")
    rows = timelines.public_timeline(info["account_id"] if info else None, paging, local=local, remote=remote)
    out = [s for s in (serializers.status_dict(r, info["account_id"] if info else None) for r in rows) if s]
    headers = {}
    lh = _paged_link_header(request, out, "/api/v1/timelines/public")
    if lh: headers["Link"] = lh
    return json_response(out, headers=headers)


async def timeline_tag(request: Request):
    name = request.path_params["name"]
    info = maybe_token(request)
    paging = timelines.parse_paging(dict(request.query_params))
    rows = timelines.hashtag_timeline(name, paging)
    out = [s for s in (serializers.status_dict(r, info["account_id"] if info else None) for r in rows) if s]
    headers = {}
    lh = _paged_link_header(request, out, f"/api/v1/timelines/tag/{name}")
    if lh: headers["Link"] = lh
    return json_response(out, headers=headers)


async def timeline_list(request: Request):
    info = require_token(request, "read")
    lid = int(request.path_params["id"])
    lst = db.query_one("SELECT * FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"]))
    if not lst:
        return error("not found", 404)
    paging = timelines.parse_paging(dict(request.query_params))
    rows = timelines.list_timeline(lid, info["account_id"], paging)
    out = [s for s in (serializers.status_dict(r, info["account_id"]) for r in rows) if s]
    return json_response(out)


# ---- favourites/bookmarks ----

async def favourites_list(request: Request):
    info = require_token(request, "read")
    rows = db.query_all(
        "SELECT s.* FROM statuses s JOIN favourites f ON f.status_id = s.id WHERE f.account_id = ? AND s.deleted = 0 ORDER BY f.created_at DESC LIMIT 40",
        (info["account_id"],),
    )
    return json_response([serializers.status_dict(dict(r), info["account_id"]) for r in rows])


async def bookmarks_list(request: Request):
    info = require_token(request, "read")
    rows = db.query_all(
        "SELECT s.* FROM statuses s JOIN bookmarks b ON b.status_id = s.id WHERE b.account_id = ? AND s.deleted = 0 ORDER BY b.created_at DESC LIMIT 40",
        (info["account_id"],),
    )
    return json_response([serializers.status_dict(dict(r), info["account_id"]) for r in rows])


# ---- follow requests / suggestions ----

async def follow_requests(request: Request):
    require_token(request, "follow")
    return json_response([])


async def follow_suggestions(request: Request):
    info = require_token(request, "read")
    rows = db.query_all(
        """SELECT a.* FROM accounts a
           WHERE a.id != ? AND a.id NOT IN (SELECT target_id FROM follows WHERE account_id = ?)
             AND a.is_local = 1
           ORDER BY a.followers_count DESC LIMIT 20""",
        (info["account_id"], info["account_id"]),
    )
    return json_response([{"source": "global", "account": serializers.account_dict(dict(r))} for r in rows])


# ---- search ----

async def search_v2(request: Request):
    info = maybe_token(request)
    q = (request.query_params.get("q") or "").strip()
    typ = request.query_params.get("type") or ""
    limit = min(int(request.query_params.get("limit", 20) or 20), 40)
    accounts = []
    statuses = []
    hashtags = []
    if not q:
        return json_response({"accounts": [], "statuses": [], "hashtags": []})
    if typ in ("", "accounts"):
        rows = db.query_all(
            "SELECT * FROM accounts WHERE username LIKE ? OR display_name LIKE ? OR acct LIKE ? LIMIT ?",
            (f"%{q}%", f"%{q}%", f"%{q}%", limit),
        )
        accounts = [serializers.account_dict(dict(r)) for r in rows]
    if typ in ("", "statuses"):
        rows = db.query_all(
            "SELECT * FROM statuses WHERE deleted = 0 AND visibility IN ('public','unlisted') AND text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", limit),
        )
        statuses = [s for s in (serializers.status_dict(dict(r), info["account_id"] if info else None) for r in rows) if s]
    if typ in ("", "hashtags"):
        qn = q.lstrip("#").lower()
        rows = db.query_all("SELECT * FROM tags WHERE name LIKE ? LIMIT ?", (f"%{qn}%", limit))
        hashtags = [serializers.tag_dict(r["name"]) for r in rows]
    return json_response({"accounts": accounts, "statuses": statuses, "hashtags": hashtags})


async def search_v1(request: Request):
    res = await search_v2(request)
    # v1 returns same structure
    return res


# ---- notifications ----

async def notifications_list(request: Request):
    info = require_token(request, "read")
    paging = timelines.parse_paging(dict(request.query_params))
    types = request.query_params.getlist("types[]") or request.query_params.getlist("types")
    excludes = request.query_params.getlist("exclude_types[]") or request.query_params.getlist("exclude_types")
    where = ["account_id = ?"]
    params = [info["account_id"]]
    if paging["max_id"]:
        where.append("id < ?")
        params.append(int(paging["max_id"]))
    if paging["since_id"]:
        where.append("id > ?")
        params.append(int(paging["since_id"]))
    if types:
        ph = ",".join(["?"] * len(types))
        where.append(f"type IN ({ph})")
        params.extend(types)
    if excludes:
        ph = ",".join(["?"] * len(excludes))
        where.append(f"type NOT IN ({ph})")
        params.extend(excludes)
    rows = db.query_all(
        f"SELECT * FROM notifications WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        (*params, paging["limit"]),
    )
    out = []
    for r in rows:
        nd = serializers.notification_dict(dict(r), info["account_id"])
        if nd:
            out.append(nd)
    return json_response(out)


async def notifications_get(request: Request):
    info = require_token(request, "read")
    nid = int(request.path_params["id"])
    row = db.query_one("SELECT * FROM notifications WHERE id = ? AND account_id = ?", (nid, info["account_id"]))
    if not row:
        return error("not found", 404)
    return json_response(serializers.notification_dict(dict(row), info["account_id"]))


async def notifications_clear(request: Request):
    info = require_token(request, "write")
    db.execute("DELETE FROM notifications WHERE account_id = ?", (info["account_id"],))
    return json_response({})


async def notifications_dismiss(request: Request):
    info = require_token(request, "write")
    nid = int(request.path_params["id"])
    db.execute("DELETE FROM notifications WHERE id = ? AND account_id = ?", (nid, info["account_id"]))
    return json_response({})


# ---- markers ----

async def markers_get(request: Request):
    info = require_token(request, "read")
    timelines_q = request.query_params.getlist("timeline[]") or request.query_params.getlist("timeline") or ["home", "notifications"]
    out = {}
    for t in timelines_q:
        row = db.query_one("SELECT * FROM markers WHERE account_id = ? AND timeline = ?", (info["account_id"], t))
        if row:
            out[t] = {
                "last_read_id": row["last_read_id"],
                "version": row["version"],
                "updated_at": row["updated_at"],
            }
    return json_response(out)


async def markers_set(request: Request):
    info = require_token(request, "write")
    data = await get_form_or_json(request)
    out = {}
    for t in ("home", "notifications"):
        block = data.get(t)
        if isinstance(block, dict) and block.get("last_read_id"):
            db.execute(
                """INSERT INTO markers (account_id, timeline, last_read_id, version, updated_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(account_id, timeline) DO UPDATE SET
                     last_read_id = excluded.last_read_id,
                     version = markers.version + 1,
                     updated_at = excluded.updated_at""",
                (info["account_id"], t, str(block["last_read_id"]), now_iso()),
            )
            row = db.query_one("SELECT * FROM markers WHERE account_id = ? AND timeline = ?", (info["account_id"], t))
            out[t] = {
                "last_read_id": row["last_read_id"],
                "version": row["version"],
                "updated_at": row["updated_at"],
            }
    return json_response(out)


# ---- conversations ----

async def conversations_list(request: Request):
    info = require_token(request, "read")
    rows = db.query_all(
        "SELECT * FROM conversations WHERE account_id = ? ORDER BY updated_at DESC LIMIT 40",
        (info["account_id"],),
    )
    return json_response([serializers.conversation_dict(dict(r), info["account_id"]) for r in rows])


# ---- lists ----

async def lists_list(request: Request):
    info = require_token(request, "read")
    rows = db.query_all("SELECT * FROM lists WHERE account_id = ? ORDER BY id DESC", (info["account_id"],))
    return json_response([serializers.list_dict(dict(r)) for r in rows])


async def lists_get(request: Request):
    info = require_token(request, "read")
    lid = int(request.path_params["id"])
    row = db.query_one("SELECT * FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"]))
    if not row:
        return error("not found", 404)
    return json_response(serializers.list_dict(dict(row)))


async def lists_create_api(request: Request):
    info = require_token(request, "write")
    data = await get_form_or_json(request)
    title = (data.get("title") or "").strip()
    if not title:
        return error("title required", 422)
    cur = db.execute(
        "INSERT INTO lists (account_id, title, replies_policy, exclusive, created_at) VALUES (?, ?, ?, ?, ?)",
        (info["account_id"], title, data.get("replies_policy", "list"), 1 if data.get("exclusive") else 0, now_iso()),
    )
    row = db.query_one("SELECT * FROM lists WHERE id = ?", (cur.lastrowid,))
    return json_response(serializers.list_dict(dict(row)))


async def lists_update(request: Request):
    info = require_token(request, "write")
    lid = int(request.path_params["id"])
    data = await get_form_or_json(request)
    db.execute(
        "UPDATE lists SET title = COALESCE(?, title), replies_policy = COALESCE(?, replies_policy), exclusive = COALESCE(?, exclusive) WHERE id = ? AND account_id = ?",
        (data.get("title"), data.get("replies_policy"), data.get("exclusive"), lid, info["account_id"]),
    )
    row = db.query_one("SELECT * FROM lists WHERE id = ?", (lid,))
    if not row:
        return error("not found", 404)
    return json_response(serializers.list_dict(dict(row)))


async def lists_delete(request: Request):
    info = require_token(request, "write")
    lid = int(request.path_params["id"])
    db.execute("DELETE FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"]))
    return json_response({})


async def lists_accounts(request: Request):
    info = require_token(request, "read")
    lid = int(request.path_params["id"])
    if not db.query_one("SELECT 1 FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"])):
        return error("not found", 404)
    rows = db.query_all(
        "SELECT a.* FROM accounts a JOIN list_accounts la ON la.account_id = a.id WHERE la.list_id = ?",
        (lid,),
    )
    return json_response([serializers.account_dict(dict(r)) for r in rows])


async def lists_accounts_add(request: Request):
    info = require_token(request, "write")
    lid = int(request.path_params["id"])
    if not db.query_one("SELECT 1 FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"])):
        return error("not found", 404)
    data = await get_form_or_json(request)
    ids = data.get("account_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    for x in ids:
        try:
            tid = int(x)
        except (TypeError, ValueError):
            continue
        try:
            db.execute("INSERT INTO list_accounts (list_id, account_id) VALUES (?, ?)", (lid, tid))
        except Exception:
            pass
    return json_response({})


async def lists_accounts_remove(request: Request):
    info = require_token(request, "write")
    lid = int(request.path_params["id"])
    if not db.query_one("SELECT 1 FROM lists WHERE id = ? AND account_id = ?", (lid, info["account_id"])):
        return error("not found", 404)
    data = await get_form_or_json(request)
    ids = data.get("account_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    for x in ids:
        try:
            tid = int(x)
        except (TypeError, ValueError):
            continue
        db.execute("DELETE FROM list_accounts WHERE list_id = ? AND account_id = ?", (lid, tid))
    return json_response({})


# ---- media ----

async def media_create(request: Request):
    info = require_token(request, "write")
    form = await request.form()
    f = form.get("file")
    if not f or not hasattr(f, "filename"):
        return error("file required", 422)
    ext = os.path.splitext(f.filename or "")[1].lower() or ".bin"
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".svg"}:
        return error("unsupported media type", 415)
    data = await f.read()
    media_dir = "/app/data/media"
    os.makedirs(media_dir, exist_ok=True)
    tok = secrets.token_urlsafe(16)
    out = os.path.join(media_dir, f"{tok}{ext}")
    with open(out, "wb") as fp:
        fp.write(data)
    media_type = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"} else "video"
    url = f"{BASE_URL}/media/{tok}{ext}"
    description = form.get("description") or ""
    cur = db.execute(
        "INSERT INTO media_attachments (account_id, type, url, preview_url, description, file_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (info["account_id"], media_type, url, url, description, out, now_iso()),
    )
    row = db.query_one("SELECT * FROM media_attachments WHERE id = ?", (cur.lastrowid,))
    return json_response(serializers.media_attachment_dict(dict(row)))


async def media_get(request: Request):
    require_token(request, "read")
    mid = int(request.path_params["id"])
    row = db.query_one("SELECT * FROM media_attachments WHERE id = ?", (mid,))
    if not row:
        return error("not found", 404)
    return json_response(serializers.media_attachment_dict(dict(row)))


async def media_update(request: Request):
    info = require_token(request, "write")
    mid = int(request.path_params["id"])
    data = await get_form_or_json(request)
    desc = data.get("description")
    if desc is not None:
        db.execute("UPDATE media_attachments SET description = ? WHERE id = ? AND account_id = ?",
                   (desc, mid, info["account_id"]))
    row = db.query_one("SELECT * FROM media_attachments WHERE id = ?", (mid,))
    if not row:
        return error("not found", 404)
    return json_response(serializers.media_attachment_dict(dict(row)))


async def serve_media(request: Request):
    fname = request.path_params["fname"]
    if "/" in fname or ".." in fname:
        return error("not found", 404)
    path = os.path.join("/app/data/media", fname)
    if not os.path.exists(path):
        return error("not found", 404)
    import mimetypes
    mt, _ = mimetypes.guess_type(path)
    return Response(open(path, "rb").read(), media_type=mt or "application/octet-stream",
                    headers={"Cache-Control": "public, max-age=300"})


# ---- polls ----

async def poll_get(request: Request):
    pid = int(request.path_params["id"])
    info = maybe_token(request)
    row = db.query_one("SELECT * FROM polls WHERE id = ?", (pid,))
    if not row:
        return error("not found", 404)
    return json_response(serializers.poll_dict(dict(row), info["account_id"] if info else None))


async def poll_vote(request: Request):
    info = require_token(request, "write")
    pid = int(request.path_params["id"])
    data = await get_form_or_json(request)
    choices = data.get("choices") or []
    if isinstance(choices, str):
        choices = [choices]
    choices = [int(c) for c in choices if str(c).isdigit()]
    row = db.query_one("SELECT * FROM polls WHERE id = ?", (pid,))
    if not row:
        return error("not found", 404)
    try:
        opts = json.loads(row["options"])
        voters = json.loads(row.get("voters") or "{}")
    except Exception:
        return error("bad data", 500)
    key = str(info["account_id"])
    if key in voters:
        return error("already voted", 422)
    if not row["multiple"]:
        choices = choices[:1]
    for c in choices:
        if 0 <= c < len(opts):
            opts[c]["votes_count"] = opts[c].get("votes_count", 0) + 1
    voters[key] = choices
    db.execute(
        "UPDATE polls SET options = ?, voters = ?, voters_count = ? WHERE id = ?",
        (json.dumps(opts), json.dumps(voters), len(voters), pid),
    )
    row = db.query_one("SELECT * FROM polls WHERE id = ?", (pid,))
    return json_response(serializers.poll_dict(dict(row), info["account_id"]))


# ---- reports ----

async def reports_create(request: Request):
    info = require_token(request, "write")
    data = await get_form_or_json(request)
    target = data.get("account_id")
    if not target:
        return error("account_id required", 422)
    target = int(target)
    sids = data.get("status_ids") or []
    if isinstance(sids, str):
        sids = [sids]
    cur = db.execute(
        "INSERT INTO reports (account_id, target_account_id, comment, category, status_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (info["account_id"], target, data.get("comment", ""), data.get("category", "other"),
         json.dumps([int(s) for s in sids if str(s).isdigit()]), now_iso()),
    )
    db.execute(
        "INSERT INTO audit_log (actor_id, action, target_type, target_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (info["account_id"], "report.create", "report", str(cur.lastrowid), now_iso()),
    )
    row = db.query_one("SELECT * FROM reports WHERE id = ?", (cur.lastrowid,))
    return json_response(serializers.report_dict(dict(row)))


# ---- trends ----

async def trends_tags(request: Request):
    rows = db.query_all(
        """SELECT t.name, COUNT(*) AS uses
           FROM tags t JOIN status_tags st ON st.tag_id = t.id
           JOIN statuses s ON s.id = st.status_id
           WHERE s.deleted = 0 AND s.visibility IN ('public','unlisted')
           GROUP BY t.id ORDER BY uses DESC LIMIT 10"""
    )
    return json_response([
        {
            "name": r["name"],
            "url": f"{BASE_URL}/tags/{r['name']}",
            "history": [{"day": str(int(time.time()) - 86400 * i), "uses": str(r["uses"]), "accounts": "1"} for i in range(7)],
        }
        for r in rows
    ])


async def trends_statuses(request: Request):
    rows = db.query_all(
        "SELECT * FROM statuses WHERE deleted = 0 AND visibility = 'public' ORDER BY favourites_count DESC, reblogs_count DESC LIMIT 10",
    )
    return json_response([serializers.status_dict(dict(r)) for r in rows])


# ---- preferences ----

async def preferences(request: Request):
    require_token(request, "read")
    return json_response({
        "posting:default:visibility": "public",
        "posting:default:sensitive": False,
        "posting:default:language": "en",
        "reading:expand:media": "default",
        "reading:expand:spoilers": False,
    })


# ---- domain blocks ----

async def domain_blocks_list(request: Request):
    require_token(request, "follow")
    return json_response([])


async def filters_list(request: Request):
    require_token(request, "read")
    return json_response([])


async def filters_v2_list(request: Request):
    require_token(request, "read")
    return json_response([])


# ---- custom_emojis ----

async def custom_emojis(request: Request):
    return json_response([])


# ---- streaming health (long-poll-ish; SSE used elsewhere) ----

async def streaming_health(request: Request):
    return Response("OK", media_type="text/plain")


# ---- announcements ----

async def announcements(request: Request):
    return json_response([])


# ---- admin endpoints ----

def _require_admin(request: Request) -> dict:
    info = require_token(request)
    if not auth.any_scope_satisfies(info["scopes"], ["admin", "admin:read", "admin:write"]):
        raise HTTPException(403, "Admin scope required")
    if info.get("account_id"):
        a = auth.find_account_by_id(info["account_id"])
        if not a or not a.get("is_admin"):
            raise HTTPException(403, "Admin user required")
    return info


async def admin_queues(request: Request):
    _require_admin(request)
    rows = db.query_all(
        "SELECT queue, status, COUNT(*) AS c FROM jobs GROUP BY queue, status"
    )
    queues = {}
    for r in rows:
        q = queues.setdefault(r["queue"], {"name": r["queue"], "pending": 0, "completed": 0, "failed": 0})
        q[r["status"]] = q.get(r["status"], 0) + r["c"]
    if not queues:
        queues["default"] = {"name": "default", "pending": 0, "completed": 0, "failed": 0}
    return json_response({"queues": list(queues.values())})


async def admin_audit(request: Request):
    _require_admin(request)
    limit = min(int(request.query_params.get("limit", 50) or 50), 200)
    rows = db.query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        try:
            details = json.loads(r["details"])
        except Exception:
            details = {}
        out.append({
            "id": str(r["id"]),
            "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "details": details,
            "created_at": r["created_at"],
        })
    return json_response(out)


async def admin_reports(request: Request):
    _require_admin(request)
    rows = db.query_all("SELECT * FROM reports ORDER BY id DESC LIMIT 100")
    return json_response([serializers.report_dict(dict(r)) for r in rows])


async def admin_reports_resolve(request: Request):
    _require_admin(request)
    rid = int(request.path_params["id"])
    db.execute("UPDATE reports SET action_taken = 1 WHERE id = ?", (rid,))
    row = db.query_one("SELECT * FROM reports WHERE id = ?", (rid,))
    if not row:
        return error("not found", 404)
    return json_response(serializers.report_dict(dict(row)))


# ---- generic 404 for /api/* ----

async def api_not_found(request: Request):
    return error("Endpoint not found", 404)
