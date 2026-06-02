import os, re, time, secrets, hashlib, base64, html, json, datetime
from urllib.parse import quote

INSTANCE = os.environ.get("CHIRP_DOMAIN", "chirp.local")

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def snowflake():
    # Mastodon-like increasing numeric ids
    t = int(time.time() * 1000)
    rnd = secrets.randbits(16)
    return str((t << 16) | rnd)

def new_token(n=32):
    return secrets.token_urlsafe(n)

def pwhash(pw: str) -> str:
    try:
        from argon2 import PasswordHasher
        return PasswordHasher().hash(pw)
    except Exception:
        salt = secrets.token_hex(8)
        return "sha256$" + salt + "$" + hashlib.sha256((salt+pw).encode()).hexdigest()

def pwverify(pw: str, h: str) -> bool:
    if not h: return False
    if h.startswith("sha256$"):
        _, salt, dig = h.split("$")
        return hashlib.sha256((salt+pw).encode()).hexdigest() == dig
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError
        try:
            PasswordHasher().verify(h, pw)
            return True
        except VerifyMismatchError:
            return False
    except Exception:
        return False

URL_RE = re.compile(r'(?<!["\'>])(https?://[^\s<>"\']+)')
MENTION_RE = re.compile(r'(^|[^\w/])@([a-zA-Z0-9_]+)(@[a-zA-Z0-9.\-]+)?')
TAG_RE = re.compile(r'(^|[^\w/])#([\w\u00c0-\uffff]+)')

def render_content(text: str) -> str:
    if text is None: return ""
    esc = html.escape(text)
    # URLs first
    def linkify_url(m):
        u = m.group(1)
        return f'<a href="{u}" rel="nofollow noopener noreferrer" target="_blank">{u}</a>'
    esc = URL_RE.sub(linkify_url, esc)
    def linkify_mention(m):
        pre, user, dom = m.group(1), m.group(2), m.group(3) or ""
        href = f"/@{user}" if not dom else f"/@{user}{dom}"
        return f'{pre}<span class="h-card"><a href="{href}" class="u-url mention">@<span>{user}{dom}</span></a></span>'
    esc = MENTION_RE.sub(linkify_mention, esc)
    def linkify_tag(m):
        pre, tag = m.group(1), m.group(2)
        return f'{pre}<a href="/tags/{tag.lower()}" class="mention hashtag" rel="tag">#<span>{tag}</span></a>'
    esc = TAG_RE.sub(linkify_tag, esc)
    paragraphs = ["<p>" + p.replace("\n", "<br>") + "</p>" for p in esc.split("\n\n")]
    return "".join(paragraphs)

def extract_tags(text):
    return list({m.group(2).lower() for m in TAG_RE.finditer(text or "")})

def extract_mentions(text):
    out = []
    for m in MENTION_RE.finditer(text or ""):
        out.append((m.group(2), (m.group(3) or "")[1:] or None))
    return out

def visibility_ok(v): return v in ("public","unlisted","private","direct")

def parse_link(s):
    if not s: return {}
    out = {}
    for part in s.split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if m: out[m.group(2)] = m.group(1)
    return out
