# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Role-based access control: the product's functions, its roles, and the one
check every interface (API, CLI, `--output json` automation, Web UI) goes
through before a function runs.

A *function* is one thing the product does, named by a stable ID such as
``job.start`` or ``backup.delete``. A *role* holds a permission, allowed or
disabled, for each function; a pair that is missing is disabled, so access is
denied unless a role grants it. Every user holds exactly one role.

The built-in roles are immutable: they live here, not in the catalog, so no
interface can edit or delete them. A new product function is added by appending
it to ``FUNCTIONS`` and, unless only the Administrator should have it, to the
grant list of the roles that need it.

Custom roles live in the catalog (``CUSTOM_ROLES``, managed by
``role_service``) and hold an explicit list of allowed functions. They are read
on every check, so a change to one applies to its users' next request. Every
role, custom ones included, allows ``BASELINE_FUNCTIONS``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn

from python.srv.core.mongo_db_wrapper import CUSTOM_ROLES

from .api_errors import api_error

READ = "read"
OPERATE = "operate"
CONFIGURE = "configure"
DESTRUCTIVE = "destructive"
ADMINISTER = "administer"
SELF_SERVICE = "self-service"
CATEGORIES = (READ, OPERATE, CONFIGURE, DESTRUCTIVE, ADMINISTER, SELF_SERVICE)

FUNCTION_ID_PATTERN = re.compile(r"^[a-z]+(?:_[a-z]+)*\.[a-z]+(?:_[a-z]+)*$")


@dataclass(frozen=True, slots=True)
class Function:
    id: str
    name: str
    category: str
    starts_session: bool = False


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    name: str
    description: str
    built_in: bool = True


@dataclass(frozen=True, slots=True)
class Permission:
    role_id: str
    function_id: str
    allowed: bool


def _f(function_id: str, name: str, category: str, *, starts_session: bool = False) -> Function:
    return Function(function_id, name, category, starts_session)


FUNCTIONS: tuple[Function, ...] = (
    # Every signed-in user manages their own session and password.
    _f("auth.login", "Sign in", SELF_SERVICE),
    _f("auth.logout", "Sign out", SELF_SERVICE),
    _f("auth.whoami", "Show the signed-in user, role and permissions", READ),
    _f("auth.change_password", "Change one's own password", SELF_SERVICE),
    _f("auth.extend_session", "Change the current web session's inactivity timeout", SELF_SERVICE),
    _f("auth.touch_session", "Keep the current session alive after a user action", SELF_SERVICE),
    _f("dashboard.view", "View the dashboard summary", READ),
    _f("system.view", "View version, appliance information and health", READ),
    _f("system.logs", "Read the appliance's server log", READ),
    _f("system.support_bundle", "Generate a support bundle", ADMINISTER),
    _f("system.export", "Export the catalog and configuration", ADMINISTER),
    _f("system.import", "Import a catalog export over the current catalog", ADMINISTER),
    _f("system.clock_confirm", "Confirm the server time after a clock jump so retention deletes again", ADMINISTER),
    _f("tls.view", "View the HTTPS certificate", READ),
    _f("tls.edit", "Install or revert the HTTPS certificate", ADMINISTER),
    _f("user.view", "List and view users", READ),
    _f("user.create", "Create a user", ADMINISTER),
    _f("user.edit", "Change a user's name, role or enabled state", ADMINISTER),
    _f("user.delete", "Delete a user", ADMINISTER),
    _f("role.view", "List roles, their permissions and the product functions", READ),
    _f("role.create", "Create a custom role, or clone a role into one", ADMINISTER),
    _f("role.edit", "Change a custom role's name, description or permissions", ADMINISTER),
    _f("role.delete", "Delete a custom role no user holds", ADMINISTER),
    _f("audit.view", "Read the audit log", READ),
    _f("repository.view", "List and view repositories", READ),
    _f("repository.create", "Add a repository, or test its connection before adding it", CONFIGURE),
    _f("repository.edit", "Change a repository", CONFIGURE),
    _f("repository.delete", "Remove a repository", DESTRUCTIVE),
    _f("repository.rescan", "Rescan a repository for backups", OPERATE),
    _f("repository.verify", "Verify a repository and its immutability", OPERATE),
    _f("repository.apply_immutability", "Apply immutability to a repository's existing backups", CONFIGURE),
    _f("inventory.view", "List and view inventories and their VMs", READ),
    _f("inventory.create", "Add an inventory", CONFIGURE),
    _f("inventory.edit", "Change an inventory", CONFIGURE),
    _f("inventory.delete", "Remove an inventory", DESTRUCTIVE),
    _f("inventory.rescan", "Rescan an inventory", OPERATE),
    _f("inventory.test", "Test an inventory's connection", OPERATE),
    _f("job.view", "List and view jobs", READ),
    _f("job.create", "Create a backup or backup copy job", CONFIGURE),
    _f("job.edit", "Change, enable or disable a job", CONFIGURE),
    _f("job.delete", "Delete a job", DESTRUCTIVE),
    _f("job.start", "Start a job", OPERATE, starts_session=True),
    _f("job.retention", "Apply or preview a job's retention", DESTRUCTIVE),
    _f("session.view", "List and view sessions and their logs", READ),
    _f("session.stop", "Stop a running session", OPERATE),
    _f("backup.view", "List and view backups and restore points", READ),
    _f("backup.verify", "Verify a backup", OPERATE, starts_session=True),
    _f("backup.delete", "Delete a backup or restore points", DESTRUCTIVE, starts_session=True),
    _f("backup.import", "Import a backup from a repository path", CONFIGURE),
    _f("restore.view", "List restore points and restore placement options", READ),
    _f("restore.start", "Restore a VM", OPERATE, starts_session=True),
)

