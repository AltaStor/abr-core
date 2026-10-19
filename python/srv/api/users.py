# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Request

from python.srv.services import auth_service

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


def create_user(payload: CreateUserRequest = Body(default_factory=CreateUserRequest)) -> PublicUserDTO:
    """Create a user holding one role: administrator, backup_administrator,
    backup_operator, restore_operator or auditor."""
    return auth_service.create_user(unset_excluded(payload))


def update_user(
    user_id: str, request: Request, payload: UpdateUserRequest = Body(default_factory=UpdateUserRequest)
) -> PublicUserDTO:
    """Change a user's full name, role or enabled state. Disabling a user ends
    their sessions. The last enabled Administrator cannot be demoted or
    disabled."""
    user, changes = auth_service.update_user(user_id, unset_excluded(payload))
    if changes:
        request.state.audit.details["changes"] = changes
    return user


def delete_user(user_id: str) -> DeletedResult:
    """Delete a user. The last enabled Administrator cannot be deleted."""
    return auth_service.remove_user(user_id)


register(router, [
    ("", list_users, ["GET"], "user.view"),
    ("", create_user, ["POST"], "user.create"),
    ("/{user_id}", get_user, ["GET"], "user.view"),
    ("/{user_id}", update_user, ["PATCH"], "user.edit"),
    ("/{user_id}", delete_user, ["DELETE"], "user.delete"),
])
