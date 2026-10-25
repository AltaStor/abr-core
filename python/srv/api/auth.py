# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from python.srv.services import auth_service

from ._routing import build_router, register
from .auth_guard import require_session
from .schemas import ExtendSessionResult, LoginResult, LogoutResult, UserResult, WhoAmIResult

router = build_router("/auth", tag="auth", protected=False)


WEB_SESSION_DURATION = (
    "Web sessions only: sign out after this many seconds of inactivity, between ABR_WEB_SESSION_MIN_SECONDS and "
    "ABR_WEB_SESSION_MAX_SECONDS; leaving it out uses ABR_WEB_SESSION_TTL_SECONDS"
)


class LoginRequest(BaseModel):
    name: str
    password: str
    duration_seconds: int | None = Field(default=None, description=WEB_SESSION_DURATION)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ExtendSessionRequest(BaseModel):
    duration_seconds: int = Field(
        description="The current web session's new inactivity timeout, seconds, between ABR_WEB_SESSION_MIN_SECONDS "
        "and ABR_WEB_SESSION_MAX_SECONDS; the limit from sign-in (ABR_WEB_SESSION_LIFETIME_SECONDS) does not move"
    )


def login(payload: LoginRequest, request: Request) -> LoginResult:
    client_type = request.headers.get("X-Client-Type", "")
    return auth_service.login(
        payload.name, payload.password, client_type=client_type, duration_seconds=payload.duration_seconds
    )


def logout(session: dict = Depends(require_session)) -> LogoutResult:
    auth_service.logout(session["token"])
    return {"status": "ok"}


def change_password(payload: ChangePasswordRequest, session: dict = Depends(require_session)) -> UserResult:
    return auth_service.change_password(
        session["user_id"], payload.current_password, payload.new_password, keep_token=session["token"]
    )


def extend_session(payload: ExtendSessionRequest, session: dict = Depends(require_session)) -> ExtendSessionResult:
    return auth_service.extend_session(session["token"], payload.duration_seconds)


def touch_session(session: dict = Depends(require_session)) -> ExtendSessionResult:
    return auth_service.touch_session(session["token"])


def whoami(session: dict = Depends(require_session)) -> WhoAmIResult:
    """The signed-in user, their role, and the function IDs that role allows."""
    return auth_service.whoami(session["user_id"])


register(router, [
    ("/login", login, ["POST"], "auth.login"),
    ("/logout", logout, ["POST"], "auth.logout"),
    ("/change-password", change_password, ["POST"], "auth.change_password"),
    ("/session/duration", extend_session, ["PUT"], "auth.extend_session"),
    ("/session/touch", touch_session, ["POST"], "auth.touch_session"),
    ("/me", whoami, ["GET"], "auth.whoami"),
])
