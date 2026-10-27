# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time as time_module
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    USERS,
    catalog_records,
    delete_other_sessions_for_user,
    delete_session,
    delete_sessions_for_user,
    delete_user,
    get_session,
    persist_session,
    persist_user,
    renew_session,
)
from python.srv.core.server_defaults import env_int
from python.srv.core.utils import utc_now

from . import rbac, role_service
from .api_errors import api_error

LOGGER = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
MAX_USER_NAME_LENGTH = 64
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

_LOCKOUT_LOCK = threading.Lock()
_FAILED_ATTEMPTS: dict[str, list[float]] = {}
_SESSION_RENEWAL_LOCK = threading.Lock()
_SESSION_RENEWALS_IN_FLIGHT: set[str] = set()


def session_ttl_seconds() -> int:
    return env_int("ABR_SESSION_TTL_SECONDS")


def web_session_ttl_seconds() -> int:
    return env_int("ABR_WEB_SESSION_TTL_SECONDS")


def web_session_ttl_min_seconds() -> int:
    return env_int("ABR_WEB_SESSION_MIN_SECONDS")


def web_session_ttl_max_seconds() -> int:
    return env_int("ABR_WEB_SESSION_MAX_SECONDS")


def web_session_lifetime_seconds() -> int:
    return env_int("ABR_WEB_SESSION_LIFETIME_SECONDS", minimum=0)


def login_max_attempts() -> int:
    return env_int("ABR_LOGIN_MAX_ATTEMPTS")


def login_lockout_seconds() -> int:
    return env_int("ABR_LOGIN_LOCKOUT_SECONDS")


def hash_password(name: str, raw_password: str) -> str:
    material = f"{name},{raw_password}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _verify_password(name: str, raw_password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_password(name, raw_password), stored_hash)


def _hash_name(user: dict[str, Any], name: str) -> str:
    """The name a user's password hash was made with. A rename without a new
    password keeps the old name for it in ``passwd_name``, since the hash can
    only be made again from the password itself."""
    return str(user.get("passwd_name") or name)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _prune_and_count_failures(key: str, window_seconds: int) -> int:
    now = time_module.monotonic()
    with _LOCKOUT_LOCK:
        attempts = [moment for moment in _FAILED_ATTEMPTS.get(key, []) if now - moment < window_seconds]
        _FAILED_ATTEMPTS[key] = attempts
        return len(attempts)


def _record_failure(key: str) -> None:
    with _LOCKOUT_LOCK:
        _FAILED_ATTEMPTS.setdefault(key, []).append(time_module.monotonic())


def _clear_failures(key: str) -> None:
    with _LOCKOUT_LOCK:
        _FAILED_ATTEMPTS.pop(key, None)


def reset_login_lockout_state_for_testing() -> None:
    with _LOCKOUT_LOCK:
        _FAILED_ATTEMPTS.clear()


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "name": user["name"],
        "fullName": user.get("fullName", ""),
        "role": user.get("role"),
        "enabled": rbac.is_enabled(user),
        "created_at": user.get("created_at"),
        "passwordPolicyExempt": bool(user.get("password_policy_exempt", False)),
    }


def _find_user_by_name(name: str) -> dict[str, Any] | None:
    lowered = name.strip().lower()
    return next((user for user in catalog_records(USERS) if str(user.get("name") or "").lower() == lowered), None)


def list_users() -> list[dict[str, Any]]:
    return [
        _public_user(user)
        for user in sorted(catalog_records(USERS), key=lambda item: str(item.get("name") or "").lower())
    ]


def get_user(user_id: str) -> dict[str, Any]:
    return _public_user(_user_or_404(user_id))


def _user_or_404(user_id: str) -> dict[str, Any]:
    user = USERS.get(user_id)
    if user is None:
        raise api_error(404, "user_not_found", f"User not found: {user_id}")
    return user


def _role_or_422(value: Any) -> str:
    role = rbac.resolve_role(value)
    if role is None:
        known = ", ".join(role.id for role in rbac.ROLES) + ", or a custom role's ID or name"
        if value is None or value == "":
            raise api_error(422, "invalid_user", f"A role is required: one of {known}.")
        raise api_error(422, "invalid_role", f"Unknown role {value!r}: use one of {known}.", role=value)
    return role.id


