# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The audit log: one record for every executed command and every user or
role change, whatever interface it came from (Web UI, CLI, `--output json`
automation or a direct API call), and whether it was allowed, denied or
failed.

Records go to the catalog's ``audit`` collection (memory without a catalog
database) and, one line each, to the server log. Credentials never reach
either: parameters are masked by key and every text is redacted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from python.srv.core.credential_encryption import token_digest
from python.srv.core.logger_conf import redact_text
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    TASKS,
    USERS,
    append_audit_record,
    catalog_records,
    query_audit_records,
)

from . import rbac
from .api_errors import api_error

LOGGER = logging.getLogger(__name__)

ALLOWED = "allowed"
DENIED = "denied"
FAILED = "failed"
RESULTS = (ALLOWED, DENIED, FAILED)

MASK = "********"
MAX_TEXT_LENGTH = 256
MAX_ITEMS = 50
MAX_DEPTH = 6
LIST_LIMIT_DEFAULT = 100
LIST_LIMIT_MAX = 1000

_SENSITIVE_KEY = re.compile(
    r"password|passwd|passphrase|secret|token|accesskey|privatekey|encryptionkey|apikey|credential|"
    r"authorization|cookie"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_COMMAND_PATTERN = re.compile(r"^[a-z][a-z -]{0,63}$")
# Denials that name an account rather than a function still deny the request.
_DENIAL_CODES = {"permission_denied", "account_disabled", "role_invalid"}
_PATH_TARGETS: dict[str, tuple[str, dict[str, dict[str, Any]] | None]] = {
    "job_id": ("job", JOBS),
    "repository_id": ("repository", REPOSITORIES),
    "inventory_id": ("inventory", HYPERVISORS),
    "backup_id": ("backup", BACKUPS),
    "session_id": ("session", TASKS),
    "user_id": ("user", USERS),
    "role_id": ("role", None),
    "vm_id": ("vm", None),
}


@dataclass(slots=True)
class AuditContext:
    """What the authorization step knows about a request, kept on
    ``request.state.audit`` until the record is written."""

    function_id: str
    user: str | None = None
    user_id: str | None = None
    role: str | None = None
    auth_session_id: str | None = None
    target: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)


def timestamp(moment: datetime | None = None) -> str:
    """UTC, fixed width, so records sort and filter as text."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def auth_session_ref(token: str | None) -> str | None:
    """A reference to a sign-in session that cannot be used to sign in."""
    return token_digest(token)[:16] if token else None


def _is_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", key.lower())
    return normalized == "key" or bool(_SENSITIVE_KEY.search(normalized))


def mask(value: Any, *, depth: int = 0) -> Any:
    """``value`` with every credential replaced by a mask: the value of any key
    that names one, and anything in a text that reads like one. Long texts and
    lists are cut short."""
    if depth > MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {
            str(key): MASK if _is_sensitive(str(key)) and item not in (None, "") else mask(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [mask(item, depth=depth + 1) for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"… {len(value) - MAX_ITEMS} more")
        return items
    if isinstance(value, str):
        text = _URL_CREDENTIALS.sub(rf"\1{MASK}@", redact_text(value))
        return text if len(text) <= MAX_TEXT_LENGTH else f"{text[:MAX_TEXT_LENGTH]}… ({len(text)} characters)"
    return value


def target_from_path(path_params: dict[str, Any]) -> dict[str, Any] | None:
    for param, (kind, records) in _PATH_TARGETS.items():
        value = path_params.get(param)
        if not value:
            continue
        target = {"type": kind, "id": str(value)}
        if kind == "role":
            role = rbac.ROLES_BY_ID.get(str(value))
            target["name"] = role.name if role else None
        elif records is not None:
            record = records.get(str(value))
            name = record.get("name") or record.get("job_name") or record.get("vm_name") if record else None
            target["name"] = str(name) if name else None
        return target
    return None


def _target_from_body(function_id: str, body: dict[str, Any], response: dict[str, Any]) -> dict[str, Any] | None:
    area, _, action = function_id.partition(".")
    if function_id == "restore.start":
        point = body.get("restore_point_id") or body.get("point_id") or body.get("backup_id") or body.get("backup")
        name = body.get("target_vm_name") or body.get("name")
        return {"type": "restore_point", "id": str(point) if point else None, "name": str(name) if name else None}
    if action == "create" or function_id == "backup.import":
        item_id = response.get("id")
        name = response.get("name") or body.get("name")
        if item_id or name:
            return {"type": area, "id": str(item_id) if item_id else None, "name": str(name) if name else None}
    return None


def session_ids_from(response: dict[str, Any]) -> list[str]:
    for key in ("session_id", "restore_session_id"):
        if isinstance(response.get(key), str) and response[key]:
            return [response[key]]
    items = response.get("items")
    if isinstance(items, list):
        return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]
    return [str(response["id"])] if response.get("id") else []


def interface_of(client_type: str | None) -> str:
    client = (client_type or "").strip().lower()
    return client if client in {"web", "cli"} else "api"


def _error_of(exc: BaseException | None, status: int) -> dict[str, Any] | None:
    if exc is None and status < 400:
        return None
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        inner = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        code = code or inner.get("code")
        message = message or inner.get("message")
    elif detail:
        message = message or str(detail)
    if code is None and exc is not None and exc.__class__.__name__ == "RequestValidationError":
        code, message = "validation_error", "Request validation failed"
    if not message:
        message = exc.__class__.__name__ if exc is not None else f"HTTP {status}"
    return {"code": str(code or ("internal_error" if exc is not None else "http_error")), "message": mask(str(message))}


def record(
    *,
    function_id: str,
    result: str,
    user: str | None,
    user_id: str | None = None,
    role: str | None = None,
    target: dict[str, Any] | None = None,
    session_id: str | None = None,
    auth_session_id: str | None = None,
    interface: str = "api",
    command: str | None = None,
    client: str | None = None,
    status: int | None = None,
    error: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one audit record. Never raises: a catalog that cannot take it
    still gets the server-log line."""
    entry: dict[str, Any] = {
        "id": str(uuid4()),
        "timestamp": timestamp(),
        "user": user,
        "user_id": user_id,
        "role": role,
        "function_id": function_id,
        "target": mask(target) if target else None,
        "result": result,
        "session_id": session_id,
        "auth_session_id": auth_session_id,
        "interface": interface,
        "command": command,
        "client": client,
        "status": status,
        "error": error,
        "parameters": mask(parameters) if parameters else None,
        "details": mask(details) if details else None,
    }
    entry = {key: value for key, value in entry.items() if value is not None}
    try:
        append_audit_record(entry)
    except Exception as exc:
        LOGGER.warning("Could not write audit record %s to the catalog: %s", entry["id"], exc)
    target_text = ""
    if target:
        target_text = f" target={target.get('type')}:{target.get('name') or target.get('id')}"
    LOGGER.log(
        logging.WARNING if result == DENIED else logging.INFO,
        "Audit: %s user=%s role=%s function=%s%s result=%s%s",
        entry["interface"],
        user or "-",
        role or "-",
        function_id,
        mask(target_text),
        result,
        f" session={session_id}" if session_id else "",
    )
    return entry


