# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from pydantic import BaseModel

from python.srv.services import auth_service

from ._routing import build_router, register
from .auth_guard import require_session

router = build_router("/auth", tag="auth", protected=False)


class LoginRequest(BaseModel):
    name: str
    password: str
    duration_seconds: int | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ExtendSessionRequest(BaseModel):
    duration_seconds: int


def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    client_type = request.headers.get("X-Client-Type", "")
    return auth_service.login(
        payload.name, payload.password, client_type=client_type, duration_seconds=payload.duration_seconds
    )


def logout(session: dict = Depends(require_session)) -> dict[str, Any]:
    auth_service.logout(session["token"])
    return {"status": "ok"}


def change_password(payload: ChangePasswordRequest, session: dict = Depends(require_session)) -> dict[str, Any]:
    return auth_service.change_password(
        session["user_id"], payload.current_password, payload.new_password, keep_token=session["token"]
    )


def extend_session(payload: ExtendSessionRequest, session: dict = Depends(require_session)) -> dict[str, Any]:
    return auth_service.extend_session(session["token"], payload.duration_seconds)


register(router, [
    ("/login", login, ["POST"]),
    ("/logout", logout, ["POST"]),
    ("/change-password", change_password, ["POST"]),
    ("/session/duration", extend_session, ["PUT"]),
])
