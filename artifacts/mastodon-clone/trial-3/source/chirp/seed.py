"""Idempotent seed for Chirp."""
import os, json, time, datetime
from . import db, actions
from .db import q, qone, qall
from .util import now_iso, snowflake

SAMPLES = [
    ("bob",   "Bob Choudhury", "weekend hiker. opinions on coffee. #coffee #outdoors", False),
    ("carla", "Carla Reyes", "infosec @ a small co. CTFs and #security threads.", False),
    ("dom",   "Dom Nguyen", "music nerd; making beats. #music #studio", False),
    ("eve",   "Eve Bachmann", "PhD candidate, complexity theory. coffee fan.", False),
    ("finn",  "Finn O Brien", "climate journalist. #climate #policy", False),
    ("gita",  "Gita Iyer", "product designer. interfaces and accessibility. #design", False),
    ("henri", "Henri Laurent", "chef in training. #food and ferment.", False),
    ("remote_friend", "Remote Friend", "posts from across the fediverse.", True),
]

STATUSES = [
    ("alice","Welcome to Chirp! It speaks the Mastodon API. #welcome", "public", ""),
    ("alice","Reading about #urbanism this morning -- the bus is a great place to think.", "public", ""),
    ("bob",  "Today's coffee: a single-origin Ethiopian. Bright. #coffee", "public", ""),
    ("bob",  "Did the ridge trail. Foggy and quiet. #outdoors", "public", ""),
    ("carla","New CTF writeup dropping later -- a sneaky JWT alg=none case. #security", "public", ""),
    ("carla","PSA: rotate your tokens after CI leaks. #security", "public", "some boring infosec"),
    ("dom",  "Loop pack v2 is up. Lots of analog warmth. #music", "public", ""),
    ("dom",  "Sketching a new track -- 92 bpm, dusty Rhodes. #studio", "unlisted", ""),
    ("eve",  "Office hours moved to Thursdays.", "public", ""),
    ("eve",  "Reading 'The Nature of Computation' chapter 3. Slowly.", "public", ""),
    ("finn", "Quick thread on #climate adaptation funding -- it's not just emissions.", "public", ""),
    ("finn", "Spent the morning at city hall. Lots of #policy theatre, some wins.", "public", ""),
    ("gita", "A11y win: increased our hit areas to 44px. Fewer rage-taps. #design", "public", ""),
    ("gita", "New iconography for visibility states. Globe / eye / lock / envelope.", "public", ""),
    ("henri","Lacto-fermented hot sauce: day 7. Smells correct. #food", "public", ""),
    ("henri","Big tip: salt your eggplant 30 min before roasting. #food", "public", "discussing food prep"),
    ("alice","Posting from the train -- delays, but the views are great.", "public", ""),
    ("bob",  "Friendly reminder to drink water.", "public", ""),
    ("carla","It works on my machine. #security", "public", ""),
    ("dom",  "https://example.com/loop-pack -- preview link.", "public", ""),
    ("remote_friend","Hello from across the fediverse!", "public", ""),
    ("remote_friend","Cross-instance post. Federation works in spirit here.", "public", ""),
]

