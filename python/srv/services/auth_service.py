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

from .api_errors import api_error

LOGGER = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
MIN_PASSWORD_LENGTH = 8

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


def login_max_attempts() -> int:
    return env_int("ABR_LOGIN_MAX_ATTEMPTS")


def login_lockout_seconds() -> int:
    return env_int("ABR_LOGIN_LOCKOUT_SECONDS")


def hash_password(name: str, raw_password: str) -> str:
    material = f"{name},{raw_password}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _verify_password(name: str, raw_password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_password(name, raw_password), stored_hash)


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


def create_user(payload: dict[str, Any], *, enforce_password_policy: bool = True) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    full_name = str(payload.get("fullName") or "").strip()
    password = payload.get("password")

    if not NAME_PATTERN.fullmatch(name):
        raise api_error(
            422,
            "invalid_user",
            "User name must use Latin letters, numbers, spaces, dots, underscores, or hyphens, "
            "and cannot start with a separator.",
        )
    password_too_short = not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH
    if password_too_short and enforce_password_policy:
        raise api_error(422, "invalid_user", f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if _find_user_by_name(name) is not None:
        raise api_error(409, "user_exists", f"A user named {name!r} already exists.")

    now = utc_now()
    record = {
        "id": str(uuid4()),
        "name": name,
        "fullName": full_name,
        "passwd": hash_password(name, password),
        "created_at": now,
        "updated_at": now,
        # True only when the actual password fails the length policy (so a
        # bootstrap account using a strong password isn't nagged just because
        # enforcement was skipped for it). Surfaced to callers so CLI/UI can
        # prompt to fix it until there's a real change-password flow.
        "password_policy_exempt": password_too_short,
    }
    persist_user(record)
    return _public_user(record)


def remove_user(user_id: str) -> dict[str, Any]:
    user = USERS.get(user_id)
    if user is None:
        raise api_error(404, "user_not_found", f"User not found: {user_id}")
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

    if not _verify_password(name, current_password, user["passwd"]):
        _record_failure(key)
        raise api_error(401, "invalid_credentials", "Current password is incorrect.")
    _clear_failures(key)

    if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
        raise api_error(422, "invalid_user", f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    updated = dict(user)
    updated["passwd"] = hash_password(name, new_password)
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
    valid = _verify_password(name, password, stored_hash) if user is not None else False

    if user is None or not valid:
        _record_failure(key)
        raise api_error(401, "invalid_credentials", "Invalid username or password.")

    _clear_failures(key)

    client = "web" if client_type == "web" else "cli"

    if client == "web" and duration_seconds is not None:
        minimum, maximum = web_session_ttl_min_seconds(), web_session_ttl_max_seconds()
        if not isinstance(duration_seconds, int) or not (minimum <= duration_seconds <= maximum):
            raise api_error(
                422,
                "invalid_session_duration",
                f"Session duration must be between {minimum} and {maximum} seconds.",
            )

    now = _now_dt()
    ttl = session_ttl_seconds()
    expires_at = (now + timedelta(seconds=ttl)).isoformat()
    token = secrets.token_urlsafe(32)
    session = {
        "token": token,
        "user_id": user["id"],
        "expires_at": expires_at,
        "created_at": utc_now(),
        "client": client,
    }
    reported_expires_at = expires_at
    if client == "web":
        web_ttl = duration_seconds if duration_seconds is not None else web_session_ttl_seconds()
        absolute_expires_at = (now + timedelta(seconds=web_ttl)).isoformat()
        session["absolute_expires_at"] = absolute_expires_at
        reported_expires_at = absolute_expires_at
    persist_session(session)
    return {"token": token, "expires_at": reported_expires_at, "user": _public_user(user)}


def extend_session(token: str, duration_seconds: int) -> dict[str, Any]:
    """Adjust the current web session's absolute expiry in place (the
    Settings -> Security token-lifetime slider) -- takes effect immediately
    rather than waiting for the next login. CLI sessions have no adjustable
    duration and are rejected."""
    session = get_session(token)
    if session is None:
        raise api_error(401, "unauthorized", "Missing, invalid, or expired session token.")
    if str(session.get("client") or "") != "web":
        raise api_error(400, "session_not_extendable", "Only web sessions support an adjustable duration.")

    minimum, maximum = web_session_ttl_min_seconds(), web_session_ttl_max_seconds()
    if not isinstance(duration_seconds, int) or not (minimum <= duration_seconds <= maximum):
        raise api_error(
            422,
            "invalid_session_duration",
            f"Session duration must be between {minimum} and {maximum} seconds.",
        )

    now = _now_dt()
    absolute_expires_at = now + timedelta(seconds=duration_seconds)
    expires_at = min(now + timedelta(seconds=session_ttl_seconds()), absolute_expires_at)
    renew_session(
        token,
        {"expires_at": expires_at.isoformat(), "absolute_expires_at": absolute_expires_at.isoformat()},
    )
    return {"expires_at": absolute_expires_at.isoformat()}


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


def authenticate(token: str | None) -> dict[str, Any] | None:
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

    new_expiry = _now_dt() + timedelta(seconds=session_ttl_seconds())
    absolute_expires_at = _parse_dt(session.get("absolute_expires_at"))
    if absolute_expires_at is not None:
        new_expiry = min(new_expiry, absolute_expires_at)
    _renew_session_in_background(token, {"expires_at": new_expiry.isoformat()})

    return {"token": token, "user_id": user["id"], "user_name": user["name"]}


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
    create_user({"name": name, "password": password, "fullName": "Administrator"}, enforce_password_policy=False)
    LOGGER.info("Bootstrapped initial admin user %r.", name)
