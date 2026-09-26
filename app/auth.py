"""Minimal cookie-session login gate.

This isn't a real multi-user system — the app has no per-user data (the
watchlist and journal live in each visitor's own browser via
localStorage). Its only job is to keep the public URL from being wide
open to anyone who stumbles on the link, while still letting people
without the owner's password in via a labeled demo account.

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
import secrets
import time
from typing import Optional

from fastapi import Cookie, HTTPException

COOKIE_NAME = "swinglab_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days

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


def check_credentials(username: str, password: str) -> bool:
    expected = _USERS.get(username)
    return expected is not None and hmac.compare_digest(password, expected)


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
    if username not in _USERS:
        return None
    return username


def require_login(swinglab_session: Optional[str] = Cookie(default=None)) -> str:
    username = verify_session_token(swinglab_session)
    if username is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return username
