# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from pymongo.errors import DuplicateKeyError, PyMongoError

from python.srv.db.db_manager import Database
from python.srv.services.api_errors import api_error
from python.srv.services.rbac import with_default_role

from .credential_encryption import (
    KEY_REPAIR_HINT,
    CredentialKeyUnavailable,
    credential_key_path,
    credential_key_problem,
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
    is_token_digest,
    token_digest,
)
from .server_defaults import env_int
from .utils import utc_now

HYPERVISORS: dict[str, dict[str, Any]] = {}
RESOURCES_BY_HV: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
JOBS: dict[str, dict[str, Any]] = {}
TASKS: dict[str, dict[str, Any]] = {}
REPOSITORIES: dict[str, dict[str, Any]] = {}
BACKUPS: dict[str, dict[str, Any]] = {}
USERS: dict[str, dict[str, Any]] = {}
SESSIONS: dict[str, dict[str, Any]] = {}
# Audit records when there is no catalog database, newest last.
AUDIT_RECORDS: deque[dict[str, Any]] = deque(maxlen=10_000)
SYSTEM_STATE: dict[str, dict[str, Any]] = {}

TASK_LOCK = threading.Lock()
CATALOG_LOCK = threading.RLock()

LOGGER = logging.getLogger(__name__)
_DATABASE: Database | None = None

_T = TypeVar("_T")


def _run_db_write(write: Callable[[], _T]) -> _T:
    try:
        return write()
    except DuplicateKeyError as exc:
        key_value = exc.details.get("keyValue") if isinstance(exc.details, dict) else None
        detail = ", ".join(f"{key}={value!r}" for key, value in (key_value or {}).items())
        raise api_error(
            409,
            "catalog_conflict",
            f"This change conflicts with an existing catalog record{f' ({detail})' if detail else ''}.",
        ) from exc
    except PyMongoError as exc:
        raise api_error(
            503,
            "catalog_unavailable",
            "The catalog database is unavailable; the change was not applied. Retry once it recovers.",
        ) from exc


