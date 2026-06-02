"""Idempotent bootstrap and seed for Chirp."""
import json
import os
import secrets
import shutil
import sys

from . import auth, db, posting
from .util import BASE_URL, INSTANCE_DOMAIN, now_iso


SEED_ACCOUNTS = [
    # username, display, bio, is_admin, password, is_local, domain
    ("admin", "Admin", "Instance admin. Reach me with questions.", True, "ChirpAdmin1!", True, None),
    ("alice", "Alice Sparrow", "Birdwatcher in #chirp city. Loves long walks and longer threads.", False, "AliceSecret1!", True, None),
    ("bob", "Bob Linden", "Hobbyist astronomer. Telescope by night, code by day. #astronomy", False, "BobSecret1!", True, None),
    ("carol", "Carol Maple", "Designer working on small things. #design #typography", False, "CarolSecret1!", True, None),
    ("dave", "Dave River", "Indie game dev. Currently building a tiny puzzle game. #gamedev", False, "DaveSecret1!", True, None),
    ("eve", "Eve Heron", "Climate researcher. Posting field notes from the marshes.", False, "EveSecret1!", True, None),
    ("frank", "Frank Wren", "Historian — old maps, older ships, oldest stories.", False, "FrankSecret1!", True, None),
    ("grace", "Grace Tern", "Backend engineer. Caches and queues. #programming", False, "GraceSecret1!", True, None),
    # remote-flagged seed (acct includes domain)
    ("remy", "Remy Robin", "Visiting from elsewhere on the fediverse.", False, None, False, "elsewhere.example"),
]


SEED_STATUSES = [
    # author username, text, visibility, spoiler, sensitive
    ("alice", "Hello, Chirp! Just joined this little instance. Looking forward to meeting fellow #birdwatching folks here.", "public", "", False),
    ("bob", "Saw the Pleiades clearly tonight — clearest skies I've had all year. #astronomy", "public", "", False),
    ("carol", "On typography: don't underestimate a generous line-height. https://example.org/typography-tips #design", "public", "", False),
    ("dave", "Made the slime physics actually feel slimy today. Took three rewrites. #gamedev", "public", "", False),
    ("eve", "Heat-stress field notes — the cattails are flowering two weeks early.", "public", "Climate change content", True),
    ("frank", "A small thing I love about old portolan charts: the wind roses doubled as compasses for the eye, not just the ship.", "public", "", False),
    ("grace", "If your cache invalidation strategy fits in a tweet, it's wrong — but if it fits in a notebook, it might just be right. #programming", "public", "", False),
    ("admin", "Welcome to Chirp. This is a small Mastodon-compatible community. Please be kind. https://docs.joinmastodon.org", "public", "", False),
    ("alice", "Watched a kingfisher catch dinner at dusk. Pure precision.", "public", "", False),
    ("bob", "Anyone else still using a paper observation log? I find it slows me down in the right way. #astronomy", "public", "", False),
    ("carol", "@dave — those slime physics look great. Curious what you're using under the hood.", "public", "", False),
    ("dave", "@carol — vanilla Verlet integration, then a soft-body constraint pass.", "public", "", False),
    ("eve", "Sketch from today: rising water at the marsh edge. Still 40m from the path.", "public", "", False),
    ("frank", "There's a 14th century Genoese map that draws a sea-monster where the data ran out. Wonderful.", "public", "", False),
    ("grace", "Reminder that your linter is not a referee — it's a coach. #programming", "public", "", False),
    ("admin", "Reminder: the local timeline is for posts from chirp.local accounts; the federated tab includes all public posts we've seen.", "public", "", False),
    ("alice", "Trying out a #photography challenge: same tree, every Sunday.", "public", "", False),
    ("bob", "Tonight's notebook entry — Saturn at 10:42 UTC, three rings clearly resolved.", "public", "", False),
    ("carol", "Spent the morning with a wide-nibbed pen. Ink everywhere. No regrets.", "public", "", False),
    ("dave", "Streaming dev later — feel free to drop in. #gamedev", "public", "", False),
    ("remy", "Hello from elsewhere — happy to be visible on Chirp via the federated timeline.", "public", "", False),
    ("admin", "Maintenance window scheduled this weekend. Heads up.", "public", "Heads up", False),
]