FUNCTIONS_BY_ID: Mapping[str, Function] = MappingProxyType({function.id: function for function in FUNCTIONS})

# Functions that run before there is a signed-in user to check.
PUBLIC_FUNCTIONS = frozenset({"auth.login"})

ADMINISTRATOR = "administrator"
BACKUP_ADMINISTRATOR = "backup_administrator"
BACKUP_OPERATOR = "backup_operator"
RESTORE_OPERATOR = "restore_operator"
AUDITOR = "auditor"

ROLES: tuple[Role, ...] = (
    Role(ADMINISTRATOR, "Administrator", "Full access to every function, including users, roles and the appliance."),
    Role(
        BACKUP_ADMINISTRATOR,
        "Backup Administrator",
        "Manages repositories, inventories, backup jobs and backups.",
    ),
    Role(BACKUP_OPERATOR, "Backup Operator", "Runs and monitors backup jobs and verifies backups."),
    Role(
        RESTORE_OPERATOR,
        "Restore Operator",
        "Restores VMs, with the inventory operations a recovery needs.",
    ),
    Role(
        AUDITOR,
        "Auditor",
        "Read-only access to the configuration, the operational history, users, roles and the audit log.",
    ),
)

ROLES_BY_ID: Mapping[str, Role] = MappingProxyType({role.id: role for role in ROLES})

CUSTOM_ROLE_ID_PREFIX = "custom-"

# What any signed-in user does to their own session and password. Every role
# allows these, a custom role too: without them a user could not sign out, keep
# a session or change a password the console insists on changing.
BASELINE_FUNCTIONS = frozenset({
    "auth.login",
    "auth.logout",
    "auth.whoami",
    "auth.change_password",
    "auth.extend_session",
    "auth.touch_session",
})

_EVERY_ROLE = (
    "auth.login",
    "auth.logout",
    "auth.whoami",
    "auth.change_password",
    "auth.extend_session",
    "auth.touch_session",
    "dashboard.view",
    "system.view",
)

# The permission matrix: what each role is allowed. The Administrator is
# allowed everything, including functions added in later releases; every other
# role is allowed exactly what is listed and nothing else.
_GRANTS: Mapping[str, frozenset[str]] = MappingProxyType({
    ADMINISTRATOR: frozenset(FUNCTIONS_BY_ID),
    BACKUP_ADMINISTRATOR: frozenset((
        *_EVERY_ROLE,
        "system.logs",
        "repository.view",
        "repository.create",
        "repository.edit",
        "repository.delete",
        "repository.rescan",
        "repository.verify",
        "repository.apply_immutability",
        "inventory.view",
        "inventory.create",
        "inventory.edit",
        "inventory.delete",
        "inventory.rescan",
        "inventory.test",
        "job.view",
        "job.create",
        "job.edit",
        "job.delete",
        "job.start",
        "job.retention",
        "session.view",
        "session.stop",
        "backup.view",
        "backup.verify",
        "backup.delete",
        "backup.import",
    )),
    BACKUP_OPERATOR: frozenset((
        *_EVERY_ROLE,
        "repository.view",
        "inventory.view",
        "job.view",
        "job.start",
        "session.view",
        "session.stop",
        "backup.view",
        "backup.verify",
    )),
    RESTORE_OPERATOR: frozenset((
        *_EVERY_ROLE,
        "repository.view",
        "inventory.view",
        "inventory.rescan",
        "inventory.test",
        "session.view",
        "session.stop",
        "backup.view",
        "backup.verify",
        "restore.view",
        "restore.start",
    )),
    AUDITOR: frozenset((
        *_EVERY_ROLE,
        "system.logs",
        "tls.view",
        "user.view",
        "role.view",
        "audit.view",
        "repository.view",
        "inventory.view",
        "job.view",
        "session.view",
        "backup.view",
    )),
})


