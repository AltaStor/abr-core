# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Custom roles: roles an Administrator makes next to the built-in ones, each
holding an explicit list of allowed functions from the same catalog
(``rbac.FUNCTIONS``). A function the list leaves out is disabled.

Nobody can use a role to gain access they do not have: a caller can only
grant, clone, change or delete a role whose functions their own role allows,
cannot change or delete the role they hold, and cannot assign or take over a
user whose role allows more than theirs (``ensure_can_manage_user``). The
built-in roles cannot be changed, and a role that users hold cannot be
deleted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    CUSTOM_ROLES,
    USERS,
    catalog_records,
    delete_custom_role,
    persist_custom_role,
)
from python.srv.core.utils import utc_now

from . import rbac
from .api_errors import api_error

MAX_ROLE_NAME_LENGTH = 64
MAX_ROLE_DESCRIPTION_LENGTH = 256
ROLE_NAME_PATTERN = re.compile(r"^[^\W_][\w .,()&+'-]*$")


def _caller(acting_user_id: str | None) -> tuple[str | None, str | None]:
    """The acting user's ID and their role now (their record, not the session)."""
    user = USERS.get(acting_user_id or "")
    return (acting_user_id, rbac.role_id_of(user)) if user is not None else (acting_user_id, None)


def users_holding(role_id: str) -> list[dict[str, Any]]:
    return sorted(
        (user for user in catalog_records(USERS) if user.get("role") == role_id),
        key=lambda user: str(user.get("name") or "").lower(),
    )


def role_to_dto(role: rbac.Role) -> dict[str, Any]:
    dto = rbac.role_to_dto(role)
    dto["user_count"] = len(users_holding(role.id))
    record = CUSTOM_ROLES.get(role.id) if not role.built_in else None
    if record is not None:
        for key in ("created_at", "updated_at", "cloned_from"):
            if record.get(key):
                dto[key] = record[key]
    return dto


def list_roles() -> list[dict[str, Any]]:
    return [role_to_dto(role) for role in rbac.all_roles()]


def _role_or_404(role_id: str) -> rbac.Role:
    role = rbac.resolve_role(role_id)
    if role is None:
        raise api_error(404, "role_not_found", f"Role not found: {role_id}")
    return role


def get_role(role_id: str) -> dict[str, Any]:
    return role_to_dto(_role_or_404(role_id))


def _name_or_422(value: Any, *, role_id: str | None = None) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise api_error(422, "invalid_role", "A role name is required.")
    if len(name) > MAX_ROLE_NAME_LENGTH:
        raise api_error(422, "invalid_role", f"A role name must be at most {MAX_ROLE_NAME_LENGTH} characters.")
    if not ROLE_NAME_PATTERN.fullmatch(name):
        raise api_error(
            422,
            "invalid_role",
            "A role name must start with a letter or digit and use letters, digits, spaces and . , ( ) & + ' -",
        )
    key = rbac.role_key(name)
    for role in rbac.all_roles():
        if role.id != role_id and key in (rbac.role_key(role.name), rbac.role_key(role.id)):
            raise api_error(409, "role_exists", f"A role named {role.name!r} already exists.", role_id=role.id)
    return name


def _description_or_422(value: Any) -> str:
    description = str(value or "").strip()
    if len(description) > MAX_ROLE_DESCRIPTION_LENGTH:
        raise api_error(
            422, "invalid_role", f"A role description must be at most {MAX_ROLE_DESCRIPTION_LENGTH} characters."
        )
    return description


