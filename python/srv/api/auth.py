# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from python.srv.services import auth_service

from ._routing import build_router, register
from .auth_guard import require_session
from .schemas import (
    ExtendSessionResult,
    LoginResult,
    LogoutResult,
    RecoveryCodesResult,
    TotpEnableResult,
    TotpSetupResult,
    UserResult,
    WhoAmIResult,
)

router = build_router("/auth", tag="auth", protected=False)


WEB_SESSION_DURATION = (
    "Web sessions only: sign out after this many seconds of inactivity, between ABR_WEB_SESSION_MIN_SECONDS and "
    "ABR_WEB_SESSION_MAX_SECONDS; leaving it out uses ABR_WEB_SESSION_TTL_SECONDS"
)

TOTP_CODE = (
    "The 6-digit code from the authenticator app of a user with two-factor sign-in on. Leaving out both it and "
    "recovery_code answers 401 totp_required once the password is right"
)
RECOVERY_CODE = "An unused recovery code, in place of totp_code; it works once"
SECOND_FACTOR = (
    "With two-factor sign-in on: the password, and a code from the authenticator app (totp_code) or a recovery code "
    "(recovery_code)"
)


class LoginRequest(BaseModel):
    name: str
    password: str
    duration_seconds: int | None = Field(default=None, description=WEB_SESSION_DURATION)
    totp_code: str | None = Field(default=None, description=TOTP_CODE)
    recovery_code: str | None = Field(default=None, description=RECOVERY_CODE)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ReauthenticationRequest(BaseModel):
    password: str
    totp_code: str | None = Field(default=None, description=SECOND_FACTOR)
    recovery_code: str | None = Field(default=None, description=RECOVERY_CODE)


class TotpEnableRequest(BaseModel):
    totp_code: str = Field(description="The current code from the authenticator app, made from the new secret")


class ExtendSessionRequest(BaseModel):
    duration_seconds: int = Field(
        description="The current web session's new inactivity timeout, seconds, between ABR_WEB_SESSION_MIN_SECONDS "
        "and ABR_WEB_SESSION_MAX_SECONDS; the limit from sign-in (ABR_WEB_SESSION_LIFETIME_SECONDS) does not move"
    )


def login(payload: LoginRequest, request: Request) -> LoginResult:
    client_type = request.headers.get("X-Client-Type", "")
    return auth_service.login(
        payload.name,
        payload.password,
        client_type=client_type,
        duration_seconds=payload.duration_seconds,
        totp_code=payload.totp_code,
        recovery_code=payload.recovery_code,
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


def start_totp_setup(payload: ReauthenticationRequest, session: dict = Depends(require_session)) -> TotpSetupResult:
    """A new secret for the caller's authenticator app, as a QR code and as text: to turn two-factor sign-in on, or
    to set it up again while it is on, which also takes a code or a recovery code. Sign-in does not change until
    POST /auth/totp/enable has seen a code made from the new secret."""
    return auth_service.start_totp_setup(
        session["user_id"], payload.password, totp_code=payload.totp_code, recovery_code=payload.recovery_code
    )


def enable_totp(payload: TotpEnableRequest, session: dict = Depends(require_session)) -> TotpEnableResult:
    """Make the new secret the caller's: two-factor sign-in is on, a previous secret stops working, and new recovery
    codes, shown only here, replace any earlier ones. Ends the caller's other sessions."""
    return auth_service.enable_totp(session["user_id"], payload.totp_code, keep_token=session["token"])


def disable_totp(payload: ReauthenticationRequest, session: dict = Depends(require_session)) -> UserResult:
    """Turn the caller's two-factor sign-in off. An Administrator resets another user's with
    PATCH /users/{user_id}."""
    return auth_service.disable_totp(
        session["user_id"], payload.password, totp_code=payload.totp_code, recovery_code=payload.recovery_code
    )


def regenerate_recovery_codes(
    payload: ReauthenticationRequest, session: dict = Depends(require_session)
) -> RecoveryCodesResult:
    """New recovery codes for the caller, shown only here; every earlier one stops working."""
    return auth_service.regenerate_recovery_codes(
        session["user_id"], payload.password, totp_code=payload.totp_code, recovery_code=payload.recovery_code
    )


def whoami(session: dict = Depends(require_session)) -> WhoAmIResult:
    """The signed-in user, their role, the function IDs that role allows, and when the session ends.

    Sent with X-ABR-Refresh it does not renew the session, so a client can read the expiry without moving it."""
    return auth_service.whoami(session["user_id"], session["token"])


register(router, [
    ("/login", login, ["POST"], "auth.login"),
    ("/logout", logout, ["POST"], "auth.logout"),
    ("/change-password", change_password, ["POST"], "auth.change_password"),
    ("/session/duration", extend_session, ["PUT"], "auth.extend_session"),
    ("/session/touch", touch_session, ["POST"], "auth.touch_session"),
    ("/totp/setup", start_totp_setup, ["POST"], "auth.totp_setup"),
    ("/totp/enable", enable_totp, ["POST"], "auth.totp_enable"),
    ("/totp/disable", disable_totp, ["POST"], "auth.totp_disable"),
    ("/totp/recovery-codes", regenerate_recovery_codes, ["POST"], "auth.totp_recovery_codes"),
    ("/me", whoami, ["GET"], "auth.whoami"),
])
