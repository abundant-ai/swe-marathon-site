"""Authentication: OAuth tokens, sessions, scope checks."""
import json
import os
import secrets
from typing import Optional

from . import db
from .util import (
    csrf_make,
    gen_token,
    hash_password,
    now_iso,
    pkce_verify,
    verify_password,
)

SESSION_SECRET = os.environ.get("CHIRP_SESSION_SECRET", "chirp-default-secret-change-me-please-32b")

DEFAULT_SCOPES = "read write follow push"
ALL_SCOPES = {"read", "write", "follow", "push", "admin", "admin:read", "admin:write"}


def normalize_scopes(scopes: str) -> list[str]:
    if not scopes:
        return ["read"]
    parts = [s.strip() for s in scopes.replace(",", " ").split() if s.strip()]
    out = []
    for p in parts:
        if p in ALL_SCOPES or p.startswith("read:") or p.startswith("write:") or p.startswith("admin:"):
            out.append(p)
    return out or ["read"]


def scope_satisfies(token_scopes: list[str], required: str) -> bool:
    """Whether a token's scopes satisfy a required scope (Mastodon-style hierarchical match)."""
    if not required:
        return True
    if required in token_scopes:
        return True
    # `read` includes `read:something`
    if ":" in required:
        head = required.split(":", 1)[0]
        if head in token_scopes:
            return True
    # admin includes admin:read / admin:write
    if required.startswith("admin"):
        if "admin" in token_scopes:
            return True
    return False


def any_scope_satisfies(token_scopes: list[str], any_of: list[str]) -> bool:
    return any(scope_satisfies(token_scopes, s) for s in any_of)


def lookup_token(token: str) -> Optional[dict]:
    if not token:
        return None
    row = db.query_one(
        "SELECT t.*, a.id AS acct_id FROM oauth_tokens t LEFT JOIN accounts a ON a.id = t.account_id WHERE t.token = ? AND t.revoked = 0",
        (token,),
    )
    if not row:
        return None
    return {
        "token": row["token"],
        "app_id": row["app_id"],
        "account_id": row["account_id"],
        "scopes": row["scopes"].split(),
        "created_at": row["created_at"],
        "grant_type": row["grant_type"],
    }


def issue_token(app_id: Optional[int], account_id: Optional[int], scopes: list[str], grant_type: str = "authorization_code") -> str:
    tok = gen_token(40)
    db.execute(
        "INSERT INTO oauth_tokens (token, app_id, account_id, scopes, created_at, grant_type) VALUES (?, ?, ?, ?, ?, ?)",
        (tok, app_id, account_id, " ".join(scopes), now_iso(), grant_type),
    )
    return tok


def revoke_token(token: str) -> bool:
    cur = db.execute("UPDATE oauth_tokens SET revoked = 1 WHERE token = ?", (token,))
    return cur.rowcount > 0


def create_app(name: str, redirect_uris: str, scopes: str, website: Optional[str] = None,
               client_id: Optional[str] = None, client_secret: Optional[str] = None) -> dict:
    cid = client_id or gen_token(32)
    csec = client_secret or gen_token(48)
    vapid = gen_token(43)
    db.execute(
        """INSERT INTO apps (client_id, client_secret, name, redirect_uris, scopes, website, vapid_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, csec, name, redirect_uris, scopes, website, vapid, now_iso()),
    )
    row = db.query_one("SELECT * FROM apps WHERE client_id = ?", (cid,))
    return dict(row)


def find_app_by_client(client_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM apps WHERE client_id = ?", (client_id,))
    return dict(row) if row else None


def issue_authorization_code(app_id: int, account_id: int, scopes: str, redirect_uri: str,
                             code_challenge: Optional[str], code_challenge_method: Optional[str]) -> str:
    code = gen_token(40)
    db.execute(
        """INSERT INTO oauth_codes (code, app_id, account_id, scopes, redirect_uri, code_challenge, code_challenge_method, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (code, app_id, account_id, scopes, redirect_uri, code_challenge, code_challenge_method, now_iso()),
    )
    return code


def consume_authorization_code(code: str, redirect_uri: str, code_verifier: Optional[str]) -> Optional[dict]:
    row = db.query_one("SELECT * FROM oauth_codes WHERE code = ?", (code,))
    if not row or row["used"]:
        return None
    if row["redirect_uri"] != redirect_uri:
        return None
    if row["code_challenge"]:
        if not code_verifier:
            return None
        if not pkce_verify(code_verifier, row["code_challenge"], row["code_challenge_method"] or "S256"):
            return None
    db.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
    return dict(row)


def create_session(account_id: int) -> tuple[str, str]:
    sid = gen_token(32)
    csrf = csrf_make(SESSION_SECRET, sid)
    db.execute(
        "INSERT INTO sessions (id, account_id, created_at, csrf) VALUES (?, ?, ?, ?)",
        (sid, account_id, now_iso(), csrf),
    )
    return sid, csrf


def lookup_session(sid: Optional[str]) -> Optional[dict]:
    if not sid:
        return None
    row = db.query_one(
        "SELECT s.*, a.id AS acct_id FROM sessions s JOIN accounts a ON a.id = s.account_id WHERE s.id = ?",
        (sid,),
    )
    return dict(row) if row else None


def destroy_session(sid: str):
    db.execute("DELETE FROM sessions WHERE id = ?", (sid,))


def create_account(username: str, password: str, email: Optional[str] = None,
                   display_name: str = "", note: str = "", is_admin: bool = False,
                   domain: Optional[str] = None) -> int:
    """Create a local account; returns account id."""
    acct = username if not domain else f"{username}@{domain}"
    is_local = 1 if domain is None else 0
    pwh = hash_password(password) if password else None
    cur = db.execute(
        """INSERT INTO accounts (username, domain, acct, display_name, note, created_at, is_admin, is_local, password_hash, email, fields)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (username, domain, acct, display_name or username, note, now_iso(), 1 if is_admin else 0, is_local, pwh, email, "[]"),
    )
    return cur.lastrowid


def find_account_by_acct(acct: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM accounts WHERE acct = ?", (acct,))
    return dict(row) if row else None


def find_account_by_username(username: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM accounts WHERE username = ? AND is_local = 1",
        (username,),
    )
    return dict(row) if row else None


def find_account_by_id(aid: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (aid,))
    return dict(row) if row else None


def authenticate_local(username_or_email: str, password: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM accounts WHERE (username = ? OR email = ?) AND is_local = 1",
        (username_or_email, username_or_email),
    )
    if not row:
        return None
    if not verify_password(password, row["password_hash"] or ""):
        return None
    return dict(row)