MEDIA_FILES = [
    ("alice", "city.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><rect fill="#8c8dff" width="320" height="180"/><text x="50%" y="55%" text-anchor="middle" font-size="24" fill="#fff" font-family="sans-serif">city skyline</text></svg>'),
    ("henri", "plate.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><rect fill="#563acc" width="320" height="180"/><text x="50%" y="55%" text-anchor="middle" font-size="24" fill="#fff" font-family="sans-serif">food photo</text></svg>'),
]

def ensure_account(username, display_name, note, *, remote=False, password=None, is_admin=0):
    if remote:
        a = qone("SELECT * FROM accounts WHERE username=? AND domain=?", username, "remote.example")
    else:
        a = qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", username)
    if a: return a["id"]
    domain = None if not remote else "remote.example"
    return actions.create_account(username, password=password, display_name=display_name, note=note, domain=domain, is_admin=is_admin)

def seed():
    db.init()
    admin_user = os.environ.get("CHIRP_ADMIN_USER", "admin")
    admin_pass = os.environ.get("CHIRP_ADMIN_PASS", "admin1234")
    seed_acct = os.environ.get("CHIRP_SEED_USER_ACCOUNT", "alice")
    seed_pass = os.environ.get("CHIRP_SEED_USER_PASS", "alice1234")
    aid_admin = ensure_account(admin_user, "Admin", "Instance admin.", password=admin_pass, is_admin=1)
    aid_alice = ensure_account(seed_acct, "Alice Park", "writes about cities, transit, and #urbanism.", password=seed_pass)
    for u, dn, note, remote in SAMPLES:
        ensure_account(u, dn, note, remote=remote, password="chirp1234")
    # Bootstrap OAuth app + tokens
    cid = os.environ.get("CHIRP_SEED_APP_CLIENT_ID", "chirp-bootstrap-client")
    csec = os.environ.get("CHIRP_SEED_APP_CLIENT_SECRET", "chirp-bootstrap-secret")
    app_row = qone("SELECT * FROM oauth_apps WHERE client_id=?", cid)
    if not app_row:
        q("INSERT INTO oauth_apps(id, client_id, client_secret, name, redirect_uri, scopes, created_at) VALUES(?,?,?,?,?,?,?)",
          snowflake(), cid, csec, "Chirp Bootstrap", "urn:ietf:wg:oauth:2.0:oob",
          "read write follow push admin", now_iso())
        app_row = qone("SELECT * FROM oauth_apps WHERE client_id=?", cid)
    admin_token = os.environ.get("CHIRP_BOOTSTRAP_ADMIN_TOKEN", "chirp-admin-bootstrap-token")
    user_token = os.environ.get("CHIRP_SEED_USER_TOKEN", "chirp-seed-user-token")
    if not qone("SELECT 1 FROM oauth_tokens WHERE token=?", admin_token):
        q("INSERT INTO oauth_tokens(token, app_id, account_id, scopes, created_at) VALUES(?,?,?,?,?)",
          admin_token, app_row["id"], aid_admin, "read write follow push admin", int(time.time()))
    if not qone("SELECT 1 FROM oauth_tokens WHERE token=?", user_token):
        q("INSERT INTO oauth_tokens(token, app_id, account_id, scopes, created_at) VALUES(?,?,?,?,?)",
          user_token, app_row["id"], aid_alice, "read write follow push", int(time.time()))
    # Media
    media_dir = os.environ.get("CHIRP_DATA", "/app/data") + "/media"
    os.makedirs(media_dir, exist_ok=True)
    media_for = {}
    for owner, fname, body in MEDIA_FILES:
        ow = qone("SELECT * FROM accounts WHERE username=? AND domain IS NULL", owner)
        if not ow: continue
        fp = os.path.join(media_dir, fname)
        if not os.path.exists(fp):
            open(fp, "w").write(body)
        existing = qone("SELECT * FROM media WHERE file_path=?", fp)
        if existing:
            media_for[owner] = existing["id"]
        else:
            mid = snowflake()
            url = f"/media/{fname}"
            q("INSERT INTO media(id, account_id, type, url, preview_url, description, created_at, file_path) VALUES(?,?,?,?,?,?,?,?)",
              mid, ow["id"], "image", url, url, owner+" image", now_iso(), fp)
            media_for[owner] = mid
    # Statuses (idempotent: only seed if no statuses yet)
    if not qone("SELECT 1 FROM statuses LIMIT 1"):
        for u, content, vis, cw in STATUSES:
            ow = qone("SELECT * FROM accounts WHERE username=?", u)
            if not ow: continue
            mids = []
            if u in media_for:
                m = qone("SELECT * FROM media WHERE id=?", media_for[u])
                if m and m["status_id"] is None:
                    mids = [media_for[u]]
            actions.create_status(ow["id"], content=content, visibility=vis, spoiler_text=cw,
                                   sensitive=bool(cw), media_ids=mids)
    # Follow relationships
    al = qone("SELECT id FROM accounts WHERE username=? AND domain IS NULL", seed_acct)
    if al:
        for u in ("bob","carla","dom","eve","finn"):
            t = qone("SELECT id FROM accounts WHERE username=? AND domain IS NULL", u)
            if t: actions.follow(al["id"], t["id"])

if __name__ == "__main__":
    seed()
    print("seeded")
