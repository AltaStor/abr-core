# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Request

from python.srv.services import auth_service, rbac

from ._routing import build_router, register
from .schemas import (
    CreateUserRequest,
    DeletedResult,
    PublicUserDTO,
    UpdateUserRequest,
    UserList,
    unset_excluded,
)

router = build_router("/users", tag="users")


def list_users() -> UserList:
    return {"items": auth_service.list_users()}


def get_user(user_id: str) -> PublicUserDTO:
    return auth_service.get_user(user_id)


def _role_ref(role_id: str | None) -> dict | None:
    role = rbac.get_role(role_id)
    return {"id": role_id, "name": role.name if role else None, "built_in": role.built_in if role else None}


def create_user(request: Request, payload: CreateUserRequest = Body(default_factory=CreateUserRequest)) -> PublicUserDTO:
    """Create a user holding one role: administrator, backup_administrator,
    backup_operator, restore_operator, auditor or a custom role (by ID or
    name). The role can only allow functions the caller's own role allows."""
    user = auth_service.create_user(unset_excluded(payload), acting_user_id=getattr(request.state, "user_id", None))
    request.state.audit.details["role"] = _role_ref(user.get("role"))
    return user


def update_user(
    user_id: str, request: Request, payload: UpdateUserRequest = Body(default_factory=UpdateUserRequest)
) -> PublicUserDTO:
    """Change a user's login name, full name, role, enabled state or password.
    Disabling a user or giving them a new password ends their sessions; a
    user's own password needs their current one (POST /auth/change-password).
    Nobody changes their own role, nor a user, or assigns a role, allowing
    functions their own role does not. The last enabled Administrator cannot
    be demoted or disabled."""
    user, changes = auth_service.update_user(
        user_id, unset_excluded(payload), acting_user_id=getattr(request.state, "user_id", None)
    )
    if changes:
        request.state.audit.details["changes"] = changes
    if "role" in changes:
        request.state.audit.details["roles"] = {
            "from": _role_ref(changes["role"]["from"]),
            "to": _role_ref(changes["role"]["to"]),
        }
    return user


def delete_user(user_id: str, request: Request) -> DeletedResult:
    """Delete a user. The last enabled Administrator cannot be deleted, nor a
    user whose role allows functions the caller's own role does not."""
    return auth_service.remove_user(user_id, acting_user_id=getattr(request.state, "user_id", None))


register(router, [
    ("", list_users, ["GET"], "user.view"),
    ("", create_user, ["POST"], "user.create"),
    ("/{user_id}", get_user, ["GET"], "user.view"),
    ("/{user_id}", update_user, ["PATCH"], "user.edit"),
    ("/{user_id}", delete_user, ["DELETE"], "user.delete"),
])