def catalog_records(mapping: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return list(mapping.values())


def catalog_entries(mapping: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return list(mapping.items())


def mongodb_uri() -> str | None:
    # Read lazily (not at import time) so callers can set MONGODB_URI after this
    # module is imported — e.g. the server entry point defaulting it before the
    # app's startup hook loads persisted records.
    return os.getenv("MONGODB_URI") or None


def mongodb_db_name() -> str:
    return os.environ["MONGODB_DB_NAME"]


def mongodb_client_options() -> dict[str, int]:
    # Bounded pool + sane timeouts so the co-located MongoDB can't be overwhelmed
    # by a burst of connections (and a blip fails fast instead of hanging workers).
    return {
        "max_pool_size": env_int("MONGODB_MAX_POOL_SIZE"),
        "server_selection_timeout_ms": env_int("MONGODB_SERVER_SELECTION_TIMEOUT_MS"),
        "connect_timeout_ms": env_int("MONGODB_CONNECT_TIMEOUT_MS"),
        "socket_timeout_ms": env_int("MONGODB_SOCKET_TIMEOUT_MS"),
    }


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in record.items()}


def get_database() -> Database | None:
    global _DATABASE
    if _DATABASE is not None:
        return _DATABASE
    uri = mongodb_uri()
    if not uri:
        return None

    try:
        _DATABASE = Database.connect(
            uri=uri,
            db_name=mongodb_db_name(),
            **mongodb_client_options(),
        )
    except Exception as exc:
        LOGGER.warning("MongoDB persistence disabled: %s", exc)
        _DATABASE = None

    return _DATABASE


DATABASE_OK = "ok"
DATABASE_UNAVAILABLE = "unavailable"
DATABASE_NOT_CONFIGURED = "not_configured"


def database_status() -> str:
    if not mongodb_uri():
        return DATABASE_NOT_CONFIGURED

    try:
        db = get_database()
        if db is None:
            return DATABASE_UNAVAILABLE
        db.ping()
    except Exception as exc:
        LOGGER.warning("Catalog database is unreachable: %s", exc)
        return DATABASE_UNAVAILABLE

    return DATABASE_OK


def close_database() -> None:
    global _DATABASE
    if _DATABASE is not None:
        _DATABASE.close()
        _DATABASE = None


HYPERVISOR_SECRET_PATHS = (("metadata", "credentials", "password"), ("metadata", "token"))


def _hypervisor_secret_holders(record: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    holders: list[tuple[dict[str, Any], str]] = []
    for path in HYPERVISOR_SECRET_PATHS:
        holder: Any = record
        for key in path[:-1]:
            holder = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(holder, dict) and path[-1] in holder:
            holders.append((holder, path[-1]))
    return holders


def _with_hypervisor_secrets(record: dict[str, Any], convert: Callable[[Any], Any]) -> dict[str, Any]:
    converted = _normalize_record(record)
    for holder, key in _hypervisor_secret_holders(converted):
        holder[key] = convert(holder[key])
    return converted


def _hypervisor_secret_values(record: dict[str, Any] | None) -> list[tuple[str, Any]]:
    return [(key, holder[key]) for holder, key in _hypervisor_secret_holders(record or {})]


def _hypervisor_for_storage(normalized: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return _with_hypervisor_secrets(normalized, encrypt_secret)
    except CredentialKeyUnavailable as exc:
        if _hypervisor_secret_values(normalized) != _hypervisor_secret_values(previous):
            raise api_error(
                503,
                "credential_key_unavailable",
                f"{exc}. The new password is not stored. {KEY_REPAIR_HINT}, then enter the password again.",
                inventory_id=normalized.get("id"),
            ) from exc
        return normalized


def _hypervisor_holds_a_clear_secret(record: dict[str, Any]) -> bool:
    return any(
        isinstance(holder[key], str) and holder[key] and not is_encrypted(holder[key])
        for holder, key in _hypervisor_secret_holders(record)
    )


def load_registered_hypervisors() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    key_problem = credential_key_problem()
    loaded: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for item in db.find("hypervisors", {}):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        record = _normalize_record(item)
        name = str(record.get("name") or record["id"])
        if key_problem is None and _hypervisor_holds_a_clear_secret(record):
            stored = _with_hypervisor_secrets(record, encrypt_secret)
            _run_db_write(lambda stored=stored: db.upsert("hypervisors", "id", stored))
            LOGGER.info("Encrypted the stored credentials of inventory %s.", name)
        loaded[record["id"]] = _with_hypervisor_secrets(record, decrypt_secret)
        if any(is_encrypted(value) for _key, value in _hypervisor_secret_values(loaded[record["id"]])):
            unreadable.append(name)
            if key_problem is None:
                LOGGER.error(
                    "The stored password of inventory %s cannot be decrypted with the credential key at %s: it "
                    "was encrypted with another key. The inventory cannot connect until its password is entered "
                    "again: abr inventory edit --name %s --password",
                    name,
                    credential_key_path(),
                    name,
                )
    if key_problem is not None:
        LOGGER.error(
            "%s. Stored inventory credentials are left as they are until it is fixed: none is encrypted%s. "
            "%s, or move it away and enter each inventory's password again.",
            key_problem,
            f", and inventories {', '.join(unreadable)} cannot connect" if unreadable else "",
            KEY_REPAIR_HINT,
        )
    return loaded


def persist_hypervisor(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            stored = _hypervisor_for_storage(normalized, HYPERVISORS.get(record["id"]))
            _run_db_write(lambda: db.upsert("hypervisors", "id", stored))
        HYPERVISORS[record["id"]] = normalized


def delete_hypervisor(hypervisor_id: str) -> bool:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            def _delete() -> bool:
                deleted = db.delete("hypervisors", "id", hypervisor_id)
                db.delete_hypervisor_resources(hypervisor_id)
                return deleted

            deleted = _run_db_write(_delete)
        else:
            deleted = True
        HYPERVISORS.pop(hypervisor_id, None)
        RESOURCES_BY_HV.pop(hypervisor_id, None)
        return deleted


def load_jobs() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    records = db.find("jobs", {})
    return {
        item["id"]: _normalize_record(item)
        for item in records
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def persist_job(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("jobs", "id", normalized))
        JOBS[record["id"]] = normalized


def delete_job(job_id: str) -> bool:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            deleted = _run_db_write(lambda: db.delete("jobs", "id", job_id))
        else:
            deleted = True
        JOBS.pop(job_id, None)
        return deleted


def load_repositories() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    return {
        item["id"]: _normalize_record(item)
        for item in db.find("repositories", {})
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def persist_repository(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("repositories", "id", normalized))
        REPOSITORIES[record["id"]] = normalized


def delete_repository(repository_id: str) -> bool:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            deleted = _run_db_write(lambda: db.delete("repositories", "id", repository_id))
        else:
            deleted = True
        REPOSITORIES.pop(repository_id, None)
        return deleted


def load_backups() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    return {
        item["id"]: _normalize_record(item)
        for item in db.find("backups", {})
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def persist_backup(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("backups", "id", normalized))
        BACKUPS[record["id"]] = normalized


def delete_backup(backup_id: str) -> bool:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            deleted = _run_db_write(lambda: db.delete("backups", "id", backup_id))
        else:
            deleted = True
        BACKUPS.pop(backup_id, None)
        return deleted


def load_system_state(state_id: str) -> dict[str, Any] | None:
    db = get_database()
    if db is not None:
        record = db.get("system_state", "id", state_id)
        return dict(record) if isinstance(record, dict) else None
    with CATALOG_LOCK:
        record = SYSTEM_STATE.get(state_id)
        return dict(record) if record else None


def persist_system_state(record: dict[str, Any]) -> None:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("system_state", "id", record))
        SYSTEM_STATE[str(record["id"])] = dict(record)


def load_users() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    return {
        item["id"]: _normalize_record(item)
        for item in db.find("users", {})
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def persist_user(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("users", "id", normalized))
        USERS[record["id"]] = normalized


def delete_user(user_id: str) -> bool:
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            deleted = _run_db_write(lambda: db.delete("users", "id", user_id))
        else:
            deleted = True
        USERS.pop(user_id, None)
        return deleted


def session_key(token: str) -> str:
    return token_digest(token)


def get_session(token: str) -> dict[str, Any] | None:
    return SESSIONS.get(session_key(token))


def load_sessions() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    rehashed = db.rehash_legacy_session_tokens(token_digest)
    if rehashed:
        LOGGER.info("Replaced %d stored session token(s) with their digests.", rehashed)
    return {
        item["token"]: _normalize_record(item)
        for item in db.find("sessions", {})
        if isinstance(item, dict) and is_token_digest(item.get("token"))
    }


def persist_session(record: dict[str, Any]) -> None:
    stored = dict(record)
    if isinstance(stored.get("token"), str) and not is_token_digest(stored["token"]):
        stored["token"] = session_key(stored["token"])
    normalized = _normalize_record(stored)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("sessions", "token", normalized))
        SESSIONS[normalized["token"]] = normalized


def renew_session(token: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    key = session_key(token)
    with CATALOG_LOCK:
        current = SESSIONS.get(key)
        if current is None:
            return None
        merged = dict(current)
        merged.update(fields)
        normalized = _normalize_record(merged)
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("sessions", "token", normalized))
        SESSIONS[key] = normalized
        return normalized


def delete_session(token: str) -> bool:
    key = session_key(token)
    with CATALOG_LOCK:
        SESSIONS.pop(key, None)
        db = get_database()
        if db is not None:
            return db.delete("sessions", "token", key)
    return True


def delete_sessions_for_user(user_id: str) -> None:
    with CATALOG_LOCK:
        for token in [tok for tok, session in SESSIONS.items() if session.get("user_id") == user_id]:
            SESSIONS.pop(token, None)
        db = get_database()
        if db is not None:
            for session in db.find("sessions", {"user_id": user_id}):
                token = session.get("token")
                if isinstance(token, str):
                    db.delete("sessions", "token", token)


def delete_other_sessions_for_user(user_id: str, keep_token: str) -> None:
    keep_key = session_key(keep_token)
    with CATALOG_LOCK:
        for token in [
            tok for tok, session in SESSIONS.items() if session.get("user_id") == user_id and tok != keep_key
        ]:
            SESSIONS.pop(token, None)
        db = get_database()
        if db is not None:
            for session in db.find("sessions", {"user_id": user_id}):
                token = session.get("token")
                if isinstance(token, str) and token != keep_key:
                    db.delete("sessions", "token", token)


def append_audit_record(record: dict[str, Any]) -> None:
    db = get_database()
    if db is not None:
        _run_db_write(lambda: db.insert("audit", record))
        return
    with CATALOG_LOCK:
        AUDIT_RECORDS.append(dict(record))


def _matches_audit_query(record: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, wanted in query.items():
        value = record.get(key)
        if isinstance(wanted, dict):
            if "$gte" in wanted and not (isinstance(value, str) and value >= wanted["$gte"]):
                return False
            if "$lte" in wanted and not (isinstance(value, str) and value <= wanted["$lte"]):
                return False
            if "$regex" in wanted and not (isinstance(value, str) and re.search(wanted["$regex"], value)):
                return False
        elif value != wanted:
            return False
    return True


def query_audit_records(query: dict[str, Any], *, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Newest-first page of audit records matching ``query``: exact values, a
    ``{"$gte"/"$lte": ...}`` range on a timestamp, or a ``{"$regex": "^prefix"}``
    on a function ID."""
    db = get_database()
    if db is not None:
        return db.find_page("audit", query, sort_field="timestamp", skip=offset, limit=limit)
    with CATALOG_LOCK:
        matches = [dict(record) for record in reversed(AUDIT_RECORDS) if _matches_audit_query(record, query)]
    return matches[offset : offset + limit], len(matches)


def load_tasks() -> dict[str, dict[str, Any]]:
    db = get_database()
    if db is None:
        return {}

    records = db.find("tasks", {})
    return {
        item["id"]: _normalize_record(item)
        for item in records
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def persist_task(record: dict[str, Any]) -> None:
    normalized = _normalize_record(record)
    with CATALOG_LOCK:
        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.upsert("tasks", "id", normalized))
        TASKS[record["id"]] = normalized


def load_queued_tasks(limit: int | None = None) -> list[dict[str, Any]]:
    db = get_database()
    if db is not None:
        records = db.get_queued_tasks(limit=limit)
        queued: list[dict[str, Any]] = []
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                normalized = _normalize_record(item)
                TASKS[normalized["id"]] = normalized
                queued.append(normalized)
        return queued

    with TASK_LOCK:
        queued = [
            dict(task) for task in TASKS.values() if task.get("status") == "queued"
        ]
    queued.sort(key=lambda item: str(item.get("created_at", "")))
    if limit is not None:
        queued = queued[:limit]
    return queued


def claim_task(task_id: str) -> dict[str, Any] | None:
    db = get_database()
    if db is not None:
        claimed = db.claim_task(task_id)
        if not isinstance(claimed, dict):
            return None
        normalized = _normalize_record(claimed)
        TASKS[task_id] = normalized
        return normalized

    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") != "queued":
            return None
        task["status"] = "running"
        task["updated_at"] = utc_now()
        normalized = _normalize_record(task)
        TASKS[task_id] = normalized
    return normalized


def load_resources() -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    db = get_database()
    if db is None:
        return {}

    grouped: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for hypervisor_id in HYPERVISORS:
        bucket: dict[tuple[str, str], dict[str, Any]] = {}
        for resource in db.get_hypervisor_resources(hypervisor_id):
            normalized = _normalize_record(resource)
            bucket[(normalized["resource_type"], normalized["external_id"])] = (
                normalized
            )
        grouped[hypervisor_id] = bucket
    return grouped


def replace_resources_for_hypervisor(
    hypervisor_id: str, resources: list[dict[str, Any]]
) -> None:
    bucket: dict[tuple[str, str], dict[str, Any]] = {}
    for resource in resources:
        normalized = _normalize_record(resource)
        bucket[(normalized["resource_type"], normalized["external_id"])] = normalized

    with CATALOG_LOCK:
        if hypervisor_id not in HYPERVISORS:
            return

        db = get_database()
        if db is not None:
            _run_db_write(lambda: db.replace_hypervisor_resources(hypervisor_id, list(bucket.values())))
        RESOURCES_BY_HV[hypervisor_id] = bucket


def snapshot_catalog() -> dict[str, Any]:
    import copy as _copy

    with CATALOG_LOCK:
        return {
            "hypervisors": _copy.deepcopy(HYPERVISORS),
            "repositories": _copy.deepcopy(REPOSITORIES),
            "jobs": _copy.deepcopy(JOBS),
            "backups": _copy.deepcopy(BACKUPS),
            "tasks": _copy.deepcopy(TASKS),
            "users": _copy.deepcopy(USERS),
            "resources": {
                hypervisor_id: [dict(resource) for resource in bucket.values()]
                for hypervisor_id, bucket in RESOURCES_BY_HV.items()
            },
        }


def users_replaced_by_import(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    incoming: dict[str, dict[str, Any]] = {}
    for user in (snapshot.get("users") or {}).values():
        name = str(user.get("name") or "")
        if name in incoming and incoming[name].get("id") != user.get("id"):
            raise api_error(422, "invalid_export", f"The export holds two different users named {name!r}.")
        incoming[name] = user
    return [
        existing
        for existing in USERS.values()
        if str(existing.get("name") or "") in incoming
        and incoming[str(existing.get("name") or "")].get("id") != existing.get("id")
    ]


def restore_catalog(snapshot: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with CATALOG_LOCK:
        replaced = users_replaced_by_import(snapshot)
        if get_database() is not None:
            for hypervisor in (snapshot.get("hypervisors") or {}).values():
                _hypervisor_for_storage(_normalize_record(hypervisor), HYPERVISORS.get(hypervisor.get("id")))

        for existing in replaced:
            delete_sessions_for_user(str(existing["id"]))
            delete_user(str(existing["id"]))
            LOGGER.info("Import replaces user %s with the one from the export.", existing.get("name"))
        for user in (snapshot.get("users") or {}).values():
            with_default_role(user)
            persist_user(user)
        counts["users"] = len(snapshot.get("users") or {})

        for hypervisor in (snapshot.get("hypervisors") or {}).values():
            persist_hypervisor(hypervisor)
        counts["hypervisors"] = len(snapshot.get("hypervisors") or {})

        for repository in (snapshot.get("repositories") or {}).values():
            persist_repository(repository)
        counts["repositories"] = len(snapshot.get("repositories") or {})

        for job in (snapshot.get("jobs") or {}).values():
            persist_job(job)
        counts["jobs"] = len(snapshot.get("jobs") or {})

        for backup in (snapshot.get("backups") or {}).values():
            persist_backup(backup)
        counts["backups"] = len(snapshot.get("backups") or {})

        for task in (snapshot.get("tasks") or {}).values():
            persist_task(task)
        counts["tasks"] = len(snapshot.get("tasks") or {})

        resource_count = 0
        for hypervisor_id, resources in (snapshot.get("resources") or {}).items():
            replace_resources_for_hypervisor(hypervisor_id, resources)
            resource_count += len(resources)
        counts["resources"] = resource_count

    return counts


def update_hypervisor_fields(
    hypervisor_id: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    with CATALOG_LOCK:
        current = HYPERVISORS.get(hypervisor_id)
        if current is None:
            return None
        
        merged = dict(current)
        merged.update(fields)

        normalized = _normalize_record(merged)

        db = get_database()
        if db is not None:
            stored = _hypervisor_for_storage(normalized, current)
            _run_db_write(lambda: db.upsert("hypervisors", "id", stored))

        HYPERVISORS[hypervisor_id] = normalized
        return normalized