def _json_body(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def record_request(
    *,
    function_id: str,
    context: AuditContext | None,
    headers: Any,
    client: str | None,
    query: dict[str, Any],
    body: bytes | None,
    status: int,
    response_body: bytes | None,
    exc: BaseException | None,
) -> dict[str, Any] | None:
    """The audit record of one API request, from what the request handler saw.
    Requests without a signed-in user are not recorded, except sign-ins."""
    function = rbac.FUNCTIONS_BY_ID.get(function_id)
    if function is None:
        return None
    request_body = _json_body(body)
    response = _json_body(response_body) if exc is None else {}

    if function_id == "auth.login":
        name = str(request_body.get("name") or "").strip()
        user = response.get("user") if isinstance(response.get("user"), dict) else _user_named(name)
        context = AuditContext(
            function_id,
            user=name or None,
            user_id=(user or {}).get("id"),
            role=(user or {}).get("role"),
            auth_session_id=auth_session_ref(response.get("token")),
        )
    if context is None or not context.user:
        return None

    code = getattr(exc, "code", None)
    if exc is None and status < 400:
        result = ALLOWED
    elif code in _DENIAL_CODES:
        result = DENIED
    else:
        result = FAILED
    if result == ALLOWED and function.category == rbac.READ and _is_refresh(headers):
        return None

    target = context.target
    if target is None:
        target = _target_from_body(function_id, request_body, response)
    elif target is not None and not target.get("name") and response.get("name"):
        target = {**target, "name": response.get("name")}

    session_ids = session_ids_from(response) if function.starts_session and result == ALLOWED else []
    details = dict(context.details)
    if len(session_ids) > 1:
        details["session_ids"] = session_ids

    parameters = None
    if function.category not in (rbac.READ, rbac.SELF_SERVICE):
        parameters = {**({"query": query} if query else {}), **({"body": request_body} if request_body else {})}

    command = str(headers.get("x-abr-command") or "").strip().lower()
    return record(
        function_id=function_id,
        result=result,
        user=context.user,
        user_id=context.user_id,
        role=context.role,
        target=target,
        session_id=session_ids[0] if session_ids else None,
        auth_session_id=context.auth_session_id,
        interface=interface_of(headers.get("x-client-type")),
        command=command if _COMMAND_PATTERN.fullmatch(command) else None,
        client=client,
        status=status,
        error=_error_of(exc, status) if result != ALLOWED else None,
        parameters=parameters or None,
        details=details or None,
    )


def _user_named(name: str) -> dict[str, Any] | None:
    lowered = name.lower()
    return next((user for user in catalog_records(USERS) if str(user.get("name") or "").lower() == lowered), None)


def _is_refresh(headers: Any) -> bool:
    """A read the client repeats on its own (the Web UI's background refresh,
    the CLI's progress polling) rather than one a user asked for."""
    return str(headers.get("x-abr-refresh") or "").strip().lower() in {"1", "true", "yes"}


def _parse_bound(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise api_error(422, "invalid_argument", f"{name} must be an ISO 8601 timestamp, got {value!r}.") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return timestamp(moment)


def list_records(
    *,
    limit: int | None = None,
    offset: int = 0,
    user: str | None = None,
    function: str | None = None,
    result: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Newest-first page of the audit log. ``function`` is a function ID, or an
    area with a trailing dot (``job.``) for all of its functions."""
    limit = max(1, min(limit or LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX))
    query: dict[str, Any] = {}
    if user:
        query["user"] = user.strip()
    if function:
        function = function.strip()
        query["function_id"] = {"$regex": f"^{re.escape(function)}"} if function.endswith(".") else function
    if result:
        if result not in RESULTS:
            raise api_error(422, "invalid_argument", f"result must be one of {', '.join(RESULTS)}.")
        query["result"] = result
    bounds = {
        operator: bound
        for operator, bound in (("$gte", _parse_bound(since, "since")), ("$lte", _parse_bound(until, "until")))
        if bound
    }
    if bounds:
        query["timestamp"] = bounds
    items, total = query_audit_records(query, offset=offset, limit=limit)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
