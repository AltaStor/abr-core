# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Request

from python.srv.services import rbac, role_service

from ._routing import build_router, register
from .schemas import (
    CreateRoleRequest,
    DeletedResult,
    FunctionList,
    RoleDTO,
    RoleList,
    UpdateRoleRequest,
    unset_excluded,
)

router = build_router("", tag="roles")


def list_roles() -> RoleList:
    """The built-in roles, then the custom ones, each with its permission on
    every function and how many users hold it."""
    return {"items": role_service.list_roles()}


def get_role(role_id: str) -> RoleDTO:
    """A role by its ID, or by its name in any case."""
    return role_service.get_role(role_id)


def create_role(request: Request, payload: CreateRoleRequest = Body(default_factory=CreateRoleRequest)) -> RoleDTO:
    """Create a custom role holding `allowed_functions`, or clone the role
    `clone_from` (built-in or custom), whose functions and description are then
    the defaults. The new role can only allow functions the caller's own role
    allows; the self-service functions (`always_allowed` in GET /functions)
    are allowed whatever the list says."""
    role, details = role_service.create_role(
        unset_excluded(payload), acting_user_id=getattr(request.state, "user_id", None)
    )
    request.state.audit.details.update(details)
    return role


def update_role(
    role_id: str, request: Request, payload: UpdateRoleRequest = Body(default_factory=UpdateRoleRequest)
) -> RoleDTO:
    """Change a custom role's name, description or allowed functions (the
    whole list). Its users get the change on their next request. Built-in
    roles, the caller's own role and roles allowing functions the caller's
    role does not cannot be changed."""
    role, changes = role_service.update_role(
        role_id, unset_excluded(payload), acting_user_id=getattr(request.state, "user_id", None)
    )
    if changes:
        request.state.audit.details["changes"] = changes
    return role


def delete_role(role_id: str, request: Request) -> DeletedResult:
    """Delete a custom role. A role users hold is refused (`409 role_in_use`):
    give them another role first."""
    return role_service.delete_role(role_id, acting_user_id=getattr(request.state, "user_id", None))


def list_functions() -> FunctionList:
    """Every product function a role can allow, by function ID."""
    return {"items": [rbac.function_to_dto(function) for function in rbac.FUNCTIONS]}


register(router, [
    ("/roles", list_roles, ["GET"], "role.view"),
    ("/roles", create_role, ["POST"], "role.create"),
    ("/roles/{role_id}", get_role, ["GET"], "role.view"),
    ("/roles/{role_id}", update_role, ["PATCH"], "role.edit"),
    ("/roles/{role_id}", delete_role, ["DELETE"], "role.delete"),
    ("/functions", list_functions, ["GET"], "role.view"),
])