def _build_permissions() -> Mapping[tuple[str, str], Permission]:
    for function in FUNCTIONS:
        if not FUNCTION_ID_PATTERN.fullmatch(function.id):
            raise ValueError(f"Function ID {function.id!r} is not of the form area.action")
        if function.category not in CATEGORIES:
            raise ValueError(f"Function {function.id} has an unknown category {function.category!r}")
    if len(FUNCTIONS_BY_ID) != len(FUNCTIONS):
        raise ValueError("Function IDs are not unique")
    if BASELINE_FUNCTIONS - set(FUNCTIONS_BY_ID):
        raise ValueError(f"Unknown baseline functions: {sorted(BASELINE_FUNCTIONS - set(FUNCTIONS_BY_ID))}")
    for role_id, granted in _GRANTS.items():
        unknown = granted - set(FUNCTIONS_BY_ID)
        if unknown:
            raise ValueError(f"Role {role_id} is granted unknown functions: {sorted(unknown)}")
    return MappingProxyType({
        (role.id, function.id): Permission(role.id, function.id, function.id in _GRANTS.get(role.id, ()))
        for role in ROLES
        for function in FUNCTIONS
    })


# Every (role, function) pair, explicitly allowed or disabled.
PERMISSIONS: Mapping[tuple[str, str], Permission] = _build_permissions()


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    function_id: str
    role: str | None
    reason: str


def role_key(value: str) -> str:
    """What two role IDs or names that mean the same role have in common: any
    case, spaces, hyphens or underscores between the words."""
    return re.sub(r"[\s_-]+", "_", value.strip().lower())


def _custom_record(role_id: Any) -> Mapping[str, Any] | None:
    if not isinstance(role_id, str) or role_id in ROLES_BY_ID:
        return None
    return CUSTOM_ROLES.get(role_id)


def _custom_role(record: Mapping[str, Any]) -> Role:
    return Role(str(record["id"]), str(record.get("name") or record["id"]), str(record.get("description") or ""), False)


def get_role(role_id: Any) -> Role | None:
    """A built-in or custom role by its exact ID."""
    if isinstance(role_id, str) and role_id in ROLES_BY_ID:
        return ROLES_BY_ID[role_id]
    record = _custom_record(role_id)
    return _custom_role(record) if record is not None else None


def custom_roles() -> list[Role]:
    return sorted(
        (_custom_role(record) for record in list(CUSTOM_ROLES.values()) if _custom_record(record.get("id"))),
        key=lambda role: (role.name.lower(), role.id),
    )


def all_roles() -> list[Role]:
    """The built-in roles in their fixed order, then the custom ones by name."""
    return [*ROLES, *custom_roles()]


def resolve_role(value: Any) -> Role | None:
    """A role from its ID or its name, in any case, with spaces or hyphens for
    underscores ("backup-operator", "Backup Operator", "night shift")."""
    if not isinstance(value, str) or not value.strip():
        return None
    key = role_key(value)
    if key in ROLES_BY_ID:
        return ROLES_BY_ID[key]
    return next((role for role in custom_roles() if key in (role_key(role.id), role_key(role.name))), None)


def role_id_of(user: Mapping[str, Any] | None) -> str | None:
    role = (user or {}).get("role")
    return role if get_role(role) is not None else None


def is_enabled(user: Mapping[str, Any] | None) -> bool:
    return user is not None and user.get("enabled", True) is not False


def is_active_administrator(user: Mapping[str, Any] | None) -> bool:
    return is_enabled(user) and role_id_of(user) == ADMINISTRATOR


