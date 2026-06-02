"""Utility helpers."""
import base64
import hashlib
import hmac
import html as htmllib
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

INSTANCE_DOMAIN = os.environ.get("CHIRP_DOMAIN", "chirp.local")
BASE_URL = os.environ.get("CHIRP_BASE_URL", f"http://{INSTANCE_DOMAIN}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def now() -> float:
    return time.time()


def parse_iso(s: str) -> float:
    if not s:
        return 0.0
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def gen_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def b64url_decode(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def pkce_verify(verifier: str, challenge: str, method: str) -> bool:
    if method.upper() == "S256":
        h = hashlib.sha256(verifier.encode("ascii")).digest()
        return hmac.compare_digest(b64url_encode(h), challenge)
    if method.upper() == "PLAIN":
        return hmac.compare_digest(verifier, challenge)
    return False


URL_RE = re.compile(r"https?://[^\s<>\"]+")
TAG_RE = re.compile(r"(?<![\w/&])#([A-Za-z0-9_]+)")
MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]+(?:@[A-Za-z0-9.\-]+)?)")


def render_status_html(text: str, mentions: list[dict] = None, tags: list[dict] = None) -> str:
    """Convert plain status text to sanitized HTML matching Mastodon's output style."""
    if text is None:
        text = ""
    paragraphs = text.split("\n\n")
    out_parts = []
    mention_set = {m["acct"].lower(): m for m in (mentions or [])}
    for p in paragraphs:
        if not p.strip():
            continue
        lines = p.split("\n")
        rendered_lines = []
        for line in lines:
            rendered_lines.append(_render_line(line, mention_set))
        out_parts.append("<p>" + "<br />".join(rendered_lines) + "</p>")
    return "".join(out_parts)


def _render_line(line: str, mention_set: dict) -> str:
    """Linkify URLs, hashtags, and mentions while HTML-escaping the rest."""
    tokens = []
    pattern = re.compile(
        r"(?P<url>https?://[^\s<>\"]+)|(?P<tag>(?<![\w/&])#[A-Za-z0-9_]+)|(?P<mention>(?<![\w/])@[A-Za-z0-9_]+(?:@[A-Za-z0-9.\-]+)?)"
    )
    pos = 0
    out = []
    for m in pattern.finditer(line):
        if m.start() > pos:
            out.append(htmllib.escape(line[pos:m.start()]))
        if m.group("url"):
            url = m.group("url")
            display = re.sub(r"^https?://", "", url)
            out.append(
                f'<a href="{htmllib.escape(url, True)}" rel="nofollow noopener noreferrer" target="_blank"><span class="invisible">{htmllib.escape(url.split("://")[0])}://</span>{htmllib.escape(display)}</a>'
            )
        elif m.group("tag"):
            tag = m.group("tag")[1:]
            out.append(
                f'<a href="/tags/{htmllib.escape(tag.lower(), True)}" class="mention hashtag" rel="tag">#<span>{htmllib.escape(tag)}</span></a>'
            )
        elif m.group("mention"):
            acct = m.group("mention")[1:]
            out.append(
                f'<span class="h-card"><a href="/@{htmllib.escape(acct.split("@")[0], True)}" class="u-url mention">@<span>{htmllib.escape(acct.split("@")[0])}</span></a></span>'
            )
        pos = m.end()
    if pos < len(line):
        out.append(htmllib.escape(line[pos:]))
    return "".join(out)


def extract_mentions(text: str) -> list[str]:
    return [m.group(1) for m in MENTION_RE.finditer(text or "")]


def extract_tags(text: str) -> list[str]:
    return list({m.group(1).lower() for m in TAG_RE.finditer(text or "")})


def csrf_make(secret: str, session_id: str) -> str:
    return hmac.new(secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()[:32]


def safe_redirect(url: str, fallback: str = "/") -> str:
    if not url:
        return fallback
    if url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


def relative_time(iso: str) -> str:
    if not iso:
        return ""
    t = parse_iso(iso)
    delta = time.time() - t
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d"
    return datetime.fromtimestamp(t).strftime("%b %d")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def parse_qs_first(qs: str) -> dict:
    parsed = urllib.parse.parse_qs(qs, keep_blank_values=True)
    return {k: v[0] if v else "" for k, v in parsed.items()}


def link_header(prev_url: Optional[str], next_url: Optional[str]) -> Optional[str]:
    parts = []
    if next_url:
        parts.append(f'<{next_url}>; rel="next"')
    if prev_url:
        parts.append(f'<{prev_url}>; rel="prev"')
    return ", ".join(parts) if parts else None
