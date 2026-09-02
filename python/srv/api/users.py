# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body

from python.srv.services import auth_service

from ._routing import build_router, register
from .schemas import CreateUserRequest, PublicUserDTO, UserList, unset_excluded

router = build_router("/users", tag="users")


def list_users() -> UserList:
    return {"items": auth_service.list_users()}


def create_user(payload: CreateUserRequest = Body(default_factory=CreateUserRequest)) -> PublicUserDTO:
    return auth_service.create_user(unset_excluded(payload))


def delete_user(user_id: str) -> dict[str, Any]:
    return auth_service.remove_user(user_id)


register(router, [
    ("", list_users, ["GET"]),
    ("", create_user, ["POST"]),
    ("/{user_id}", delete_user, ["DELETE"]),
])
