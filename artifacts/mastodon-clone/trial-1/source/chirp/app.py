"""Chirp ASGI application."""
import json
import os
import traceback

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import api, db, web


CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception:
            traceback.print_exc()
            return _server_error(request)
        path = request.url.path
        is_api = path.startswith("/api/") or path.startswith("/oauth/") or path.startswith("/.well-known/") or path == "/_health" or path.startswith("/nodeinfo")
        # universal headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "interest-cohort=()"
        if not is_api:
            response.headers["Content-Security-Policy"] = CSP
        return response


def _server_error(request: Request) -> Response:
    if _is_api_path(request.url.path):
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    try:
        body = web.env.get_template("error.html").render(
            status=500, message="Something went wrong on Chirp.",
            viewer=None, csrf="", instance_domain=os.environ.get("CHIRP_DOMAIN", "chirp.local"),
            trending=[], active="",
        )
    except Exception:
        body = "<h1>500</h1>"
    return HTMLResponse(body, status_code=500)


def _is_api_path(path: str) -> bool:
    return (
        path.startswith("/api/")
        or path.startswith("/oauth/")
        or path.startswith("/.well-known/")
        or path == "/_health"
        or path.startswith("/nodeinfo")
        or path.startswith("/_admin")
    )


async def http_exception_handler(request: Request, exc: HTTPException):
    if _is_api_path(request.url.path):
        return JSONResponse({"error": exc.detail or "error"}, status_code=exc.status_code)
    if exc.status_code in (401, 403) and not _is_api_path(request.url.path):
        # bounce to login for unauthenticated UI access
        if exc.status_code == 401:
            from starlette.responses import RedirectResponse
            return RedirectResponse("/login?next=" + request.url.path, status_code=303)
    try:
        body = web.env.get_template("error.html").render(
            status=exc.status_code, message=exc.detail or "Page not found",
            viewer=None, csrf="", instance_domain=os.environ.get("CHIRP_DOMAIN", "chirp.local"),
            trending=[], active="",
        )
    except Exception:
        body = f"<h1>{exc.status_code}</h1>"
    return HTMLResponse(body, status_code=exc.status_code)


async def generic_exception_handler(request: Request, exc: Exception):
    return _server_error(request)


