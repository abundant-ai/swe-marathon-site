"""Railway demo shim.

The agent's app correctly ships a strict Content-Security-Policy with
`frame-ancestors 'none'` plus `X-Frame-Options: DENY`. That is good security,
but it also prevents the app from being embedded in the SWE-Marathon site's
preview iframe. This thin ASGI wrapper relaxes only those two embedding headers
so the live artifact can be shown inline; all application behavior is unchanged.
"""
import re

from chirp.app import app as _wrapped


def _relax_headers(raw):
    out = []
    for k, v in raw:
        kl = k.lower()
        if kl == b"x-frame-options":
            continue
        if kl == b"content-security-policy":
            txt = re.sub(r"frame-ancestors[^;]*;?\s*", "", v.decode("latin-1")).strip().rstrip(";")
            v = (txt + "; frame-ancestors *").encode("latin-1")
        out.append((k, v))
    return out


async def app(scope, receive, send):
    if scope.get("type") != "http":
        await _wrapped(scope, receive, send)
        return

    async def send_wrapper(message):
        if message.get("type") == "http.response.start":
            message = dict(message)
            message["headers"] = _relax_headers(message.get("headers", []))
        await send(message)

    await _wrapped(scope, receive, send_wrapper)