def _refuse_losing_the_last_administrator(user: dict[str, Any], change: str) -> None:
    """At least one enabled Administrator must remain, or nobody could manage
    users and roles any more."""
    if not rbac.is_active_administrator(user):
        return
    others = [other for other in rbac.active_administrators(catalog_records(USERS)) if other["id"] != user["id"]]
    if not others:
        raise api_error(
            409,
            "last_administrator",
            f"Cannot {change} {user['name']}: it is the last active Administrator. "
            "Give another enabled user the Administrator role first.",
            user_id=user["id"],
        )


def password_problem(name: str, password: Any) -> str | None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if not password.strip():
        return "Password cannot be only spaces."
    if password.strip().lower() == name.strip().lower():
        return "Password cannot be the user name."
    return None


def _user_name_or_422(value: Any) -> str:
    name = str(value or "").strip()
    if not NAME_PATTERN.fullmatch(name):
        raise api_error(
            422,
            "invalid_user",
            "User name must use Latin letters, numbers, spaces, dots, underscores, or hyphens, "
            "and cannot start with a separator.",
        )
    if len(name) > MAX_USER_NAME_LENGTH:
        raise api_error(422, "invalid_user", f"User name must be at most {MAX_USER_NAME_LENGTH} characters.")
    return name


def _refuse_taken_name(name: str, *, user_id: str | None = None) -> None:
    existing = _find_user_by_name(name)
    if existing is not None and existing["id"] != user_id:
        raise api_error(409, "user_exists", f"A user named {name!r} already exists.")


def create_user(
    payload: dict[str, Any], *, enforce_password_policy: bool = True, acting_user_id: str | None = None
) -> dict[str, Any]:
    name = _user_name_or_422(payload.get("name"))
    full_name = str(payload.get("fullName") or "").strip()
    password = payload.get("password")

    problem = password_problem(name, password)
    if problem and enforce_password_policy:
        raise api_error(422, "invalid_user", problem)
    role = _role_or_422(payload.get("role"))
    role_service.ensure_can_manage_user(acting_user_id, function_id="user.create", new_role=role)
    _refuse_taken_name(name)

    now = utc_now()
    record = {
        "id": str(uuid4()),
        "name": name,
        "fullName": full_name,
        "role": role,
        "enabled": True,
        "passwd": hash_password(name, password),
        "created_at": now,
        "updated_at": now,
        # True only when the actual password fails the password policy (so a
        # bootstrap account using a strong password isn't nagged just because
        # enforcement was skipped for it). Surfaced to callers so CLI/UI can
        # prompt to fix it until there's a real change-password flow.
        "password_policy_exempt": problem is not None,
    }
    persist_user(record)
    return _public_user(record)