# Routes
def build_routes():
    routes = [
        # health
        Route("/_health", api.health, methods=["GET"]),

        # well-known & nodeinfo
        Route("/.well-known/webfinger", api.well_known_webfinger, methods=["GET"]),
        Route("/.well-known/host-meta", api.well_known_host_meta, methods=["GET"]),
        Route("/.well-known/nodeinfo", api.nodeinfo_well_known, methods=["GET"]),
        Route("/nodeinfo/2.0", api.nodeinfo_v2, methods=["GET"]),

        # instance
        Route("/api/v1/instance", api.instance_v1, methods=["GET"]),
        Route("/api/v2/instance", api.instance_v2, methods=["GET"]),
        Route("/api/v1/instance/peers", api.instance_peers, methods=["GET"]),
        Route("/api/v1/instance/activity", api.instance_activity, methods=["GET"]),

        # apps
        Route("/api/v1/apps", api.apps_create, methods=["POST"]),
        Route("/api/v1/apps/verify_credentials", api.apps_verify, methods=["GET"]),

        # OAuth
        Route("/oauth/authorize", api.oauth_authorize_get, methods=["GET"]),
        Route("/oauth/authorize", api.oauth_authorize_post, methods=["POST"]),
        Route("/oauth/token", api.oauth_token, methods=["POST"]),
        Route("/oauth/revoke", api.oauth_revoke, methods=["POST"]),
        Route("/oauth/introspect", api.oauth_introspect, methods=["POST"]),

        # accounts
        Route("/api/v1/accounts/verify_credentials", api.accounts_verify_credentials, methods=["GET"]),
        Route("/api/v1/accounts/update_credentials", api.accounts_update_credentials, methods=["PATCH", "POST"]),
        Route("/api/v1/accounts/lookup", api.accounts_lookup, methods=["GET"]),
        Route("/api/v1/accounts/search", api.accounts_search, methods=["GET"]),
        Route("/api/v1/accounts/relationships", api.accounts_relationships, methods=["GET"]),
        Route("/api/v1/accounts/{id:int}", api.accounts_get, methods=["GET"]),
        Route("/api/v1/accounts/{id:int}/statuses", api.accounts_statuses, methods=["GET"]),
        Route("/api/v1/accounts/{id:int}/followers", api.accounts_followers, methods=["GET"]),
        Route("/api/v1/accounts/{id:int}/following", api.accounts_following, methods=["GET"]),
        Route("/api/v1/accounts/{id:int}/follow", api.accounts_follow, methods=["POST"]),
        Route("/api/v1/accounts/{id:int}/unfollow", api.accounts_unfollow, methods=["POST"]),
        Route("/api/v1/accounts/{id:int}/block", api.accounts_block, methods=["POST"]),
        Route("/api/v1/accounts/{id:int}/unblock", api.accounts_unblock, methods=["POST"]),
        Route("/api/v1/accounts/{id:int}/mute", api.accounts_mute, methods=["POST"]),
        Route("/api/v1/accounts/{id:int}/unmute", api.accounts_unmute, methods=["POST"]),
        Route("/api/v1/blocks", api.blocks_list, methods=["GET"]),
        Route("/api/v1/mutes", api.mutes_list, methods=["GET"]),

        # statuses
        Route("/api/v1/statuses", api.statuses_create, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}", api.statuses_get, methods=["GET"]),
        Route("/api/v1/statuses/{id:int}", api.statuses_delete, methods=["DELETE"]),
        Route("/api/v1/statuses/{id:int}", api.statuses_edit, methods=["PUT"]),
        Route("/api/v1/statuses/{id:int}/history", api.statuses_history, methods=["GET"]),
        Route("/api/v1/statuses/{id:int}/source", api.statuses_source, methods=["GET"]),
        Route("/api/v1/statuses/{id:int}/context", api.statuses_context, methods=["GET"]),
        Route("/api/v1/statuses/{id:int}/favourite", api.statuses_favourite, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/unfavourite", api.statuses_unfavourite, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/reblog", api.statuses_reblog, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/unreblog", api.statuses_unreblog, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/bookmark", api.statuses_bookmark, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/unbookmark", api.statuses_unbookmark, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/pin", api.statuses_pin, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/unpin", api.statuses_unpin, methods=["POST"]),
        Route("/api/v1/statuses/{id:int}/favourited_by", api.statuses_favourited_by, methods=["GET"]),
        Route("/api/v1/statuses/{id:int}/reblogged_by", api.statuses_reblogged_by, methods=["GET"]),

        # timelines
        Route("/api/v1/timelines/home", api.timeline_home, methods=["GET"]),
        Route("/api/v1/timelines/public", api.timeline_public, methods=["GET"]),
        Route("/api/v1/timelines/tag/{name}", api.timeline_tag, methods=["GET"]),
        Route("/api/v1/timelines/list/{id:int}", api.timeline_list, methods=["GET"]),

        # favourites/bookmarks
        Route("/api/v1/favourites", api.favourites_list, methods=["GET"]),
        Route("/api/v1/bookmarks", api.bookmarks_list, methods=["GET"]),

        # follow_requests/suggestions
        Route("/api/v1/follow_requests", api.follow_requests, methods=["GET"]),
        Route("/api/v1/suggestions", api.follow_suggestions, methods=["GET"]),
        Route("/api/v2/suggestions", api.follow_suggestions, methods=["GET"]),

        # search
        Route("/api/v1/search", api.search_v1, methods=["GET"]),
        Route("/api/v2/search", api.search_v2, methods=["GET"]),

        # notifications
        Route("/api/v1/notifications", api.notifications_list, methods=["GET"]),
        Route("/api/v1/notifications/{id:int}", api.notifications_get, methods=["GET"]),
        Route("/api/v1/notifications/clear", api.notifications_clear, methods=["POST"]),
        Route("/api/v1/notifications/{id:int}/dismiss", api.notifications_dismiss, methods=["POST"]),

        # markers
        Route("/api/v1/markers", api.markers_get, methods=["GET"]),
        Route("/api/v1/markers", api.markers_set, methods=["POST"]),

        # conversations
        Route("/api/v1/conversations", api.conversations_list, methods=["GET"]),

        # lists
        Route("/api/v1/lists", api.lists_list, methods=["GET"]),
        Route("/api/v1/lists", api.lists_create_api, methods=["POST"]),
        Route("/api/v1/lists/{id:int}", api.lists_get, methods=["GET"]),
        Route("/api/v1/lists/{id:int}", api.lists_update, methods=["PUT"]),
        Route("/api/v1/lists/{id:int}", api.lists_delete, methods=["DELETE"]),
        Route("/api/v1/lists/{id:int}/accounts", api.lists_accounts, methods=["GET"]),
        Route("/api/v1/lists/{id:int}/accounts", api.lists_accounts_add, methods=["POST"]),
        Route("/api/v1/lists/{id:int}/accounts", api.lists_accounts_remove, methods=["DELETE"]),

        # media
        Route("/api/v1/media", api.media_create, methods=["POST"]),
        Route("/api/v2/media", api.media_create, methods=["POST"]),
        Route("/api/v1/media/{id:int}", api.media_get, methods=["GET"]),
        Route("/api/v1/media/{id:int}", api.media_update, methods=["PUT"]),

        # polls
        Route("/api/v1/polls/{id:int}", api.poll_get, methods=["GET"]),
        Route("/api/v1/polls/{id:int}/votes", api.poll_vote, methods=["POST"]),

        # reports
        Route("/api/v1/reports", api.reports_create, methods=["POST"]),

        # trends
        Route("/api/v1/trends", api.trends_tags, methods=["GET"]),
        Route("/api/v1/trends/tags", api.trends_tags, methods=["GET"]),
        Route("/api/v1/trends/statuses", api.trends_statuses, methods=["GET"]),

        # preferences/filters/announcements/etc
        Route("/api/v1/preferences", api.preferences, methods=["GET"]),
        Route("/api/v1/domain_blocks", api.domain_blocks_list, methods=["GET"]),
        Route("/api/v1/filters", api.filters_list, methods=["GET"]),
        Route("/api/v2/filters", api.filters_v2_list, methods=["GET"]),
        Route("/api/v1/custom_emojis", api.custom_emojis, methods=["GET"]),
        Route("/api/v1/announcements", api.announcements, methods=["GET"]),
        Route("/api/v1/streaming/health", api.streaming_health, methods=["GET"]),

        # admin
        Route("/_admin/queues", api.admin_queues, methods=["GET"]),
        Route("/_admin/audit", api.admin_audit, methods=["GET"]),
        Route("/_admin/reports", api.admin_reports, methods=["GET"]),
        Route("/_admin/reports/{id:int}/resolve", api.admin_reports_resolve, methods=["POST"]),

        # media file serving
        Route("/media/{fname}", api.serve_media, methods=["GET"]),

        # web UI routes
        Route("/", web.home_index, methods=["GET"]),
        Route("/home", web.home_timeline_view, methods=["GET"]),
        Route("/public", web.public_timeline_view, methods=["GET"]),
        Route("/public/local", web.local_timeline_view, methods=["GET"]),
        Route("/tags/{name}", web.hashtag_view, methods=["GET"]),
        Route("/notifications", web.notifications_view, methods=["GET"]),
        Route("/login", web.login_view, methods=["GET"]),
        Route("/signup", web.signup_view, methods=["GET"]),
        Route("/web/login", web.login_post, methods=["POST"]),
        Route("/web/signup", web.signup_post, methods=["POST"]),
        Route("/web/logout", web.logout_post, methods=["POST"]),
        Route("/web/statuses", web.web_post_status, methods=["POST"]),
        Route("/web/statuses/{id:int}/{action}", web.web_action, methods=["POST"]),
        Route("/web/statuses/{id:int}/delete", web.web_delete_status, methods=["POST"]),
        Route("/web/accounts/{id:int}/{action}", web.web_account_action, methods=["POST"]),
        Route("/web/sse", web.sse_stream2, methods=["GET"]),
        Route("/settings/profile", web.settings_profile_view, methods=["GET"]),
        Route("/web/settings/profile", web.settings_profile_post, methods=["POST"]),
        Route("/statuses/{id:int}", web.status_view, methods=["GET"]),
        Route("/statuses/{id:int}/edit", web.edit_status_view, methods=["GET"]),
        Route("/web/statuses/{id:int}/edit", web.edit_status_post, methods=["POST"]),
        Route("/report", web.report_view, methods=["GET"]),
        Route("/web/report", web.report_post, methods=["POST"]),
        Route("/lists", web.lists_view, methods=["GET"]),
        Route("/web/lists", web.lists_create, methods=["POST"]),
        Route("/bookmarks", web.bookmarks_view, methods=["GET"]),
        Route("/favourites", web.favourites_view, methods=["GET"]),
        Route("/users/{username}", web.profile_legacy_redirect, methods=["GET"]),
        Route("/@{username}", web.profile_view, methods=["GET"]),
        Route("/@{username}/followers", web.followers_view, methods=["GET"]),
        Route("/@{username}/following", web.following_view, methods=["GET"]),
        Route("/@{username}/with_replies", web.profile_view, methods=["GET"]),
        Route("/@{username}/media", web.profile_view, methods=["GET"]),
        Route("/@{username}/{id:int}", web.status_view, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory="/app/static"), name="static"),
    ]
    return routes


def build_app() -> Starlette:
    db.init_db()
    return Starlette(
        debug=False,
        routes=build_routes(),
        middleware=[Middleware(SecurityMiddleware)],
        exception_handlers={
            HTTPException: http_exception_handler,
            Exception: generic_exception_handler,
            404: http_exception_handler,
            500: http_exception_handler,
        },
    )


app = build_app()
