# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from python.srv.services import rbac
from python.srv.services.api_errors import api_error

from ._routing import build_router, register
from .schemas import FunctionList, RoleDTO, RoleList

router = build_router("", tag="roles")


def list_roles() -> RoleList:
    """The built-in roles and, for each, its permission on every function.
    Roles cannot be created, changed or deleted."""
    return {"items": [rbac.role_to_dto(role) for role in rbac.ROLES]}


def get_role(role_id: str) -> RoleDTO:
    role = rbac.resolve_role(role_id)
    if role is None:
        raise api_error(404, "role_not_found", f"Role not found: {role_id}")
    return rbac.role_to_dto(role)


def list_functions() -> FunctionList:
    """Every product function a role can allow, by function ID."""
    return {"items": [rbac.function_to_dto(function) for function in rbac.FUNCTIONS]}


register(router, [
    ("/roles", list_roles, ["GET"], "role.view"),
    ("/roles/{role_id}", get_role, ["GET"], "role.view"),
    ("/functions", list_functions, ["GET"], "role.view"),
])