def update_user(
    user_id: str, payload: dict[str, Any], *, acting_user_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Change a user's name, full name, role, enabled state or password.
    Answers the user and what changed, as ``{field: {"from": old, "to": new}}``
    (a new password only as its field, without values). Nobody changes their
    own role, nor a user whose role allows more than the caller's."""
    user = _user_or_404(user_id)
    new_role = _role_or_422(payload["role"]) if payload.get("role") is not None else None
    role_service.ensure_can_manage_user(
        acting_user_id,
        function_id="user.edit",
        target=user,
        new_role=new_role if new_role != user.get("role") else None,
    )
    updated = dict(user)
    if "name" in payload and payload["name"] is not None:
        updated["name"] = _user_name_or_422(payload["name"])
        _refuse_taken_name(updated["name"], user_id=user_id)
    password = payload.get("password")
    if password is not None:
        if user_id == acting_user_id:
            raise api_error(
                409,
                "own_password",
                "Change your own password with your current one: Change password in the console, "
                "or abr auth change-password.",
                user_id=user_id,
            )
        problem = password_problem(updated["name"], password)
        if problem:
            raise api_error(422, "invalid_user", problem)
        updated["passwd"] = hash_password(updated["name"], password)
        updated.pop("passwd_name", None)
        updated["password_policy_exempt"] = False
    elif updated["name"] != user["name"]:
        updated["passwd_name"] = _hash_name(user, user["name"])
    if "fullName" in payload and payload["fullName"] is not None:
        updated["fullName"] = str(payload["fullName"]).strip()
    if new_role is not None:
        updated["role"] = new_role
    if "enabled" in payload and payload["enabled"] is not None:
        if not isinstance(payload["enabled"], bool):
            raise api_error(422, "invalid_user", "enabled must be true or false.")
        updated["enabled"] = payload["enabled"]

    changes = {
        field: {"from": user.get(field), "to": updated.get(field)}
        for field in ("name", "fullName", "role", "enabled")
        if user.get(field) != updated.get(field)
    }
    if password is not None:
        changes["password"] = {"from": None, "to": None}
    if not changes:
        return _public_user(user), changes
    if not rbac.is_active_administrator(updated):
        _refuse_losing_the_last_administrator(user, "disable" if not rbac.is_enabled(updated) else "demote")

    updated["updated_at"] = utc_now()
    persist_user(updated)
    # A new password set by somebody else signs the user out everywhere: who
    # holds a session now no longer knows the password it was opened with.
    if not rbac.is_enabled(updated) or password is not None:
        delete_sessions_for_user(user_id)
    return _public_user(updated), changes


def remove_user(user_id: str, *, acting_user_id: str | None = None) -> dict[str, Any]:
    user = _user_or_404(user_id)
    role_service.ensure_can_manage_user(acting_user_id, function_id="user.delete", target=user)
    _refuse_losing_the_last_administrator(user, "delete")
    if len(USERS) <= 1:
        raise api_error(
            409,
            "last_user",
            "Cannot delete the last remaining user: this would lock everyone out of the API.",
        )
    delete_user(user_id)
    delete_sessions_for_user(user_id)
    return {"id": user_id, "deleted": True}


def change_password(user_id: str, current_password: str, new_password: str, *, keep_token: str) -> dict[str, Any]:
    user = USERS.get(user_id)
    if user is None:
        raise api_error(404, "user_not_found", f"User not found: {user_id}")

    name = str(user["name"])
    key = name.strip().lower()

    # Same lockout counter as login: a stolen-but-not-yet-invalidated token
    # shouldn't let an attacker brute-force the current password offline via
    # this endpoint just because they're already holding a session.
    if _prune_and_count_failures(key, login_lockout_seconds()) >= login_max_attempts():
        raise api_error(429, "too_many_attempts", "Too many failed attempts. Try again later.")

    if not _verify_password(_hash_name(user, name), current_password, user["passwd"]):
        _record_failure(key)
        raise api_error(401, "invalid_credentials", "Current password is incorrect.")
    _clear_failures(key)

    problem = password_problem(name, new_password)
    if problem:
        raise api_error(422, "invalid_user", problem)
    if _verify_password(_hash_name(user, name), new_password, user["passwd"]):
        raise api_error(422, "invalid_user", "The new password must differ from the current one.")

    updated = dict(user)
    updated["passwd"] = hash_password(name, new_password)
    updated.pop("passwd_name", None)
    updated["password_policy_exempt"] = False
    updated["updated_at"] = utc_now()
    persist_user(updated)

    # Everywhere else gets logged out; the session making this call (which
    # just proved it knows the new/current password) stays valid.
    delete_other_sessions_for_user(user_id, keep_token)

    return _public_user(updated)


def login(
    name: str, password: str, *, client_type: str = "web", duration_seconds: int | None = None
) -> dict[str, Any]:
    key = name.strip().lower()
    if _prune_and_count_failures(key, login_lockout_seconds()) >= login_max_attempts():
        raise api_error(429, "too_many_attempts", "Too many failed login attempts. Try again later.")

    user = _find_user_by_name(name)
    stored_hash = user["passwd"] if user is not None else hash_password(name, "")
    valid = _verify_password(_hash_name(user, name), password, stored_hash) if user is not None else False

    if user is None or not valid:
        _record_failure(key)
        raise api_error(401, "invalid_credentials", "Invalid username or password.")

    _clear_failures(key)
    if not rbac.is_enabled(user):
        raise api_error(403, "account_disabled", "This account is disabled. Ask an Administrator to enable it.")
    if rbac.role_id_of(user) is None:
        raise api_error(
            403, "role_invalid", "This account has no valid role. Ask an Administrator to assign one.",
            role=user.get("role"),
        )

    client = "web" if client_type == "web" else "cli"

    if client == "web" and duration_seconds is not None:
        _ensure_web_session_duration(duration_seconds)

    now = _now_dt()
    token = secrets.token_urlsafe(32)
    session = {
        "token": token,
        "user_id": user["id"],
        "expires_at": (now + timedelta(seconds=session_ttl_seconds())).isoformat(),
        "created_at": utc_now(),
        "client": client,
    }
    if client == "web":
        lifetime = web_session_lifetime_seconds()
        session["idle_timeout_seconds"] = duration_seconds if duration_seconds is not None else web_session_ttl_seconds()
        session["absolute_expires_at"] = (now + timedelta(seconds=lifetime)).isoformat() if lifetime else None
        session["expires_at"] = _renewed_expiry(session, now).isoformat()
    persist_session(session)
    return {
        "token": token,
        **_session_expiry(session),
        "user": _public_user(user),
        "permissions": rbac.allowed_functions(user["role"]),
    }


def whoami(user_id: str) -> dict[str, Any]:
    user = _user_or_404(user_id)
    role = rbac.get_role(rbac.role_id_of(user))
    return {
        "user": _public_user(user),
        "role": rbac.role_to_dto(role, with_permissions=False) if role else None,
        "permissions": rbac.allowed_functions(role.id if role else None),
    }


def _ensure_web_session_duration(duration_seconds: Any) -> None:
    minimum, maximum = web_session_ttl_min_seconds(), web_session_ttl_max_seconds()
    if not isinstance(duration_seconds, int) or not (minimum <= duration_seconds <= maximum):
        raise api_error(
            422,
            "invalid_session_duration",
            f"Session duration must be between {minimum} and {maximum} seconds.",
        )


def _renewed_expiry(session: dict[str, Any], now: datetime) -> datetime:
    if str(session.get("client") or "") == "web":
        idle = session.get("idle_timeout_seconds")
        expiry = now + timedelta(seconds=idle if isinstance(idle, int) and idle > 0 else web_session_ttl_seconds())
    else:
        expiry = now + timedelta(seconds=session_ttl_seconds())
    absolute_expires_at = _parse_dt(session.get("absolute_expires_at"))
    return min(expiry, absolute_expires_at) if absolute_expires_at is not None else expiry


def _session_expiry(session: dict[str, Any]) -> dict[str, Any]:
    return {"expires_at": session.get("expires_at"), "absolute_expires_at": session.get("absolute_expires_at")}


def _live_session(token: str) -> dict[str, Any]:
    session = get_session(token)
    if session is None:
        raise api_error(401, "unauthorized", "Missing, invalid, or expired session token.")
    return session


def extend_session(token: str, duration_seconds: int) -> dict[str, Any]:
    session = _live_session(token)
    if str(session.get("client") or "") != "web":
        raise api_error(400, "session_not_extendable", "Only web sessions support an adjustable duration.")
    _ensure_web_session_duration(duration_seconds)
    updated = {**session, "idle_timeout_seconds": duration_seconds}
    updated["expires_at"] = _renewed_expiry(updated, _now_dt()).isoformat()
    renew_session(token, {"idle_timeout_seconds": duration_seconds, "expires_at": updated["expires_at"]})
    return _session_expiry(updated)


def touch_session(token: str) -> dict[str, Any]:
    session = _live_session(token)
    updated = {**session, "expires_at": _renewed_expiry(session, _now_dt()).isoformat()}
    renew_session(token, {"expires_at": updated["expires_at"]})
    return _session_expiry(updated)


def logout(token: str) -> None:
    delete_session(token)


def _session_write_best_effort(action: Callable[[], Any], description: str) -> None:
    try:
        action()
    except Exception as exc:
        LOGGER.warning("Could not persist %s to the catalog database: %s", description, exc)


def _renew_session_in_background(token: str, fields: dict[str, Any]) -> None:
    with _SESSION_RENEWAL_LOCK:
        if token in _SESSION_RENEWALS_IN_FLIGHT:
            return
        _SESSION_RENEWALS_IN_FLIGHT.add(token)

    def _run() -> None:
        try:
            _session_write_best_effort(lambda: renew_session(token, fields), "a session renewal")
        finally:
            with _SESSION_RENEWAL_LOCK:
                _SESSION_RENEWALS_IN_FLIGHT.discard(token)

    threading.Thread(target=_run, name="session-renewal", daemon=True).start()


def authenticate(token: str | None, *, background: bool = False) -> dict[str, Any] | None:
    if not token:
        return None
    session = get_session(token)
    if session is None:
        return None

    expires_at = _parse_dt(session.get("expires_at"))
    if expires_at is None or _now_dt() >= expires_at:
        _session_write_best_effort(lambda: delete_session(token), "an expired session removal")
        return None

    user = USERS.get(session.get("user_id"))
    if user is None:
        _session_write_best_effort(lambda: delete_session(token), "an orphaned session removal")
        return None
    if not rbac.is_enabled(user):
        _session_write_best_effort(lambda: delete_session(token), "a disabled user's session removal")
        return None

    if not (background and str(session.get("client") or "") == "web" and "idle_timeout_seconds" in session):
        _renew_session_in_background(token, {"expires_at": _renewed_expiry(session, _now_dt()).isoformat()})

    return {"token": token, "user_id": user["id"], "user_name": user["name"], "role": user.get("role")}


def bootstrap_admin_if_needed() -> None:
    if USERS:
        return
    password = os.getenv("ABR_BOOTSTRAP_ADMIN_PASSWORD")
    if not password:
        LOGGER.warning(
            "No users exist yet and ABR_BOOTSTRAP_ADMIN_PASSWORD is not set: the REST API has "
            "no way to authenticate until a user is created. Set ABR_BOOTSTRAP_ADMIN_PASSWORD "
            "(and optionally ABR_BOOTSTRAP_ADMIN_USER) and restart to auto-create the first admin user."
        )
        return
    name = os.environ["ABR_BOOTSTRAP_ADMIN_USER"]
    # The bootstrap password is exempt from the normal length policy: it only
    # exists to get the very first login working (e.g. local dev convenience
    # defaults), and the account it creates is flagged (password_policy_exempt)
    # so CLI/UI can nag about it -- see create_user().
    create_user(
        {"name": name, "password": password, "fullName": "Administrator", "role": rbac.ADMINISTRATOR},
        enforce_password_policy=False,
    )
    LOGGER.info("Bootstrapped initial admin user %r.", name)


def backfill_user_roles() -> int:
    """Give users from before roles existed the Administrator role, which is
    what they could do then. Answers how many changed."""
    changed = 0
    for user in catalog_records(USERS):
        updated = dict(user)
        if rbac.with_default_role(updated):
            persist_user(updated)
            changed += 1
    return changed


def ensure_import_keeps_an_administrator(snapshot: dict[str, Any], replaced: list[dict[str, Any]]) -> None:
    """Refuse a catalog import after which no enabled Administrator would be
    left: imported users replace current ones by ID and, when ``replaced``, by
    name."""
    after = {user["id"]: user for user in catalog_records(USERS)}
    for user in replaced:
        after.pop(str(user.get("id")), None)
    for user in (snapshot.get("users") or {}).values():
        if isinstance(user, dict) and user.get("id"):
            incoming = dict(user)
            rbac.with_default_role(incoming)
            after[str(incoming["id"])] = incoming
    if after and not rbac.active_administrators(after.values()):
        raise api_error(
            409,
            "last_administrator",
            "The import would leave no enabled Administrator, so nobody could manage users and roles. "
            "Nothing was imported.",
        )
