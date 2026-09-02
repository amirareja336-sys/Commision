from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def do_login(req: LoginRequest, response: Response):
    user = auth.login(req.username, req.password)
    if not user:
        response.status_code = 401
        return {"error": "Invalid username or password"}
    token = auth.create_session(user)
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, samesite="lax", max_age=auth.SESSION_TTL_SECONDS,
    )
    return {"username": user["username"], "role": user["role"]}


@router.post("/logout")
def do_logout(response: Response, session_token: str | None = Cookie(default=None)):
    auth.destroy_session(session_token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(session_token: str | None = Cookie(default=None)):
    session = auth.session_from_cookie(session_token)
    if not session:
        return {"authenticated": False}
    return {"authenticated": True, "username": session["username"], "role": session["role"]}
