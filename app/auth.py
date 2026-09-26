"""Cookie-session login gate, with real signup.

Two kinds of accounts:
  - A fixed owner account + a public demo account (below), for anyone who
    doesn't want to sign up.
  - Self-service accounts created via signup(), stored in the same SQLite
    file the stock cache uses (app/db.py) — real usernames/passwords,
    hashed with a per-user salt, never stored or logged in plaintext.

Durability note, stated plainly: on Render's free tier, that SQLite file
lives on ephemeral disk and is wiped on redeploy or a period of inactivity
(see README). A signed-up account can disappear when that happens. The
owner account (SWINGLAB_USERNAME/SWINGLAB_PASSWORD as Render environment
variables, not stored on disk) is the one login guaranteed to survive a
restart — self-service signup is for convenience, not the durable option,
unless a persistent disk is added (also in the README).

Security note, stated plainly: this repo is public on GitHub. Any
hardcoded fallback secret here would be visible to anyone reading the
source, which defeats the point of it being a secret. So:
  - SWINGLAB_SECRET_KEY (signs session cookies): if not set via an
    environment variable, a random one is generated at process startup.
    Safe by default (nobody can forge a session without it), with one
    real cost — every restart invalidates existing sessions. Set the env
    var on Render for sessions that survive a redeploy.
  - SWINGLAB_PASSWORD (the owner's real login): if not set, a random one
    is generated at startup and printed once to the server's own log
    output — never to a public place. Set SWINGLAB_USERNAME /
    SWINGLAB_PASSWORD as Render environment variables for a permanent,
    memorable login instead of re-checking the log after every restart.
  - The demo account (demo / demo1234) is meant to be public. It exists
    so anyone opening the link can actually see the site without asking
    for the real password.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Optional, Tuple

from fastapi import Cookie, HTTPException

from . import db

COOKIE_NAME = "swinglab_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days
PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")

SECRET_KEY = os.environ.get("SWINGLAB_SECRET_KEY", "").encode() or secrets.token_bytes(32)

OWNER_USERNAME = os.environ.get("SWINGLAB_USERNAME", "rishit")
OWNER_PASSWORD = os.environ.get("SWINGLAB_PASSWORD")
GENERATED_OWNER_PASSWORD: str | None = None
if not OWNER_PASSWORD:
    GENERATED_OWNER_PASSWORD = secrets.token_urlsafe(9)
    OWNER_PASSWORD = GENERATED_OWNER_PASSWORD

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo1234"

_USERS = {OWNER_USERNAME: OWNER_PASSWORD, DEMO_USERNAME: DEMO_PASSWORD}
_RESERVED_USERNAMES = {OWNER_USERNAME.lower(), DEMO_USERNAME.lower()}


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()


def signup(username: str, password: str) -> Tuple[bool, str]:
    """Creates a self-service account. Returns (ok, error_message) — the
    message is empty on success."""
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        return False, "Username must be 3-30 characters: letters, numbers, underscores only."
    if username.lower() in _RESERVED_USERNAMES:
        return False, "That username is reserved — try another."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    if db.get_user(username) is not None:
        return False, "That username is already taken."
    salt = secrets.token_hex(16)
    ok = db.create_user(username, _hash_password(password, salt), salt)
    if not ok:
        return False, "That username is already taken."
    return True, ""


def check_credentials(username: str, password: str) -> bool:
    expected = _USERS.get(username)
    if expected is not None:
        return hmac.compare_digest(password, expected)
    user = db.get_user(username)
    if user is None:
        return False
    return hmac.compare_digest(_hash_password(password, user["salt"]), user["password_hash"])


def make_session_token(username: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET_KEY, f"{username}:{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{username}:{ts}:{sig}"


def verify_session_token(token: str | None) -> str | None:
    if not token or token.count(":") != 2:
        return None
    username, ts, sig = token.split(":")
    expected_sig = hmac.new(SECRET_KEY, f"{username}:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if not ts.isdigit() or time.time() - int(ts) > SESSION_MAX_AGE_SECONDS:
        return None
    if username not in _USERS and db.get_user(username) is None:
        return None
    return username


def require_login(swinglab_session: Optional[str] = Cookie(default=None)) -> str:
    username = verify_session_token(swinglab_session)
    if username is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return username
