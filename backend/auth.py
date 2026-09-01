from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

from fastapi import Cookie, HTTPException, status

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import db_manager as dbm  # noqa: E402

SESSION_COOKIE = "session_token"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 hours

# token -> {user_id, username, role, expires_at}
_SESSIONS: dict[str, dict] = {}


def effective_role(user: dict) -> str:
    # Special-case the built-in `dev` user: assign a distinct role so
    # callers can route them to the Test UI and restrict admin pages.
    if user.get("username") == "dev":
        return "dev"
    return user.get("role") or "user"


def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    role = effective_role(user)
    _SESSIONS[token] = {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": role,
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }
    return token


def _get_session(token: str | None) -> dict | None:
    if not token:
        return None
    session = _SESSIONS.get(token)
    if not session:
        return None
    if session["expires_at"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    return session


def destroy_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def session_from_cookie(session_token: str | None) -> dict | None:
  
    return _get_session(session_token)


def login(username: str, password: str) -> dict | None:

    user = dbm.get_user_by_username(username)
    if not user or not dbm.verify_password(password, user["pass_hash"]):
        return None
    return user


def require_user(session_token: str | None = Cookie(default=None)) -> dict:
    session = _get_session(session_token)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    return session


def require_admin(session_token: str | None = Cookie(default=None)) -> dict:
    session = require_user(session_token)
    # `dev` is the test-DB operator and may run pipeline/scraper APIs.
    # Admin HTML pages still gate on role == "admin" in main.py.
    if session["role"] not in ("admin", "dev"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return session


def require_dev(session_token: str | None = Cookie(default=None)) -> dict:
    session = require_user(session_token)
    # Admins may also use Test Mode when COMMISSIONS_DB points at a test DB
    # (endpoint handlers still gate on non-default DB).
    if session.get("role") not in ("dev", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Dev access required")
    return session