def _functions_or_422(value: Any) -> list[str]:
    """The allowed function IDs, in catalog order, the baseline included."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise api_error(422, "invalid_role", "allowed_functions must be a list of function IDs.")
    unknown = sorted({item for item in value if item not in rbac.FUNCTIONS_BY_ID})
    if unknown:
        raise api_error(
            422,
            "invalid_role",
            f"Unknown function IDs: {', '.join(unknown)} (abr role functions lists them).",
            functions=unknown,
        )
    wanted = set(value) | rbac.BASELINE_FUNCTIONS
    return [function.id for function in rbac.FUNCTIONS if function.id in wanted]


def _refuse_beyond_caller(
    functions: Iterable[str], caller_role: str | None, *, function_id: str, what: str
) -> None:
    extra = rbac.beyond(functions, caller_role)
    if extra:
        rbac.deny(
            "exceeds_own_permissions",
            f"{what} would allow functions your own role does not: {', '.join(extra)}.",
            function_id=function_id,
            role=caller_role,
            functions=extra,
        )


def _refuse_built_in(role: rbac.Role) -> None:
    if role.built_in:
        raise api_error(
            409,
            "built_in_role",
            f"{role.name} is a built-in role and cannot be changed or deleted. Clone it to make a custom role.",
            role_id=role.id,
        )


def _refuse_own_role(role: rbac.Role, caller_role: str | None, *, function_id: str, change: str) -> None:
    if role.id == caller_role:
        rbac.deny(
            "own_role",
            f"You cannot {change} {role.name}: it is your own role. Ask another user who may manage roles.",
            function_id=function_id,
            role=caller_role,
        )


def _copy_name(source: rbac.Role) -> str:
    taken = {rbac.role_key(role.name) for role in rbac.all_roles()} | {rbac.role_key(role.id) for role in rbac.all_roles()}
    for number in range(1, 1000):
        name = f"{source.name} (copy)" if number == 1 else f"{source.name} (copy {number})"
        if rbac.role_key(name) not in taken and len(name) <= MAX_ROLE_NAME_LENGTH:
            return name
    return f"{source.name[: MAX_ROLE_NAME_LENGTH - 16]} ({uuid4().hex[:8]})"


def _new_role_id() -> str:
    while True:
        role_id = f"{rbac.CUSTOM_ROLE_ID_PREFIX}{uuid4().hex[:8]}"
        if role_id not in CUSTOM_ROLES:
            return role_id


def create_role(
    payload: Mapping[str, Any], *, acting_user_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a custom role, from scratch or as a clone of ``clone_from`` (a
    built-in or custom role), whose functions and description are then the
    defaults. Answers the role and the audit details."""
    _, caller_role = _caller(acting_user_id)
    source = None
    if payload.get("clone_from") not in (None, ""):
        source = _role_or_404(str(payload["clone_from"]))
    if source is not None and payload.get("name") in (None, ""):
        name = _copy_name(source)
    else:
        name = _name_or_422(payload.get("name"))
    if payload.get("description") is not None:
        description = _description_or_422(payload.get("description"))
    else:
        description = source.description if source is not None else ""
    if payload.get("allowed_functions") is not None:
        functions = _functions_or_422(payload["allowed_functions"])
    elif source is not None:
        functions = rbac.allowed_functions(source.id)
    else:
        raise api_error(422, "invalid_role", "Give the role's allowed_functions, or a role to clone_from.")
    _refuse_beyond_caller(
        functions, caller_role, function_id="role.create", what=f"Clone of {source.name}" if source else "The role"
    )

    now = utc_now()
    record: dict[str, Any] = {
        "id": _new_role_id(),
        "name": name,
        "description": description,
        "allowed_functions": functions,
        "created_at": now,
        "updated_at": now,
    }
    details: dict[str, Any] = {"allowed_functions": functions}
    if source is not None:
        record["cloned_from"] = {"id": source.id, "name": source.name}
        details["cloned_from"] = record["cloned_from"]
    persist_custom_role(record)
    return role_to_dto(_role_or_404(record["id"])), details


