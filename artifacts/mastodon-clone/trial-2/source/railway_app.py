"""Railway demo shim.

Relaxes only the iframe-embedding response headers (`X-Frame-Options` and the
CSP `frame-ancestors` directive) so the live artifact can be shown inline in the
SWE-Marathon site. The ASGI `lifespan` scope is passed straight through, so the
app's own startup seeding still runs. All application behavior is unchanged.
"""
import re

from app import app as _wrapped


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