def _custom_grants(role_id: str | None) -> frozenset[str] | None:
    record = _custom_record(role_id)
    if record is None:
        return None
    listed = record.get("allowed_functions")
    granted = {item for item in listed if item in FUNCTIONS_BY_ID} if isinstance(listed, list) else set()
    return frozenset(granted | BASELINE_FUNCTIONS)


def role_allows(role_id: str | None, function_id: str) -> bool:
    permission = PERMISSIONS.get((role_id or "", function_id))
    if permission is not None:
        return permission.allowed
    granted = _custom_grants(role_id)
    return granted is not None and function_id in granted


def check(user: Mapping[str, Any] | None, function_id: str) -> Decision:
    """Whether ``user`` may execute ``function_id``.

    ``user`` is a user record or a signed-in session (both carry ``role`` and,
    for a record, ``enabled``). Denied unless the user is enabled, holds a known
    role, and that role explicitly allows a known function.
    """
    role = (user or {}).get("role")
    role_text = role if isinstance(role, str) and role else None
    if function_id not in FUNCTIONS_BY_ID:
        return Decision(False, function_id, role_text, "unknown_function")
    if user is None or not is_enabled(user):
        return Decision(False, function_id, role_text, "account_disabled")
    if role_id_of(user) is None:
        return Decision(False, function_id, role_text, "no_role")
    if not role_allows(role_text, function_id):
        return Decision(False, function_id, role_text, "not_permitted")
    return Decision(True, function_id, role_text, "allowed")


def is_allowed(user: Mapping[str, Any] | None, function_id: str) -> bool:
    return check(user, function_id).allowed


_DENIAL_MESSAGES = {
    "unknown_function": "{function} is not a known function.",
    "account_disabled": "The account is disabled.",
    "no_role": "The account has no valid role.",
}


def require(user: Mapping[str, Any] | None, function_id: str) -> Decision:
    """Raise the API's ``permission_denied`` error unless ``check`` allows."""
    decision = check(user, function_id)
    if decision.allowed:
        return decision
    function = FUNCTIONS_BY_ID.get(function_id)
    role = get_role(decision.role)
    if decision.reason == "not_permitted":
        reason = f"The {role.name if role else decision.role} role does not allow {function_id}"
        reason += f" ({function.name.lower()})." if function else "."
    else:
        reason = _DENIAL_MESSAGES[decision.reason].format(function=function_id)
    raise api_error(
        403,
        "permission_denied",
        f"Permission denied: {reason}",
        function_id=function_id,
        role=decision.role,
        reason=decision.reason,
    )


def deny(reason: str, message: str, *, function_id: str, role: str | None, **details: Any) -> NoReturn:
    """Raise the ``permission_denied`` error for a request the role allows but
    a rule refuses, such as changing one's own role."""
    raise api_error(
        403, "permission_denied", f"Permission denied: {message}",
        function_id=function_id, role=role, reason=reason, **details,
    )


def allowed_functions(role_id: str | None) -> list[str]:
    return [function.id for function in FUNCTIONS if role_allows(role_id, function.id)]


def beyond(functions: Iterable[str], role_id: str | None) -> list[str]:
    """Those of ``functions`` that ``role_id`` does not allow, in catalog order."""
    wanted = set(functions)
    return [function.id for function in FUNCTIONS if function.id in wanted and not role_allows(role_id, function.id)]


def permissions_for(role_id: str) -> list[Permission]:
    return [Permission(role_id, function.id, role_allows(role_id, function.id)) for function in FUNCTIONS]


def function_to_dto(function: Function) -> dict[str, Any]:
    return {
        "id": function.id,
        "name": function.name,
        "category": function.category,
        "starts_session": function.starts_session,
        "always_allowed": function.id in BASELINE_FUNCTIONS,
    }


def role_to_dto(role: Role, *, with_permissions: bool = True) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "built_in": role.built_in,
    }
    if with_permissions:
        dto["permissions"] = [
            {"function_id": permission.function_id, "allowed": permission.allowed}
            for permission in permissions_for(role.id)
        ]
        dto["allowed_functions"] = allowed_functions(role.id)
    return dto


def active_administrators(users: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [user for user in users if is_active_administrator(user)]


def with_default_role(user: dict[str, Any]) -> bool:
    """Give a user record from before roles existed the Administrator role and
    the enabled flag, which is what every user could do then. Answers whether
    the record changed."""
    changed = False
    if "role" not in user:
        user["role"] = ADMINISTRATOR
        changed = True
    if "enabled" not in user:
        user["enabled"] = True
        changed = True
    return changed
