# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from python.srv.services import auth_service
from python.srv.services.api_errors import api_error

_bearer_scheme = HTTPBearer(auto_error=False, description="Session token from POST /api/v1/auth/login")


def require_session(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)
) -> dict:
    token = credentials.credentials if credentials else None
    session = auth_service.authenticate(token)
    if session is None:
        raise api_error(401, "unauthorized", "Missing, invalid, or expired session token.")

    request.state.user_id = session["user_id"]
    request.state.user_name = session["user_name"]
    request.state.session_token = session["token"]
    return session
