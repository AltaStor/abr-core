# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from python.srv.services import auth_service, rbac
from python.srv.services.api_errors import api_error
from python.srv.services.audit_service import AuditContext, auth_session_ref, is_background_refresh, target_from_path

_bearer_scheme = HTTPBearer(auto_error=False, description="Session token from POST /api/v1/auth/login")


def require_session(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)
) -> dict:
    token = credentials.credentials if credentials else None
    session = auth_service.authenticate(token, background=is_background_refresh(request.headers))
    if session is None:
        raise api_error(401, "unauthorized", "Missing, invalid, or expired session token.")

    request.state.user_id = session["user_id"]
    request.state.user_name = session["user_name"]
    request.state.session_token = session["token"]
    return session


def authorize(function_id: str) -> Callable[..., dict]:
    """The dependency that lets a request run ``function_id`` only when the
    signed-in user's role allows it, before the route's handler runs."""
    if function_id not in rbac.FUNCTIONS_BY_ID:
        raise ValueError(f"Unknown function ID {function_id!r}")

    def dependency(request: Request, session: dict = Depends(require_session)) -> dict:
        request.state.audit = AuditContext(
            function_id,
            user=session.get("user_name"),
            user_id=session.get("user_id"),
            role=session.get("role"),
            auth_session_id=auth_session_ref(session.get("token")),
            target=target_from_path(dict(request.path_params)),
        )
        rbac.require(session, function_id)
        return session

    dependency.__name__ = f"authorize_{function_id.replace('.', '_')}"
    return dependency