SEED_FOLLOWS = [
    ("alice", "bob"),
    ("alice", "carol"),
    ("bob", "alice"),
    ("bob", "grace"),
    ("carol", "alice"),
    ("carol", "dave"),
    ("dave", "carol"),
    ("eve", "frank"),
    ("frank", "eve"),
    ("grace", "bob"),
    ("admin", "alice"),
    ("admin", "bob"),
    ("alice", "admin"),
]


def _ensure_static_seed_media():
    """Copy bundled SVGs into /app/data/media so seeded statuses can attach them."""
    out_dir = "/app/data/media"
    os.makedirs(out_dir, exist_ok=True)
    samples = [
        ("seed_marsh.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240"><defs><linearGradient id="s" x1="0" x2="0" y1="0" y2="1"><stop stop-color="#8c8dff" offset="0"/><stop stop-color="#563acc" offset="1"/></linearGradient></defs><rect width="400" height="240" fill="url(#s)"/><circle cx="320" cy="60" r="30" fill="#fff8cf"/><path d="M0 200 Q 100 170 200 195 T 400 190 L 400 240 L 0 240 Z" fill="#1f1b2e"/><text x="20" y="30" fill="#fff" font-family="sans-serif" font-size="14">Field sketch — marsh edge</text></svg>'),
        ("seed_telescope.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240"><rect width="400" height="240" fill="#101027"/><circle cx="200" cy="120" r="80" fill="#1f1b2e" stroke="#8c8dff" stroke-width="2"/><circle cx="160" cy="100" r="2" fill="#fff"/><circle cx="220" cy="90" r="2" fill="#fff"/><circle cx="180" cy="160" r="2" fill="#fff"/><circle cx="240" cy="150" r="2" fill="#fff"/><text x="20" y="30" fill="#fff" font-family="sans-serif" font-size="14">Saturn — sketch</text></svg>'),
    ]
    paths = []
    for name, body in samples:
        p = os.path.join(out_dir, name)
        if not os.path.exists(p):
            with open(p, "w") as fp:
                fp.write(body)
        paths.append((name, "image"))
    return paths


def bootstrap():
    """Idempotently seed the instance. Safe to run on every boot."""
    db.init_db()

    # bootstrap admin app
    app_client_id = os.environ.get("CHIRP_SEED_APP_CLIENT_ID", "chirp-default-client")
    app_client_secret = os.environ.get("CHIRP_SEED_APP_CLIENT_SECRET", "chirp-default-secret-please-rotate-me")
    if not db.query_one("SELECT 1 FROM apps WHERE client_id = ?", (app_client_id,)):
        auth.create_app(
            "Chirp Seed", "urn:ietf:wg:oauth:2.0:oob",
            "read write follow push admin",
            None, app_client_id, app_client_secret,
        )

    # accounts
    user_to_id = {}
    for username, display, bio, is_admin, password, is_local, domain in SEED_ACCOUNTS:
        existing = db.query_one(
            "SELECT * FROM accounts WHERE username = ? AND COALESCE(domain,'') = COALESCE(?, '')",
            (username, domain),
        )
        if existing:
            user_to_id[username] = existing["id"]
            db.execute(
                "UPDATE accounts SET display_name = ?, note = ?, is_admin = ? WHERE id = ?",
                (display, bio, 1 if is_admin else 0, existing["id"]),
            )
            continue
        aid = auth.create_account(
            username, password or secrets.token_urlsafe(16),
            email=f"{username}@{INSTANCE_DOMAIN}",
            display_name=display, note=bio, is_admin=is_admin, domain=domain,
        )
        user_to_id[username] = aid

    # personalize avatars: write a unique tinted SVG per user.
    palette = ["#563acc", "#6364ff", "#8c8dff", "#7d40c0", "#9c44ff", "#5b3fe1", "#7156db", "#a479ff", "#3d2bb8"]
    out_dir = "/app/data/media"
    os.makedirs(out_dir, exist_ok=True)
    for i, (username, aid) in enumerate(sorted(user_to_id.items())):
        color = palette[i % len(palette)]
        initial = username[0].upper()
        avatar_path = os.path.join(out_dir, f"avatar_{username}.svg")
        with open(avatar_path, "w") as fp:
            fp.write(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
                f'<rect width="96" height="96" rx="12" fill="{color}"/>'
                f'<text x="48" y="62" text-anchor="middle" font-family="sans-serif" '
                f'font-size="48" font-weight="700" fill="white">{initial}</text></svg>'
            )
        db.execute(
            "UPDATE accounts SET avatar = ? WHERE id = ?",
            (f"{BASE_URL}/media/avatar_{username}.svg", aid),
        )

    # tokens
    admin_token = os.environ.get("CHIRP_BOOTSTRAP_ADMIN_TOKEN", "chirp-default-admin-token-please-rotate")
    seed_user_token = os.environ.get("CHIRP_SEED_USER_TOKEN", "chirp-default-user-token-please-rotate")
    seed_user_acct = os.environ.get("CHIRP_SEED_USER_ACCOUNT", "alice")

    app_row = dict(db.query_one("SELECT * FROM apps WHERE client_id = ?", (app_client_id,)))

    admin_id = user_to_id.get("admin")
    if admin_id and not db.query_one("SELECT 1 FROM oauth_tokens WHERE token = ?", (admin_token,)):
        db.execute(
            "INSERT INTO oauth_tokens (token, app_id, account_id, scopes, created_at, grant_type) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_token, app_row["id"], admin_id, "read write follow push admin", now_iso(), "authorization_code"),
        )

    seed_uid = user_to_id.get(seed_user_acct, user_to_id.get("alice"))
    if seed_uid and not db.query_one("SELECT 1 FROM oauth_tokens WHERE token = ?", (seed_user_token,)):
        db.execute(
            "INSERT INTO oauth_tokens (token, app_id, account_id, scopes, created_at, grant_type) VALUES (?, ?, ?, ?, ?, ?)",
            (seed_user_token, app_row["id"], seed_uid, "read write follow push", now_iso(), "authorization_code"),
        )

    # follows
    for follower, target in SEED_FOLLOWS:
        if follower in user_to_id and target in user_to_id:
            posting.follow(user_to_id[follower], user_to_id[target])

    # statuses (only if no statuses yet)
    statuses_count = db.query_one("SELECT COUNT(*) AS c FROM statuses")["c"]
    media_files = _ensure_static_seed_media()
    if statuses_count < 5:
        # mark some as having media
        media_picks = {4: media_files[0], 17: media_files[1]}  # eve marsh, bob saturn (indices)
        sids = []
        for i, (username, text, vis, spoiler, sensitive) in enumerate(SEED_STATUSES):
            aid = user_to_id.get(username)
            if not aid:
                continue
            s = posting.create_status(
                aid, text, visibility=vis, spoiler_text=spoiler, sensitive=sensitive or bool(spoiler),
            )
            sids.append(s["id"])
            if i in media_picks:
                fname, mtype = media_picks[i]
                url = f"{BASE_URL}/media/{fname}"
                cur = db.execute(
                    "INSERT INTO media_attachments (account_id, status_id, type, url, preview_url, file_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (aid, s["id"], mtype, url, url, f"/app/data/media/{fname}", now_iso()),
                )
                db.execute(
                    "INSERT INTO status_media (status_id, media_id, position) VALUES (?, ?, ?)",
                    (s["id"], cur.lastrowid, 0),
                )
        # a few favourites + a boost to give the seed some life
        if len(sids) > 5:
            posting.favourite(user_to_id["bob"], sids[0])
            posting.favourite(user_to_id["carol"], sids[0])
            posting.favourite(user_to_id["alice"], sids[1])
            posting.reblog(user_to_id["grace"], sids[7])

    print("[chirp] seed bootstrap complete", file=sys.stderr)


if __name__ == "__main__":
    bootstrap()