def update_role(
    role_id: str, payload: Mapping[str, Any], *, acting_user_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change a custom role's name, description or allowed functions (the
    whole list). Answers the role and what changed: ``{"name": {"from", "to"},
    "permissions": {"granted": [...], "revoked": [...]}}``."""
    _, caller_role = _caller(acting_user_id)
    role = _role_or_404(role_id)
    _refuse_built_in(role)
    _refuse_own_role(role, caller_role, function_id="role.edit", change="change")
    current = dict(CUSTOM_ROLES[role.id])
    before = rbac.allowed_functions(role.id)
    _refuse_beyond_caller(before, caller_role, function_id="role.edit", what=f"Changing {role.name}")

    updated = dict(current)
    if payload.get("name") is not None:
        updated["name"] = _name_or_422(payload["name"], role_id=role.id)
    if payload.get("description") is not None:
        updated["description"] = _description_or_422(payload["description"])
    after = before
    if payload.get("allowed_functions") is not None:
        after = _functions_or_422(payload["allowed_functions"])
        _refuse_beyond_caller(after, caller_role, function_id="role.edit", what=f"The new {role.name}")
        updated["allowed_functions"] = after

    changes: dict[str, Any] = {
        field: {"from": current.get(field, ""), "to": updated.get(field, "")}
        for field in ("name", "description")
        if current.get(field, "") != updated.get(field, "")
    }
    granted = [function_id for function_id in after if function_id not in before]
    revoked = [function_id for function_id in before if function_id not in after]
    if granted or revoked:
        changes["permissions"] = {"granted": granted, "revoked": revoked}
    if changes:
        updated["updated_at"] = utc_now()
        persist_custom_role(updated)
    return role_to_dto(_role_or_404(role.id)), changes


def delete_role(role_id: str, *, acting_user_id: str | None) -> dict[str, Any]:
    _, caller_role = _caller(acting_user_id)
    role = _role_or_404(role_id)
    _refuse_built_in(role)
    _refuse_own_role(role, caller_role, function_id="role.delete", change="delete")
    _refuse_beyond_caller(
        rbac.allowed_functions(role.id), caller_role, function_id="role.delete", what=f"Deleting {role.name}"
    )
    holders = users_holding(role.id)
    if holders:
        names = [str(user.get("name") or user.get("id")) for user in holders]
        raise api_error(
            409,
            "role_in_use",
            f"{role.name} cannot be deleted while users hold it: {', '.join(names)}. Give them another role first.",
            role_id=role.id,
            users=names,
        )
    delete_custom_role(role.id)
    return {"id": role.id, "deleted": True}


def ensure_can_manage_user(
    acting_user_id: str | None,
    *,
    function_id: str,
    target: Mapping[str, Any] | None = None,
    new_role: str | None = None,
) -> None:
    """Refuse a user change that would give the caller, or anyone, more than
    the caller's own role allows: assigning a role with functions the caller
    lacks, changing a user whose role has such functions (their password, say,
    after which the caller could sign in as them), or changing one's own
    role."""
    if acting_user_id is None:
        return
    _, caller_role = _caller(acting_user_id)
    if target is not None:
        name = str(target.get("name") or target.get("id"))
        current = rbac.role_id_of(target)
        extra = rbac.beyond(rbac.allowed_functions(current), caller_role) if current else []
        if extra:
            rbac.deny(
                "exceeds_own_permissions",
                f"{name}'s role allows functions your own role does not ({', '.join(extra)}), "
                "so you cannot change or delete them.",
                function_id=function_id,
                role=caller_role,
                functions=extra,
            )
        if new_role is not None and new_role != target.get("role") and target.get("id") == acting_user_id:
            rbac.deny(
                "own_role",
                "You cannot change your own role. Ask another user who may manage users.",
                function_id=function_id,
                role=caller_role,
            )
    if new_role is not None:
        role = rbac.get_role(new_role)
        _refuse_beyond_caller(
            rbac.allowed_functions(new_role),
            caller_role,
            function_id=function_id,
            what=f"The {role.name if role else new_role} role",
        )
