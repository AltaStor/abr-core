# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import binascii
import calendar
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    DATABASE_NOT_CONFIGURED,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASK_LOCK,
    TASKS,
    catalog_entries,
    catalog_records,
    database_status,
    delete_backup,
    delete_hypervisor,
    delete_job,
    delete_repository,
    persist_backup,
    persist_hypervisor,
    persist_job,
    persist_repository,
    persist_task,
    replace_resources_for_hypervisor,
    restore_catalog,
    snapshot_catalog,
    update_hypervisor_fields,
)
from python.srv.core.server_defaults import env_int
from python.srv.core.secure_archive import (
    ArchiveError,
    WrongPasswordError,
    decrypt_payload,
    encrypt_payload,
)
from python.srv.core.storage_tool import (
    StorageToolError,
    build_s3_location,
    extract_s3_config,
    hydrate_manifest_via_disk_tool,
    list_disk_tool_backups,
    mask_credential_fields,
    probe_repository_access,
    remove_disk_tool_backup,
    s3_endpoint_connect_error,
    sanitize_s3_config,
    verify_disk_tool_backup,
)
from python.srv.core.utils import utc_now
from python.srv.services.inventory_management import (
    INVENTORY_KIND_BY_TYPE,
    EsxHostInClusterError,
    InventoryProviderRegistry,
    apply_inventory_updates,
    assert_inventory_registrable,
    creatable_inventory_types,
    default_port_for_type,
    extract_host,
    inventory_references,
    inventory_type_from_record,
    normalize_inventory_type,
)
from python.srv.plugins.vcenter.vcenter_backup import DEFAULT_COMPRESSION_LEVEL

from .api_errors import ApiError, api_error

LOGGER = logging.getLogger(__name__)

# Whether `inventory add` kicks off VM discovery in the background. Off by
# default so engine-level tests stay deterministic; the running server enables
# it during startup (see backup_server lifespan). Discovery is best-effort —
# failures are logged, and the lazy discovery inside `job create` remains as the
# safety net if the background scan has not finished or failed.
AUTO_DISCOVER_ON_INVENTORY_ADD = False

# One lock per inventory so a background add-time scan and a lazy job-create
# scan never run concurrently for the same inventory (no duplicate discovery).
_INVENTORY_DISCOVERY_REGISTRY_LOCK = threading.Lock()
_INVENTORY_DISCOVERY_LOCKS: dict[str, threading.Lock] = {}


def _inventory_discovery_lock(inventory_id: str) -> threading.Lock:
    with _INVENTORY_DISCOVERY_REGISTRY_LOCK:
        lock = _INVENTORY_DISCOVERY_LOCKS.get(inventory_id)
        if lock is None:
            lock = threading.Lock()
            _INVENTORY_DISCOVERY_LOCKS[inventory_id] = lock
        return lock


def discover_inventory_vms(inventory_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    """Discover and persist an inventory's resources, idempotently.

    Serialised per-inventory: if another caller (e.g. a background add-time
    scan) already populated VMs while we waited for the lock, we skip the
    rescan instead of running it twice. Propagates ``rescan_inventory``'s
    ``502 inventory_rescan_failed`` if discovery itself fails.
    """
    lock = _inventory_discovery_lock(inventory_id)
    with lock:
        if not force and _resolve_inventory_vm_records(inventory_id):
            return _resolve_inventory_vm_records(inventory_id)
        rescan_inventory(inventory_id)
        return _resolve_inventory_vm_records(inventory_id)


def schedule_inventory_discovery(inventory_id: str, *, force: bool = False) -> threading.Thread:
    """Run discovery for an inventory in a background daemon thread.

    Best-effort: keeps `inventory add` responsive and never lets a discovery
    failure escape. Returns the thread so callers/tests can join if needed.
    ``force`` bypasses discover_inventory_vms's "already has VMs, skip"
    shortcut -- needed by callers (e.g. a completed in-place restore) that
    know the inventory is stale and must actually rescan, not just ensure
    *some* discovery has run at some point.
    """

    def _run() -> None:
        try:
            discover_inventory_vms(inventory_id, force=force)
        except Exception:
            LOGGER.exception("Background inventory discovery failed for %s", inventory_id)

    thread = threading.Thread(
        target=_run,
        name=f"inventory-discovery-{inventory_id}",
        daemon=True,
    )
    thread.start()
    return thread

PRODUCT_NAME = "AltaStor Backup & Replication"
PRODUCT_VERSION = "4.1.0"

REPO_TYPES = ("local", "nfs", "smb", "s3")

PRODUCT_STORAGE_MODE = 0o750

EXECUTION_TYPES = ("Incremental", "ActiveFull", "SyntheticFull")
EXECUTION_REASONS = (
    "FirstRun",
    "ForceFull",
    "ActiveFullWindow",
    "SyntheticFullWindow",
    "ActiveOverridesSynthetic",
    "RepositorySyntheticUnsupportedEscalatedToActive",
    "StandardIncremental",
)
JOB_TYPES = ("backup", "backup-copy")
TASK_TYPES = ("backup", "restore")

VERIFY_ARTIFACT_TIMEOUT_SECONDS = env_int("ALTASTOR_VERIFY_TIMEOUT_SECONDS")
VERIFY_CONCURRENCY = env_int("ALTASTOR_VERIFY_CONCURRENCY")
SESSION_STATES = (
    "queued",
    "running",
    "stopping",
    "stopped",
    "completed",
    "failed",
    "canceled",
)
# Largest page GET /sessions will hand back in one response. Sessions are the
# fastest-growing catalog (one per VM per run) and the console re-polls the
# list every few seconds, so the endpoint refuses to serve an unbounded slice
# above this -- callers walk the rest with `offset`.
SESSION_PAGE_LIMIT_MAX = 500

DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_TO_INDEX = {name: index for index, name in enumerate(DAY_ORDER)}
DAY_TO_LONG_NAME = {
    "Mon": "monday",
    "Tue": "tuesday",
    "Wed": "wednesday",
    "Thu": "thursday",
    "Fri": "friday",
    "Sat": "saturday",
    "Sun": "sunday",
}
LONG_TO_DAY = {value: key for key, value in DAY_TO_LONG_NAME.items()}
DAY_ALIASES = {
    "mon": "Mon",
    "monday": "Mon",
    "tue": "Tue",
    "tues": "Tue",
    "tuesday": "Tue",
    "wed": "Wed",
    "wednesday": "Wed",
    "thu": "Thu",
    "thur": "Thu",
    "thurs": "Thu",
    "thursday": "Thu",
    "fri": "Fri",
    "friday": "Fri",
    "sat": "Sat",
    "saturday": "Sat",
    "sun": "Sun",
    "sunday": "Sun",
}
CRON_WEEKDAY_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
CRON_PATTERN = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")
DURATION_PATTERN = re.compile(r"^(?P<value>[1-9]\d*)(?P<unit>[mhdMHD])$")
ASCII_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
# Free-form text fields (paths, hostnames, URLs, descriptions) are allowed any
# printable ASCII character (0x20-0x7E) -- letters, digits, spaces and
# punctuation -- but nothing outside it. This blocks Cyrillic/CJK/emoji and control
# characters (the DB-corrupting input) while still accepting dots, slashes, colons
# and symbols that names cannot use.
PRINTABLE_ASCII_PATTERN = re.compile(r"^[\x20-\x7e]*$")
# External-system credentials (inventory username/password) belong to the target
# platform's own account namespace (e.g. AD/SSO), which can legitimately contain
# Cyrillic or other non-Latin characters -- the orchestrator has no basis to police
# their charset. Only control characters are rejected, since those are what can
# corrupt DB storage; the target platform rejects genuinely invalid credentials
# itself on connect.
NO_CONTROL_CHARS_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]*$")
MAX_NAME_LENGTH = 125
MAX_TEXT_LENGTH = 255
MAX_LONG_TEXT_LENGTH = 1024
NFS_PATTERN = re.compile(r"^[^:/]+:.+$")
SMB_PATTERN = re.compile(r"^(//|\\\\)[^/\\\\]+[/\\\\].+$")
ZONEINFO_ROOT = Path("/usr/share/zoneinfo")

CATALOG_SNAPSHOT_FORMAT_VERSION = 1
# Entropy behind an auto-generated job encryption key. token_urlsafe renders it
# as ~43 printable characters, which the data mover takes as a passphrase.
_GENERATED_ENCRYPTION_KEY_BYTES = 32
CONFIGURATION_EXPORT_ALLOWLIST: tuple[str, ...] = (
    "ALTASTOR_BACKUP_APP",
    "ALTASTOR_BACKUP_HOST",
    "ALTASTOR_BACKUP_PORT",
    "ABR_HTTP_REDIRECT_PORT",
    "MONGODB_URI",
    "MONGODB_DB_NAME",
    "MONGODB_MAX_POOL_SIZE",
    "MONGODB_SERVER_SELECTION_TIMEOUT_MS",
    "MONGODB_CONNECT_TIMEOUT_MS",
    "MONGODB_SOCKET_TIMEOUT_MS",
    "BACKUPSERVER_STORAGE_ROOT",
    "BACKUPSERVER_VCENTER_TOOL_TIMEOUT_SECONDS",
    "BACKUPSERVER_VCENTER_MIN_TRANSFER_MBPS",
    "BACKUPSERVER_VCENTER_SESSION_KEEPALIVE_SECONDS",
    "BACKUP_WORKER_COUNT",
    "MAX_CONCURRENT_BACKUPS",
    "BACKUP_TOOL_MAX_CAPTURE_BYTES",
    "BACKUP_TOOL_NICE",
    "BACKUP_TOOL_IONICE_CLASS",
    "ALTASTOR_DISK_TOOL_PATH",
    "ALTASTOR_LOG_DIR",
    "TZ",
    "ABR_BOOTSTRAP_ADMIN_USER",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD",
    "ABR_SESSION_TTL_SECONDS",
    "ABR_LOGIN_MAX_ATTEMPTS",
    "ABR_LOGIN_LOCKOUT_SECONDS",
)
SERVER_DEFAULTS_FILE_ENV_VAR = "ABR_SERVER_DEFAULTS_FILE"
DEFAULT_SERVER_DEFAULTS_FILE = Path("/etc/default/altastor-backup-server")


def _now_utc() -> datetime:
    return datetime.fromisoformat(utc_now())


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("created_at") or ""), str(record.get("id") or "")


def _local_zoneinfo_name() -> str | None:
    tz_env = str(os.getenv("TZ") or "").strip()
    if tz_env:
        try:
            ZoneInfo(tz_env)
        except ZoneInfoNotFoundError:
            pass
        else:
            return tz_env

    localtime_path = Path("/etc/localtime")
    try:
        if localtime_path.exists():
            resolved = localtime_path.resolve(strict=True)
            if resolved.is_relative_to(ZONEINFO_ROOT):
                return str(resolved.relative_to(ZONEINFO_ROOT))
    except OSError:
        pass

    timezone_path = Path("/etc/timezone")
    try:
        if timezone_path.exists():
            candidate = timezone_path.read_text(encoding="utf-8").strip()
            if candidate:
                ZoneInfo(candidate)
                return candidate
    except (OSError, ZoneInfoNotFoundError):
        pass

    local_tz = datetime.now().astimezone().tzinfo
    if isinstance(local_tz, ZoneInfo) and local_tz.key:
        return local_tz.key
    return None


def _server_timezone_name() -> str:
    zone_name = _local_zoneinfo_name()
    if zone_name:
        return zone_name
    tz_name = str(datetime.now().astimezone().tzname() or "").upper()
    if tz_name in {"UTC", "GMT"}:
        return "UTC"
    return "UTC"


def _server_timezone_aliases() -> set[str]:
    try:
        server_zone = ZoneInfo(_server_timezone_name())
    except ZoneInfoNotFoundError:
        return set()
    current_year = datetime.now(timezone.utc).year
    aliases = set()
    for month in (1, 7):
        alias = datetime(current_year, month, 1, 12, 0, tzinfo=server_zone).tzname()
        if alias:
            aliases.add(str(alias).upper())
    return aliases


def _load_timezone(name: str | None) -> ZoneInfo:
    candidate = str(name or _server_timezone_name()).strip() or "UTC"
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        if candidate.upper() in _server_timezone_aliases():
            try:
                return ZoneInfo(_server_timezone_name())
            except ZoneInfoNotFoundError:
                pass
        return ZoneInfo("UTC")


def _title_day(value: str) -> str:
    normalized = DAY_ALIASES.get(str(value or "").strip().lower())
    if normalized is None:
        raise api_error(
            422,
            "invalid_weekday",
            "Weekdays must use Mon-Sun values",
            value=value,
            valid_values=list(DAY_ORDER),
        )
    return normalized


def _normalize_day_list(values: Iterable[str] | None) -> list[str]:
    items = []
    for value in values or ():
        day = _title_day(str(value))
        if day not in items:
            items.append(day)
    return items


def _split_csv(raw: str | None) -> list[str]:
    if raw in {None, ""}:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _ensure(condition: bool, status_code: int, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise api_error(status_code, code, message, **details)


def _ensure_ascii_name(value: str, *, field: str, code: str) -> None:
    _ensure(
        len(value) <= MAX_NAME_LENGTH,
        400,
        code,
        f"{field} must be at most {MAX_NAME_LENGTH} characters",
        field=field,
        value=value,
        max_length=MAX_NAME_LENGTH,
    )
    _ensure(
        bool(ASCII_NAME_PATTERN.fullmatch(value)),
        400,
        code,
        f"{field} must use Latin letters, numbers, spaces, dots, underscores, or hyphens",
        field=field,
        value=value,
    )


def _ensure_ascii_text(
    value: str,
    *,
    field: str,
    code: str,
    max_length: int = MAX_TEXT_LENGTH,
    redact: bool = False,
    allow_unicode: bool = False,
) -> None:
    """Validate a free-form text field: reject control characters and cap length.
    Looser than :func:`_ensure_ascii_name` -- punctuation, slashes, colons and
    other printable-ASCII symbols are allowed (so paths, hostnames and URLs
    pass). By default, anything outside printable ASCII (Cyrillic, CJK, emoji,
    control characters) is also rejected; pass ``allow_unicode=True`` for
    external-system credentials, which may legitimately use any script and are
    only checked for control characters. Empty values are treated as "not
    provided" and skipped; required-ness is enforced separately by callers.
    Set ``redact`` for secrets so the offending value is not echoed in errors.
    """
    if not value:
        return
    details: dict[str, Any] = {"field": field, "max_length": max_length}
    if not redact:
        details["value"] = value
    _ensure(
        len(value) <= max_length,
        400,
        code,
        f"{field} must be at most {max_length} characters",
        **details,
    )
    if allow_unicode:
        _ensure(
            bool(NO_CONTROL_CHARS_PATTERN.fullmatch(value)),
            400,
            code,
            f"{field} must not contain control characters",
            **details,
        )
    else:
        _ensure(
            bool(PRINTABLE_ASCII_PATTERN.fullmatch(value)),
            400,
            code,
            f"{field} must use Latin letters, numbers, and standard keyboard symbols "
            f"(no Cyrillic or other non-Latin characters)",
            **details,
        )


def _validate_text_fields(
    payload: dict[str, Any],
    specs: tuple[tuple[str, str, int, bool], ...] | tuple[tuple[str, str, int, bool, bool], ...],
    *,
    code: str,
) -> None:
    """Apply :func:`_ensure_ascii_text` to each present, non-null field in
    ``payload``. ``specs`` is a tuple of ``(key, label, max_length, redact)`` or
    ``(key, label, max_length, redact, allow_unicode)``."""
    for spec in specs:
        key, label, max_length, redact = spec[:4]
        allow_unicode = spec[4] if len(spec) > 4 else False
        if payload.get(key) is not None:
            _ensure_ascii_text(
                str(payload[key]),
                field=label,
                code=code,
                max_length=max_length,
                redact=redact,
                allow_unicode=allow_unicode,
            )


INVENTORY_TEXT_FIELDS = (
    ("address", "Inventory address", MAX_TEXT_LENGTH, False),
    ("username", "Inventory username", MAX_TEXT_LENGTH, False, True),
    ("password", "Inventory password", MAX_TEXT_LENGTH, True, True),
    ("token", "Inventory token", MAX_LONG_TEXT_LENGTH, True),
    ("description", "Inventory description", MAX_LONG_TEXT_LENGTH, False),
)

REPOSITORY_TEXT_FIELDS = (
    ("path", "Repository path", MAX_LONG_TEXT_LENGTH, False),
    ("address", "Repository address", MAX_LONG_TEXT_LENGTH, False),
    ("server", "Repository server", MAX_TEXT_LENGTH, False),
    ("share", "Repository share", MAX_TEXT_LENGTH, False),
    ("username", "Repository username", MAX_TEXT_LENGTH, False),
    ("password", "Repository password", MAX_TEXT_LENGTH, True),
    ("description", "Repository description", MAX_LONG_TEXT_LENGTH, False),
)


def _get_required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise KeyError(key)
    return mapping[key]


def _find_by_id(items: Iterable[dict[str, Any]], record_id: str, *, label: str) -> dict[str, Any]:
    for item in items:
        if str(item.get("id") or "") == str(record_id):
            return item
    raise api_error(404, f"{label.lower()}_not_found", f"{label} not found", record_id=record_id)


def _find_by_name(items: Iterable[dict[str, Any]], name: str, *, label: str) -> dict[str, Any]:
    lowered = str(name).lower()
    matches = [item for item in items if str(item.get("name") or "").lower() == lowered]
    if not matches:
        raise api_error(404, f"{label.lower()}_not_found", f"{label} not found", name=name)
    if len(matches) > 1:
        raise api_error(409, f"ambiguous_{label.lower()}", f"Multiple {label.lower()} records matched the provided name", name=name)
    return matches[0]


def resolve_selector(
    items: Iterable[dict[str, Any]],
    *,
    name: str | None = None,
    record_id: str | None = None,
    label: str,
) -> dict[str, Any]:
    resolved_by_name = _find_by_name(items, name, label=label) if name else None
    resolved_by_id = _find_by_id(items, record_id, label=label) if record_id else None
    if resolved_by_name is not None and resolved_by_id is not None:
        if str(resolved_by_name.get("id")) != str(resolved_by_id.get("id")):
            raise api_error(
                409,
                "selector_mismatch",
                f"{label} selector mismatch: --name and --id refer to different records",
                name=name,
                record_id=record_id,
            )
        return resolved_by_id
    if resolved_by_name is not None:
        return resolved_by_name
    if resolved_by_id is not None:
        return resolved_by_id
    raise api_error(422, "missing_selector", f"{label} selection requires --name or --id")


def _duration_to_minutes(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(str(value or "").strip())
    _ensure(bool(match), 422, "invalid_schedule", "Periodic schedules must use durations like 15m, 1h, 6h, or 1d", every=value)
    amount = int(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = {"m": 1, "h": 60, "d": 1440}[unit]
    return amount * multiplier


def _format_interval_minutes(interval_minutes: int) -> str:
    _ensure(interval_minutes > 0, 422, "invalid_schedule", "Periodic schedules require interval_minutes > 0", interval_minutes=interval_minutes)
    if interval_minutes % 1440 == 0:
        return f"{interval_minutes // 1440}d"
    if interval_minutes % 60 == 0:
        return f"{interval_minutes // 60}h"
    return f"{interval_minutes}m"


def _day_values_to_long_names(values: Iterable[str] | None) -> list[str]:
    return [DAY_TO_LONG_NAME[day] for day in _normalize_day_list(values)]


def _normalize_schedule_text(raw: str) -> dict[str, Any]:
    value = str(raw or "").strip()
    _ensure(bool(value), 422, "invalid_schedule", "Schedule must not be empty")
    timezone_name = _server_timezone_name()
    lowered = value.lower()
    if lowered.startswith("daily "):
        hhmm = value[6:].strip()
        _ensure(bool(TIME_PATTERN.fullmatch(hhmm)), 422, "invalid_schedule", "Daily schedule must use `daily HH:MM`", schedule=value)
        return {"kind": "daily", "time": hhmm, "timezone": timezone_name, "enabled": True}
    if lowered.startswith("weekly "):
        parts = value.split()
        _ensure(len(parts) == 3, 422, "invalid_schedule", "Weekly schedule must use `weekly DAY HH:MM`", schedule=value)
        days = _normalize_day_list(parts[1].split(","))
        _ensure(bool(days), 422, "invalid_schedule", "Weekly schedules require at least one weekday", schedule=value)
        hhmm = parts[2]
        _ensure(bool(TIME_PATTERN.fullmatch(hhmm)), 422, "invalid_schedule", "Weekly schedule must use `weekly DAY HH:MM`", schedule=value)
        return {"kind": "weekly", "weekdays": days, "time": hhmm, "timezone": timezone_name, "enabled": True}
    if lowered.startswith("monthly "):
        parts = value.split()
        _ensure(len(parts) == 3, 422, "invalid_schedule", "Monthly schedule must use `monthly DAY HH:MM`", schedule=value)
        _ensure(parts[1].isdigit(), 422, "invalid_schedule", "Monthly schedules require a numeric day-of-month", schedule=value)
        day_of_month = int(parts[1])
        _ensure(1 <= day_of_month <= 31, 422, "invalid_schedule", "Monthly schedules require a day-of-month between 1 and 31", schedule=value)
        hhmm = parts[2]
        _ensure(bool(TIME_PATTERN.fullmatch(hhmm)), 422, "invalid_schedule", "Monthly schedule must use `monthly DAY HH:MM`", schedule=value)
        return {"kind": "monthly", "day_of_month": day_of_month, "time": hhmm, "timezone": timezone_name, "enabled": True}
    if lowered.startswith("periodic "):
        parts = value.split()
        _ensure(len(parts) == 2, 422, "invalid_schedule", "Periodic schedules must use `periodic <duration>`", schedule=value)
        every = parts[1].strip().lower()
        interval_minutes = _duration_to_minutes(every)
        return {"kind": "periodic", "every": every, "interval_minutes": interval_minutes, "timezone": timezone_name, "enabled": True}
    _ensure(bool(CRON_PATTERN.fullmatch(value)), 422, "invalid_schedule", "Schedule must use a supported schedule syntax or cron expression", schedule=value)
    validate_cron_expression(value)
    return {"kind": "cron", "expression": value, "timezone": timezone_name, "enabled": True}


def normalize_schedule_payload(raw: Any) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        return _normalize_schedule_text(raw)

    _ensure(isinstance(raw, dict), 422, "invalid_schedule", "Schedule must be an object or a legacy schedule string")
    source = dict(raw)
    timezone_name = str(source.get("timezone") or _server_timezone_name())
    enabled = bool(source.get("enabled", True))
    requested_type = str(source.get("type") or source.get("schedule_type") or source.get("kind") or "").strip().lower()

    if requested_type in {"interval"}:
        requested_type = "periodic"

    if requested_type == "cron":
        expression = str(source.get("expression") or "").strip()
        _ensure(bool(expression), 422, "invalid_schedule", "Cron schedules require expression")
        validate_cron_expression(expression)
        return {"kind": "cron", "expression": expression, "timezone": timezone_name, "enabled": enabled}

    if requested_type == "periodic":
        every_value = str(source.get("every") or "").strip().lower()
        interval_minutes = source.get("interval_minutes")
        if every_value:
            interval_minutes = _duration_to_minutes(every_value)
        else:
            try:
                interval_minutes = int(interval_minutes)
            except (TypeError, ValueError) as exc:
                raise api_error(422, "invalid_schedule", "Periodic schedules require `every` or interval_minutes") from exc
            _ensure(interval_minutes > 0, 422, "invalid_schedule", "Periodic schedules require interval_minutes > 0")
            every_value = _format_interval_minutes(interval_minutes)
        _ensure(source.get("time") in {None, ""} and source.get("start_time") in {None, ""}, 422, "invalid_schedule", "Periodic schedules do not accept time")
        _ensure(not source.get("days") and not source.get("weekdays"), 422, "invalid_schedule", "Periodic schedules do not accept weekdays")
        _ensure(source.get("day_of_month") in {None, ""}, 422, "invalid_schedule", "Periodic schedules do not accept day_of_month")
        return {
            "kind": "periodic",
            "every": every_value,
            "interval_minutes": int(interval_minutes),
            "timezone": timezone_name,
            "enabled": enabled,
        }

    time_value = str(source.get("time") or source.get("start_time") or "").strip()
    _ensure(bool(TIME_PATTERN.fullmatch(time_value)), 422, "invalid_schedule", "Schedules with calendar times must use strict HH:MM", time=time_value)

    if requested_type == "daily":
        _ensure(not source.get("days") and not source.get("weekdays"), 422, "invalid_schedule", "Daily schedules do not accept weekdays")
        _ensure(source.get("day_of_month") in {None, ""}, 422, "invalid_schedule", "Daily schedules do not accept day_of_month")
        return {"kind": "daily", "time": time_value, "timezone": timezone_name, "enabled": enabled}

    if requested_type == "weekly":
        days = source.get("days") if source.get("days") is not None else source.get("weekdays")
        if isinstance(days, str):
            days = days.split(",")
        normalized_days = _normalize_day_list(days or [])
        _ensure(bool(normalized_days), 422, "invalid_schedule", "Weekly schedules require at least one weekday")
        _ensure(source.get("day_of_month") in {None, ""}, 422, "invalid_schedule", "Weekly schedules do not accept day_of_month")
        return {"kind": "weekly", "weekdays": normalized_days, "time": time_value, "timezone": timezone_name, "enabled": enabled}

    if requested_type == "monthly":
        try:
            day_of_month = int(source.get("day_of_month"))
        except (TypeError, ValueError) as exc:
            raise api_error(422, "invalid_schedule", "Monthly schedules require day_of_month") from exc
        _ensure(1 <= day_of_month <= 31, 422, "invalid_schedule", "Monthly schedules require day_of_month from 1 to 31")
        _ensure(not source.get("days") and not source.get("weekdays"), 422, "invalid_schedule", "Monthly schedules do not accept weekdays")
        return {"kind": "monthly", "day_of_month": day_of_month, "time": time_value, "timezone": timezone_name, "enabled": enabled}

    raise api_error(422, "invalid_schedule", "Schedule type must be one of: daily, weekly, monthly, periodic", schedule_type=requested_type or None)


def _schedule_dto(schedule: dict[str, Any] | None) -> dict[str, Any] | None:
    if not schedule:
        return None
    kind = str(schedule.get("kind") or "")
    enabled = bool(schedule.get("enabled", True))
    timezone_name = str(schedule.get("timezone") or _server_timezone_name())
    if kind == "cron":
        return {
            "type": "cron",
            "expression": str(schedule.get("expression") or ""),
            "enabled": enabled,
            "timezone": timezone_name,
        }
    if kind == "daily":
        return {
            "type": "daily",
            "time": str(schedule.get("time") or ""),
            "enabled": enabled,
            "timezone": timezone_name,
        }
    if kind == "weekly":
        return {
            "type": "weekly",
            "days": _day_values_to_long_names(schedule.get("weekdays")),
            "time": str(schedule.get("time") or ""),
            "enabled": enabled,
            "timezone": timezone_name,
        }
    if kind == "monthly":
        return {
            "type": "monthly",
            "day_of_month": int(schedule.get("day_of_month") or 1),
            "time": str(schedule.get("time") or ""),
            "enabled": enabled,
            "timezone": timezone_name,
        }
    if kind == "periodic":
        interval_minutes = int(schedule.get("interval_minutes") or _duration_to_minutes(str(schedule.get("every") or "")))
        return {
            "type": "periodic",
            "every": str(schedule.get("every") or _format_interval_minutes(interval_minutes)),
            "interval_minutes": interval_minutes,
            "enabled": enabled,
            "timezone": timezone_name,
        }
    return _json_clone(schedule)


def _render_schedule_summary(schedule: dict[str, Any] | None) -> str | None:
    if not schedule:
        return None
    timezone_name = str(schedule.get("timezone") or _server_timezone_name())
    kind = str(schedule.get("kind") or "")
    if kind == "cron":
        return f"Cron {schedule['expression']} ({timezone_name})"
    if kind == "daily":
        return f"Daily at {schedule['time']} ({timezone_name})"
    if kind == "weekly":
        days = ", ".join(schedule.get("weekdays", []))
        return f"Weekly on {days} at {schedule['time']} ({timezone_name})"
    if kind == "monthly":
        return f"Monthly on day {schedule['day_of_month']} at {schedule['time']} ({timezone_name})"
    if kind == "periodic":
        return f"Every {schedule.get('every') or _format_interval_minutes(int(schedule['interval_minutes']))} ({timezone_name})"
    return None


def _parse_hhmm(value: str) -> tuple[int, int]:
    _ensure(bool(TIME_PATTERN.fullmatch(value)), 422, "invalid_time", "Time must use strict HH:MM", value=value)
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _localize(dt_value: datetime, schedule: dict[str, Any]) -> datetime:
    return dt_value.astimezone(_load_timezone(str(schedule.get("timezone") or _server_timezone_name())))


def _cron_field_values(field: str, *, minimum: int, maximum: int, names: dict[str, int] | None = None) -> set[int]:
    result: set[int] = set()
    aliases = names or {}

    def parse_value(raw: str) -> int:
        if raw in aliases:
            return aliases[raw]
        _ensure(raw.isdigit(), 422, "invalid_schedule", "Cron field value must be numeric", field=field, value=raw)
        return int(raw)

    for part in field.split(","):
        piece = part.strip().lower()
        _ensure(bool(piece), 422, "invalid_schedule", "Invalid cron field", field=field)
        step = 1
        if "/" in piece:
            base, step_value = piece.split("/", 1)
            piece = base
            _ensure(step_value.isdigit() and int(step_value) > 0, 422, "invalid_schedule", "Cron step must be a positive integer", field=field)
            step = int(step_value)
        if piece == "*":
            start = minimum
            end = maximum
        elif "-" in piece:
            left, right = piece.split("-", 1)
            start = parse_value(left)
            end = parse_value(right)
        else:
            start = parse_value(piece)
            end = start
        if names is CRON_WEEKDAY_NAMES:
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        _ensure(minimum <= start <= maximum, 422, "invalid_schedule", "Cron field value is out of range", field=field, value=start)
        _ensure(minimum <= end <= maximum or (names is CRON_WEEKDAY_NAMES and end == 0), 422, "invalid_schedule", "Cron field value is out of range", field=field, value=end)
        if names is CRON_WEEKDAY_NAMES and start > end and end == 0:
            span = list(range(start, maximum + 1, step))
            span.extend(range(minimum, end + 1, step))
        else:
            _ensure(start <= end, 422, "invalid_schedule", "Cron field range is invalid", field=field, start=start, end=end)
            span = list(range(start, end + 1, step))
        result.update(span)
    return result


def validate_cron_expression(expression: str) -> None:
    parts = expression.split()
    _ensure(len(parts) == 5, 422, "invalid_schedule", "Cron expressions must use five fields", schedule=expression)
    _cron_field_values(parts[0], minimum=0, maximum=59)
    _cron_field_values(parts[1], minimum=0, maximum=23)
    _cron_field_values(parts[2], minimum=1, maximum=31)
    _cron_field_values(parts[3], minimum=1, maximum=12)
    _cron_field_values(parts[4], minimum=0, maximum=6, names=CRON_WEEKDAY_NAMES)


def schedule_next_run(schedule: dict[str, Any], *, after: datetime) -> datetime:
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    local_after = _localize(after, schedule)
    kind = str(schedule.get("kind") or "")
    if not bool(schedule.get("enabled", True)):
        raise api_error(422, "invalid_schedule", "Disabled schedules do not have a next run")
    if kind == "daily":
        hour, minute = _parse_hhmm(str(schedule.get("time")))
        candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    if kind == "weekly":
        weekdays = _normalize_day_list(schedule.get("weekdays", []))
        hour, minute = _parse_hhmm(str(schedule.get("time")))
        wanted = {DAY_TO_INDEX[day] for day in weekdays}
        for offset in range(0, 8):
            candidate_date = local_after.date() + timedelta(days=offset)
            if candidate_date.weekday() not in wanted:
                continue
            candidate = datetime.combine(candidate_date, time(hour=hour, minute=minute), tzinfo=local_after.tzinfo)
            if candidate > local_after:
                return candidate.astimezone(timezone.utc)
        raise api_error(422, "invalid_schedule", "Unable to compute next weekly run")
    if kind == "monthly":
        hour, minute = _parse_hhmm(str(schedule.get("time")))
        try:
            day_of_month = int(schedule.get("day_of_month"))
        except (TypeError, ValueError) as exc:
            raise api_error(422, "invalid_schedule", "Monthly schedules require day_of_month") from exc
        _ensure(day_of_month >= 1, 422, "invalid_schedule", "Monthly schedules require day_of_month >= 1")
        year = local_after.year
        month = local_after.month
        for _ in range(0, 14):
            last_day = calendar.monthrange(year, month)[1]
            candidate_day = min(day_of_month, last_day)
            candidate = datetime(year, month, candidate_day, hour, minute, tzinfo=local_after.tzinfo)
            if candidate > local_after:
                return candidate.astimezone(timezone.utc)
            month += 1
            if month > 12:
                month = 1
                year += 1
        raise api_error(422, "invalid_schedule", "Unable to compute next monthly run")
    if kind == "periodic":
        interval_minutes = schedule.get("interval_minutes")
        try:
            interval = int(interval_minutes if interval_minutes is not None else _duration_to_minutes(str(schedule.get("every") or "")))
        except (TypeError, ValueError) as exc:
            raise api_error(422, "invalid_schedule", "Periodic schedules require interval_minutes") from exc
        _ensure(interval > 0, 422, "invalid_schedule", "Periodic schedules require interval_minutes > 0")
        return (after.astimezone(timezone.utc) + timedelta(minutes=interval)).astimezone(timezone.utc)
    if kind == "cron":
        expression = str(schedule.get("expression") or "")
        validate_cron_expression(expression)
        minute_values, hour_values, day_values, month_values, weekday_values = (
            _cron_field_values(expression.split()[0], minimum=0, maximum=59),
            _cron_field_values(expression.split()[1], minimum=0, maximum=23),
            _cron_field_values(expression.split()[2], minimum=1, maximum=31),
            _cron_field_values(expression.split()[3], minimum=1, maximum=12),
            _cron_field_values(expression.split()[4], minimum=0, maximum=6, names=CRON_WEEKDAY_NAMES),
        )
        probe = local_after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = probe + timedelta(days=366)
        while probe <= limit:
            cron_weekday = (probe.weekday() + 1) % 7
            if (
                probe.minute in minute_values
                and probe.hour in hour_values
                and probe.day in day_values
                and probe.month in month_values
                and cron_weekday in weekday_values
            ):
                return probe.astimezone(timezone.utc)
            probe += timedelta(minutes=1)
        raise api_error(422, "invalid_schedule", "Unable to compute next cron run", schedule=expression)
    raise api_error(422, "invalid_schedule", "Unsupported schedule kind", kind=kind)


def schedule_today_matches_days(job: dict[str, Any], days: list[str], *, now: datetime) -> bool:
    if not days:
        return False
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    local_now = _localize(now, schedule)
    return DAY_ORDER[local_now.weekday()] in _normalize_day_list(days)


def repository_supports_synthetic(repository: dict[str, Any]) -> bool:
    metadata = repository.get("metadata") if isinstance(repository.get("metadata"), dict) else {}
    if isinstance(metadata.get("supports_synthetic_full"), bool):
        return bool(metadata["supports_synthetic_full"])
    return str(repository.get("type") or "") in {"local", "nfs"}


def repository_catalog_summary(repository_id: str) -> dict[str, int]:
    chains: set[str] = set()
    restore_points = 0
    used_bytes = 0
    for point in normalized_backup_points():
        if str(point.get("repository_id") or "") != repository_id:
            continue
        chains.add(str(point.get("chain_id") or ""))
        restore_points += 1
        used_bytes += _safe_int(point.get("size_bytes")) or 0
    return {"backup_chains": len(chains), "restore_points": restore_points, "used_bytes": used_bytes}


def _jobs_referencing_repository_id(repository_id: str) -> list[dict[str, Any]]:
    linked_jobs = [
        {"id": job["id"], "name": job["name"]}
        for job in normalized_job_records()
        if str(job.get("repo_id") or "") == repository_id
    ]
    linked_jobs.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    return linked_jobs


def _active_sessions_for_job(job_id: str) -> list[dict[str, Any]]:
    sessions = []
    for task in catalog_records(TASKS):
        if str(task.get("job_id") or "") != job_id:
            continue
        state = _session_state(task)
        if state in {"queued", "running", "stopping"}:
            sessions.append({"id": task.get("id"), "state": state})
    sessions.sort(key=lambda item: (str(item.get("state") or ""), str(item.get("id") or "")))
    return sessions


def normalize_inventory_record(record: dict[str, Any]) -> dict[str, Any]:
    item = _json_clone(record)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    credentials = metadata.get("credentials") if isinstance(metadata.get("credentials"), dict) else {}
    metadata["credentials"] = credentials
    item["metadata"] = metadata
    item["type"] = inventory_type_from_record(item)
    item["status"] = str(item.get("status") or "unknown")
    item["created_at"] = str(item.get("created_at") or utc_now())
    item["updated_at"] = str(item.get("updated_at") or item["created_at"])
    item["last_scan_at"] = item.get("last_scan_at")
    item["discovery"] = {
        "last_scan_at": item.get("last_scan_at"),
        "last_scan_error": item.get("last_scan_error"),
        "vm_count": int(item.get("vm_count") or 0),
        "host_count": int(item.get("host_count") or 0),
        "cluster_count": int(item.get("cluster_count") or 0),
    }
    item["connection"] = {
        "address": item.get("address") or item.get("endpoint"),
        "endpoint": item.get("endpoint") or item.get("address"),
        "port": metadata.get("port"),
        "username": credentials.get("username") or item.get("username"),
        "token_present": bool(metadata.get("token")),
        "insecure": not bool(metadata.get("verify_ssl", True)),
    }
    refs = inventory_references(str(item.get("id") or ""))
    referenced_jobs = refs["jobs"] if isinstance(refs.get("jobs"), list) else []
    active_sessions = refs["tasks"] if isinstance(refs.get("tasks"), list) else []
    item["referenced_jobs"] = referenced_jobs
    item["jobs_count"] = len(referenced_jobs)
    item["active_sessions"] = active_sessions
    item["active_session_count"] = len(active_sessions)
    item["can_delete"] = not referenced_jobs and not active_sessions
    item["delete_disabled_reason"] = (
        "Inventory is referenced by backup jobs"
        if referenced_jobs
        else "Inventory has queued or running sessions"
        if active_sessions
        else None
    )
    mask_credential_fields(metadata)
    return item


def list_inventory_records() -> list[dict[str, Any]]:
    items = [normalize_inventory_record(item) for item in catalog_records(HYPERVISORS)]
    items.sort(key=lambda entry: (str(entry.get("name") or "").lower(), str(entry.get("id") or "")))
    return items


def _inventory_record_for_update(record_id: str) -> dict[str, Any]:
    return _find_by_id(catalog_records(HYPERVISORS), record_id, label="Inventory")


def resolve_inventory(name: str | None = None, record_id: str | None = None) -> dict[str, Any]:
    return normalize_inventory_record(resolve_selector(list_inventory_records(), name=name, record_id=record_id, label="Inventory"))


def _normalize_inventory_tree_health(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"ok", "healthy", "connected", "available", "online", "poweredon", "powered_on", "running"}:
        return "healthy"
    if value in {"warning", "degraded"}:
        return "warning"
    if value in {"error", "failed", "disconnected", "offline", "invalid", "unavailable"}:
        return "error"
    return "unknown"


def _aggregate_inventory_tree_health(values: Iterable[Any]) -> str:
    normalized = {_normalize_inventory_tree_health(value) for value in values}
    if "error" in normalized:
        return "warning" if len(normalized) > 1 else "error"
    if "warning" in normalized:
        return "warning"
    if "healthy" in normalized:
        return "healthy"
    return "unknown"


def _nullable_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _nullable_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _new_inventory_vm_tree_node(
    *,
    node_id: str,
    kind: str,
    label: str,
    inventory_id: str | None = None,
    inventory_name: str | None = None,
    datacenter_id: str | None = None,
    datacenter_name: str | None = None,
    cluster_id: str | None = None,
    cluster_name: str | None = None,
    host_id: str | None = None,
    host_name: str | None = None,
    vm_id: str | None = None,
    guest_os: str | None = None,
    cpu_count: int | float | None = None,
    memory_mb: int | float | None = None,
    power_state: str | None = None,
    ip_address: str | None = None,
    host_status: str | None = None,
    cpu_model: str | None = None,
    health: str | None = None,
    disk_capacity_bytes: int | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "inventory_id": inventory_id,
        "inventory_name": inventory_name,
        "datacenter_id": datacenter_id,
        "datacenter_name": datacenter_name,
        "disk_capacity_bytes": disk_capacity_bytes,
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "host_id": host_id,
        "host_name": host_name,
        "vm_id": vm_id,
        "guest_os": guest_os,
        "cpu_count": cpu_count,
        "memory_mb": memory_mb,
        "power_state": power_state,
        "ip_address": ip_address,
        "host_status": host_status,
        "cpu_model": cpu_model,
        "vm_count": 0,
        "health": health or "unknown",
        "children": [],
        "_child_index": {},
    }


def _ensure_inventory_vm_tree_child(parent: dict[str, Any], key: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    child_index = parent.setdefault("_child_index", {})
    node = child_index.get(key)
    if node is None:
        node = factory()
        child_index[key] = node
        parent.setdefault("children", []).append(node)
    return node


def _finalize_inventory_vm_tree_node(node: dict[str, Any]) -> dict[str, Any]:
    children = [_finalize_inventory_vm_tree_node(child) for child in node.get("children", [])]
    children.sort(key=lambda child: (str(child.get("label") or "").lower(), str(child.get("id") or "")))
    node["children"] = children
    node.pop("_child_index", None)

    if node["kind"] == "vm":
        node["vm_count"] = 1
        node["health"] = _normalize_inventory_tree_health(node.get("power_state") or node.get("health"))
        return node

    node["vm_count"] = sum(int(child.get("vm_count") or 0) for child in children)
    if children:
        node["health"] = _aggregate_inventory_tree_health(child.get("health") for child in children)
    else:
        node["health"] = _normalize_inventory_tree_health(node.get("health"))
    return node


def _strip_inventory_vm_tree_internal_keys(node: dict[str, Any]) -> dict[str, Any]:
    node.pop("_child_index", None)
    node["children"] = [_strip_inventory_vm_tree_internal_keys(child) for child in node.get("children", [])]
    return node


def _resource_payload(resource: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    payload = resource.get("payload")
    return payload if isinstance(payload, dict) else {}


def _resource_label(resource: dict[str, Any] | None, fallback: str | None = None) -> str | None:
    if not isinstance(resource, dict):
        return fallback
    return _nullable_str(resource.get("name") or resource.get("external_id")) or fallback


def list_inventory_vm_tree() -> list[dict[str, Any]]:
    root = {"children": [], "_child_index": {}}
    inventories = list_inventory_records()

    for inventory in inventories:
        inventory_id = str(inventory.get("id") or "")
        if not inventory_id:
            continue
        inventory_name = str(inventory.get("name") or inventory_id)
        resources = list(RESOURCES_BY_HV.get(inventory_id, {}).values())
        clusters = {
            str(resource.get("external_id") or ""): resource
            for resource in resources
            if str(resource.get("resource_type") or "") == "cluster"
        }
        hosts = {
            str(resource.get("external_id") or ""): resource
            for resource in resources
            if str(resource.get("resource_type") or "") == "host"
        }
        hosts_by_name = {
            str(resource.get("name") or ""): resource
            for resource in hosts.values()
            if str(resource.get("name") or "")
        }
        single_host = next(iter(hosts.values()), None) if len(hosts) == 1 else None

        # Sum discovered disk capacity per VM (vSphere reports real capacityInKB;
        # the generic/simulated discovery reports size_gb) so the tree can carry
        # each VM's real provisioned size instead of leaving it at 0.
        disk_capacity_by_vm: dict[str, int] = {}
        for resource in resources:
            if str(resource.get("resource_type") or "") != "disk":
                continue
            disk_payload = _resource_payload(resource)
            disk_vm_id = str(disk_payload.get("vm_id") or "")
            if not disk_vm_id:
                continue
            capacity = _capacity_bytes_from_disk_entry(disk_payload)
            if capacity is None:
                continue
            disk_capacity_by_vm[disk_vm_id] = disk_capacity_by_vm.get(disk_vm_id, 0) + capacity

        inventory_node = _ensure_inventory_vm_tree_child(
            root,
            f"inventory::{inventory_id}",
            lambda: _new_inventory_vm_tree_node(
                node_id=f"inventory::{inventory_id}",
                kind="inventory",
                label=inventory_name,
                inventory_id=inventory_id,
                inventory_name=inventory_name,
                health=str(inventory.get("status") or "unknown"),
            ),
        )

        vms = [
            resource
            for resource in resources
            if str(resource.get("resource_type") or "") == "virtual_machine"
        ]
        vms.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("external_id") or "")))

        for vm in vms:
            payload = _resource_payload(vm)
            vm_id = str(vm.get("external_id") or vm.get("id") or "")
            vm_name = str(vm.get("name") or vm_id or "Virtual Machine")

            host_id = _nullable_str(payload.get("host_external_id") or payload.get("host_id"))
            host_name = _nullable_str(payload.get("host_name") or payload.get("host"))
            host_resource = (hosts.get(host_id or "") if host_id else None) or (hosts_by_name.get(host_name or "") if host_name else None)
            if host_resource is None and host_id is None and host_name is None:
                host_resource = single_host
            host_payload = _resource_payload(host_resource)
            host_id = _nullable_str(host_id or (host_resource or {}).get("external_id"))
            host_name = _resource_label(host_resource, host_name)

            cluster_id = _nullable_str(payload.get("cluster_external_id") or payload.get("cluster_id") or host_payload.get("cluster_external_id"))
            cluster_resource = clusters.get(cluster_id or "") if cluster_id else None
            cluster_name = _nullable_str(payload.get("cluster_name")) or _resource_label(cluster_resource, cluster_id)

            datacenter_name = _nullable_str(payload.get("datacenter_name") or payload.get("datacenter") or host_payload.get("datacenter"))
            datacenter_id = _nullable_str(payload.get("datacenter_id")) or datacenter_name

            parent = inventory_node
            if datacenter_id or datacenter_name:
                datacenter_key = f"datacenter::{inventory_id}::{datacenter_id or datacenter_name}"
                parent = _ensure_inventory_vm_tree_child(
                    parent,
                    datacenter_key,
                    lambda: _new_inventory_vm_tree_node(
                        node_id=datacenter_key,
                        kind="datacenter",
                        label=datacenter_name or datacenter_id or "Unknown Datacenter",
                        inventory_id=inventory_id,
                        inventory_name=inventory_name,
                        datacenter_id=datacenter_id,
                        datacenter_name=datacenter_name,
                    ),
                )

            if cluster_id or cluster_name:
                cluster_key = f"cluster::{inventory_id}::{datacenter_id or datacenter_name or 'root'}::{cluster_id or cluster_name}"
                parent = _ensure_inventory_vm_tree_child(
                    parent,
                    cluster_key,
                    lambda: _new_inventory_vm_tree_node(
                        node_id=cluster_key,
                        kind="cluster",
                        label=cluster_name or cluster_id or "Unknown Cluster",
                        inventory_id=inventory_id,
                        inventory_name=inventory_name,
                        datacenter_id=datacenter_id,
                        datacenter_name=datacenter_name,
                        cluster_id=cluster_id,
                        cluster_name=cluster_name,
                    ),
                )

            if host_id or host_name:
                host_key = f"host::{inventory_id}::{datacenter_id or datacenter_name or 'root'}::{cluster_id or cluster_name or 'root'}::{host_id or host_name}"
                parent = _ensure_inventory_vm_tree_child(
                    parent,
                    host_key,
                    lambda: _new_inventory_vm_tree_node(
                        node_id=host_key,
                        kind="host",
                        # Prefer the host's DNS name for the label; host_name stays
                        # vCenter's own name (often the IP) because that is the
                        # identity restore resolves targets by.
                        label=_nullable_str(host_payload.get("dns_name")) or host_name or host_id or "Unknown Host",
                        inventory_id=inventory_id,
                        inventory_name=inventory_name,
                        datacenter_id=datacenter_id,
                        datacenter_name=datacenter_name,
                        cluster_id=cluster_id,
                        cluster_name=cluster_name,
                        host_id=host_id,
                        host_name=host_name,
                        host_status=_nullable_str(host_payload.get("status")),
                        cpu_model=_nullable_str(host_payload.get("cpu_model")),
                    ),
                )

            memory_mb = _nullable_number(payload.get("memory_mb"))
            if memory_mb is None and payload.get("memory_gb") is not None:
                memory_gb = _nullable_number(payload.get("memory_gb"))
                memory_mb = memory_gb * 1024 if memory_gb is not None else None

            power_state = _nullable_str(payload.get("power_state") or payload.get("state") or vm.get("status"))
            vm_node = _new_inventory_vm_tree_node(
                node_id=f"vm::{inventory_id}::{vm_id or vm_name}",
                kind="vm",
                label=vm_name,
                inventory_id=inventory_id,
                inventory_name=inventory_name,
                datacenter_id=datacenter_id,
                datacenter_name=datacenter_name,
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                host_id=host_id,
                host_name=host_name,
                vm_id=vm_id or vm_name,
                guest_os=_nullable_str(payload.get("guest_os") or payload.get("guest_full_name")),
                cpu_count=_nullable_number(payload.get("num_cpu") or payload.get("cpu_count") or payload.get("vcpus")),
                memory_mb=memory_mb,
                power_state=power_state,
                ip_address=_nullable_str(payload.get("ip_address") or payload.get("ip")),
                health=power_state,
                disk_capacity_bytes=disk_capacity_by_vm.get(vm_id),
            )
            parent.setdefault("children", []).append(vm_node)

    nodes = [_finalize_inventory_vm_tree_node(node) for node in root["children"]]
    nodes.sort(key=lambda node: (str(node.get("label") or "").lower(), str(node.get("id") or "")))
    return [_strip_inventory_vm_tree_internal_keys(node) for node in nodes]


def _inventory_endpoint_key(record: dict[str, Any]) -> tuple[str, int]:
    """(host, port) of the box a record points at, for duplicate detection.

    The host is re-derived from `address` rather than read from
    `metadata["host"]` so that records written by either inventory-create path
    compare equal regardless of how each normalized the address.
    """
    metadata = record.get("metadata")
    raw_port = metadata.get("port") if isinstance(metadata, dict) else None
    try:
        port = int(raw_port or 0)
    except (TypeError, ValueError):
        port = 0
    host = extract_host(str(record.get("address") or record.get("endpoint") or ""))
    return host.strip().lower(), port


def create_inventory_record(payload: dict[str, Any]) -> dict[str, Any]:
    inventory_type = normalize_inventory_type(str(payload.get("type") or ""))
    if inventory_type not in creatable_inventory_types():
        raise api_error(
            422,
            "invalid_inventory",
            f"Unsupported inventory type: {inventory_type}. Supported types: {', '.join(creatable_inventory_types())}",
        )
    name = str(payload.get("name") or "").strip()
    address = str(payload.get("address") or "").strip()
    port = int(payload.get("port") or 0) if payload.get("port") is not None else None
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    token = str(payload.get("token") or "").strip()
    insecure = bool(payload.get("insecure", False))
    description = str(payload.get("description") or "")
    _ensure(bool(name), 422, "invalid_inventory", "Inventory name is required")
    _ensure_ascii_name(name, field="Inventory name", code="invalid_inventory")
    _validate_text_fields(payload, INVENTORY_TEXT_FIELDS, code="invalid_inventory")
    _ensure(bool(address), 422, "invalid_inventory", "Inventory address is required")
    if inventory_type == "vsphere":
        _ensure(bool(username), 422, "invalid_inventory", "vSphere inventory requires username")
        _ensure(bool(password), 422, "invalid_inventory", "vSphere inventory requires password")
    else:
        _ensure(bool(token or (username and password)), 422, "invalid_inventory", "Inventory requires either token or username/password")
    created_at = utc_now()
    effective_port = port if port is not None else default_port_for_type(inventory_type)
    # Reject both a reused name and a second registration of the same box: the
    # same host under two names yields duplicate snapshots and duplicate jobs
    # racing each other on one vCenter.
    endpoint_key = (extract_host(address).strip().lower(), effective_port)
    for existing in catalog_records(HYPERVISORS):
        if str(existing.get("name") or "").lower() == name.lower():
            raise api_error(409, "inventory_name_exists", "Inventory name already exists", name=name)
        if endpoint_key[0] and _inventory_endpoint_key(existing) == endpoint_key:
            existing_name = str(existing.get("name") or "")
            raise api_error(
                409,
                "inventory_address_exists",
                f"This host is already registered as inventory '{existing_name}'",
                address=address,
                port=effective_port,
                existing_id=str(existing.get("id") or ""),
                existing_name=existing_name,
            )
    record = {
        "id": str(uuid4()),
        "name": name,
        "type": inventory_type,
        "kind": INVENTORY_KIND_BY_TYPE[inventory_type],
        "address": address,
        "endpoint": address,
        "username": username or None,
        "status": "unknown",
        "description": description,
        "created_at": created_at,
        "updated_at": created_at,
        "last_scan_at": None,
        "vm_count": 0,
        "host_count": 0,
        "cluster_count": 0,
        "metadata": {
            "host": address.split("/", 1)[0].split(":", 1)[0],
            "port": effective_port,
            "verify_ssl": not insecure,
            "credentials": {
                "username": username or None,
                "password": password or None,
            },
            "token": token or None,
        },
    }
    # Refuse to register a standalone ESXi host that is part of a cluster --
    # such a host must be backed up through its managing vCenter. Best-effort:
    # an unreachable endpoint does not block registration here (re-checked at
    # job-run time), only a positively detected cluster membership does.
    try:
        assert_inventory_registrable(record)
    except EsxHostInClusterError as exc:
        raise api_error(
            409,
            "esx_host_in_cluster",
            str(exc),
            cluster=exc.cluster_name,
        ) from exc
    persist_hypervisor(record)
    RESOURCES_BY_HV.setdefault(record["id"], {})
    if AUTO_DISCOVER_ON_INVENTORY_ADD:
        # Kick off discovery now so VM names are usable quickly, but keep `add`
        # responsive by doing it in the background. Lazy discovery in
        # `job create` still covers the case where this has not finished.
        schedule_inventory_discovery(record["id"])
    return normalize_inventory_record(record)


def edit_inventory_record(record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = _inventory_record_for_update(record_id)
    _validate_text_fields(payload, INVENTORY_TEXT_FIELDS, code="invalid_inventory")
    normalized = dict(record)
    updates: dict[str, Any] = {}
    for field_name in ("name", "address", "port", "username", "password", "description"):
        if field_name in payload and payload[field_name] is not None:
            updates[field_name] = payload[field_name]
    if "insecure" in payload and payload["insecure"] is not None:
        metadata = dict(normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {})
        metadata["verify_ssl"] = not bool(payload["insecure"])
        normalized["metadata"] = metadata
        normalized["updated_at"] = utc_now()
    if "token" in payload and payload["token"] is not None:
        metadata = dict(normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {})
        metadata["token"] = str(payload["token"] or "") or None
        normalized["metadata"] = metadata
        normalized["updated_at"] = utc_now()
    if updates:
        if "name" in updates:
            new_name = str(updates["name"] or "").strip()
            _ensure(bool(new_name), 422, "invalid_inventory", "Inventory name must not be empty")
            _ensure_ascii_name(new_name, field="Inventory name", code="invalid_inventory")
        normalized = apply_inventory_updates(normalized, updates)
    _ensure(any(key in payload for key in ("name", "address", "port", "username", "password", "description", "token", "insecure")), 422, "invalid_inventory", "inventory edit requires at least one editable option")
    persist_hypervisor(normalized)
    return normalize_inventory_record(normalized)


def test_inventory(record_id: str) -> dict[str, Any]:
    record = _inventory_record_for_update(record_id)
    provider = InventoryProviderRegistry.resolve(record)
    try:
        provider.test_connection(record)
    except EsxHostInClusterError as exc:
        update_hypervisor_fields(record["id"], {"status": "unavailable", "updated_at": utc_now()})
        return {
            "id": record["id"],
            "name": record.get("name"),
            "type": inventory_type_from_record(record),
            "status": "error",
            "code": "esx_host_in_cluster",
            "message": str(exc),
            "cluster": exc.cluster_name,
            "error": str(exc),
        }
    except Exception as exc:
        update_hypervisor_fields(record["id"], {"status": "unavailable", "updated_at": utc_now()})
        return {
            "id": record["id"],
            "name": record.get("name"),
            "type": inventory_type_from_record(record),
            "status": "error",
            "message": "Unable to connect to virtualization environment",
            "error": str(exc),
        }
    update_hypervisor_fields(record["id"], {"status": "available", "updated_at": utc_now()})
    return {
        "id": record["id"],
        "name": record.get("name"),
        "type": inventory_type_from_record(record),
        "status": "success",
        "message": "Connection successful",
    }


def _reconcile_jobs_after_vm_identity_change(
    inventory_id: str,
    previous_vm_ids_by_name: dict[str, str],
    discovered_items: list[dict[str, Any]],
) -> None:
    renamed_ids: dict[str, str] = {}
    for entry in discovered_items:
        if str(entry.get("resource_type") or "") != "virtual_machine":
            continue
        name = str(entry.get("name") or "")
        new_id = str(entry.get("external_id") or "")
        old_id = previous_vm_ids_by_name.get(name)
        if name and old_id and new_id and old_id != new_id:
            renamed_ids[old_id] = new_id
    if not renamed_ids:
        return
    for job in catalog_records(JOBS):
        if str(job.get("inventory_id") or job.get("hypervisor_id") or "") != inventory_id:
            continue
        vm_ids = job.get("vm_ids")
        if not isinstance(vm_ids, list) or not vm_ids:
            continue
        updated_ids = [renamed_ids.get(str(vm_id), vm_id) for vm_id in vm_ids]
        if updated_ids == vm_ids:
            continue
        LOGGER.info(
            "Job %r: one or more protected VMs kept their name but got a new id since the last "
            "scan (typically an in-place restore); updating the job to track the new id(s) so "
            "its next backup resumes automatically instead of failing.",
            job.get("name") or job.get("id"),
        )
        _persist_canonical_job({**job, "vm_ids": updated_ids, "updated_at": utc_now()})


def rescan_inventory(record_id: str) -> dict[str, Any]:
    record = _inventory_record_for_update(record_id)
    provider = InventoryProviderRegistry.resolve(record)
    previous_vm_ids_by_name = {
        str(resource.get("name") or ""): str(resource.get("external_id") or "")
        for resource in RESOURCES_BY_HV.get(record_id, {}).values()
        if resource.get("resource_type") == "virtual_machine" and resource.get("name")
    }
    try:
        resources = provider.rescan(record)
    except Exception as exc:
        # Persisted (not just raised) so the reason survives past this one
        # response -- a later GET (page reload, background auto-discovery
        # retry) still shows *why* the environment is unavailable instead of
        # just that it is. Cleared below on the next successful rescan.
        update_hypervisor_fields(
            record_id, {"status": "unavailable", "last_scan_error": str(exc), "updated_at": utc_now()}
        )
        raise api_error(
            502,
            "inventory_rescan_failed",
            "Unable to query virtualization environment",
            inventory_id=record_id,
            error=str(exc),
        ) from exc
    discovered_at = utc_now()
    items = []
    counts = {"vm_count": 0, "host_count": 0, "cluster_count": 0}
    for resource in resources:
        entry = dict(resource)
        entry["discovered_at"] = discovered_at
        items.append(entry)
        resource_type = str(entry.get("resource_type") or "")
        if resource_type == "virtual_machine":
            counts["vm_count"] += 1
        elif resource_type == "host":
            counts["host_count"] += 1
        elif resource_type == "cluster":
            counts["cluster_count"] += 1
    replace_resources_for_hypervisor(record_id, items)
    _reconcile_jobs_after_vm_identity_change(record_id, previous_vm_ids_by_name, items)
    updated = dict(record)
    updated.update(counts)
    updated["status"] = "available"
    updated["last_scan_at"] = discovered_at
    updated["updated_at"] = discovered_at
    updated["last_scan_error"] = None
    update_hypervisor_fields(
        record_id,
        {
            **counts,
            "status": "available",
            "last_scan_at": discovered_at,
            "updated_at": discovered_at,
            "last_scan_error": None,
        },
    )
    return {
        "id": record_id,
        "name": updated.get("name"),
        "status": updated.get("status"),
        "last_scan_at": updated.get("last_scan_at"),
        "discovery": counts,
    }


def remove_inventory_record(record_id: str) -> dict[str, Any]:
    inventory = _find_by_id(catalog_records(HYPERVISORS), record_id, label="Inventory")
    refs = inventory_references(record_id)
    if refs["jobs"] or refs["tasks"]:
        raise api_error(
            409,
            "inventory_in_use",
            "Inventory cannot be removed while jobs or active sessions still reference it",
            jobs=refs["jobs"],
            sessions=refs["tasks"],
        )
    delete_hypervisor(record_id)
    return {"id": str(inventory.get("id") or record_id), "removed": True}


def normalize_repository_record(record: dict[str, Any]) -> dict[str, Any]:
    item = _json_clone(record)
    metadata = dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {})
    repo_type = str(item.get("type") or "").strip().lower()
    item["status"] = str(item.get("status") or "available")
    item["created_at"] = str(item.get("created_at") or utc_now())
    item["updated_at"] = str(item.get("updated_at") or item["created_at"])
    item["last_scan_at"] = str(metadata.get("last_scan_at") or item.get("last_scan_at") or "") or None
    if metadata.get("address") is not None:
        item["address"] = metadata.get("address")
    if metadata.get("server") is not None:
        item["server"] = metadata.get("server")
    if metadata.get("share") is not None:
        item["share"] = metadata.get("share")
    if metadata.get("username") is not None:
        item["username"] = metadata.get("username")
    if metadata.get("description") is not None:
        item["description"] = metadata.get("description")
    if repo_type == "s3":
        s3_config = sanitize_s3_config(extract_s3_config(item))
        item["s3"] = s3_config
        metadata["s3"] = dict(s3_config)
        item["bucket"] = s3_config.get("bucket")
        item["prefix"] = s3_config.get("prefix")
        item["region"] = s3_config.get("region")
        item["endpoint_url"] = s3_config.get("endpoint_url")
        item["path_style"] = s3_config.get("path_style")
        item["verify_tls"] = s3_config.get("verify_tls")
        item["description"] = s3_config.get("description")
        item["path"] = s3_config.get("location")
        item["location"] = item.get("path")
    else:
        item["location"] = item.get("path") or metadata.get("address")
    mask_credential_fields(metadata)
    item["metadata"] = metadata
    repository_id = str(item.get("id") or "")
    catalog = repository_catalog_summary(repository_id)
    storage: dict[str, Any] = {
        "capacity_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "available": False,
        "error": None,
        "permissions": None,
    }
    warnings: list[str] = []
    if repo_type == "s3":
        # S3 buckets are elastic — there is no fixed capacity/free figure to
        # probe, unlike a local filesystem. "Used" is real, though: it's the
        # sum of every restore point this repository's catalog already
        # tracks, so the repository page reflects actual occupied space
        # instead of sitting at zero forever.
        storage["available"] = True
        storage["used_bytes"] = catalog["used_bytes"]
    else:
        path_value = str(item.get("path") or "").strip()
        if path_value and repo_type == "local":
            permissions = _local_path_permissions(Path(path_value))
            storage["permissions"] = permissions
            warnings.extend(_local_repository_permission_warnings(Path(path_value), permissions))
        if path_value:
            try:
                usage = shutil.disk_usage(path_value)
                storage.update(
                    {
                        "capacity_bytes": int(usage.total),
                        "used_bytes": int(usage.used),
                        "free_bytes": int(usage.free),
                        "available": True,
                    }
                )
            except OSError as exc:
                storage["error"] = str(exc)
        else:
            storage["error"] = "Repository path is empty"
        if not storage["available"]:
            item["status"] = "unavailable"
            item["health"] = "unavailable"
    item["storage"] = storage
    item["warnings"] = warnings
    item["capacity_bytes"] = storage["capacity_bytes"]
    item["used_bytes"] = storage["used_bytes"]
    item["free_bytes"] = storage["free_bytes"]
    item["capabilities"] = {
        "supports_synthetic_full": repository_supports_synthetic(item),
    }
    item["catalog"] = catalog
    referenced_jobs = _jobs_referencing_repository_id(repository_id)
    item["referenced_jobs"] = referenced_jobs
    item["jobs_count"] = len(referenced_jobs)
    item["backup_chain_count"] = int(item["catalog"].get("backup_chains") or 0)
    item["restore_point_count"] = int(item["catalog"].get("restore_points") or 0)
    item["can_delete"] = not referenced_jobs and item["restore_point_count"] == 0
    item["delete_disabled_reason"] = (
        "Repository is referenced by backup jobs"
        if referenced_jobs
        else "Repository is referenced by backup records"
        if item["restore_point_count"] > 0
        else None
    )
    return item


def _split_repository_address(repo_type: str, address: str) -> tuple[str, str]:
    value = str(address or "").strip()
    _ensure(bool(value), 422, "invalid_repository", "Repository address is required")
    if repo_type == "nfs":
        _ensure(bool(NFS_PATTERN.fullmatch(value)), 422, "invalid_repository", "NFS repositories must use host:/export/path", address=value)
        server, share = value.split(":", 1)
        return server.strip(), share.strip()
    if repo_type == "smb":
        normalized = value.replace("\\", "/")
        _ensure(bool(SMB_PATTERN.fullmatch(normalized)), 422, "invalid_repository", "SMB repositories must use //host/share", address=value)
        server, share = normalized.lstrip("/").split("/", 1)
        return server.strip(), share.strip()
    raise api_error(422, "invalid_repository", "Only NFS and SMB repositories use remote addresses", repo_type=repo_type)


def _build_repository_path(repo_type: str, server: str, share: str) -> str:
    return f"{server}:{share}" if repo_type == "nfs" else f"//{server}/{share.lstrip('/')}"


def _coerce_bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _raw_s3_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("s3") if isinstance(payload.get("s3"), dict) else {}


def _normalize_s3_configuration(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    current = dict(existing or {})
    raw = _raw_s3_payload(payload)

    def _pick(name: str) -> Any:
        if name in raw:
            return raw.get(name)
        return payload.get(name)

    if "bucket" in raw or "bucket" in payload:
        current["bucket"] = str(_pick("bucket") or "").strip()
    else:
        current["bucket"] = str(current.get("bucket") or "").strip()

    if "prefix" in raw or "prefix" in payload:
        current["prefix"] = str(_pick("prefix") or "").strip().replace("\\", "/").strip("/")
    else:
        current["prefix"] = str(current.get("prefix") or "").strip().replace("\\", "/").strip("/")

    if "region" in raw or "region" in payload:
        current["region"] = str(_pick("region") or "").strip() or None
    else:
        current["region"] = str(current.get("region") or "").strip() or None

    if "endpoint_url" in raw or "endpoint_url" in payload:
        current["endpoint_url"] = str(_pick("endpoint_url") or "").strip() or None
    else:
        current["endpoint_url"] = str(current.get("endpoint_url") or "").strip() or None

    if "access_key" in raw or "access_key" in payload:
        current["access_key"] = str(_pick("access_key") or "").strip() or None
    else:
        current["access_key"] = str(current.get("access_key") or "").strip() or None

    if "secret_key" in raw or "secret_key" in payload:
        value = _pick("secret_key")
        current["secret_key"] = str(value) if value not in (None, "") else None
    else:
        current["secret_key"] = current.get("secret_key")

    if "profile" in raw or "profile" in payload:
        current["profile"] = str(_pick("profile") or "").strip() or None
    else:
        current["profile"] = str(current.get("profile") or "").strip() or None

    if "path_style" in raw or "path_style" in payload:
        current["path_style"] = _coerce_bool_value(_pick("path_style"), default=bool(current.get("endpoint_url")))
    else:
        current["path_style"] = _coerce_bool_value(current.get("path_style"), default=bool(current.get("endpoint_url")))

    if "verify_tls" in raw or "verify_tls" in payload:
        current["verify_tls"] = _coerce_bool_value(_pick("verify_tls"), default=True)
    else:
        current["verify_tls"] = _coerce_bool_value(current.get("verify_tls"), default=True)

    if "description" in raw or "description" in payload:
        current["description"] = str(_pick("description") or "").strip() or None
    else:
        current["description"] = str(current.get("description") or "").strip() or None

    return current


def _validate_s3_configuration(config: dict[str, Any]) -> None:
    for key, label, max_length, redact in (
        ("bucket", "S3 bucket", MAX_TEXT_LENGTH, False),
        ("prefix", "S3 prefix", MAX_LONG_TEXT_LENGTH, False),
        ("region", "S3 region", MAX_TEXT_LENGTH, False),
        ("endpoint_url", "S3 endpoint_url", MAX_LONG_TEXT_LENGTH, False),
        ("access_key", "S3 access_key", MAX_TEXT_LENGTH, True),
        ("secret_key", "S3 secret_key", MAX_TEXT_LENGTH, True),
        ("profile", "S3 profile", MAX_TEXT_LENGTH, False),
        ("description", "S3 description", MAX_LONG_TEXT_LENGTH, False),
    ):
        if config.get(key) is not None:
            _ensure_ascii_text(
                str(config[key]),
                field=label,
                code="invalid_repository",
                max_length=max_length,
                redact=redact,
            )

    bucket = str(config.get("bucket") or "").strip()
    _ensure(bool(bucket), 422, "invalid_repository", "s3 repositories require bucket")

    region = str(config.get("region") or "").strip()
    _ensure(bool(region), 422, "invalid_repository", "s3 repositories require region")

    endpoint_url = str(config.get("endpoint_url") or "").strip()
    if endpoint_url:
        parsed = urlparse(endpoint_url)
        _ensure(
            bool(parsed.scheme in {"http", "https"} and parsed.netloc),
            422,
            "invalid_repository",
            "S3 endpoint_url must be a valid http(s) URL",
        )

    access_key = str(config.get("access_key") or "").strip()
    secret_key = str(config.get("secret_key") or "")
    if bool(access_key) != bool(secret_key):
        raise api_error(
            422,
            "invalid_repository",
            "S3 access_key and secret_key must either both be provided or both be omitted",
        )


def _build_s3_repository_path(config: dict[str, Any]) -> str:
    return build_s3_location(str(config.get("bucket") or "").strip(), config.get("prefix"))


def _service_account_name() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (AttributeError, ImportError, KeyError, OSError):
        return str(os.geteuid()) if hasattr(os, "geteuid") else "unknown"


def _local_path_permissions(path: Path) -> dict[str, Any] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    owner: Any = info.st_uid
    group: Any = info.st_gid
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(info.st_uid).pw_name
        group = grp.getgrgid(info.st_gid).gr_name
    except (ImportError, KeyError, OSError):
        pass
    mode = stat.S_IMODE(info.st_mode)
    parent_world_writable = False
    if path.parent != path:
        try:
            parent_world_writable = bool(stat.S_IMODE(path.parent.stat().st_mode) & stat.S_IWOTH)
        except OSError:
            parent_world_writable = False
    return {
        "mode": f"{mode:04o}",
        "owner": str(owner),
        "group": str(group),
        "world_writable": bool(mode & stat.S_IWOTH),
        "sticky": bool(mode & stat.S_ISVTX),
        "parent_world_writable": parent_world_writable,
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
    }


def _world_writable_repository_message(path: Path, permissions: dict[str, Any]) -> str:
    exposure = (
        "any local account can add files to it and fill it (the sticky bit only blocks deletion)"
        if permissions["sticky"]
        else "any local account can read, replace or delete the backups in it"
    )
    return (
        f"Repository path {path} is world-writable (mode {permissions['mode']} "
        f"{permissions['owner']}:{permissions['group']}): {exposure}."
    )


def _tighten_mode_hint(path: Path) -> str:
    return f"Run: chmod {PRODUCT_STORAGE_MODE:o} {path}"


def _local_repository_permission_warnings(path: Path, permissions: dict[str, Any] | None) -> list[str]:
    if not permissions:
        return []
    warnings: list[str] = []
    if permissions["world_writable"]:
        warnings.append(f"{_world_writable_repository_message(path, permissions)} {_tighten_mode_hint(path)}")
    elif permissions["parent_world_writable"]:
        warnings.append(
            f"Repository path {path} has a world-writable parent ({path.parent}): any local account "
            f"can move the repository aside and replace it. Run: chmod o-w {path.parent}"
        )
    if not permissions["readable"] or not permissions["writable"]:
        missing = " or ".join(
            label
            for label, granted in (("readable", permissions["readable"]), ("writable", permissions["writable"]))
            if not granted
        )
        warnings.append(
            f"Repository path {path} is not {missing} by the service account "
            f"({_service_account_name()}): it is mode {permissions['mode']} "
            f"{permissions['owner']}:{permissions['group']}, so backups into it will fail."
        )
    return warnings


def _reject_insecure_local_repository_path(path: Path) -> None:
    permissions = _local_path_permissions(path)
    if permissions is None or not permissions["world_writable"]:
        return
    raise api_error(
        422,
        "insecure_repository_path",
        f"{_world_writable_repository_message(path, permissions)} {_tighten_mode_hint(path)}",
        path=str(path),
        mode=permissions["mode"],
        owner=permissions["owner"],
        group=permissions["group"],
    )


def _stat_operator_path(path: Path) -> tuple[bool, bool, str | None]:
    try:
        if not path.exists():
            return False, False, None
    except OSError as exc:
        return False, False, str(exc)
    try:
        return True, path.is_dir(), None
    except OSError as exc:
        return True, False, str(exc)


def list_repository_records() -> list[dict[str, Any]]:
    items = [normalize_repository_record(item) for item in catalog_records(REPOSITORIES)]
    items.sort(key=lambda entry: (str(entry.get("name") or "").lower(), str(entry.get("id") or "")))
    return items


def resolve_repository(name: str | None = None, record_id: str | None = None) -> dict[str, Any]:
    return normalize_repository_record(resolve_selector(catalog_records(REPOSITORIES), name=name, record_id=record_id, label="Repository"))


def create_repository_record(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    repo_type = str(payload.get("type") or "").strip().lower()
    path = str(payload.get("path") or "").strip() or None
    address = str(payload.get("address") or "").strip() or None
    server = str(payload.get("server") or "").strip() or None
    share = str(payload.get("share") or "").strip() or None
    username = str(payload.get("username") or "").strip() or None
    password = str(payload.get("password") or "") or None
    description = str(payload.get("description") or "").strip() or None
    _ensure(bool(name), 422, "invalid_repository", "Repository name is required")
    _ensure_ascii_name(name, field="Repository name", code="invalid_repository")
    _validate_text_fields(payload, REPOSITORY_TEXT_FIELDS, code="invalid_repository")
    _ensure(repo_type in REPO_TYPES, 422, "invalid_repository", "Repository type must be one of: local, nfs, smb, s3", repo_type=repo_type)
    s3_config: dict[str, Any] | None = None
    if repo_type == "local":
        if not path and address:
            path = address
        _ensure(bool(path), 422, "invalid_repository", "local repositories require --path")
        local_path = Path(path)
        exists, is_dir, access_error = _stat_operator_path(local_path)
        if access_error is not None:
            raise api_error(422, "invalid_repository", f"Path is not accessible: {local_path} ({access_error})", path=path)
        _ensure(exists, 422, "invalid_repository", f"Path does not exist: {local_path}", path=path)
        _ensure(is_dir, 422, "invalid_repository", f"Path is not a directory: {local_path}", path=path)
        _reject_insecure_local_repository_path(local_path)
    elif repo_type == "s3":
        s3_config = _normalize_s3_configuration(payload)
        if description and not s3_config.get("description"):
            s3_config["description"] = description
        _validate_s3_configuration(s3_config)
        path = _build_s3_repository_path(s3_config)
        description = str(s3_config.get("description") or "").strip() or None
    else:
        raw_address = address or path
        if raw_address and (not server or not share):
            parsed_server, parsed_share = _split_repository_address(repo_type, raw_address)
            server = server or parsed_server
            share = share or parsed_share
        _ensure(bool(server), 422, "invalid_repository", f"{repo_type} repositories require --server")
        _ensure(bool(share), 422, "invalid_repository", f"{repo_type} repositories require --share")
        path = _build_repository_path(repo_type, server, share)
        if repo_type == "smb":
            _ensure(bool(username and password), 422, "invalid_repository", "smb repositories require username and password")
    for existing in catalog_records(REPOSITORIES):
        if str(existing.get("name") or "").lower() == name.lower():
            raise api_error(409, "repository_name_exists", "Repository name already exists", name=name)
    if repo_type != "s3":
        conflict = _find_overlapping_repository(path)
        if conflict is not None:
            raise api_error(
                409,
                "repository_path_overlaps",
                "Repository path overlaps with an existing repository's storage root",
                conflicting_repository=conflict.get("name"),
                path=path,
            )
    now = utc_now()
    record = {
        "id": str(uuid4()),
        "name": name,
        "type": repo_type,
        "path": path,
        "status": "available",
        "created_at": now,
        "updated_at": now,
        "metadata": (
            {
                "description": description,
                "last_scan_at": None,
                "s3": {
                    "bucket": s3_config["bucket"],
                    "prefix": s3_config["prefix"],
                    "region": s3_config["region"],
                    "endpoint_url": s3_config["endpoint_url"],
                    "access_key": s3_config["access_key"],
                    "secret_key": s3_config["secret_key"],
                    "profile": s3_config["profile"],
                    "path_style": s3_config["path_style"],
                    "verify_tls": s3_config["verify_tls"],
                    "description": description,
                },
            }
            if repo_type == "s3"
            else {
                "server": server,
                "share": share,
                "username": username,
                "password": password,
                "description": description,
                "address": path,
                "last_scan_at": None,
            }
        ),
    }
    persist_repository(record)
    return normalize_repository_record(record)


def _reject_storage_location_change_if_repository_has_backups(
    record_id: str, *, original_path: str, new_path: str
) -> None:
    if new_path == original_path:
        return
    repo_points = [point for point in normalized_backup_points() if str(point.get("repository_id") or "") == record_id]
    if repo_points:
        raise api_error(
            409,
            "repository_has_backups",
            "Repository storage location cannot be changed while backup chains still reference it",
            restore_point_count=len(repo_points),
        )


def edit_repository_record(record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    original_path = str(record.get("path") or "")
    _validate_text_fields(payload, REPOSITORY_TEXT_FIELDS, code="invalid_repository")
    updated = _json_clone(record)
    changed = False
    if "name" in payload and payload["name"] is not None:
        new_name = str(payload["name"]).strip()
        _ensure(bool(new_name), 422, "invalid_repository", "Repository name must not be empty")
        _ensure_ascii_name(new_name, field="Repository name", code="invalid_repository")
        for existing_id, existing in catalog_entries(REPOSITORIES):
            if existing_id == record_id:
                continue
            if str(existing.get("name") or "").lower() == new_name.lower():
                raise api_error(409, "repository_name_exists", "Repository name already exists", name=new_name)
        updated["name"] = new_name
        changed = True
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    repo_type = str(updated.get("type") or "")
    if repo_type == "local":
        local_path = None
        if "path" in payload and payload["path"] is not None:
            local_path = str(payload["path"]).strip()
        elif "address" in payload and payload["address"] is not None:
            local_path = str(payload["address"]).strip()
        if local_path is not None:
            updated["path"] = local_path
            changed = True
        _ensure(bool(str(updated.get("path") or "").strip()), 422, "invalid_repository", "local repositories require --path")
        if local_path is not None and local_path != original_path:
            _reject_insecure_local_repository_path(Path(local_path))
    elif repo_type == "s3":
        current_s3 = extract_s3_config(updated)
        next_s3 = _normalize_s3_configuration(payload, existing=current_s3)
        _validate_s3_configuration(next_s3)
        description = str(next_s3.get("description") or "").strip() or None
        next_metadata = dict(metadata)
        next_metadata["description"] = description
        next_metadata["s3"] = {
            "bucket": next_s3.get("bucket"),
            "prefix": next_s3.get("prefix"),
            "region": next_s3.get("region"),
            "endpoint_url": next_s3.get("endpoint_url"),
            "access_key": next_s3.get("access_key"),
            "secret_key": next_s3.get("secret_key"),
            "profile": next_s3.get("profile"),
            "path_style": next_s3.get("path_style"),
            "verify_tls": next_s3.get("verify_tls"),
            "description": description,
        }
        next_path = _build_s3_repository_path(next_s3)
        _reject_storage_location_change_if_repository_has_backups(
            record_id, original_path=original_path, new_path=next_path
        )
        if next_path != updated.get("path") or next_metadata != metadata:
            changed = True
        updated["path"] = next_path
        updated["metadata"] = next_metadata
    else:
        parsed_address = None
        if "address" in payload and payload["address"] is not None:
            parsed_address = str(payload["address"]).strip()
        elif "path" in payload and payload["path"] is not None and "server" not in payload and "share" not in payload:
            parsed_address = str(payload["path"]).strip()
        if parsed_address is not None:
            metadata["server"], metadata["share"] = _split_repository_address(repo_type, parsed_address)
            changed = True
        if "server" in payload and payload["server"] is not None:
            metadata["server"] = str(payload["server"]).strip()
            changed = True
        if "share" in payload and payload["share"] is not None:
            metadata["share"] = str(payload["share"]).strip()
            changed = True
        server = str(metadata.get("server") or "").strip()
        share = str(metadata.get("share") or "").strip()
        _ensure(bool(server), 422, "invalid_repository", f"{repo_type} repositories require --server")
        _ensure(bool(share), 422, "invalid_repository", f"{repo_type} repositories require --share")
        updated["path"] = _build_repository_path(repo_type, server, share)
    for field_name in ("username", "password", "description"):
        if repo_type == "s3":
            continue
        if field_name in payload and payload[field_name] is not None:
            value = str(payload[field_name]).strip() if field_name != "password" else str(payload[field_name])
            metadata[field_name] = value or None
            changed = True
    if repo_type != "s3":
        metadata["address"] = updated.get("path")
        updated["metadata"] = metadata
        new_path = str(updated.get("path") or "")

        if new_path != original_path:
            _reject_storage_location_change_if_repository_has_backups(
                record_id, original_path=original_path, new_path=new_path
            )
            conflict = _find_overlapping_repository(new_path, exclude_id=record_id)
            if conflict is not None:
                raise api_error(
                    409,
                    "repository_path_overlaps",
                    "Repository path overlaps with an existing repository's storage root",
                    conflicting_repository=conflict.get("name"),
                    path=updated.get("path"),
                )
    _ensure(changed, 422, "invalid_repository", "repo edit requires at least one editable option")
    updated["updated_at"] = utc_now()
    persist_repository(updated)
    return normalize_repository_record(updated)


def _repository_storage_location_mismatch(repository: dict[str, Any]) -> str | None:
    repository_id = str(repository.get("id") or "")
    current_root_raw = str(repository.get("path") or "").strip()
    if not current_root_raw:
        return None
    current_root = Path(current_root_raw).resolve()
    mismatched: set[str] = set()
    for point in _repository_latest_points(repository_id):
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        for disk in artifact.get("disks", []):
            if not isinstance(disk, dict):
                continue
            abr_path = str(disk.get("abr_path") or "").strip()
            if not abr_path or not Path(abr_path).is_absolute():
                continue
            resolved = Path(abr_path).resolve()
            if resolved != current_root and current_root not in resolved.parents:
                mismatched.add(str(resolved))
    if not mismatched:
        return None
    example = min(mismatched)
    return (
        f"Repository path {current_root} does not match the location recorded for its existing "
        f"backups (e.g. {example}) -- it may have been repointed without moving the data"
    )


def _verify_local_repository(repository: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(repository.get("path") or ""))
    checks: list[dict[str, Any]] = []
    exists, is_dir, access_error = _stat_operator_path(path)
    if access_error is not None:
        return {"status": "error", "checks": [{"name": "exists", "status": "failed"}], "message": f"Path is not accessible: {path} ({access_error})"}
    if not exists:
        return {"status": "error", "checks": [{"name": "exists", "status": "failed"}], "message": f"Path does not exist: {path}"}
    checks.append({"name": "exists", "status": "ok"})
    if not is_dir:
        return {"status": "error", "checks": [*checks, {"name": "is_directory", "status": "failed"}], "message": f"Path is not a directory: {path}"}
    checks.append({"name": "is_directory", "status": "ok"})
    readable = os.access(path, os.R_OK)
    writable = os.access(path, os.W_OK)
    checks.append({"name": "readable", "status": "ok" if readable else "failed"})
    checks.append({"name": "writable", "status": "ok" if writable else "failed"})
    if not readable or not writable:
        return {"status": "error", "checks": checks, "message": f"Path is not readable and writable: {path}"}
    permissions = _local_path_permissions(path)
    permission_warnings = _local_repository_permission_warnings(path, permissions)
    checks.append(
        {
            "name": "permissions",
            "status": "warning" if permission_warnings else "ok",
            "message": " ".join(permission_warnings)
            or (
                f"mode {permissions['mode']} {permissions['owner']}:{permissions['group']}"
                if permissions
                else "Path mode could not be read"
            ),
        }
    )
    try:
        with NamedTemporaryFile(dir=path, prefix=".abr-probe-", delete=True) as handle:
            handle.write(b"abr probe\n")
            handle.flush()
        checks.append({"name": "filesystem_probe", "status": "ok"})
    except OSError as exc:
        checks.append({"name": "filesystem_probe", "status": "failed", "message": str(exc)})
        return {"status": "error", "checks": checks, "message": f"Filesystem probe failed: {exc}"}

    location_mismatch = _repository_storage_location_mismatch(repository)
    checks.append(
        {
            "name": "storage_location_consistency",
            "status": "failed" if location_mismatch else "ok",
            **({"message": location_mismatch} if location_mismatch else {}),
        }
    )
    if location_mismatch:
        return {"status": "error", "checks": checks, "message": location_mismatch}

    backup_health = _verify_repository_backup_health(str(repository.get("id") or ""))
    checks.append(
        {
            "name": "block_consistency",
            "status": backup_health["status"],
            "message": backup_health["message"],
            **({"errors": backup_health["errors"]} if backup_health["errors"] else {}),
        }
    )
    if backup_health["status"] == "failed":
        return {"status": "error", "checks": checks, "message": f"Backup integrity check failed: {backup_health['message']}"}
    
    message = "Repository is available"
    if permission_warnings:
        message = f"{message}. Warning: {' '.join(permission_warnings)}"
    return {"status": "success", "checks": checks, "message": message}


def _verify_remote_repository(repository: dict[str, Any]) -> dict[str, Any]:
    repo_type = str(repository.get("type") or "")
    metadata = repository.get("metadata") if isinstance(repository.get("metadata"), dict) else {}
    server = str(metadata.get("server") or "").strip()
    share = str(metadata.get("share") or "").strip()
    username = str(metadata.get("username") or "").strip()
    password = str(metadata.get("password") or "").strip()
    checks = []
    if repo_type == "nfs":
        path = str(repository.get("path") or "")
        if not NFS_PATTERN.fullmatch(path):
            return {"status": "error", "checks": [{"name": "format", "status": "failed"}], "message": "NFS repositories must use host:/export/path"}
        checks.append({"name": "format", "status": "ok"})
    else:
        path = str(repository.get("path") or "")
        if not SMB_PATTERN.fullmatch(path):
            return {"status": "error", "checks": [{"name": "format", "status": "failed"}], "message": "SMB repositories must use //host/share"}
        checks.append({"name": "format", "status": "ok"})
        if not username or not password:
            return {"status": "error", "checks": [*checks, {"name": "credentials", "status": "failed"}], "message": "SMB repositories require credentials"}
        checks.append({"name": "credentials", "status": "ok"})
    _ensure(bool(server and share), 422, "invalid_repository", "Remote repository metadata is incomplete", repository_id=repository.get("id"))
    try:
        socket.getaddrinfo(server, None)
        checks.append({"name": "dns", "status": "ok"})
    except OSError as exc:
        checks.append({"name": "dns", "status": "failed", "message": str(exc)})
        return {"status": "error", "checks": checks, "message": f"Unable to resolve repository host: {server}"}
    checks.append({"name": "block_consistency", "status": "skipped", "message": "Remote block consistency is not available in mock mode"})
    return {"status": "success", "checks": checks, "message": "Repository address and connectivity checks passed"}


def _s3_connectivity_checks(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Format checks plus a real storage-tool probe against an S3 config —
    shared by the post-create `/verify` endpoint and the pre-create connection
    test, so both report the exact same real checks. Returns the accumulated
    `checks` list and, if a check failed, the error message to report (checks
    already reflects the failure in that case)."""
    checks: list[dict[str, Any]] = []
    bucket = str(config.get("bucket") or "").strip()
    prefix = str(config.get("prefix") or "").strip()
    region = str(config.get("region") or "").strip()
    endpoint_url = str(config.get("endpoint_url") or "").strip()

    if not bucket:
        return [{"name": "bucket", "status": "failed"}], "S3 bucket is missing"
    checks.append({"name": "bucket", "status": "ok", "message": bucket})

    if not region:
        checks.append({"name": "region", "status": "failed"})
        return checks, "S3 region is missing"
    checks.append({"name": "region", "status": "ok", "message": region})

    if endpoint_url:
        parsed = urlparse(endpoint_url)
        if not (parsed.scheme in {"http", "https"} and parsed.netloc):
            checks.append({"name": "endpoint_url", "status": "failed"})
            return checks, "S3 endpoint URL is invalid"
        checks.append({"name": "endpoint_url", "status": "ok", "message": endpoint_url})

        unreachable = s3_endpoint_connect_error(endpoint_url)
        if unreachable is not None:
            checks.append({"name": "endpoint_reachable", "status": "failed", "message": unreachable})
            return checks, f"S3 endpoint is not reachable: {unreachable}"
        checks.append({"name": "endpoint_reachable", "status": "ok", "message": endpoint_url})

    try:
        probe_repository_access(
            {"type": "s3", "s3": config},
            abr_path=prefix,
            backup_name=f"repo-probe-{uuid4().hex[:12]}",
            timeout=120,
        )
    except StorageToolError as exc:
        checks.append({"name": "connectivity", "status": "failed", "message": str(exc)})
        return checks, "S3 repository validation failed"

    checks.append({"name": "connectivity", "status": "ok", "message": "Bucket/prefix access validated (bucket auto-created if missing) through native storage tool"})
    return checks, None


def _verify_s3_repository(repository: dict[str, Any]) -> dict[str, Any]:
    config = extract_s3_config(repository)
    checks, error = _s3_connectivity_checks(config)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}

    backup_health = _verify_repository_backup_health(str(repository.get("id") or ""))
    checks.append(
        {
            "name": "block_consistency",
            "status": backup_health["status"],
            "message": backup_health["message"],
            **({"errors": backup_health["errors"]} if backup_health["errors"] else {}),
        }
    )
    if backup_health["status"] == "failed":
        return {"status": "error", "checks": checks, "message": f"Backup integrity check failed: {backup_health['message']}"}

    return {"status": "success", "checks": checks, "message": "S3 repository is available"}


def test_s3_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Real pre-create S3 connectivity test (repository creation's "Test
    connection"): validates the given S3 settings and probes them through the
    native storage tool exactly like `/repositories/{id}/verify` does, but
    without persisting a repository record — there isn't one yet."""
    config = _normalize_s3_configuration(payload)
    _validate_s3_configuration(config)
    checks, error = _s3_connectivity_checks(config)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}
    return {"status": "success", "checks": checks, "message": "S3 connection succeeded"}


def verify_repository(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    repo_type = str(repository.get("type") or "")
    if repo_type == "local":
        result = _verify_local_repository(repository)
    elif repo_type == "s3":
        result = _verify_s3_repository(repository)
    else:
        result = _verify_remote_repository(repository)
    payload = normalize_repository_record(repository)
    payload.update(result)
    return payload


def rescan_repository(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    updated = _json_clone(repository)
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    metadata["last_scan_at"] = utc_now()
    metadata["catalog"] = repository_catalog_summary(record_id)
    updated["metadata"] = metadata
    updated["updated_at"] = metadata["last_scan_at"]
    persist_repository(updated)
    payload = normalize_repository_record(updated)
    return {
        "id": payload["id"],
        "name": payload["name"],
        "status": payload["status"],
        "last_scan_at": payload["last_scan_at"],
        "catalog": payload["catalog"],
    }


def remove_repository_record(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    linked_jobs = _jobs_referencing_repository_id(record_id)
    if linked_jobs:
        raise api_error(409, "repository_in_use", "Repository cannot be removed while jobs still reference it", jobs=linked_jobs)
    repo_points = [point for point in normalized_backup_points() if str(point.get("repository_id") or "") == record_id]
    if repo_points:
        raise api_error(409, "repository_has_backups", "Repository cannot be removed while backup chains still reference it", restore_point_count=len(repo_points))
    delete_repository(record_id)
    return {"id": str(repository.get("id") or record_id), "removed": True}


def _resolve_inventory_vm_records(inventory_id: str) -> list[dict[str, Any]]:
    items = []
    for resource in RESOURCES_BY_HV.get(inventory_id, {}).values():
        if str(resource.get("resource_type") or "") != "virtual_machine":
            continue
        items.append(resource)
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("external_id") or "")))
    return items


def _resolve_vm_selection(
    inventory_id: str,
    vm_tokens: list[str],
    *,
    auto_discover: bool = False,
) -> tuple[list[str], list[str]]:
    vms = _resolve_inventory_vm_records(inventory_id)
    if not vms and auto_discover:
        # Lazily discover VMs the first time names need resolving so callers do
        # not have to run an explicit `inventory rescan` between `inventory add`
        # and `job create`. rescan_inventory is idempotent (it replaces the
        # discovered resource set, so no duplicate entries) and raises a
        # meaningful 502 `inventory_rescan_failed` if discovery itself fails,
        # which surfaces a real error instead of the misleading
        # "no discovered VMs" message below. discover_inventory_vms is
        # serialised per-inventory, so it coalesces with any in-flight
        # background scan from `inventory add` rather than scanning twice.
        vms = discover_inventory_vms(inventory_id)
    if not vms:
        if auto_discover:
            message = "Inventory discovery returned no virtual machines."
        else:
            message = (
                "Inventory has no discovered VMs. After inventory add, run "
                "inventory rescan before using VM names in job create."
            )
        raise api_error(
            409,
            "inventory_not_scanned",
            message,
            inventory_id=inventory_id,
        )
    vm_ids = []
    vm_names = []
    for token in vm_tokens:
        lowered = token.lower()
        matches = [
            item
            for item in vms
            if str(item.get("external_id") or "") == token
            or str(item.get("name") or "").lower() == lowered
            or (
                isinstance(item.get("payload"), dict)
                and lowered in {
                    str(item["payload"].get(key) or "").lower()
                    for key in ("path", "inventory_path", "vm_path", "vmx_path")
                    if str(item["payload"].get(key) or "").strip()
                }
            )
        ]
        if not matches:
            raise api_error(404, "vm_not_found", "VM not found in inventory", inventory_id=inventory_id, vm=token)
        if len(matches) > 1:
            raise api_error(409, "ambiguous_vm", "Multiple VMs matched the provided selector", inventory_id=inventory_id, vm=token)
        vm_ids.append(str(matches[0].get("external_id")))
        vm_names.append(str(matches[0].get("name") or matches[0].get("external_id")))
    return vm_ids, vm_names


def normalize_job_record(record: dict[str, Any]) -> dict[str, Any]:
    item = _json_clone(record)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    item["name"] = str(item.get("name") or metadata.get("name") or item.get("id") or "")
    raw_job_type = str(item.get("job_type") or "backup")
    if raw_job_type == "backup" and (metadata.get("copy_job") or item.get("source_job_id") or metadata.get("source_job_id")):
        raw_job_type = "backup-copy"
    item["job_type"] = raw_job_type if raw_job_type in JOB_TYPES else "backup"
    item["inventory_id"] = str(item.get("inventory_id") or item.get("hypervisor_id") or metadata.get("inventory_id") or item.get("source_inventory_id") or "")
    item["inventory_name"] = str(item.get("inventory_name") or metadata.get("inventory_name") or "")
    item["vm_ids"] = [
        str(value)
        for value in (
            item.get("vm_ids")
            or metadata.get("vm_ids")
            or metadata.get("vm_external_ids")
            or ([metadata.get("vm_external_id")] if metadata.get("vm_external_id") else [])
        )
        if str(value or "").strip()
    ]
    item["vm_names"] = [
        str(value)
        for value in (
            item.get("vm_names")
            or metadata.get("vm_names")
            or metadata.get("vm")
            or ([metadata.get("vm_name")] if metadata.get("vm_name") else [])
        )
        if str(value or "").strip()
    ]
    item["repo_id"] = str(item.get("repo_id") or metadata.get("repo_id") or metadata.get("repository_id") or (_split_csv(str(metadata.get("repository_ids") or ""))[0] if isinstance(metadata.get("repository_ids"), str) else (metadata.get("repository_ids") or [None])[0]) or "")
    item["repo_name"] = str(item.get("repo_name") or metadata.get("repo_name") or metadata.get("repository_name") or (_split_csv(str(metadata.get("repo") or ""))[0] if isinstance(metadata.get("repo"), str) else (metadata.get("repo") or [None])[0]) or "")
    item["source_job_id"] = str(item.get("source_job_id") or metadata.get("source_job_id") or "")
    item["source_job_name"] = str(item.get("source_job_name") or metadata.get("source_job_name") or "")
    raw_schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else None
    if raw_schedule is None and isinstance(metadata.get("schedule"), dict):
        raw_schedule = metadata.get("schedule")
    item["schedule"] = normalize_schedule_payload(raw_schedule)
    item["schedule_summary"] = item.get("schedule_summary") or _render_schedule_summary(item["schedule"])
    item["retention_days"] = int(item.get("retention_days") or metadata.get("retention_days") or 14)
    gfs = item.get("gfs") if isinstance(item.get("gfs"), dict) else {}
    if not gfs and isinstance(item.get("gfs_policy"), dict):
        gfs = item["gfs_policy"]
    item["gfs"] = {
        "weekly": int(gfs.get("weekly") or 0),
        "monthly": int(gfs.get("monthly") or 0),
        "yearly": int(gfs.get("yearly") or 0),
        "enabled": _coerce_gfs_enabled(gfs.get("enabled")),
    }
    item["active_full_days"] = _normalize_day_list(item.get("active_full_days") or metadata.get("active_full_days") or ())
    item["synthetic_full_days"] = _normalize_day_list(item.get("synthetic_full_days") or metadata.get("synthetic_full_days") or ())
    item["description"] = str(item.get("description") or metadata.get("description") or "")
    task_parameters = item.get("task_parameters")
    if not isinstance(task_parameters, dict):
        task_parameters = metadata.get("task_parameters") if isinstance(metadata.get("task_parameters"), dict) else {}
    item["task_parameters"] = dict(task_parameters)
    item["enabled"] = bool(item["schedule"] and item["schedule"].get("enabled", True))
    item["schedule_state"] = "enabled" if item["schedule"] and item["enabled"] else "disabled" if item["schedule"] else "manual"
    item["vm_count"] = len(item["vm_ids"]) or len(item["vm_names"])
    if item["schedule"] is None:
        item["status"] = str(item.get("status") or "planned")
    elif item["enabled"]:
        item["status"] = "active"
    else:
        item["status"] = "disabled"
    item["created_at"] = str(item.get("created_at") or utc_now())
    item["updated_at"] = str(item.get("updated_at") or item["created_at"])
    item["next_run_at"] = item.get("next_run_at")
    item["last_run_at"] = item.get("last_run_at") or item.get("last_scheduled_at")
    item["last_session_id"] = item.get("last_session_id") or item.get("last_task_id")
    return item


def normalized_job_records() -> list[dict[str, Any]]:
    items = [normalize_job_record(record) for record in catalog_records(JOBS)]
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    return items


def _persist_canonical_job(job: dict[str, Any]) -> dict[str, Any]:
    canonical = normalize_job_record(job)
    persist_job(canonical)
    return canonical


def resolve_job(name: str | None = None, record_id: str | None = None) -> dict[str, Any]:
    return normalize_job_record(resolve_selector(normalized_job_records(), name=name, record_id=record_id, label="Job"))


def _validate_gfs_counts(gfs: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key in ("weekly", "monthly", "yearly"):
        try:
            result[key] = int(gfs.get(key) or 0)
        except (TypeError, ValueError) as exc:
            raise api_error(422, "invalid_job", "GFS counts must be integers", field=key) from exc
        _ensure(result[key] >= 0, 422, "invalid_job", "GFS counts must be >= 0", field=key)
    return result


def _coerce_gfs_enabled(value: Any, *, default: bool = True) -> bool:
    """`enabled` suspends GFS protection without discarding the configured counts."""
    return default if value is None else bool(value)


def _validate_task_parameters(params: dict[str, Any]) -> dict[str, Any]:
    result = dict(params)
    if "compression_level" in result and result["compression_level"] is not None:
        try:
            level = int(result["compression_level"])
        except (TypeError, ValueError) as exc:
            raise api_error(422, "invalid_job", "task_parameters.compression_level must be an integer between 0 and 9") from exc
        _ensure(0 <= level <= 9, 422, "invalid_job", "task_parameters.compression_level must be an integer between 0 and 9")
        result["compression_level"] = level

    if result.get("encryption_enabled"):
        key = result.get("encryption_key")
        if isinstance(key, str) and key.strip():
            result["encryption_key"] = key.strip()
        else:
            # Encryption is a switch, not a key-management exercise: a caller
            # that just turns it on (the web UI has no passphrase field) gets a
            # key generated here and stored on the job, which is already where
            # both the backup task parameters and restore look for one
            # (_resolve_restore_encryption_key). An explicit key still wins, so
            # `abr job create --set-encryption --encryption-key ...` is
            # unchanged. On an update the merge carries the stored key through
            # ahead of this, so a key is only ever minted once per job -- never
            # rotated out from under the restore points written with it.
            result["encryption_key"] = secrets.token_urlsafe(_GENERATED_ENCRYPTION_KEY_BYTES)
    return result


def _merge_task_parameters(current: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current or {})
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def job_effective_policy(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "first_run": {"type": "ActiveFull", "reason": "FirstRun"},
        "manual_force_full": {"type": "ActiveFull", "reason": "ForceFull"},
        "active_full_days": list(job.get("active_full_days") or []),
        "synthetic_full_days": list(job.get("synthetic_full_days") or []),
        "active_overrides_synthetic": True,
        "synthetic_requires_repository_support": True,
        "default": {"type": "Incremental", "reason": "StandardIncremental"},
    }


def job_to_dto(job: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_job_record(job)
    active_sessions = _active_sessions_for_job(str(normalized["id"]))
    task_parameters = normalized.get("task_parameters") if isinstance(normalized.get("task_parameters"), dict) else {}
    compression_level = task_parameters.get("compression_level")
    if compression_level is None:
        compression_level = DEFAULT_COMPRESSION_LEVEL
    encryption_enabled = bool(task_parameters.get("encryption_enabled"))
    has_encryption_key = bool(str(task_parameters.get("encryption_key") or "").strip())
    task_parameters_view = mask_credential_fields(dict(task_parameters))
    return {
        "id": normalized["id"],
        "name": normalized["name"],
        "job_type": normalized["job_type"],
        "description": normalized.get("description") or None,
        "inventory": {
            "id": normalized.get("inventory_id") or None,
            "name": normalized.get("inventory_name") or None,
        },
        "vm_ids": list(normalized.get("vm_ids") or []),
        "vm_names": list(normalized.get("vm_names") or []),
        "vm_count": int(normalized.get("vm_count") or 0),
        "vms": [
            {"id": vm_id or None, "name": vm_name or None}
            for vm_id, vm_name in zip(normalized.get("vm_ids") or [], normalized.get("vm_names") or [])
        ],
        "repository": {
            "id": normalized.get("repo_id") or None,
            "name": normalized.get("repo_name") or None,
        },
        "source_job": {
            "id": normalized.get("source_job_id") or None,
            "name": normalized.get("source_job_name") or None,
        } if normalized["job_type"] == "backup-copy" else None,
        "schedule": _schedule_dto(normalized.get("schedule")),
        "schedule_summary": normalized.get("schedule_summary"),
        "enabled": bool(normalized.get("enabled")),
        "schedule_state": normalized.get("schedule_state"),
        "retention_days": normalized.get("retention_days"),
        "gfs": normalized.get("gfs"),
        "active_full_days": normalized.get("active_full_days"),
        "synthetic_full_days": normalized.get("synthetic_full_days"),
        "compression_level": compression_level,
        "encryption": {"enabled": encryption_enabled, "key_set": has_encryption_key},
        "task_parameters": task_parameters_view,
        "backup_options": task_parameters_view,
        "effective_execution_policy": job_effective_policy(normalized),
        "status": normalized.get("status"),
        "created_at": normalized.get("created_at"),
        "updated_at": normalized.get("updated_at"),
        "next_run_at": normalized.get("next_run_at"),
        "last_run_at": normalized.get("last_run_at"),
        "last_session_id": normalized.get("last_session_id"),
        "active_sessions": active_sessions,
        "active_session_count": len(active_sessions),
        "can_delete": not active_sessions,
        "delete_disabled_reason": "Job has queued or running sessions" if active_sessions else None,
    }


def list_jobs() -> list[dict[str, Any]]:
    return [job_to_dto(job) for job in normalized_job_records()]


def create_backup_job(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    vm_tokens = payload.get("vms")
    repo_name = payload.get("repo")
    repo_id = payload.get("repo_id")
    inventory_name = payload.get("inventory")
    inventory_id = payload.get("inventory_id")
    raw_schedule = payload.get("schedule")
    schedule = normalize_schedule_payload(raw_schedule) if raw_schedule not in (None, "") else None
    raw_retention_days = payload.get("retention_days")
    retention_days = int(raw_retention_days) if raw_retention_days is not None else 14
    raw_gfs = payload.get("gfs")
    gfs = _validate_gfs_counts(raw_gfs if isinstance(raw_gfs, dict) else payload)
    gfs["enabled"] = _coerce_gfs_enabled(raw_gfs.get("enabled") if isinstance(raw_gfs, dict) else None)
    active_full_days = _normalize_day_list(payload.get("active_full_days") or _split_csv(payload.get("active_full_days_csv")))
    synthetic_full_days = _normalize_day_list(payload.get("synthetic_full_days") or _split_csv(payload.get("synthetic_full_days_csv")))
    description = str(payload.get("description") or "").strip()
    task_parameters = payload.get("task_parameters") if isinstance(payload.get("task_parameters"), dict) else payload.get("backup_options")
    if task_parameters is None:
        task_parameters = {}
    _ensure(isinstance(task_parameters, dict), 422, "invalid_job", "task_parameters must be an object")
    task_parameters = _validate_task_parameters(task_parameters)
    _ensure(bool(name), 422, "invalid_job", "Job name is required")
    _ensure_ascii_name(name, field="Job name", code="invalid_job")
    _ensure_ascii_text(description, field="Job description", code="invalid_job", max_length=MAX_LONG_TEXT_LENGTH)
    _ensure(retention_days > 0, 422, "invalid_job", "retention-days must be greater than 0")
    _ensure(bool(vm_tokens), 422, "invalid_job", "Backup jobs require at least one VM")
    _ensure(not any(str(existing.get("name") or "").lower() == name.lower() for existing in catalog_records(JOBS)), 409, "job_name_exists", "Job name already exists", name=name)
    repository = resolve_repository(name=repo_name, record_id=repo_id)
    tokens = vm_tokens if isinstance(vm_tokens, list) else _split_csv(str(vm_tokens))
    if inventory_name or inventory_id:
        inventory = resolve_inventory(name=inventory_name, record_id=inventory_id)
    else:
        candidate_inventories = []
        for item in list_inventory_records():
            try:
                _resolve_vm_selection(str(item["id"]), tokens)
                candidate_inventories.append(item)
            except ApiError:
                continue
        _ensure(
            len(candidate_inventories) == 1,
            422 if not candidate_inventories else 409,
            "inventory_required" if not candidate_inventories else "ambiguous_inventory",
            "Job create requires --inventory/--inventory-id unless VM names resolve uniquely to one inventory",
            vm_tokens=tokens,
        )
        inventory = candidate_inventories[0]
    vm_ids, vm_names = _resolve_vm_selection(str(inventory["id"]), tokens, auto_discover=True)
    now = utc_now()
    job = {
        "id": str(uuid4()),
        "name": name,
        "job_type": "backup",
        "inventory_id": inventory["id"],
        "inventory_name": inventory["name"],
        "vm_ids": vm_ids,
        "vm_names": vm_names,
        "repo_id": repository["id"],
        "repo_name": repository["name"],
        "schedule": schedule,
        "retention_days": retention_days,
        "gfs": gfs,
        "active_full_days": active_full_days,
        "synthetic_full_days": synthetic_full_days,
        "description": description,
        "task_parameters": dict(task_parameters),
        "status": "active" if schedule and bool(schedule.get("enabled", True)) else "disabled" if schedule else "planned",
        "created_at": now,
        "updated_at": now,
        "next_run_at": schedule_next_run(schedule, after=_parse_dt(now)).isoformat() if schedule and bool(schedule.get("enabled", True)) else None,
        "last_run_at": None,
        "last_session_id": None,
    }
    return job_to_dto(_persist_canonical_job(job))


def create_copy_job(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    source_job = resolve_job(
        name=payload.get("source") or payload.get("source_job"),
        record_id=payload.get("source_id") or payload.get("source_job_id"),
    )
    repository_selector_name = payload.get("repo") or payload.get("repository")
    repository_selector_id = payload.get("repo_id") or payload.get("repository_id")
    repository = resolve_repository(name=repository_selector_name, record_id=repository_selector_id) if (repository_selector_name or repository_selector_id) else None
    raw_schedule = payload.get("schedule")
    schedule = normalize_schedule_payload(raw_schedule) if raw_schedule not in (None, "") else _json_clone(source_job.get("schedule"))
    enabled_override = payload.get("enabled")
    if isinstance(schedule, dict) and enabled_override is not None:
        schedule["enabled"] = bool(enabled_override)
    raw_retention_days = payload.get("retention_days")
    if raw_retention_days is None:
        raw_retention_days = source_job.get("retention_days")
    retention_days = int(raw_retention_days) if raw_retention_days is not None else 14
    description = str(payload.get("description") or source_job.get("description") or "").strip()
    _ensure(bool(name), 422, "invalid_job", "Job name is required")
    _ensure_ascii_name(name, field="Job name", code="invalid_job")
    _ensure_ascii_text(description, field="Job description", code="invalid_job", max_length=MAX_LONG_TEXT_LENGTH)
    _ensure(retention_days > 0, 422, "invalid_job", "retention-days must be greater than 0")
    _ensure(not any(str(existing.get("name") or "").lower() == name.lower() for existing in catalog_records(JOBS)), 409, "job_name_exists", "Job name already exists", name=name)
    source_gfs = source_job.get("gfs") if isinstance(source_job.get("gfs"), dict) else {"weekly": 0, "monthly": 0, "yearly": 0, "enabled": True}
    source_task_parameters = source_job.get("task_parameters") if isinstance(source_job.get("task_parameters"), dict) else {}
    now = utc_now()
    job = {
        "id": str(uuid4()),
        "name": name,
        "job_type": source_job["job_type"],
        "inventory_id": source_job["inventory_id"],
        "inventory_name": source_job["inventory_name"],
        "vm_ids": list(source_job.get("vm_ids") or []),
        "vm_names": list(source_job.get("vm_names") or []),
        "source_job_id": source_job.get("source_job_id") or (source_job["id"] if source_job["job_type"] == "backup-copy" else ""),
        "source_job_name": source_job.get("source_job_name") or (source_job["name"] if source_job["job_type"] == "backup-copy" else ""),
        "repo_id": repository["id"] if repository is not None else source_job["repo_id"],
        "repo_name": repository["name"] if repository is not None else source_job["repo_name"],
        "schedule": schedule,
        "retention_days": retention_days,
        "gfs": {
            "weekly": int(source_gfs.get("weekly") or 0),
            "monthly": int(source_gfs.get("monthly") or 0),
            "yearly": int(source_gfs.get("yearly") or 0),
            "enabled": _coerce_gfs_enabled(source_gfs.get("enabled")),
        },
        "active_full_days": list(source_job.get("active_full_days") or []),
        "synthetic_full_days": list(source_job.get("synthetic_full_days") or []),
        "description": description,
        "task_parameters": dict(source_task_parameters),
        "status": "active" if schedule and bool(schedule.get("enabled", True)) else "disabled" if schedule else "planned",
        "created_at": now,
        "updated_at": now,
        "next_run_at": schedule_next_run(schedule, after=_parse_dt(now)).isoformat() if schedule and bool(schedule.get("enabled", True)) else None,
        "last_run_at": None,
        "last_session_id": None,
    }
    if not job["source_job_id"]:
        job.pop("source_job_id", None)
        job.pop("source_job_name", None)
    return job_to_dto(_persist_canonical_job(job))


def edit_job_record(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = resolve_job(record_id=job_id)
    updated = _json_clone(current)
    changed = False
    if payload.get("name"):
        new_name = str(payload["name"]).strip()
        _ensure(bool(new_name), 422, "invalid_job", "Job name must not be empty")
        _ensure_ascii_name(new_name, field="Job name", code="invalid_job")
        for existing_id, existing in catalog_entries(JOBS):
            if existing_id == job_id:
                continue
            if str(existing.get("name") or "").lower() == new_name.lower():
                raise api_error(409, "job_name_exists", "Job name already exists", name=new_name)
        updated["name"] = new_name
        changed = True
    if "schedule" in payload and payload["schedule"] is not None:
        updated["schedule"] = normalize_schedule_payload(payload["schedule"])
        updated["next_run_at"] = schedule_next_run(updated["schedule"], after=_now_utc()).isoformat() if bool(updated["schedule"].get("enabled", True)) else None
        changed = True
    if "retention_days" in payload and payload["retention_days"] is not None:
        retention_days = int(payload["retention_days"])
        _ensure(retention_days > 0, 422, "invalid_job", "retention-days must be greater than 0")
        updated["retention_days"] = retention_days
        changed = True
    gfs_enabled_given = payload.get("gfs_enabled") is not None
    if gfs_enabled_given or any(key in payload for key in ("gfs_weekly", "gfs_monthly", "gfs_yearly")):
        current_gfs = updated.get("gfs") if isinstance(updated.get("gfs"), dict) else {}
        new_gfs = _validate_gfs_counts(
            {
                "weekly": payload.get("gfs_weekly", current_gfs.get("weekly", 0)),
                "monthly": payload.get("gfs_monthly", current_gfs.get("monthly", 0)),
                "yearly": payload.get("gfs_yearly", current_gfs.get("yearly", 0)),
            }
        )
        # `gfs_enabled` toggles protection on/off independently of the counts
        # above — omit it to leave the job's current enabled state untouched.
        new_gfs["enabled"] = bool(payload["gfs_enabled"]) if gfs_enabled_given else _coerce_gfs_enabled(current_gfs.get("enabled"))
        updated["gfs"] = new_gfs
        changed = True
    if "active_full_days" in payload and payload["active_full_days"] is not None:
        updated["active_full_days"] = _normalize_day_list(payload["active_full_days"])
        changed = True
    if "synthetic_full_days" in payload and payload["synthetic_full_days"] is not None:
        updated["synthetic_full_days"] = _normalize_day_list(payload["synthetic_full_days"])
        changed = True
    if "description" in payload:
        description = str(payload.get("description") or "").strip()
        _ensure_ascii_text(description, field="Job description", code="invalid_job", max_length=MAX_LONG_TEXT_LENGTH)
        updated["description"] = description
        changed = True
    if "task_parameters" in payload and payload["task_parameters"] is not None:
        _ensure(isinstance(payload["task_parameters"], dict), 422, "invalid_job", "task_parameters must be an object")
        merged = _merge_task_parameters(updated.get("task_parameters"), payload["task_parameters"])
        updated["task_parameters"] = _validate_task_parameters(merged)
        changed = True
    if updated["job_type"] == "backup":
        if "repo" in payload or "repo_id" in payload:
            repository = resolve_repository(name=payload.get("repo"), record_id=payload.get("repo_id"))
            updated["repo_id"] = repository["id"]
            updated["repo_name"] = repository["name"]
            changed = True
        if "inventory" in payload or "inventory_id" in payload:
            inventory = resolve_inventory(name=payload.get("inventory"), record_id=payload.get("inventory_id"))
            updated["inventory_id"] = inventory["id"]
            updated["inventory_name"] = inventory["name"]
            changed = True
        if "vms" in payload and payload["vms"] is not None:
            tokens = payload["vms"] if isinstance(payload["vms"], list) else _split_csv(str(payload["vms"]))
            vm_ids, vm_names = _resolve_vm_selection(str(updated["inventory_id"]), tokens, auto_discover=True)
            updated["vm_ids"] = vm_ids
            updated["vm_names"] = vm_names
            changed = True
    else:
        if "repo" in payload or "repo_id" in payload:
            repository = resolve_repository(name=payload.get("repo"), record_id=payload.get("repo_id"))
            updated["repo_id"] = repository["id"]
            updated["repo_name"] = repository["name"]
            changed = True
        if "source_job" in payload or "source_job_id" in payload:
            source = resolve_job(name=payload.get("source_job"), record_id=payload.get("source_job_id"))
            updated["source_job_id"] = source["id"]
            updated["source_job_name"] = source["name"]
            updated["inventory_id"] = source["inventory_id"]
            updated["inventory_name"] = source["inventory_name"]
            changed = True
    _ensure(changed, 422, "invalid_job", "job edit requires at least one editable option")
    updated["updated_at"] = utc_now()
    if updated.get("schedule"):
        updated["schedule_summary"] = _render_schedule_summary(updated["schedule"])
        updated["status"] = "active" if bool(updated["schedule"].get("enabled", True)) else "disabled"
        if not bool(updated["schedule"].get("enabled", True)):
            updated["next_run_at"] = None
    else:
        updated["status"] = "planned"
        updated["next_run_at"] = None
    return job_to_dto(_persist_canonical_job(updated))


def delete_job_record(job_id: str) -> dict[str, Any]:
    job = _find_by_id(catalog_records(JOBS), job_id, label="Job")
    for task in catalog_records(TASKS):
        if str(task.get("job_id") or "") != job_id:
            continue
        if str(task.get("status") or "") in {"queued", "running", "stopping"}:
            raise api_error(409, "job_has_active_sessions", "Job cannot be deleted while sessions are queued or running", session_id=task.get("id"))
    delete_job(job_id)
    return {"id": str(job.get("id") or job_id), "deleted": True}


def _chain_id_for_point(job_id: str | None, vm_id: str | None, repository_id: str | None) -> str:
    return f"backup::{job_id or '-'}::{vm_id or '-'}::{repository_id or '-'}"


def _backup_points_raw() -> list[dict[str, Any]]:
    return [dict(item) for item in catalog_records(BACKUPS)]


def normalize_backup_point(record: dict[str, Any]) -> dict[str, Any]:
    point = _json_clone(record)
    origin = point.get("origin") if isinstance(point.get("origin"), dict) else {}
    source = point.get("source") if isinstance(point.get("source"), dict) else {}
    placements = point.get("placements") if isinstance(point.get("placements"), list) else []
    placement = next((item for item in placements if isinstance(item, dict)), {})
    job_id = str(point.get("job_id") or origin.get("job_id") or "")
    vm_id = str(point.get("vm_id") or source.get("vm_external_id") or source.get("resource_external_id") or "")
    repository_id = str(point.get("repository_id") or placement.get("repository_id") or "")
    execution_type = str(point.get("execution_type") or point.get("resolved_backup_type") or point.get("backup_mode") or "")
    if execution_type.lower() == "incremental":
        execution_type = "Incremental"
    elif execution_type.lower() in {"full", "activefull", "active_full", "active-full"}:
        execution_type = "ActiveFull"
    elif execution_type.lower() in {"syntheticfull", "synthetic_full", "synthetic-full"}:
        execution_type = "SyntheticFull"
    execution_reason = str(point.get("execution_reason") or point.get("resolved_backup_reason") or "")
    full_backup = execution_type in {"ActiveFull", "SyntheticFull"} or bool(point.get("full_backup"))
    point["id"] = str(point.get("id") or uuid4())
    point["job_id"] = job_id or None
    point["job_name"] = str(point.get("job_name") or origin.get("job_name") or "")
    point["vm_id"] = vm_id or None
    point["vm_name"] = str(point.get("vm_name") or source.get("vm_name") or source.get("resource_name") or vm_id or "")
    point["inventory_id"] = str(point.get("inventory_id") or source.get("hypervisor_id") or "")
    point["inventory_name"] = str(point.get("inventory_name") or source.get("hypervisor_name") or "")
    point["repository_id"] = repository_id or None
    point["repository_name"] = str(point.get("repository_name") or placement.get("repository_name") or "")
    point["repository_type"] = str(point.get("repository_type") or placement.get("repository_type") or "")
    point["execution_type"] = execution_type or ("ActiveFull" if full_backup else "Incremental")
    point["execution_reason"] = execution_reason or ("StandardIncremental" if point["execution_type"] == "Incremental" else "")
    point["full_backup"] = full_backup
    point["created_at"] = str(point.get("created_at") or origin.get("created_at") or utc_now())
    point["status"] = str(point.get("status") or "available")
    point["gfs_marks"] = point.get("gfs_marks") if isinstance(point.get("gfs_marks"), dict) else {"weekly": False, "monthly": False, "yearly": False}
    point["gfs_protected"] = bool(point.get("gfs_protected") or any(bool(value) for value in point["gfs_marks"].values()))
    point["parent_point_id"] = point.get("parent_point_id")
    point["segment_id"] = str(point.get("segment_id") or (point["id"] if point["full_backup"] else ""))
    point["chain_id"] = str(point.get("chain_id") or _chain_id_for_point(job_id or None, vm_id or None, repository_id or None))
    point["artifact"] = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    point["placements"] = placements
    return point


def normalized_backup_points() -> list[dict[str, Any]]:
    items = [normalize_backup_point(item) for item in _backup_points_raw()]
    items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    return items


def grouped_backup_points() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in normalized_backup_points():
        grouped[str(point.get("chain_id") or "")].append(point)
    return grouped


def _points_for_job_vm_repo(job_id: str, vm_id: str, repository_id: str | None) -> list[dict[str, Any]]:
    chain_id = _chain_id_for_point(job_id, vm_id, repository_id)
    return list(grouped_backup_points().get(chain_id, []))


def _latest_successful_point(job_id: str, vm_id: str, repository_id: str | None) -> dict[str, Any] | None:
    points = [
        point
        for point in _points_for_job_vm_repo(job_id, vm_id, repository_id)
        if str(point.get("status") or "") in {"available", "completed", "healthy"}
    ]
    return points[-1] if points else None


def resolve_job_execution(
    job: dict[str, Any],
    *,
    vm_id: str,
    force_full: bool,
    now: datetime | None = None,
) -> tuple[str, str]:
    current = now or _now_utc()
    repository = resolve_repository(record_id=str(job.get("repo_id") or "")) if job.get("repo_id") else {}
    if force_full:
        return "ActiveFull", "ForceFull"
    previous = _latest_successful_point(str(job["id"]), vm_id, str(job.get("repo_id") or "") or None)
    if previous is None:
        return "ActiveFull", "FirstRun"
    active_match = schedule_today_matches_days(job, list(job.get("active_full_days") or []), now=current)
    synthetic_match = schedule_today_matches_days(job, list(job.get("synthetic_full_days") or []), now=current)
    if active_match and synthetic_match:
        return "ActiveFull", "ActiveOverridesSynthetic"
    if active_match:
        return "ActiveFull", "ActiveFullWindow"
    if synthetic_match:
        if repository and not repository_supports_synthetic(repository):
            return "ActiveFull", "RepositorySyntheticUnsupportedEscalatedToActive"
        return "SyntheticFull", "SyntheticFullWindow"
    return "Incremental", "StandardIncremental"


def _point_period_key(point: dict[str, Any], *, kind: str) -> str:
    created = _parse_dt(point.get("created_at"))
    if kind == "weekly":
        iso_year, iso_week, _ = created.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if kind == "monthly":
        return f"{created.year:04d}-{created.month:02d}"
    return f"{created.year:04d}"


def _apply_gfs_marks(points: list[dict[str, Any]], gfs: dict[str, Any]) -> None:
    enabled = _coerce_gfs_enabled(gfs.get("enabled"))
    for point in points:
        marks = point.get("gfs_marks") if isinstance(point.get("gfs_marks"), dict) else {}
        point["gfs_marks"] = {
            "weekly": bool(marks.get("weekly")),
            "monthly": bool(marks.get("monthly")),
            "yearly": bool(marks.get("yearly")),
        }
    full_points = [
        point
        for point in points
        if bool(point.get("full_backup")) and str(point.get("execution_reason") or "") != "ForceFull"
    ]
    for key in ("weekly", "monthly", "yearly"):
        # A disabled policy suspends protection (counts stay configured for a later re-enable).
        count = int(gfs.get(key) or 0) if enabled else 0
        if count <= 0:
            for point in full_points:
                point["gfs_marks"][key] = False
            continue
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in full_points:
            grouped[_point_period_key(point, kind=key)].append(point)
        chosen = []
        for period_key, bucket in grouped.items():
            bucket.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
            chosen.append((period_key, bucket[-1]))
        chosen.sort(key=lambda item: item[0], reverse=True)
        keep_ids = {entry["id"] for _period_key, entry in chosen[:count]}
        for point in full_points:
            point["gfs_marks"][key] = point["id"] in keep_ids
    for point in points:
        point["gfs_protected"] = any(bool(value) for value in point["gfs_marks"].values())


def _repository_storage_roots(repository: dict[str, Any] | None) -> list[Path]:
    if not isinstance(repository, dict):
        return []
    
    repo_type = str(repository.get("type") or "").strip().lower()

    if repo_type == "s3":
        return []
    
    roots: list[Path] = []
    repo_path_value = repository.get("path")

    if isinstance(repo_path_value, str) and repo_path_value:
        try:
            roots.append(Path(repo_path_value).expanduser().resolve())
        except OSError:
            pass

    return roots


def _find_overlapping_repository(candidate_path: str, *, exclude_id: str | None = None) -> dict[str, Any] | None:
    try:
        candidate = Path(candidate_path).expanduser().resolve()
    except OSError:
        return None
    
    for repo_id, repo in catalog_entries(REPOSITORIES):
        if repo_id == exclude_id:
            continue

        for root in _repository_storage_roots(repo):
            if candidate == root or root in candidate.parents or candidate in root.parents:
                return repo
    return None


def _resolve_owning_repository_id(path: Path) -> str | None:
    best_id: str | None = None
    best_depth = -1

    for repo_id, repo in catalog_entries(REPOSITORIES):
        for root in _repository_storage_roots(repo):
            if path == root or root in path.parents:
                depth = len(root.parts)

                if depth > best_depth:
                    best_depth = depth
                    best_id = repo_id
    return best_id


def _path_is_within_repository(path: Path, storage_roots: list[Path], repository_id: str | None) -> bool:
    if not storage_roots:
        return True
    
    if not any(path == root or root in path.parents for root in storage_roots):
        return False
    
    owner_id = _resolve_owning_repository_id(path)
    return owner_id is None or owner_id == repository_id


def _delete_backup_artifacts_from_point(point: dict[str, Any]) -> bool:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    point_repository_id = str(point.get("repository_id") or "")
    repository = REPOSITORIES.get(point_repository_id)

    if not isinstance(repository, dict):
        repository = None

    storage_roots = _repository_storage_roots(repository)
    paths = []
    manifest_abr_paths: set[str] = set()
    all_removed = True

    for key in ("backup_root", "manifest_path"):
        value = artifact.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    for disk in artifact.get("disks", []):
        if not isinstance(disk, dict):
            continue
        abr_path = str(disk.get("abr_path") or "").strip()
        abr_backup_name = str(disk.get("abr_backup_name") or "").strip()
        if repository is not None and abr_path and abr_backup_name:
            # Covers both local and S3 disks -- both are stored in the .abr
            # artifact format (see storage_tool.is_abr_repository). For local
            # disks this duplicates the backup_root rmtree below, which is
            # harmless; it's required for S3, where there is no local folder
            # to wipe.
            if not _path_is_within_repository(Path(abr_path).resolve(), storage_roots, point_repository_id):
                all_removed = False
                LOGGER.warning(
                    "Refusing to delete backup artifact outside repository storage root: point=%s repository_id=%s path=%s",
                    point.get("id"),
                    point.get("repository_id"),
                    abr_path,
                )
                continue
            manifest_abr_paths.add(abr_path)
            try:
                remove_disk_tool_backup(repository, abr_path, abr_backup_name, timeout=120)
            except StorageToolError:
                all_removed = False
                LOGGER.warning(
                    "Failed to delete remote backup artifact: point=%s path=%s name=%s",
                    point.get("id"),
                    abr_path,
                    abr_backup_name,
                    exc_info=True,
                )
            continue
        destination_path = disk.get("destination_path")
        if isinstance(destination_path, str) and destination_path:
            paths.append(Path(destination_path))

    for abr_path in manifest_abr_paths:
        try:
            remove_disk_tool_backup(repository, abr_path, "manifest", timeout=120)
        except StorageToolError:
            LOGGER.debug(
                "Ignoring manifest metadata cleanup failure for point %s path=%s",
                point.get("id"),
                abr_path,
                exc_info=True,
            )

    for candidate in sorted({path.resolve() for path in paths if path.exists()}, key=lambda item: len(str(item)), reverse=True):
        if not _path_is_within_repository(candidate, storage_roots, point_repository_id):
            all_removed = False
            LOGGER.warning(
                "Refusing to delete backup artifact outside repository storage root: point=%s repository_id=%s path=%s",
                point.get("id"),
                point.get("repository_id"),
                candidate,
            )
            continue
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError:
            all_removed = False
            LOGGER.warning(
                "Failed to delete backup artifact: point=%s repository_id=%s path=%s",
                point.get("id"),
                point.get("repository_id"),
                candidate,
                exc_info=True,
            )
    return all_removed


def _segment_points(points: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    segments: list[list[dict[str, Any]]] = []
    current_segment: list[dict[str, Any]] = []
    for point in points:
        if point.get("full_backup") and current_segment:
            segments.append(current_segment)
            current_segment = []
        current_segment.append(point)
    if current_segment:
        segments.append(current_segment)
    return segments


def apply_retention_to_chain(job: dict[str, Any], *, vm_id: str, repository_id: str | None) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    points = _points_for_job_vm_repo(str(job["id"]), vm_id, repository_id)
    if not points:
        return failures
    _apply_gfs_marks(points, dict(job.get("gfs") or {}))
    cutoff = _now_utc() - timedelta(days=int(job.get("retention_days") or 0))
    to_delete: list[dict[str, Any]] = []
    for segment in _segment_points(points):
        full_point = segment[0]
        other_points = segment[1:]
        expired_flags = [_parse_dt(point.get("created_at")) < cutoff for point in segment]
        all_expired = all(expired_flags)
        if full_point.get("gfs_protected"):
            if other_points and all(_parse_dt(point.get("created_at")) < cutoff and not point.get("gfs_protected") for point in other_points):
                to_delete.extend(other_points)
            continue
        if all_expired and not any(point.get("gfs_protected") for point in segment):
            to_delete.extend(segment)
    if not to_delete:
        for point in points:
            persist_backup(point)
        return failures
    delete_ids = {point["id"] for point in to_delete}
    for segment in _segment_points(points):
        segment_to_delete = [point for point in segment if point["id"] in delete_ids]
        if not segment_to_delete:
            for point in segment:
                persist_backup(point)
            continue

        blocked = False
        for point in reversed(segment_to_delete):
            if blocked:
                persist_backup(point)
                continue
            if _delete_backup_artifacts_from_point(point):
                delete_backup(point["id"])
            else:
                LOGGER.error(
                    "Retention failed to delete backup artifacts; keeping catalog entry for retry: "
                    "point=%s job=%s vm=%s repository=%s",
                    point.get("id"),
                    job.get("id"),
                    vm_id,
                    repository_id,
                )
                failures.append(
                    {
                        "point_id": str(point.get("id") or ""),
                        "job_id": str(job.get("id") or ""),
                        "vm_id": vm_id,
                        "repository_id": repository_id,
                    }
                )
                persist_backup(point)
                blocked = True
        for point in segment:
            if point["id"] not in delete_ids:
                persist_backup(point)
    return failures


def _append_task_log(task: dict[str, Any], *, level: str, message: str, component: str = "engine", **extra: Any) -> None:
    logs = task.get("logs") if isinstance(task.get("logs"), list) else []
    entry = {
        "timestamp": utc_now(),
        "level": level,
        "component": component,
        "message": message,
    }
    entry.update(extra)
    logs.append(entry)
    task["logs"] = logs


def _session_state(task: dict[str, Any]) -> str:
    raw = str(task.get("status") or "queued").lower()
    if raw == "cancelled":
        return "canceled"
    if raw == "pending":
        return "queued"
    if raw in SESSION_STATES:
        return raw
    if raw in {"done", "success"}:
        return "completed"
    return raw or "queued"


def _session_vm_names(task: dict[str, Any]) -> tuple[str | None, str | None]:
    """The VM a session acts on, and (restores only) the VM it came from.

    A backup task names the VM it protects in ``parameters.vm_name``; a restore
    task names the VM it *creates* in ``parameters.target_vm_name`` and the VM
    the restore point was taken from in ``parameters.source_vm_name``. Neither
    is reachable from ``resource_external_id`` alone (that is an opaque moref),
    so the console had nothing but the job name to show in a "Virtual machine"
    column -- which is why it used to render the job name there, and "-" for a
    restore, which has no job at all.
    """
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    if str(task.get("task_type") or "") == "restore":
        target = str(parameters.get("target_vm_name") or "").strip() or None
        source = str(parameters.get("source_vm_name") or "").strip() or None
        return target or source, source
    return str(parameters.get("vm_name") or "").strip() or None, None


def session_to_dto(task: dict[str, Any], *, include_logs: bool = True) -> dict[str, Any]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    job = resolve_job(record_id=str(task.get("job_id") or "")) if task.get("job_id") and str(task.get("job_id")) in JOBS else None
    vm_name, source_vm_name = _session_vm_names(task)
    dto: dict[str, Any] = {
        "id": task.get("id"),
        "state": _session_state(task),
        "type": task.get("task_type"),
        "job": {"id": job["id"], "name": job["name"]} if job else None,
        "vm_id": task.get("resource_external_id") if str(task.get("resource_type") or "") == "virtual_machine" else None,
        # The VM this session acts on, by name. For a restore this is the VM
        # being created (e.g. "testVM-restore"); source_vm_name is where the
        # restore point came from.
        "vm_name": vm_name,
        "source_vm_name": source_vm_name,
        "started_at": task.get("created_at"),
        "finished_at": result.get("finished_at"),
        "resolved_backup_type": result.get("resolved_backup_type"),
        "resolved_backup_reason": result.get("resolved_backup_reason"),
        # "schedule" only for tasks the JobScheduler enqueued on its own; every
        # other path (Run now/Run full backup, restores, backup-copy) is a
        # human clicking a button, so default to "manual" when unset.
        "triggered_by": task.get("trigger") or "manual",
        "progress": result.get("progress"),
        "message": result.get("message"),
        # Best-effort byte counters (see run_vcenter_backup) -- absent/0 for
        # non-vCenter (mock) engines and for restores, which don't track them.
        "processed_bytes": result.get("processed_bytes"),
        "transferred_bytes": result.get("transferred_bytes"),
        "errors": error or ([] if not result.get("message") or _session_state(task) == "completed" else [{"message": result.get("message")}]),
        "warnings": task.get("warnings") if isinstance(task.get("warnings"), list) else [],
    }
    if str(task.get("task_type") or "") == "verify":
        dto["verify"] = result.get("verify")
    # A session's log is the bulk of its DTO (task_pool appends a line per
    # progress update), so list callers that only need the row can drop it and
    # read GET /sessions/{id}/log on demand instead.
    if include_logs:
        dto["logs"] = task.get("logs") if isinstance(task.get("logs"), list) else []
    return dto


def list_sessions(*, job_name: str | None = None, include_logs: bool = True) -> list[dict[str, Any]]:
    sessions = [session_to_dto(task, include_logs=include_logs) for task in catalog_records(TASKS)]
    if job_name:
        lowered = job_name.lower()
        sessions = [
            session
            for session in sessions
            if isinstance(session.get("job"), dict) and str(session["job"].get("name") or "").lower() == lowered
        ]
    sessions.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("id") or "")), reverse=True)
    return sessions


def list_sessions_page(
    *,
    job_name: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """One newest-first page of the session list, with the unpaged match count.

    The whole task catalog lives in memory (see mongo_db_wrapper.TASKS), so the
    page is a slice, not a query -- what it saves is the response body, which
    had grown to ~1.8 MB at a thousand sessions. Items here carry no ``logs``
    (GET /sessions/{id}/log serves those per session), and ``total`` lets a
    client page through without guessing when it is done.
    """
    sessions = list_sessions(job_name=job_name, include_logs=False)
    start = max(0, offset)
    items = sessions[start:] if limit is None else sessions[start : start + limit]
    return {"items": items, "total": len(sessions), "limit": limit, "offset": start}


def get_session(session_id: str) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    return session_to_dto(task)


def get_session_log(session_id: str) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    return {
        "id": task.get("id"),
        "logs": task.get("logs") if isinstance(task.get("logs"), list) else [],
    }


def stop_session(session_id: str, *, force: bool = False) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    current_state = _session_state(task)
    if current_state in {"completed", "failed", "stopped", "canceled"}:
        return {
            "id": task["id"],
            "state": current_state,
            "message": "Session is already finished",
        }
    if current_state == "queued":
        task["status"] = "stopped"
        _append_task_log(task, level="warn", message="Session stopped before execution started", component="session")
    else:
        task["status"] = "stopping"
        task["cancel_requested"] = True
        _append_task_log(
            task,
            level="warn",
            message="Stop requested for running session" if not force else "Forced stop requested for running session",
            component="session",
            force=force,
        )
    task["updated_at"] = utc_now()
    persist_task(task)
    return {
        "id": task["id"],
        "state": _session_state(task),
        "message": "Stop requested",
    }


def _chain_name(points: list[dict[str, Any]]) -> str:
    latest = points[-1]
    return f"{latest.get('job_name') or latest.get('job_id') or 'Unknown Job'} / {latest.get('vm_name') or latest.get('vm_id') or 'Unknown VM'} / {latest.get('repository_name') or 'Unknown Repo'}"


def backup_chain_to_dto(points: list[dict[str, Any]]) -> dict[str, Any]:
    latest = points[-1]
    restore_points = [
        {
            "id": point["id"],
            "created_at": point.get("created_at"),
            "execution_type": point.get("execution_type"),
            "execution_reason": point.get("execution_reason"),
            "gfs_marks": point.get("gfs_marks"),
            "gfs_protected": point.get("gfs_protected"),
            "parent_point_id": point.get("parent_point_id"),
            "status": point.get("status"),
            "vm_id": point.get("vm_id"),
            "vm_name": point.get("vm_name"),
            "repository_id": point.get("repository_id"),
            "repository_name": point.get("repository_name"),
            "repository_type": point.get("repository_type"),
            "size_bytes": _restore_point_size_bytes(point),
            # The uncompressed data behind the stored bytes, and what fraction
            # of it the repository actually holds -- both straight from what
            # the mover counted, absent on points written before it reported.
            "logical_size_bytes": point.get("logical_size_bytes"),
            "compression_ratio": point.get("compression_ratio"),
            "compression_level": point.get("compression_level"),
            "encryption_enabled": point.get("encryption_enabled"),
        }
        for point in points
    ]
    return {
        "id": latest["chain_id"],
        "name": _chain_name(points),
        "job": {"id": latest.get("job_id"), "name": latest.get("job_name")},
        "vm": {"id": latest.get("vm_id"), "name": latest.get("vm_name")},
        "inventory": {"id": latest.get("inventory_id"), "name": latest.get("inventory_name")},
        "repository": {
            "id": latest.get("repository_id"),
            "name": latest.get("repository_name"),
            "type": latest.get("repository_type"),
        },
        "status": latest.get("status"),
        "restore_point_count": len(points),
        "created_at": points[0].get("created_at"),
        "last_restore_point_at": latest.get("created_at"),
        "latest_execution_type": latest.get("execution_type"),
        "latest_execution_reason": latest.get("execution_reason"),
        "gfs_protected": any(bool(point.get("gfs_protected")) for point in points),
        "restore_points": restore_points,
    }


def list_backup_chains(*, repo_name: str | None = None) -> list[dict[str, Any]]:
    filtered = grouped_backup_points()
    if repo_name:
        lowered = repo_name.lower()
        filtered = {
            chain_id: points
            for chain_id, points in filtered.items()
            if any(str(point.get("repository_name") or "").lower() == lowered for point in points)
        }
    chains = [backup_chain_to_dto(points) for _chain_id, points in filtered.items()]
    chains.sort(key=lambda item: (str(item.get("last_restore_point_at") or ""), str(item.get("id") or "")), reverse=True)
    return chains


def _normalize_backup_health(value: Any) -> str:
    normalized = str(value or "unknown").strip().lower()
    if normalized in {"available", "completed", "healthy", "success", "ok", "valid"}:
        return "valid"
    if normalized in {"warning", "degraded"}:
        return "warning"
    if normalized in {"missing", "deleted", "failed", "corrupt", "invalid", "error", "unavailable"}:
        return "invalid"
    return "unknown"


def _aggregate_backup_health(values: list[str]) -> str:
    normalized_values = {_normalize_backup_health(value) for value in values if str(value or "").strip()}
    if not normalized_values:
        return "unknown"
    if normalized_values == {"invalid"}:
        return "invalid"
    if "invalid" in normalized_values:
        return "warning"
    if "warning" in normalized_values:
        return "warning"
    if "valid" in normalized_values:
        return "valid"
    return "unknown"


def _restore_disabled_reason_for_kind(kind: str) -> str:
    if kind == "job":
        return "Select a protected object, backup chain, or restore point to restore."
    return "Select a protected object or restore point to restore."


def _restore_candidate_from_point(point: dict[str, Any]) -> dict[str, Any] | None:
    available = _restore_point_is_available(point)
    summary = {
        "id": point.get("id"),
        "created_at": point.get("created_at"),
        "point_type": _restore_point_type(point),
        "status": point.get("status"),
        "health": _normalize_backup_health(point.get("status")),
        "available": available,
    }
    if not available:
        return None
    payload = {
        "point_id": point.get("id"),
        "backup": point.get("chain_id"),
        "backup_id": point.get("chain_id"),
        "restore_point_id": point.get("id"),
        "repository_id": point.get("repository_id"),
        "repository_name": point.get("repository_name"),
        "repository_type": point.get("repository_type"),
        "inventory_id": point.get("inventory_id"),
        "inventory_name": point.get("inventory_name"),
        "job_id": point.get("job_id"),
        "job_name": point.get("job_name"),
        "protected_object_id": point.get("vm_id"),
        "protected_object_name": point.get("vm_name"),
        "vm_id": point.get("vm_id"),
        "vm_name": point.get("vm_name"),
    }
    return {
        "point": point,
        "summary": summary,
        "payload": payload,
    }


def _new_backup_tree_node(
    *,
    node_id: str,
    kind: str,
    label: str,
    repository_id: str | None = None,
    repository_name: str | None = None,
    inventory_id: str | None = None,
    inventory_name: str | None = None,
    job_id: str | None = None,
    job_name: str | None = None,
    protected_object_id: str | None = None,
    protected_object_name: str | None = None,
    backup_id: str | None = None,
    backup_name: str | None = None,
    restore_point_id: str | None = None,
    timestamp: str | None = None,
    health: str | None = None,
    backup_type: str | None = None,
    can_restore: bool = False,
    disabled_reason: str | None = None,
    restore_payload: dict[str, Any] | None = None,
    latest_restore_point: dict[str, Any] | None = None,
    restore_points_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "repository_id": repository_id,
        "repository_name": repository_name,
        "inventory_id": inventory_id,
        "inventory_name": inventory_name,
        "job_id": job_id,
        "job_name": job_name,
        "protected_object_id": protected_object_id,
        "protected_object_name": protected_object_name,
        "backup_id": backup_id,
        "backup_name": backup_name,
        "restore_point_id": restore_point_id,
        "timestamp": timestamp,
        "last_restore_point_at": timestamp,
        "backup_type": backup_type,
        "health": health or "unknown",
        "restore_points_count": restore_points_count,
        "can_restore": can_restore,
        "disabled_reason": disabled_reason,
        "restore_payload": restore_payload,
        "latest_restore_point": latest_restore_point,
        "children": [],
        "_child_index": {},
        "_restore_candidate": None,
    }


def _ensure_backup_tree_child(parent: dict[str, Any], key: str, factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    child_index = parent.setdefault("_child_index", {})
    node = child_index.get(key)
    if node is None:
        node = factory()
        child_index[key] = node
        parent.setdefault("children", []).append(node)
    return node


def _finalize_backup_tree_node(node: dict[str, Any]) -> dict[str, Any]:
    children = [_finalize_backup_tree_node(child) for child in node.get("children", [])]
    children.sort(
        key=lambda child: (
            str(child.get("last_restore_point_at") or ""),
            str(child.get("label") or "").lower(),
            str(child.get("id") or ""),
        ),
        reverse=True,
    )
    node["children"] = children
    node.pop("_child_index", None)

    if node["kind"] == "restore_point":
        return node

    node["restore_points_count"] = sum(int(child.get("restore_points_count") or 0) for child in children)
    node["last_restore_point_at"] = next(
        (child.get("last_restore_point_at") for child in children if child.get("last_restore_point_at")),
        None,
    )
    node["health"] = _aggregate_backup_health([str(child.get("health") or "") for child in children])

    candidates = [child.get("_restore_candidate") for child in children if isinstance(child.get("_restore_candidate"), dict)]
    if candidates:
        latest_candidate = max(
            candidates,
            key=lambda candidate: _sort_key(candidate["point"]),
        )
        node["_restore_candidate"] = latest_candidate
        node["latest_restore_point"] = latest_candidate["summary"]
    else:
        node["_restore_candidate"] = None
        node["latest_restore_point"] = None

    if node["kind"] in {"backup_chain", "protected_object"}:
        candidate = node.get("_restore_candidate")
        if isinstance(candidate, dict):
            node["can_restore"] = True
            node["disabled_reason"] = None
            node["restore_payload"] = candidate["payload"]
        else:
            node["can_restore"] = False
            node["disabled_reason"] = "No valid restore points are available under this node."
            node["restore_payload"] = None
    else:
        node["can_restore"] = False
        node["disabled_reason"] = _restore_disabled_reason_for_kind(str(node.get("kind") or ""))
        node["restore_payload"] = None

    if node["kind"] in {"backup_chain", "protected_object"} and children:
        point_types = {str(child.get("backup_type") or "") for child in children if child.get("backup_type")}
        node["backup_type"] = "incremental" if "incremental" in point_types else "full" if point_types else None
    else:
        node["backup_type"] = None

    return node


def _strip_backup_tree_internal_keys(node: dict[str, Any]) -> dict[str, Any]:
    node.pop("_child_index", None)
    node.pop("_restore_candidate", None)
    node["children"] = [_strip_backup_tree_internal_keys(child) for child in node.get("children", [])]
    return node


def list_backup_tree() -> list[dict[str, Any]]:
    root = {"children": [], "_child_index": {}}
    for chain in list_backup_chains():
        restore_points = [
            normalize_backup_point(point)
            for point in chain.get("restore_points", [])
            if isinstance(point, dict)
        ]
        if not restore_points:
            continue

        restore_points.sort(key=_sort_key, reverse=True)
        latest_point = restore_points[0]
        repository_id = str(chain.get("repository", {}).get("id") or chain.get("repository_id") or latest_point.get("repository_id") or "") or None
        repository_name = str(chain.get("repository", {}).get("name") or chain.get("repository_name") or latest_point.get("repository_name") or "") or None
        inventory_id = str(chain.get("inventory", {}).get("id") or chain.get("inventory_id") or latest_point.get("inventory_id") or "") or None
        inventory_name = str(chain.get("inventory", {}).get("name") or chain.get("inventory_name") or latest_point.get("inventory_name") or "") or None
        job_id = str(chain.get("job", {}).get("id") or chain.get("job_id") or latest_point.get("job_id") or "") or None
        job_name = str(chain.get("job", {}).get("name") or chain.get("job_name") or latest_point.get("job_name") or "") or None
        protected_object_id = str(chain.get("vm", {}).get("id") or chain.get("vm_id") or latest_point.get("vm_id") or "") or None
        protected_object_name = str(chain.get("vm", {}).get("name") or chain.get("vm_name") or latest_point.get("vm_name") or "") or None
        backup_id = str(chain.get("id") or latest_point.get("chain_id") or "")
        backup_name = str(chain.get("name") or "")
        backup_type = "incremental" if any(_restore_point_type(point) == "incremental" for point in restore_points) else "full"

        repository_label = repository_name or repository_id or "Unassigned Repository"
        inventory_label = inventory_name or inventory_id or "Unassigned Inventory"
        job_label = job_name or job_id or "Unknown Job"
        protected_object_label = protected_object_name or protected_object_id or "Unknown Protected Object"
        chain_label = "Incremental chain" if backup_type == "incremental" else "Full chain" if backup_type == "full" else "Backup chain"

        repository_node = _ensure_backup_tree_child(
            root,
            f"repository::{repository_id or repository_name or backup_id}",
            lambda: _new_backup_tree_node(
                node_id=f"repository::{repository_id or repository_name or backup_id}",
                kind="repository",
                label=repository_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
            ),
        )
        inventory_node = _ensure_backup_tree_child(
            repository_node,
            f"inventory::{repository_id or repository_label}::{inventory_id or inventory_name or backup_id}",
            lambda: _new_backup_tree_node(
                node_id=f"inventory::{repository_id or repository_label}::{inventory_id or inventory_name or backup_id}",
                kind="inventory",
                label=inventory_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
                inventory_id=inventory_id,
                inventory_name=inventory_name or inventory_label,
            ),
        )
        job_node = _ensure_backup_tree_child(
            inventory_node,
            f"job::{repository_id or repository_label}::{inventory_id or inventory_label}::{job_id or job_name or backup_id}",
            lambda: _new_backup_tree_node(
                node_id=f"job::{repository_id or repository_label}::{inventory_id or inventory_label}::{job_id or job_name or backup_id}",
                kind="job",
                label=job_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
                inventory_id=inventory_id,
                inventory_name=inventory_name or inventory_label,
                job_id=job_id,
                job_name=job_name or job_label,
            ),
        )
        protected_object_node = _ensure_backup_tree_child(
            job_node,
            f"protected_object::{repository_id or repository_label}::{inventory_id or inventory_label}::{job_id or job_label}::{protected_object_id or protected_object_name or backup_id}",
            lambda: _new_backup_tree_node(
                node_id=f"protected_object::{repository_id or repository_label}::{inventory_id or inventory_label}::{job_id or job_label}::{protected_object_id or protected_object_name or backup_id}",
                kind="protected_object",
                label=protected_object_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
                inventory_id=inventory_id,
                inventory_name=inventory_name or inventory_label,
                job_id=job_id,
                job_name=job_name or job_label,
                protected_object_id=protected_object_id,
                protected_object_name=protected_object_name or protected_object_label,
            ),
        )
        chain_node = _ensure_backup_tree_child(
            protected_object_node,
            f"backup_chain::{backup_id}",
            lambda: _new_backup_tree_node(
                node_id=f"backup_chain::{backup_id}",
                kind="backup_chain",
                label=chain_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
                inventory_id=inventory_id,
                inventory_name=inventory_name or inventory_label,
                job_id=job_id,
                job_name=job_name or job_label,
                protected_object_id=protected_object_id,
                protected_object_name=protected_object_name or protected_object_label,
                backup_id=backup_id,
                backup_name=backup_name or chain_label,
            ),
        )

        chain_node["children"] = []
        chain_node["_child_index"] = {}
        for point in restore_points:
            point_health = _normalize_backup_health(point.get("status"))
            restore_candidate = _restore_candidate_from_point(point)
            point_label = str(point.get("created_at") or point.get("id") or "Restore point")
            point_node = _new_backup_tree_node(
                node_id=f"restore_point::{point.get('id')}",
                kind="restore_point",
                label=point_label,
                repository_id=repository_id,
                repository_name=repository_name or repository_label,
                inventory_id=inventory_id,
                inventory_name=inventory_name or inventory_label,
                job_id=job_id,
                job_name=job_name or job_label,
                protected_object_id=protected_object_id,
                protected_object_name=protected_object_name or protected_object_label,
                backup_id=backup_id,
                backup_name=backup_name or chain_label,
                restore_point_id=str(point.get("id") or ""),
                timestamp=str(point.get("created_at") or "") or None,
                health=point_health,
                backup_type=_restore_point_type(point),
                can_restore=restore_candidate is not None,
                disabled_reason=None if restore_candidate is not None else f"Restore point status is {point.get('status') or 'unavailable'}.",
                restore_payload=restore_candidate["payload"] if restore_candidate is not None else None,
                latest_restore_point=restore_candidate["summary"] if restore_candidate is not None else {
                    "id": point.get("id"),
                    "created_at": point.get("created_at"),
                    "point_type": _restore_point_type(point),
                    "status": point.get("status"),
                    "health": point_health,
                    "available": False,
                },
                restore_points_count=1,
            )
            point_node["_restore_candidate"] = restore_candidate
            chain_node["children"].append(point_node)

    nodes = [_finalize_backup_tree_node(node) for node in root["children"]]
    nodes.sort(
        key=lambda node: (
            str(node.get("last_restore_point_at") or ""),
            str(node.get("label") or "").lower(),
            str(node.get("id") or ""),
        ),
        reverse=True,
    )
    return [_strip_backup_tree_internal_keys(node) for node in nodes]


def get_backup_chain(backup_id: str) -> dict[str, Any]:
    chains = grouped_backup_points()
    points = chains.get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)
    return backup_chain_to_dto(points)


class _VerifyProgressTracker:
    def __init__(self, total_items: int, on_change: Callable[[int], None] | None) -> None:
        self._total = max(int(total_items), 1)
        self._on_change = on_change
        self._lock = threading.Lock()
        self._item_percent: dict[int, int] = {}
        self._published = 0

    def report(self, item_index: int, percent: int) -> None:
        bounded = max(0, min(100, int(percent)))
        with self._lock:
            if self._item_percent.get(item_index, 0) >= bounded:
                return
            self._item_percent[item_index] = bounded
            overall = min(99, sum(self._item_percent.values()) // self._total)
            if overall <= self._published:
                return
            self._published = overall
            callback = self._on_change
        if callback is not None:
            callback(overall)

    def finish_item(self, item_index: int) -> None:
        self.report(item_index, 100)


def verify_backup_chain(
    backup_id: str,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    points = grouped_backup_points().get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)

    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    work_items: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for point in points:
        if point["execution_type"] == "Incremental" and not point.get("parent_point_id"):
            warnings.append({"point_id": point["id"], "message": "Incremental restore point does not have a recorded parent"})

        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        for disk in artifact.get("disks", []):
            if isinstance(disk, dict):
                work_items.append((point, disk))

    def _emit(progress: int, message: str) -> None:
        if progress_callback is not None:
            progress_callback({"progress": progress, "message": message})

    total_items = len(work_items)
    checked_artifacts = 0
    canceled = False
    _emit(0, f"Verifying {total_items} backup artifact(s)")

    if work_items:
        tracker = _VerifyProgressTracker(
            total_items,
            lambda overall: _emit(overall, f"Verifying backup artifacts: {overall}%"),
        )
        with ThreadPoolExecutor(max_workers=min(VERIFY_CONCURRENCY, total_items)) as pool:
            futures = {
                pool.submit(
                    _verify_disk_artifact,
                    point,
                    disk,
                    on_progress=_verify_artifact_progress_reporter(tracker, index),
                ): (point, index)
                for index, (point, disk) in enumerate(work_items)
            }

            for future in as_completed(futures):
                point, index = futures[future]
                tracker.finish_item(index)

                if future.cancelled():
                    continue
                checked_artifacts += 1

                try:
                    result = future.result()
                except Exception as exc:
                    result = {"disk_key": None, "status": "failed", "message": str(exc)}

                if result["status"] != "ok":
                    errors.append(
                        {
                            "point_id": point["id"],
                            "disk_key": result.get("disk_key"),
                            "message": result["message"],
                        }
                    )

                if not canceled and should_cancel is not None and should_cancel():
                    canceled = True
                    for pending in futures:
                        pending.cancel()

    if canceled:
        result_state = "canceled"
    elif errors:
        result_state = "invalid"
    elif warnings:
        result_state = "warning"
    else:
        result_state = "valid"

    report = {
        "id": backup_id,
        "result": result_state,
        "warnings": warnings,
        "errors": errors,
        "checked_restore_points": len(points),
        "checked_artifacts": checked_artifacts,
    }
    if canceled:
        report["canceled"] = True
    _emit(100, _verify_report_summary(report))
    return report


def _verify_report_summary(report: dict[str, Any]) -> str:
    checked = int(report.get("checked_artifacts") or 0)
    if report.get("canceled"):
        return f"Verification stopped after {checked} artifact(s)"
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if errors:
        return f"{len(errors)} of {checked} backup artifact(s) failed verification"
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    suffix = f" with {len(warnings)} warning(s)" if warnings else ""
    return f"Verified {checked} backup artifact(s) across {report.get('checked_restore_points')} restore point(s){suffix}"


def _verify_artifact_progress_reporter(
    tracker: _VerifyProgressTracker, item_index: int
) -> Callable[[int, int], None]:
    def _report(percent: int, _block_index: int) -> None:
        tracker.report(item_index, percent)

    return _report


def _create_verify_session(backup_id: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    latest = points[-1] if points else {}
    task = {
        "id": str(uuid4()),
        "job_id": None,
        "task_type": "verify",
        "hypervisor_id": None,
        "resource_type": "virtual_machine",
        "resource_external_id": latest.get("vm_id"),
        "status": "running",
        "parameters": {
            "backup_id": backup_id,
            "vm_name": str(latest.get("vm_name") or "").strip() or None,
            "repository_id": latest.get("repository_id"),
            "repository_name": latest.get("repository_name"),
        },
        "result": {"progress": 0},
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "logs": [],
    }
    _append_task_log(task, level="info", message=f"Started verification of backup {backup_id}", component="session")
    persist_task(task)
    return task


def _update_verify_session(task_id: str, update: dict[str, Any], *, status: str | None = None) -> None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        if status is not None:
            task["status"] = status
        fields = dict(update)
        as_log_entry = bool(fields.pop("log_entry", True))
        current_result = task.get("result") if isinstance(task.get("result"), dict) else {}
        current_result.update(fields)
        task["result"] = current_result
        if as_log_entry and fields.get("message"):
            _append_task_log(task, level="info", message=str(fields["message"]), component="engine")
        task["updated_at"] = utc_now()
        try:
            persist_task(dict(task))
        except Exception as exc:
            LOGGER.warning("Failed to persist verify session progress for '%s': %s", task_id, exc)


def _verify_session_cancel_requested(task_id: str) -> bool:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        return bool(task and task.get("cancel_requested"))


def _run_verify_session(backup_id: str, task_id: str) -> dict[str, Any]:
    try:
        report = verify_backup_chain(
            backup_id,
            progress_callback=lambda update: _update_verify_session(
                task_id,
                {**update, "log_entry": False},
            ),
            should_cancel=lambda: _verify_session_cancel_requested(task_id),
        )
    except ApiError as exc:
        _update_verify_session(
            task_id,
            {"progress": 100, "message": str(exc.message), "finished_at": utc_now()},
            status="failed",
        )
        raise
    except Exception as exc:
        LOGGER.exception("Verification of backup '%s' failed", backup_id)
        _update_verify_session(
            task_id,
            {"progress": 100, "message": str(exc), "finished_at": utc_now()},
            status="failed",
        )
        raise

    if report.get("canceled"):
        status = "stopped"
    elif report["result"] == "invalid":
        status = "failed"
    else:
        status = "completed"
    _update_verify_session(
        task_id,
        {
            "progress": 100,
            "message": _verify_report_summary(report),
            "verify": report,
            "finished_at": utc_now(),
        },
        status=status,
    )
    return report


def verify_backup_chain_request(backup_id: str, *, wait: bool = True) -> dict[str, Any]:
    points = grouped_backup_points().get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)

    task = _create_verify_session(backup_id, points)
    task_id = str(task["id"])

    if wait:
        report = _run_verify_session(backup_id, task_id)
        return {**report, "session_id": task_id}

    thread = threading.Thread(
        target=_run_verify_session,
        args=(backup_id, task_id),
        name=f"verify-{task_id}",
        daemon=True,
    )
    thread.start()
    return {
        "id": backup_id,
        "session_id": task_id,
        "state": _session_state(task),
        "result": "running",
        "checked_restore_points": 0,
        "checked_artifacts": 0,
        "warnings": [],
        "errors": [],
    }


def delete_backup_chain(backup_id: str, *, segment_id: str | None = None, force: bool = False) -> dict[str, Any]:
    chains = grouped_backup_points()
    points = chains.get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)
    if segment_id is not None:
        # A segment is one full backup and the incrementals chained onto it —
        # the same grouping `apply_retention_to_chain` uses, keyed by the id of
        # the segment's oldest point (normally the full backup).
        segment = next((seg for seg in _segment_points(points) if seg[0]["id"] == segment_id), None)
        if segment is None:
            raise api_error(
                404, "backup_segment_not_found", "Backup segment not found",
                backup_id=backup_id, segment_id=segment_id,
            )
        points = segment
    gfs_protected_point_ids = [point["id"] for point in points if bool(point.get("gfs_protected"))]
    if gfs_protected_point_ids and not force:
        raise api_error(409, "gfs_protected_backup", "Backup delete would violate GFS protection", backup_id=backup_id)
    if gfs_protected_point_ids:
        LOGGER.warning(
            "Force-deleting GFS-protected backup chain: backup_id=%s segment_id=%s protected_points=%s",
            backup_id,
            segment_id,
            gfs_protected_point_ids,
        )
    inconsistent = [
        point["id"]
        for point in points
        if point["execution_type"] == "Incremental" and not point.get("parent_point_id")
    ]
    if inconsistent:
        raise api_error(409, "inconsistent_backup_chain", "Repository/catalog state is inconsistent", restore_point_ids=inconsistent)

    artifacts_removed = True
    for point in points:
        if not _delete_backup_artifacts_from_point(point):
            artifacts_removed = False
        delete_backup(point["id"])

    result = {"id": backup_id, "deleted": True, "restore_point_count": len(points), "artifacts_removed": artifacts_removed}
    if segment_id is not None:
        result["segment_id"] = segment_id
    if gfs_protected_point_ids:
        result["gfs_protection_overridden"] = True
    if not artifacts_removed:
        result["message"] = (
            "Catalog entry removed. Some backup files were outside this repository's storage root "
            "(e.g. an imported/reference chain) and were left untouched to avoid deleting another "
            "repository's data."
        )
    return result


def import_backup_chain(*, path_value: str, repo_name: str | None = None, repo_id: str | None = None) -> dict[str, Any]:
    # Imported at call time: backup_catalog imports this module, so a top-level
    # import here would close the cycle.
    from python.srv.services.backup_catalog import (
        resolve_compression_ratio,
        resolve_logical_size_bytes,
    )

    repository = resolve_repository(name=repo_name, record_id=repo_id)
    path = Path(path_value).expanduser()
    manifest_path = path / "manifest.json" if path.is_dir() else path
    _ensure(manifest_path.exists(), 404, "import_path_not_found", "Import path not found", path=str(path))
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise api_error(422, "invalid_import", "Import manifest must contain valid JSON", path=str(manifest_path)) from exc
    vm_name = str(payload.get("vm_name") or payload.get("name") or path.stem)
    vm_id = str(payload.get("vm_id") or f"imported-{uuid4()}")
    point_id = str(uuid4())
    try:
        compression_level = int(payload.get("compression_level"))
    except (TypeError, ValueError):
        compression_level = None
    point = normalize_backup_point(
        {
            "id": point_id,
            "job_id": None,
            "job_name": "Imported Backup",
            "vm_id": vm_id,
            "vm_name": vm_name,
            "inventory_id": None,
            "inventory_name": None,
            "repository_id": repository["id"],
            "repository_name": repository["name"],
            "repository_type": repository["type"],
            "execution_type": "ActiveFull",
            "execution_reason": "FirstRun",
            "full_backup": True,
            "compression_level": compression_level,
            "encryption_enabled": bool(payload.get("encryption_enabled")),
            "logical_size_bytes": resolve_logical_size_bytes(payload),
            "compression_ratio": resolve_compression_ratio(payload),
            "created_at": utc_now(),
            "artifact": {
                "manifest_path": str(manifest_path),
                "backup_root": str(manifest_path.parent),
                "disks": payload.get("disks", []),
            },
            "placements": [
                {
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": repository["type"],
                    "manifest_path": str(manifest_path),
                    "backup_root": str(manifest_path.parent),
                }
            ],
        }
    )
    persist_backup(point)
    return get_backup_chain(point["chain_id"])


def _inventory_host_records(inventory_id: str) -> list[dict[str, Any]]:
    hosts = []
    for resource in RESOURCES_BY_HV.get(inventory_id, {}).values():
        if str(resource.get("resource_type") or "") == "host":
            hosts.append(resource)
    hosts.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("external_id") or "")))
    return hosts


def _host_selector_aliases(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    aliases = {raw.lower()}
    candidate = raw if "://" in raw else f"//{raw}"
    parsed = urlparse(candidate)
    if parsed.hostname:
        aliases.add(str(parsed.hostname).lower())
    return aliases


def _inventory_selector_aliases(inventory_id: str) -> set[str]:
    record = HYPERVISORS.get(inventory_id, {})
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    aliases: set[str] = set()
    for value in (
        record.get("address"),
        record.get("endpoint"),
        metadata.get("host"),
    ):
        aliases.update(_host_selector_aliases(value))
    return aliases


def _resolve_host(inventory_id: str, identifier: str) -> dict[str, Any]:
    lowered = identifier.lower()
    host_records = _inventory_host_records(inventory_id)
    matches = []
    for resource in host_records:
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        resource_aliases = {
            str(resource.get("external_id") or "").lower(),
            str(resource.get("name") or "").lower(),
        }
        resource_aliases.update(_host_selector_aliases(payload.get("management_ip")))
        resource_aliases.update(_host_selector_aliases(payload.get("address")))
        if lowered in resource_aliases:
            matches.append(resource)
    if not matches:
        inventory_aliases = _inventory_selector_aliases(inventory_id)
        if lowered in inventory_aliases:
            if len(host_records) == 1:
                return host_records[0]
            if len(host_records) > 1:
                raise api_error(
                    409,
                    "ambiguous_host",
                    "Target host matched the inventory endpoint, but the inventory contains multiple hosts. Use the discovered host name.",
                    inventory_id=inventory_id,
                    host=identifier,
                    available_hosts=[str(resource.get("name") or "") for resource in host_records],
                )
        raise api_error(404, "host_not_found", "Target host not found in inventory", inventory_id=inventory_id, host=identifier)
    if len(matches) > 1:
        raise api_error(409, "ambiguous_host", "Multiple hosts matched the provided selector", inventory_id=inventory_id, host=identifier)
    return matches[0]


def _validate_datastore_on_host(host: dict[str, Any], datastore: str) -> None:
    payload = host.get("payload") if isinstance(host.get("payload"), dict) else {}
    datastores = payload.get("datastores") if isinstance(payload.get("datastores"), list) else []
    lowered = datastore.lower()
    if not any(str(item).lower() == lowered for item in datastores):
        raise api_error(404, "datastore_not_found", "Target datastore is not available on the selected host", host=host.get("name"), datastore=datastore)


def _restore_point_artifact(point: dict[str, Any]) -> dict[str, Any]:
    return point.get("artifact") if isinstance(point.get("artifact"), dict) else {}


def _restore_point_placements(point: dict[str, Any]) -> list[dict[str, Any]]:
    placements = point.get("placements") if isinstance(point.get("placements"), list) else []
    return [item for item in placements if isinstance(item, dict)]


def _find_restore_point_placement(point: dict[str, Any], repo: dict[str, Any] | None) -> dict[str, Any] | None:
    placements = _restore_point_placements(point)
    if repo is None:
        return placements[0] if placements else None

    repo_id = str(repo.get("id") or "")
    for placement in placements:
        if str(placement.get("repository_id") or "") == repo_id:
            return placement

    point_repo_id = str(point.get("repository_id") or "")
    point_repo_name = str(point.get("repository_name") or "")
    matches_point_repository = bool(repo_id) and repo_id == point_repo_id
    matches_point_name = bool(point_repo_name) and str(repo.get("name") or "").lower() == point_repo_name.lower()
    if matches_point_repository or matches_point_name:
        artifact = _restore_point_artifact(point)
        return {
            "repository_id": repo.get("id"),
            "repository_name": repo.get("name"),
            "manifest_path": artifact.get("manifest_path"),
            "backup_root": artifact.get("backup_root"),
        }
    return None


def _select_restore_point_placement(point: dict[str, Any], repo: dict[str, Any] | None) -> dict[str, Any] | None:
    placement = _find_restore_point_placement(point, repo)
    if placement is None and repo is not None:
        raise api_error(
            404,
            "restore_point_not_in_repository",
            "Restore point is not available in the selected repository",
            point_id=point.get("id"),
            repository_id=repo.get("id"),
        )
    return placement


def _resolve_restore_manifest_path(point: dict[str, Any], placement: dict[str, Any] | None) -> str:
    artifact = _restore_point_artifact(point)
    candidates = []
    if isinstance(placement, dict):
        candidates.append(placement.get("manifest_path"))
    candidates.append(artifact.get("manifest_path"))
    for value in candidates:
        path_value = str(value or "").strip()
        if path_value:
            return path_value
    raise api_error(
        409,
        "restore_point_manifest_missing",
        "Selected restore point does not contain a manifest path",
        point_id=point.get("id"),
    )


def _restore_point_is_available(point: dict[str, Any]) -> bool:
    return str(point.get("status") or "available").lower() in {"available", "completed", "healthy"}


def _restore_point_size_bytes(point: dict[str, Any]) -> int | None:
    for field_name in ("size_bytes", "data_size_bytes"):
        raw = point.get(field_name)
        if raw not in (None, ""):
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass

    artifact = _restore_point_artifact(point)
    total = 0
    found_any = False
    for disk in artifact.get("disks", []):
        if not isinstance(disk, dict):
            continue
        destination_path = str(disk.get("destination_path") or "").strip()
        if not destination_path:
            continue
        try:
            candidate = Path(destination_path)
            if candidate.is_file():
                total += candidate.stat().st_size
                found_any = True
        except OSError:
            continue
    manifest_path = str(artifact.get("manifest_path") or "").strip()
    if manifest_path:
        try:
            candidate = Path(manifest_path)
            if candidate.is_file():
                total += candidate.stat().st_size
                found_any = True
        except OSError:
            pass
    return total if found_any else None


def _restore_point_type(point: dict[str, Any]) -> str:
    return "full" if bool(point.get("full_backup")) or str(point.get("execution_type") or "") != "Incremental" else "incremental"


def restore_point_to_dto(point: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": point.get("id"),
        "restore_point_id": point.get("id"),
        "backup_id": point.get("chain_id"),
        "job_id": point.get("job_id"),
        "job_name": point.get("job_name"),
        "vm_id": point.get("vm_id"),
        "vm_name": point.get("vm_name"),
        "inventory_id": point.get("inventory_id"),
        "inventory_name": point.get("inventory_name"),
        "repository_id": point.get("repository_id"),
        "repository_name": point.get("repository_name"),
        "repository_type": point.get("repository_type"),
        "inventory": {
            "id": point.get("inventory_id"),
            "name": point.get("inventory_name"),
        },
        "repository": {
            "id": point.get("repository_id"),
            "name": point.get("repository_name"),
            "type": point.get("repository_type"),
        },
        "created_at": point.get("created_at"),
        "point_type": _restore_point_type(point),
        "execution_type": point.get("execution_type"),
        "execution_reason": point.get("execution_reason"),
        "size_bytes": _restore_point_size_bytes(point),
        "consistency_state": point.get("consistency_state"),
        "status": point.get("status"),
        "available": _restore_point_is_available(point),
        "gfs_marks": point.get("gfs_marks"),
    }


def _restore_point_matches_vm(point: dict[str, Any], selector: str) -> bool:
    lowered = selector.lower()
    return str(point.get("vm_id") or "") == selector or str(point.get("vm_name") or "").lower() == lowered


def _restore_point_matches_backup(point: dict[str, Any], selector: str) -> bool:
    return str(point.get("chain_id") or "") == selector


def _restore_point_matches_inventory(point: dict[str, Any], selector: str) -> bool:
    lowered = selector.lower()
    return str(point.get("inventory_id") or "") == selector or str(point.get("inventory_name") or "").lower() == lowered


def list_restore_points(*, vm: str | None = None, backup_id: str | None = None, inventory: str | None = None) -> list[dict[str, Any]]:
    vm_selector = str(vm or "").strip()
    backup_selector = str(backup_id or "").strip()
    inventory_selector = str(inventory or "").strip()
    points = normalized_backup_points()
    if vm_selector:
        points = [point for point in points if _restore_point_matches_vm(point, vm_selector)]
    if backup_selector:
        points = [point for point in points if _restore_point_matches_backup(point, backup_selector)]
    if inventory_selector:
        points = [point for point in points if _restore_point_matches_inventory(point, inventory_selector)]
    points.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    return [restore_point_to_dto(point) for point in points]


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _capacity_bytes_from_disk_entry(disk: dict[str, Any]) -> int | None:
    direct = _safe_int(disk.get("capacity_bytes"))
    if direct is not None and direct >= 0:
        return direct

    # "capacity_gb" is vCenter's field name; "size_gb" is what the generic /
    # simulated hypervisor discovery uses for the same value.
    capacity_gb = disk.get("capacity_gb", disk.get("size_gb"))
    if capacity_gb not in (None, ""):
        try:
            return int(float(capacity_gb) * 1024 * 1024 * 1024)
        except (TypeError, ValueError):
            return None

    capacity_mb = disk.get("capacity_mb")
    if capacity_mb not in (None, ""):
        try:
            return int(float(capacity_mb) * 1024 * 1024)
        except (TypeError, ValueError):
            return None
    return None


def _parse_datastore_path_parts(datastore_path: str | None) -> tuple[str | None, str | None, str | None]:
    raw = str(datastore_path or "").strip()
    if not raw.startswith("[") or "]" not in raw:
        return None, None, None
    datastore, relative_path = raw.split("]", 1)
    datastore_name = datastore[1:].strip() or None
    relative = relative_path.strip()
    if not relative:
        return datastore_name, None, None
    folder = str(Path(relative).parent)
    if folder == ".":
        folder = ""
    return datastore_name, folder or None, Path(relative).name or None


def _load_restore_manifest_record(point: dict[str, Any]) -> dict[str, Any]:
    artifact = _restore_point_artifact(point)
    manifest_path = str(artifact.get("manifest_path") or "").strip()
    if not manifest_path:
        return {}

    path = Path(manifest_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        repository = REPOSITORIES.get(str(point.get("repository_id") or ""))
        abr_path = next(
            (
                str(disk.get("abr_path") or "")
                for disk in artifact.get("disks", [])
                if isinstance(disk, dict) and disk.get("abr_path")
            ),
            "",
        )
        if isinstance(repository, dict) and abr_path and hydrate_manifest_via_disk_tool(repository, abr_path, path):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                LOGGER.warning("Recovered restore manifest for point %s is not valid JSON", point.get("id"))
                return {}
        else:
            LOGGER.debug("Restore manifest not found for point %s at %s; falling back to discovered metadata", point.get("id"), path)
            return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.warning("Unable to load restore manifest for point %s from %s", point.get("id"), path)
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _inventory_resources_by_type(inventory_id: str, resource_type: str) -> list[dict[str, Any]]:
    resources = []
    for resource in RESOURCES_BY_HV.get(inventory_id, {}).values():
        if str(resource.get("resource_type") or "") == resource_type:
            resources.append(resource)
    resources.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("external_id") or "")))
    return resources


def _find_inventory_resource(
    inventory_id: str,
    resource_type: str,
    *,
    external_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any] | None:
    external_selector = str(external_id or "").strip()
    name_selector = str(name or "").strip().lower()
    for resource in _inventory_resources_by_type(inventory_id, resource_type):
        if external_selector and str(resource.get("external_id") or "") == external_selector:
            return resource
        if name_selector and str(resource.get("name") or "").strip().lower() == name_selector:
            return resource
    return None


def _source_disk_id(vm_id: str | None, index: int, disk: dict[str, Any], discovered: dict[str, Any] | None = None) -> str:
    for value in (
        disk.get("id"),
        disk.get("uuid"),
        disk.get("disk_id"),
        discovered.get("external_id") if isinstance(discovered, dict) else None,
    ):
        token = str(value or "").strip()
        if token:
            return token
    return f"{str(vm_id or 'vm').strip() or 'vm'}::disk::{index}"


def _source_nic_id(vm_id: str | None, index: int, adapter: dict[str, Any], discovered: dict[str, Any] | None = None) -> str:
    for value in (
        adapter.get("id"),
        adapter.get("nic_id"),
        discovered.get("external_id") if isinstance(discovered, dict) else None,
    ):
        token = str(value or "").strip()
        if token:
            return token
    return f"{str(vm_id or 'vm').strip() or 'vm'}::nic::{index}"


def _source_network_fallbacks(manifest_vm_config: dict[str, Any], discovered_nics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest_adapters = manifest_vm_config.get("network_adapters") if isinstance(manifest_vm_config.get("network_adapters"), list) else []
    nic_count = max(len(manifest_adapters), len(discovered_nics))
    adapters = []
    for index in range(nic_count):
        manifest_adapter = manifest_adapters[index] if index < len(manifest_adapters) and isinstance(manifest_adapters[index], dict) else {}
        discovered = discovered_nics[index] if index < len(discovered_nics) else None
        payload = discovered.get("payload") if isinstance(discovered, dict) and isinstance(discovered.get("payload"), dict) else {}
        adapters.append(
            {
                "id": _source_nic_id(manifest_vm_config.get("instance_uuid") or manifest_vm_config.get("name"), index + 1, manifest_adapter, discovered),
                "label": str(manifest_adapter.get("label") or (discovered or {}).get("name") or f"Network adapter {index + 1}"),
                "source_network": str(manifest_adapter.get("network") or payload.get("network") or "").strip() or None,
                "mac_address": str(manifest_adapter.get("mac_address") or payload.get("mac") or "").strip() or None,
                "connected": bool(manifest_adapter.get("connected", payload.get("connected", True))),
            }
        )
    return adapters


def _build_restore_source_vm(point: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    inventory_id = str(point.get("inventory_id") or "").strip()
    vm_id = str(manifest.get("vm_external_id") or point.get("vm_id") or "").strip() or None
    vm_name = str(manifest.get("vm_name") or point.get("vm_name") or "").strip() or "Restored VM"
    manifest_vm_config = manifest.get("vm_config") if isinstance(manifest.get("vm_config"), dict) else {}

    vm_resource = None
    if inventory_id:
        vm_resource = _find_inventory_resource(inventory_id, "virtual_machine", external_id=vm_id, name=vm_name)
    vm_payload = vm_resource.get("payload") if isinstance(vm_resource, dict) and isinstance(vm_resource.get("payload"), dict) else {}

    disk_resources = []
    nic_resources = []
    if inventory_id and vm_id:
        disk_resources = [
            resource
            for resource in _inventory_resources_by_type(inventory_id, "disk")
            if isinstance(resource.get("payload"), dict) and str(resource["payload"].get("vm_id") or "") == vm_id
        ]
        nic_resources = [
            resource
            for resource in _inventory_resources_by_type(inventory_id, "network_adapter")
            if isinstance(resource.get("payload"), dict) and str(resource["payload"].get("vm_id") or "") == vm_id
        ]

    manifest_disks = manifest.get("disks") if isinstance(manifest.get("disks"), list) else []
    artifact_disks = _restore_point_artifact(point).get("disks") if isinstance(_restore_point_artifact(point).get("disks"), list) else []
    disk_entries = [entry for entry in manifest_disks if isinstance(entry, dict)] or [entry for entry in artifact_disks if isinstance(entry, dict)]

    disks = []
    original_datastore = None
    original_folder = None
    for index, disk_entry in enumerate(disk_entries, start=1):
        datastore_path = (
            str(disk_entry.get("datastore_path") or "").strip()
            or str(disk_entry.get("current_datastore_path") or "").strip()
            or None
        )
        discovered = None
        for candidate in disk_resources:
            payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
            if str(disk_entry.get("uuid") or "").strip() and str(payload.get("uuid") or "").strip() == str(disk_entry.get("uuid") or "").strip():
                discovered = candidate
                break
            if datastore_path and str(payload.get("datastore_path") or "").strip() == datastore_path:
                discovered = candidate
                break
        discovered_payload = discovered.get("payload") if isinstance(discovered, dict) and isinstance(discovered.get("payload"), dict) else {}
        effective_path = datastore_path or str(discovered_payload.get("datastore_path") or "").strip() or None
        datastore_name, folder_name, _filename = _parse_datastore_path_parts(effective_path)
        if original_datastore is None and datastore_name:
            original_datastore = datastore_name
        if original_folder is None and folder_name:
            original_folder = folder_name
        disks.append(
            {
                "id": _source_disk_id(vm_id, index, disk_entry, discovered),
                "label": str(disk_entry.get("disk_label") or (discovered or {}).get("name") or f"Hard disk {index}"),
                "capacity_bytes": _capacity_bytes_from_disk_entry(disk_entry) or _capacity_bytes_from_disk_entry(discovered_payload),
                "provisioning": str(disk_entry.get("source_disk_type_name") or discovered_payload.get("source_disk_type_name") or "").strip() or None,
                "controller_type": str(disk_entry.get("controller_type") or discovered_payload.get("bus") or "scsi").strip() or None,
                "datastore_path": effective_path,
            }
        )

    adapters = _source_network_fallbacks(manifest_vm_config, nic_resources)
    host_name = str(manifest_vm_config.get("source_host") or "").strip() or None
    if host_name is None and isinstance(vm_payload, dict):
        host_id = str(vm_payload.get("host_external_id") or "").strip()
        if inventory_id and host_id:
            host_resource = _find_inventory_resource(inventory_id, "host", external_id=host_id, name=host_id)
            host_name = str((host_resource or {}).get("name") or host_id).strip() or None

    firmware = str(manifest_vm_config.get("firmware") or vm_payload.get("firmware") or "").strip().lower() or None
    if firmware not in {None, "bios", "efi"}:
        firmware = None

    return {
        "id": vm_id,
        "instance_uuid": str(manifest_vm_config.get("instance_uuid") or "").strip() or None,
        "name": vm_name,
        "guest_id": str(manifest_vm_config.get("guest_id") or "").strip() or None,
        "guest_os": str(manifest_vm_config.get("guest_os") or vm_payload.get("guest_os") or "").strip() or None,
        "firmware": firmware,
        "cpu_count": _safe_int(manifest_vm_config.get("num_cpu")) or _safe_int(vm_payload.get("num_cpu")) or _safe_int(vm_payload.get("vcpus")),
        "memory_mb": _safe_int(manifest_vm_config.get("memory_mb")) or _safe_int(vm_payload.get("memory_mb")),
        "original_host": host_name,
        "original_datastore": original_datastore,
        "folder": str(manifest_vm_config.get("folder") or vm_payload.get("folder") or original_folder or "").strip() or None,
        "resource_pool": str(manifest_vm_config.get("resource_pool") or vm_payload.get("resource_pool") or "").strip() or None,
        "disks": disks,
        "nics": adapters,
    }


def _restore_target_datastore_id(inventory_id: str, datastore_name: str) -> str:
    return f"{inventory_id}::datastore::{datastore_name}"


def _restore_target_network_id(inventory_id: str, network_name: str) -> str:
    return f"{inventory_id}::network::{network_name}"


def _build_restore_target_hosts(inventory_id: str) -> list[dict[str, Any]]:
    hosts = []
    inventory = HYPERVISORS.get(inventory_id, {})
    inventory_status = str(inventory.get("status") or "unknown").strip().lower()
    default_status = "connected" if inventory_status in {"available", "ok", "connected", "unknown"} else inventory_status or "unknown"
    for resource in _inventory_host_records(inventory_id):
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        host_id = str(resource.get("external_id") or resource.get("name") or "").strip()
        if not host_id:
            continue
        hosts.append(
            {
                "id": host_id,
                "inventory_id": inventory_id,
                "name": str(resource.get("name") or host_id),
                "status": str(payload.get("status") or default_status or "unknown"),
                "datastore_ids": [
                    _restore_target_datastore_id(inventory_id, str(item))
                    for item in (payload.get("datastores") if isinstance(payload.get("datastores"), list) else [])
                    if str(item).strip()
                ],
                "network_ids": [
                    _restore_target_network_id(inventory_id, str(item))
                    for item in (payload.get("networks") if isinstance(payload.get("networks"), list) else [])
                    if str(item).strip()
                ],
            }
        )
    return hosts


def _build_restore_target_datastores(inventory_id: str) -> list[dict[str, Any]]:
    # Walks host *records* directly (same source `_build_restore_target_hosts`
    # reads) rather than that function's already-flattened output, so it can
    # reach each host's `datastore_capacity` payload — capacity is per
    # datastore, not per host, so it's merged in here rather than carried on
    # the hosts DTO.
    datastore_index: dict[str, dict[str, Any]] = {}
    for resource in _inventory_host_records(inventory_id):
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        host_id = str(resource.get("external_id") or resource.get("name") or "").strip()
        if not host_id:
            continue
        capacity_by_name = payload.get("datastore_capacity") if isinstance(payload.get("datastore_capacity"), dict) else {}
        for name in (payload.get("datastores") if isinstance(payload.get("datastores"), list) else []):
            name = str(name).strip()
            if not name:
                continue
            datastore_id = _restore_target_datastore_id(inventory_id, name)
            entry = datastore_index.setdefault(
                datastore_id,
                {
                    "id": datastore_id,
                    "inventory_id": inventory_id,
                    "name": name,
                    "free_bytes": None,
                    "capacity_bytes": None,
                    "host_ids": [],
                },
            )
            if host_id not in entry["host_ids"]:
                entry["host_ids"].append(host_id)
            capacity = capacity_by_name.get(name) if isinstance(capacity_by_name, dict) else None
            if isinstance(capacity, dict):
                if entry["capacity_bytes"] is None and capacity.get("capacity_bytes") is not None:
                    entry["capacity_bytes"] = _safe_int(capacity.get("capacity_bytes"))
                if capacity.get("free_bytes") is not None:
                    # A shared datastore's free space can drift slightly between
                    # hosts' cached views of it — take the most recently seen
                    # host's figure rather than freezing on the first.
                    entry["free_bytes"] = _safe_int(capacity.get("free_bytes"))
    return sorted(datastore_index.values(), key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))


def _build_restore_target_networks(inventory_id: str, *, fallback_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Networks a restored VM's NICs can land on, each scoped to the hosts that
    actually expose it.

    ``host_ids`` comes from host discovery (a standard virtual switch, and so its
    port groups, belongs to one host). Names only known from VM NICs or from the
    source VM's own adapters get an empty ``host_ids``: their scope is unknown,
    and callers read that as "offer everywhere" rather than hiding them — an
    inventory discovered before per-host networks were collected must keep
    working instead of presenting an empty list.
    """
    network_index: dict[str, dict[str, Any]] = {}

    def _entry(name: str) -> dict[str, Any]:
        network_id = _restore_target_network_id(inventory_id, name)
        return network_index.setdefault(
            network_id,
            {
                "id": network_id,
                "inventory_id": inventory_id,
                "name": name,
                "host_ids": [],
            },
        )

    for host in _build_restore_target_hosts(inventory_id):
        for network_id in host.get("network_ids", []):
            network_name = network_id.split("::network::", 1)[1] if "::network::" in network_id else network_id
            entry = _entry(network_name)
            if host["id"] not in entry["host_ids"]:
                entry["host_ids"].append(host["id"])

    for resource in _inventory_resources_by_type(inventory_id, "network_adapter"):
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        network_name = str(payload.get("network") or "").strip()
        if network_name:
            _entry(network_name)
    for fallback_name in fallback_names or []:
        if str(fallback_name).strip():
            _entry(str(fallback_name).strip())

    return sorted(network_index.values(), key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))


def _restore_network_available_on_host(network: dict[str, Any], host_id: str) -> bool:
    """Whether a target network can be used on a given host. A network with no
    known host scope stays available on every host (see
    :func:`_build_restore_target_networks`)."""
    host_ids = network.get("host_ids") if isinstance(network.get("host_ids"), list) else []
    return not host_ids or not host_id or host_id in host_ids


def _restore_host_identifier(host: dict[str, Any] | None) -> str:
    if not isinstance(host, dict):
        return ""
    return str(host.get("external_id") or host.get("id") or host.get("name") or "").strip()


def _build_restore_existing_vms(inventory_id: str) -> list[dict[str, Any]]:
    items = []
    for resource in _resolve_inventory_vm_records(inventory_id):
        vm_id = str(resource.get("external_id") or "").strip()
        vm_name = str(resource.get("name") or "").strip()
        if not vm_name:
            continue
        items.append(
            {
                "id": vm_id or vm_name,
                "inventory_id": inventory_id,
                "name": vm_name,
            }
        )
    return items


def _default_target_vm_name(inventory_id: str | None, source_vm_name: str, existing_vms: list[dict[str, Any]]) -> str:
    base_name = str(source_vm_name or "").strip() or "restored-vm"
    preferred = f"{base_name}-restored" if not base_name.endswith("-restored") else base_name
    relevant_names = {
        str(item.get("name") or "").strip().lower()
        for item in existing_vms
        if inventory_id is None or str(item.get("inventory_id") or "") == inventory_id
    }
    if preferred.lower() not in relevant_names:
        return preferred
    suffix = 2
    while f"{preferred}-{suffix}".lower() in relevant_names:
        suffix += 1
    return f"{preferred}-{suffix}"


def get_restore_context(restore_point_id: str) -> dict[str, Any]:
    point = _resolve_restore_source_point({"point_id": restore_point_id})
    manifest = _load_restore_manifest_record(point)
    source_vm = _build_restore_source_vm(point, manifest)
    inventories = list_inventory_records()
    target_inventories = [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "type": str(item.get("type") or ""),
            "kind": str(item.get("kind") or ""),
            "status": str(item.get("status") or "unknown"),
        }
        for item in inventories
        if str(item.get("id") or "").strip()
    ]

    source_network_names = [str(item.get("source_network") or "").strip() for item in source_vm.get("nics", []) if str(item.get("source_network") or "").strip()]
    hosts = []
    datastores = []
    networks = []
    existing_vms = []
    for inventory in target_inventories:
        inventory_id = str(inventory["id"])
        hosts.extend(_build_restore_target_hosts(inventory_id))
        datastores.extend(_build_restore_target_datastores(inventory_id))
        networks.extend(
            _build_restore_target_networks(
                inventory_id,
                fallback_names=source_network_names if inventory_id == str(point.get("inventory_id") or "") else None,
            )
        )
        existing_vms.extend(_build_restore_existing_vms(inventory_id))

    default_inventory_id = next(
        (
            inventory["id"]
            for inventory in target_inventories
            if inventory["id"] == str(point.get("inventory_id") or "")
        ),
        target_inventories[0]["id"] if target_inventories else None,
    )
    default_hosts = [item for item in hosts if item.get("inventory_id") == default_inventory_id]
    default_host_id = next(
        (
            item["id"]
            for item in default_hosts
            if str(item.get("name") or "").strip().lower() == str(source_vm.get("original_host") or "").strip().lower()
        ),
        default_hosts[0]["id"] if default_hosts else None,
    )
    default_datastores = [item for item in datastores if item.get("inventory_id") == default_inventory_id]
    default_datastore_id = next(
        (
            item["id"]
            for item in default_datastores
            if item.get("name") == source_vm.get("original_datastore") and (default_host_id is None or default_host_id in item.get("host_ids", []))
        ),
        next(
            (
                item["id"]
                for item in default_datastores
                if default_host_id is None or default_host_id in item.get("host_ids", [])
            ),
            default_datastores[0]["id"] if default_datastores else None,
        ),
    )
    # Only networks the default host can actually reach may be auto-paired —
    # pre-filling a port group that lives on another host's switch would hand the
    # user a mapping that fails at restore time.
    default_networks = [
        item
        for item in networks
        if item.get("inventory_id") == default_inventory_id
        and (default_host_id is None or _restore_network_available_on_host(item, default_host_id))
    ]
    default_network_index = {str(item.get("name") or ""): str(item.get("id") or "") for item in default_networks}
    default_network_mappings = []
    for nic in source_vm.get("nics", []):
        source_network = str(nic.get("source_network") or "").strip()
        default_network_mappings.append(
            {
                "source_nic_id": nic.get("id"),
                "target_network_id": default_network_index.get(source_network) or None,
            }
        )

    source_firmware = str(source_vm.get("firmware") or "").strip().lower()
    supports_disk_provisioning = any(
        str(disk.get("provisioning") or "").strip()
        for disk in source_vm.get("disks", [])
    )
    # In-place restore replaces the still-live source VM (create a fully
    # verified replacement at its original host/datastore, then retire the
    # original and rename the replacement into place) — it only makes sense
    # when that original placement is actually known.
    supports_in_place_restore = bool(source_vm.get("original_host")) and bool(source_vm.get("original_datastore"))

    return {
        "restore_point": {
            "id": str(point.get("id") or ""),
            "backup_id": str(point.get("chain_id") or "") or None,
            "timestamp": point.get("created_at"),
            "type": _restore_point_type(point),
            "health": _normalize_backup_health(point.get("status")),
            "status": point.get("status"),
            "available": _restore_point_is_available(point),
            "repository_id": point.get("repository_id"),
            "repository": point.get("repository_name"),
            "repository_type": point.get("repository_type"),
            "inventory_id": point.get("inventory_id"),
            "inventory": point.get("inventory_name"),
            "job_id": point.get("job_id"),
            "job_name": point.get("job_name"),
        },
        "source_vm": source_vm,
        "targets": {
            "inventories": target_inventories,
            "hosts": hosts,
            "datastores": datastores,
            "networks": networks,
            "folders": [],
            "resource_pools": [],
            "existing_vms": existing_vms,
        },
        "defaults": {
            "restore_mode": "new_vm",
            "target_inventory_id": default_inventory_id,
            "target_host_id": default_host_id,
            "target_datastore_id": default_datastore_id,
            "target_vm_name": _default_target_vm_name(default_inventory_id, str(source_vm.get("name") or ""), existing_vms),
            "power_on_after_restore": False,
            "preserve_mac_addresses": False,
            "preserve_firmware": bool(source_firmware in {"bios", "efi"}),
            "connect_nics": True,
            "disk_provisioning": "original" if supports_disk_provisioning else None,
            "network_mappings": default_network_mappings,
        },
        "capabilities": {
            "supports_in_place_restore": supports_in_place_restore,
            "supports_network_mapping": bool(source_vm.get("nics")),
            "supports_preserve_mac": False,
            "supports_power_on_after_restore": True,
            "supports_folder_selection": False,
            "supports_resource_pool_selection": False,
            "supports_connect_nics": True,
            "supports_disk_provisioning": supports_disk_provisioning,
        },
    }


def _resolve_restore_target_datastore(inventory_id: str, datastore_identifier: str, *, host: dict[str, Any]) -> dict[str, Any]:
    identifier = str(datastore_identifier or "").strip()
    _ensure(bool(identifier), 422, "invalid_restore", "Restore target datastore is required")
    for datastore in _build_restore_target_datastores(inventory_id):
        datastore_id = str(datastore.get("id") or "")
        datastore_name = str(datastore.get("name") or "")
        if identifier == datastore_id or identifier.lower() == datastore_name.lower():
            _ensure(
                str(host.get("external_id") or host.get("name") or "") in set(datastore.get("host_ids") or []),
                404,
                "datastore_not_found",
                "Target datastore is not available on the selected host",
                host=host.get("name"),
                datastore=identifier,
            )
            return datastore
    raise api_error(
        404,
        "datastore_not_found",
        "Target datastore not found in inventory",
        inventory_id=inventory_id,
        datastore=identifier,
    )


def _ensure_restore_target_capacity(source_vm: dict[str, Any], datastore: dict[str, Any], restore_mode: str) -> None:
    """Hard-reject only when we KNOW the datastore can't fit the restore —
    unknown capacity (a datastore whose free_bytes hasn't been reported, or a
    source disk whose capacity wasn't recorded) is never enough evidence to
    block, since thin-provisioned disks routinely use far less than their
    declared capacity. In-place restore needs a full second copy of the VM's
    disks to coexist with the still-live original until the swap completes,
    so it's checked against 2x the provisioned size rather than 1x — free_bytes
    already excludes the original's own footprint, so this is a deliberate
    safety margin (thin-disk growth during write, buffer before the original
    is destroyed), not a strict minimum.
    """
    disks = source_vm.get("disks") if isinstance(source_vm.get("disks"), list) else []
    capacities = [_safe_int(disk.get("capacity_bytes")) for disk in disks if isinstance(disk, dict)]
    required = sum(capacities) if capacities and all(c is not None for c in capacities) else None
    required_total = None if required is None else required * (2 if restore_mode == "in_place" else 1)
    free_bytes = _safe_int(datastore.get("free_bytes"))
    _ensure(
        required_total is None or free_bytes is None or free_bytes >= required_total,
        409,
        "insufficient_datastore_space",
        "Target datastore does not have enough free space for this restore"
        + (
            " — an in-place restore needs room for a full second copy of the VM's disks "
            "while the original stays live during the swap"
            if restore_mode == "in_place"
            else ""
        ),
        datastore=datastore.get("name"),
        free_bytes=free_bytes,
        required_bytes=required_total,
    )


def _resolve_restore_target_network(
    inventory_id: str,
    network_identifier: str,
    *,
    source_networks: list[str],
    host: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identifier = str(network_identifier or "").strip()
    if not identifier:
        raise api_error(422, "invalid_restore", "Target network is required")
    networks = _build_restore_target_networks(inventory_id, fallback_names=source_networks)
    host_id = _restore_host_identifier(host)
    for network in networks:
        network_id = str(network.get("id") or "")
        network_name = str(network.get("name") or "")
        if identifier == network_id or identifier.lower() == network_name.lower():
            _ensure(
                _restore_network_available_on_host(network, host_id),
                404,
                "network_not_found",
                "Target network is not available on the selected host",
                host=(host or {}).get("name"),
                network=identifier,
            )
            return network
    raise api_error(
        404,
        "network_not_found",
        "Target network not found in inventory",
        inventory_id=inventory_id,
        network=identifier,
    )


def _normalize_restore_mode(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"new_vm", "new-vm", "new"}:
        return "new_vm"
    if normalized in {"in_place", "in-place", "overwrite"}:
        return "in_place"
    raise api_error(
        422,
        "invalid_restore",
        "Restore mode must be one of: new_vm, in_place",
        restore_mode=value,
    )


def _normalize_restore_request_payload(payload: dict[str, Any], point: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    network_mappings = payload.get("network_mappings") if isinstance(payload.get("network_mappings"), list) else []

    restore_mode = _normalize_restore_mode(payload.get("restore_mode"))
    target_inventory_id = str(target.get("inventory_id") or payload.get("target_inventory_id") or "").strip() or None
    target_inventory_name = str(target.get("inventory") or payload.get("target_inventory") or "").strip() or None
    target_host_identifier = str(target.get("host_id") or payload.get("target_host_id") or payload.get("target_host") or "").strip() or None
    target_datastore_identifier = (
        str(target.get("datastore_id") or payload.get("target_datastore_id") or payload.get("target_datastore") or "").strip() or None
    )
    target_vm_name = str(target.get("vm_name") or payload.get("target_vm_name") or payload.get("name") or "").strip() or None
    repository_id = str(payload.get("repository_id") or payload.get("repo_id") or "").strip() or None
    repository_name = str(payload.get("repository") or payload.get("repo") or "").strip() or None
    backup_id = str(payload.get("backup_id") or payload.get("backup") or "").strip() or None
    source_inventory_id = str(payload.get("inventory_id") or "").strip() or None
    force = bool(payload.get("force", False))

    has_explicit_target_block = bool(target)
    has_legacy_target_values = any([target_inventory_id, target_inventory_name, target_host_identifier, target_datastore_identifier, target_vm_name])
    if restore_mode is None:
        restore_mode = "new_vm" if has_explicit_target_block or has_legacy_target_values or not force else "in_place"

    return {
        "restore_point_id": str(payload.get("restore_point_id") or payload.get("point_id") or point.get("id") or "").strip(),
        "backup_id": backup_id,
        "repository_id": repository_id,
        "repository_name": repository_name,
        "source_inventory_id": source_inventory_id,
        "restore_mode": restore_mode,
        "target_inventory_id": target_inventory_id,
        "target_inventory_name": target_inventory_name,
        "target_host_identifier": target_host_identifier,
        "target_datastore_identifier": target_datastore_identifier,
        "target_vm_name": target_vm_name,
        "target_folder_id": str(target.get("folder_id") or "").strip() or None,
        "target_resource_pool_id": str(target.get("resource_pool_id") or "").strip() or None,
        "power_on_after_restore": bool(options.get("power_on_after_restore", payload.get("power_on", payload.get("power_on_after_restore", False)))),
        "preserve_mac_addresses": bool(options.get("preserve_mac_addresses", payload.get("preserve_mac_addresses", False))),
        "preserve_firmware": options.get("preserve_firmware") if "preserve_firmware" in options else payload.get("preserve_firmware"),
        "connect_nics": bool(options.get("connect_nics", payload.get("connect_nics", True))),
        "disk_provisioning": str(options.get("disk_provisioning") or payload.get("disk_provisioning") or payload.get("disk_type") or "").strip() or None,
        "network_mappings": [item for item in network_mappings if isinstance(item, dict)],
        "legacy_network_map": payload.get("network_map"),
        "force": force,
    }


def _validate_restore_request_matches_point(payload: dict[str, Any], point: dict[str, Any]) -> None:
    restore_point_id = str(payload.get("restore_point_id") or "").strip()
    backup_id = str(payload.get("backup_id") or "").strip()
    source_inventory_id = str(payload.get("source_inventory_id") or "").strip()

    if restore_point_id and restore_point_id != str(point.get("id") or ""):
        raise api_error(
            409,
            "restore_point_mismatch",
            "Restore point selector does not match the selected restore point",
            requested_restore_point_id=restore_point_id,
            restore_point_id=point.get("id"),
        )
    if backup_id and backup_id != str(point.get("chain_id") or ""):
        raise api_error(
            409,
            "backup_mismatch",
            "Restore point does not belong to the selected backup chain",
            requested_backup_id=backup_id,
            backup_id=point.get("chain_id"),
        )
    if source_inventory_id and source_inventory_id != str(point.get("inventory_id") or ""):
        raise api_error(
            409,
            "inventory_mismatch",
            "Restore point does not belong to the selected inventory",
            requested_inventory_id=source_inventory_id,
            inventory_id=point.get("inventory_id"),
        )


def _resolve_restore_network_selection(
    *,
    point: dict[str, Any],
    manifest: dict[str, Any],
    target_inventory_id: str,
    mappings: list[dict[str, Any]],
    legacy_network_map: Any,
    host: dict[str, Any] | None = None,
) -> dict[str, str]:
    source_vm = _build_restore_source_vm(point, manifest)
    source_nics = [item for item in source_vm.get("nics", []) if isinstance(item, dict)]
    if not source_nics:
        return _parse_restore_network_map(legacy_network_map)

    source_networks = [str(item.get("source_network") or "").strip() for item in source_nics if str(item.get("source_network") or "").strip()]
    # A port group on another host's virtual switch is not a valid destination
    # for this restore, so scope every lookup below to the selected host.
    host_id = _restore_host_identifier(host)
    all_networks = _build_restore_target_networks(target_inventory_id, fallback_names=source_networks)
    target_networks = [item for item in all_networks if _restore_network_available_on_host(item, host_id)]
    off_host_ids = {str(item.get("id") or "") for item in all_networks} - {str(item.get("id") or "") for item in target_networks}
    off_host_names = {str(item.get("name") or "") for item in all_networks} - {str(item.get("name") or "") for item in target_networks}
    target_network_ids = {str(item.get("id") or ""): item for item in target_networks}
    target_network_names = {str(item.get("name") or ""): item for item in target_networks}

    if mappings:
        resolved: dict[str, str] = {}
        source_index = {str(item.get("id") or ""): item for item in source_nics}
        for entry in mappings:
            source_nic_id = str(entry.get("source_nic_id") or "").strip()
            target_network_id = str(entry.get("target_network_id") or "").strip()
            if not source_nic_id and not target_network_id:
                continue
            _ensure(bool(source_nic_id and target_network_id), 422, "invalid_restore", "Each network mapping requires source_nic_id and target_network_id")
            source_nic = source_index.get(source_nic_id)
            _ensure(source_nic is not None, 404, "network_adapter_not_found", "Source network adapter not found", source_nic_id=source_nic_id)
            source_network = str(source_nic.get("source_network") or "").strip()
            _ensure(bool(source_network), 422, "invalid_restore", "Source network adapter is missing a source network", source_nic_id=source_nic_id)
            target_network = target_network_ids.get(target_network_id)
            _ensure(
                target_network is not None or target_network_id not in off_host_ids,
                404,
                "network_not_found",
                "Target network is not available on the selected host",
                target_network_id=target_network_id,
                host=(host or {}).get("name"),
                inventory_id=target_inventory_id,
            )
            _ensure(target_network is not None, 404, "network_not_found", "Target network not found in inventory", target_network_id=target_network_id, inventory_id=target_inventory_id)
            resolved[source_network] = str(target_network.get("name") or "")
        missing = [
            str(item.get("label") or item.get("id") or "network adapter")
            for item in source_nics
            if str(item.get("source_network") or "").strip() and str(item.get("source_network") or "").strip() not in resolved
        ]
        _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for all source adapters", adapters=missing)
        return resolved

    legacy = _parse_restore_network_map(legacy_network_map)
    if legacy:
        source_network_set = set(source_networks)
        for source_network, target_network_name in legacy.items():
            _ensure(
                source_network in source_network_set,
                404,
                "network_adapter_not_found",
                "Source network not found on this VM",
                source_network=source_network,
            )
            if target_networks and target_network_name not in target_network_names:
                raise api_error(
                    404,
                    "network_not_found",
                    (
                        "Target network is not available on the selected host"
                        if target_network_name in off_host_names
                        else "Target network not found in inventory"
                    ),
                    inventory_id=target_inventory_id,
                    host=(host or {}).get("name"),
                    network=target_network_name,
                )
        missing = [source_network for source_network in source_networks if source_network not in legacy]
        _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for all source adapters", networks=missing)
        return legacy

    automatic = {
        source_network: source_network
        for source_network in source_networks
        if source_network in target_network_names
    }
    missing = [source_network for source_network in source_networks if source_network and source_network not in automatic]
    _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for the selected restore target", networks=missing)
    return automatic


def _parse_restore_network_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        mappings: dict[str, str] = {}
        for source, target in value.items():
            source_name = str(source or "").strip()
            target_name = str(target or "").strip()
            if source_name and target_name:
                mappings[source_name] = target_name
        return mappings
    if not value:
        return {}
    if not isinstance(value, list):
        raise api_error(
            422,
            "invalid_restore",
            "Each --network-map value must use the form <source>=<target> or <source>:<target>",
        )
    mappings: dict[str, str] = {}
    for raw in value:
        text = str(raw or "")
        left, separator, right = text.partition("=")
        if separator != "=":
            left, separator, right = text.partition(":")
        source_name = left.strip()
        target_name = right.strip()
        _ensure(
            separator in {"=", ":"} and bool(source_name) and bool(target_name),
            422,
            "invalid_restore",
            "Each --network-map value must use the form <source>=<target> or <source>:<target>",
            value=text,
        )
        mappings[source_name] = target_name
    return mappings


def restore_vm_list(*, inventory_name: str | None = None) -> list[dict[str, Any]]:
    points = normalized_backup_points()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        grouped[str(point.get("vm_id") or point.get("vm_name") or "")].append(point)
    items = []
    for vm_id, bucket in grouped.items():
        bucket.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        latest = bucket[-1]
        items.append(
            {
                "vm_id": vm_id,
                "vm_name": latest.get("vm_name"),
                "inventory": {
                    "id": latest.get("inventory_id"),
                    "name": latest.get("inventory_name"),
                },
                "restore_available": True,
                "latest_restore_point": {
                    "id": latest.get("id"),
                    "created_at": latest.get("created_at"),
                    "repository": {
                        "id": latest.get("repository_id"),
                        "name": latest.get("repository_name"),
                    },
                },
            }
        )
    if inventory_name:
        lowered = inventory_name.lower()
        items = [item for item in items if isinstance(item.get("inventory"), dict) and str(item["inventory"].get("name") or "").lower() == lowered]
    items.sort(key=lambda item: (str(item["latest_restore_point"]["created_at"]), str(item["vm_id"])), reverse=True)
    return items


def restore_point_list(vm_id: str) -> list[dict[str, Any]]:
    _ensure(bool(vm_id), 422, "invalid_restore", "restore point list requires --vm-id")
    points = [point for point in normalized_backup_points() if str(point.get("vm_id") or "") == vm_id]
    if not points:
        raise api_error(404, "restore_points_not_found", "Restore points not found for VM", vm_id=vm_id)
    points.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    return [restore_point_to_dto(point) for point in points]


def _create_completed_copy_session(job: dict[str, Any]) -> dict[str, Any]:
    source_job_id = str(job.get("source_job_id") or "")
    source_points = [point for point in normalized_backup_points() if str(point.get("job_id") or "") == source_job_id]
    session = {
        "id": str(uuid4()),
        "job_id": job["id"],
        "task_type": "backup",
        "hypervisor_id": job.get("inventory_id"),
        "resource_type": "virtual_machine",
        "resource_external_id": source_points[-1]["vm_id"] if source_points else source_job_id,
        "status": "completed",
        "trigger": "manual",
        "parameters": {
            "copy_job": True,
            "target_repository_id": job.get("repo_id"),
            "target_repository_name": job.get("repo_name"),
        },
        "result": {
            "message": "Backup copy session completed",
            "finished_at": utc_now(),
            "progress": 100,
            "resolved_backup_type": "ActiveFull",
            "resolved_backup_reason": "ForceFull",
        },
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "logs": [],
    }
    _append_task_log(session, level="info", message="Backup copy session completed", component="copy")
    persist_task(session)
    return session_to_dto(session)


def _create_backup_session(job: dict[str, Any], *, vm_id: str, vm_name: str, execution_type: str, execution_reason: str) -> dict[str, Any]:
    task_parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else {}
    parameters = dict(task_parameters)
    parameters.update(
        {
            "repository_id": job.get("repo_id"),
            "repository_name": job.get("repo_name"),
            "vm_name": vm_name,
            "resolved_backup_type": execution_type,
            "resolved_backup_reason": execution_reason,
            "backup_mode_request": "full" if execution_type in {"ActiveFull", "SyntheticFull"} else "auto",
            "incremental": execution_type == "Incremental",
            "synthetic_full": execution_type == "SyntheticFull",
            "active_full": execution_type == "ActiveFull",
        }
    )
    task = {
        "id": str(uuid4()),
        "job_id": job["id"],
        "task_type": "backup",
        "hypervisor_id": job.get("inventory_id"),
        "resource_type": "virtual_machine",
        "resource_external_id": vm_id,
        "status": "queued",
        "trigger": "manual",
        "parameters": parameters,
        "result": {
            "progress": 0,
            "resolved_backup_type": execution_type,
            "resolved_backup_reason": execution_reason,
        },
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "logs": [],
    }
    _append_task_log(task, level="info", message=f"Queued backup session for {vm_name}", component="session")
    persist_task(task)
    return session_to_dto(task)


def start_job_record(job_id: str, *, force_full: bool = False) -> dict[str, Any]:
    job = resolve_job(record_id=job_id)
    sessions = []
    if job["job_type"] == "backup-copy":
        sessions.append(_create_completed_copy_session(job))
    else:
        for vm_id, vm_name in zip(job.get("vm_ids") or [], job.get("vm_names") or []):
            execution_type, execution_reason = resolve_job_execution(job, vm_id=vm_id, force_full=force_full)
            sessions.append(
                _create_backup_session(
                    job,
                    vm_id=vm_id,
                    vm_name=vm_name,
                    execution_type=execution_type,
                    execution_reason=execution_reason,
                )
            )
    schedule = job.get("schedule")
    # A manual "Run now"/"Run full backup" must push next_run_at forward the
    # same way the scheduler does after its own dispatch (_dispatch_due_jobs
    # in job_scheduler.py) — otherwise a job whose scheduled time has already
    # arrived (or was already due when triggered manually) keeps a stale,
    # now-past next_run_at, and the scheduler's next poll (every 5s) sees it
    # as still due and fires it again right behind the manual run, which
    # looks like the job running twice.
    next_run_at = (
        schedule_next_run(schedule, after=_now_utc()).isoformat()
        if isinstance(schedule, dict) and bool(schedule.get("enabled", True))
        else job.get("next_run_at")
    )
    persisted = _persist_canonical_job(
        {
            **job,
            "last_run_at": utc_now(),
            "last_session_id": sessions[-1]["id"] if sessions else None,
            "next_run_at": next_run_at,
            "updated_at": utc_now(),
        }
    )
    return {
        "job": {"id": persisted["id"], "name": persisted["name"], "job_type": persisted["job_type"]},
        "items": sessions,
    }


def _resolve_restore_source_point(payload: dict[str, Any]) -> dict[str, Any]:
    point_id = str(payload.get("point_id") or payload.get("restore_point_id") or "").strip()
    backup_id = str(payload.get("backup") or payload.get("backup_id") or "").strip()
    vm_selector = str(payload.get("vm") or "").strip()

    if point_id:
        points = {point["id"]: point for point in normalized_backup_points()}
        point = points.get(point_id)
        if point is None:
            raise api_error(404, "restore_point_not_found", "Restore point not found", point_id=point_id)
        return point

    _ensure(bool(backup_id), 422, "invalid_restore", "restore start requires --point-id/--restore-point or --backup")
    candidates = [point for point in normalized_backup_points() if _restore_point_matches_backup(point, backup_id)]
    if vm_selector:
        candidates = [point for point in candidates if _restore_point_matches_vm(point, vm_selector)]
    candidates = [point for point in candidates if _restore_point_is_available(point)]
    if not candidates:
        raise api_error(
            404,
            "restore_point_not_found",
            "Restore point not found for the selected backup",
            backup_id=backup_id,
            vm=vm_selector or None,
        )
    candidates.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    return candidates[0]


def _inventory_vm_name_exists(inventory_id: str, vm_name: str) -> bool:
    lowered = vm_name.lower()
    return any(str(resource.get("name") or "").lower() == lowered for resource in _resolve_inventory_vm_records(inventory_id))


def _resolve_restore_encryption_key(point: dict[str, Any]) -> str | None:
    job_id = str(point.get("job_id") or "")
    if not job_id or job_id not in JOBS:
        return None
    source_job = resolve_job(record_id=job_id)
    task_parameters = source_job.get("task_parameters") if isinstance(source_job.get("task_parameters"), dict) else {}
    if not task_parameters.get("encryption_enabled"):
        return None
    return str(task_parameters.get("encryption_key") or "").strip() or None


def restore_start(payload: dict[str, Any]) -> dict[str, Any]:
    point = _resolve_restore_source_point(payload)
    point_id = str(point.get("id") or "")
    _ensure(
        _restore_point_is_available(point),
        409,
        "restore_point_unavailable",
        "Restore point is not available for restore",
        point_id=point_id,
        status=point.get("status"),
    )

    request = _normalize_restore_request_payload(payload, point)
    _validate_restore_request_matches_point(request, point)

    restore_context = get_restore_context(point_id)
    manifest = _load_restore_manifest_record(point)
    source_vm = restore_context.get("source_vm") if isinstance(restore_context.get("source_vm"), dict) else {}
    defaults = restore_context.get("defaults") if isinstance(restore_context.get("defaults"), dict) else {}
    capabilities = restore_context.get("capabilities") if isinstance(restore_context.get("capabilities"), dict) else {}

    restore_mode = str(request.get("restore_mode") or "new_vm")
    force = bool(request.get("force"))
    if restore_mode == "in_place":
        _ensure(
            bool(capabilities.get("supports_in_place_restore")),
            422,
            "restore_mode_unsupported",
            "In-place VM restore is not currently supported by this backend",
        )
        _ensure(force, 422, "force_required", "--force is required to authorize an in-place restore")
    else:
        _ensure(not force, 422, "invalid_restore", "--force is only valid for in-place restore")

    _ensure(
        not request.get("target_folder_id"),
        422,
        "restore_option_unsupported",
        "Folder selection is not currently supported by this backend",
    )
    _ensure(
        not request.get("target_resource_pool_id"),
        422,
        "restore_option_unsupported",
        "Resource pool selection is not currently supported by this backend",
    )
    _ensure(
        not bool(request.get("preserve_mac_addresses")),
        422,
        "restore_option_unsupported",
        "Preserving MAC addresses is not currently supported by this backend",
    )

    repo = None
    if request.get("repository_id") or request.get("repository_name"):
        repo = resolve_repository(name=request.get("repository_name"), record_id=request.get("repository_id"))
        requested_repository_id = str(request.get("repository_id") or "").strip()
        if requested_repository_id and _find_restore_point_placement(point, repo) is None:
            raise api_error(
                404,
                "repository_mismatch",
                "Restore point does not belong to the selected repository",
                point_id=point.get("id"),
                requested_repository_id=requested_repository_id,
                repository_id=point.get("repository_id"),
            )
    placement = _select_restore_point_placement(point, repo)
    manifest_path = _resolve_restore_manifest_path(point, placement)

    target_inventory_id = str(request.get("target_inventory_id") or defaults.get("target_inventory_id") or "").strip()
    target_inventory_name = str(request.get("target_inventory_name") or "").strip() or None
    _ensure(bool(target_inventory_id or target_inventory_name), 422, "invalid_restore", "Restore target inventory is required")
    target_inventory = resolve_inventory(name=target_inventory_name, record_id=target_inventory_id or None)
    _ensure(
        str(target_inventory.get("status") or "unknown").strip().lower() != "unavailable",
        409,
        "target_inventory_unavailable",
        "Target inventory is not reachable",
        inventory_id=target_inventory.get("id"),
    )
    _ensure(
        bool(_inventory_host_records(str(target_inventory["id"]))),
        409,
        "inventory_not_scanned",
        "Target inventory does not expose any discovered hosts. Run inventory rescan first.",
        inventory_id=target_inventory["id"],
    )

    # In-place restore has no user-chosen placement — it always targets the
    # source VM's own current host, datastore and name, enforced here rather
    # than merely by omission from the wizard, since the API can be called
    # directly (CLI, scripts) bypassing any UI-level restriction.
    if restore_mode == "in_place":
        target_host = str(source_vm.get("original_host") or "").strip()
        _ensure(
            bool(target_host),
            422,
            "invalid_restore",
            "In-place restore requires the source VM's original host to be known",
        )
    else:
        target_host = str(request.get("target_host_identifier") or defaults.get("target_host_id") or "").strip()
    _ensure(bool(target_host), 422, "invalid_restore", "Restore target host is required")
    host = _resolve_host(str(target_inventory["id"]), target_host)
    resolved_target_host_id = str(host.get("external_id") or host.get("name") or "").strip() or None
    resolved_target_host_name = str(host.get("name") or target_host).strip() or target_host

    if restore_mode == "in_place":
        target_datastore_identifier = str(source_vm.get("original_datastore") or "").strip()
        _ensure(
            bool(target_datastore_identifier),
            422,
            "invalid_restore",
            "In-place restore requires the source VM's original datastore to be known",
        )
    else:
        target_datastore_identifier = str(
            request.get("target_datastore_identifier") or defaults.get("target_datastore_id") or ""
        ).strip()
    _ensure(bool(target_datastore_identifier), 422, "invalid_restore", "Restore target datastore is required")
    datastore = _resolve_restore_target_datastore(str(target_inventory["id"]), target_datastore_identifier, host=host)
    resolved_target_datastore_id = str(datastore.get("id") or "").strip() or None
    target_datastore_name = str(datastore.get("name") or "").strip()
    _ensure_restore_target_capacity(source_vm, datastore, restore_mode)

    if restore_mode == "in_place":
        target_vm_name = str(source_vm.get("name") or point.get("vm_name") or "").strip()
    else:
        target_vm_name = str(request.get("target_vm_name") or defaults.get("target_vm_name") or point.get("vm_name") or "").strip()
    _ensure(bool(target_vm_name), 422, "invalid_restore", "Restore target VM name is required")
    _ensure_ascii_text(target_vm_name, field="Restore target VM name", code="invalid_restore", max_length=MAX_NAME_LENGTH)
    if restore_mode == "in_place":
        _ensure(
            _inventory_vm_name_exists(str(target_inventory["id"]), target_vm_name),
            404,
            "in_place_source_vm_not_found",
            "Source VM not found at its original location for in-place restore",
            inventory_id=target_inventory["id"],
            vm_name=target_vm_name,
        )
    else:
        _ensure(
            not _inventory_vm_name_exists(str(target_inventory["id"]), target_vm_name),
            409,
            "target_vm_exists",
            "VM with target name already exists",
            inventory_id=target_inventory["id"],
            vm_name=target_vm_name,
        )

    network_map = _resolve_restore_network_selection(
        point=point,
        manifest=manifest,
        target_inventory_id=str(target_inventory["id"]),
        mappings=request.get("network_mappings") if isinstance(request.get("network_mappings"), list) else [],
        legacy_network_map=request.get("legacy_network_map"),
        host=host,
    )

    preserve_firmware_raw = request.get("preserve_firmware")
    if preserve_firmware_raw is None:
        preserve_firmware = bool(defaults.get("preserve_firmware"))
    else:
        preserve_firmware = bool(preserve_firmware_raw)
    source_firmware = str(source_vm.get("firmware") or "").strip().lower()
    if source_firmware == "efi" and not preserve_firmware:
        raise api_error(
            422,
            "firmware_preservation_required",
            "EFI restore points must preserve EFI firmware to avoid boot failure",
            restore_point_id=point_id,
        )
    requested_firmware = source_firmware if preserve_firmware and source_firmware in {"bios", "efi"} else None

    scenario = "backup_copy" if repo is not None and str(repo.get("id") or "") != str(point.get("repository_id") or "") else "out_of_place"
    power_on = bool(request.get("power_on_after_restore"))
    connect_nics = bool(request.get("connect_nics"))
    disk_provisioning = str(request.get("disk_provisioning") or "").strip() or None
    encryption_key = _resolve_restore_encryption_key(point)
    task = {
        "id": str(uuid4()),
        "job_id": None,
        "task_type": "restore",
        "hypervisor_id": target_inventory["id"],
        "resource_type": "virtual_machine",
        "resource_external_id": point.get("vm_id") or point_id,
        "source_resource_available": bool(point.get("inventory_id")),
        "status": "queued",
        "parameters": {
            "point_id": point_id,
            "scenario": scenario,
            "restore_mode": restore_mode,
            "target_inventory_id": target_inventory["id"],
            "target_inventory_name": target_inventory["name"],
            "target_host": target_host,
            "target_host_id": resolved_target_host_id,
            "target_host_name": resolved_target_host_name,
            "target_datastore_id": resolved_target_datastore_id,
            "target_datastore_name": target_datastore_name,
            "target_vm_name": target_vm_name,
            # The VM the restore point was taken from -- kept alongside the
            # target so a restore session can name both ends (see
            # _session_vm_names); the point itself may be deleted later.
            "source_vm_name": str(point.get("vm_name") or "").strip() or None,
            "repository_id": (placement or {}).get("repository_id") if isinstance(placement, dict) else (repo["id"] if repo else point.get("repository_id")),
            "repository_name": (placement or {}).get("repository_name") if isinstance(placement, dict) else (repo["name"] if repo else point.get("repository_name")),
            "manifest_path": manifest_path,
            "backup_root": (placement or {}).get("backup_root") if isinstance(placement, dict) else _restore_point_artifact(point).get("backup_root"),
            "network_map": network_map,
            "force": force,
            "power_on": power_on,
            "connect_nics": connect_nics,
            "preserve_mac_addresses": False,
            "firmware": requested_firmware,
            **({"disk_type": disk_provisioning} if disk_provisioning else {}),
            **({"encryption_key": encryption_key} if encryption_key else {}),
        },
        "result": {
            "progress": 0,
            "scenario": scenario,
        },
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "logs": [],
    }
    _append_task_log(task, level="info", message=f"Queued restore session for restore point {point_id}", component="session")
    persist_task(task)
    session = session_to_dto(task)
    return {
        **session,
        "status": "started",
        "restore_session_id": session.get("id"),
        "session_state": session.get("state"),
        "restore_point_id": point_id,
        "backup_id": point.get("chain_id"),
        "source_inventory_id": point.get("inventory_id"),
        "source_inventory_name": point.get("inventory_name"),
        "source_vm_id": point.get("vm_id"),
        "source_vm_name": point.get("vm_name"),
        "target_inventory_id": task["parameters"].get("target_inventory_id"),
        "target_inventory_name": task["parameters"].get("target_inventory_name"),
        "target_host_id": resolved_target_host_id,
        "target_host_name": task["parameters"].get("target_host_name"),
        "target_datastore_id": resolved_target_datastore_id,
        "datastore_name": task["parameters"].get("target_datastore_name"),
        "new_name": task["parameters"].get("target_vm_name"),
        "restore_mode": restore_mode,
    }


def system_version() -> dict[str, Any]:
    return {
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "cli_binary": "abr",
    }


def _linux_os_pretty_name() -> str | None:
    """Distro display name from /etc/os-release (e.g. "Debian GNU/Linux 12").

    Falls back to the bare platform name/release on anything without that file
    (non-Linux, a minimal container) rather than failing the whole probe.
    """
    try:
        fields: dict[str, str] = {}
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep:
                    fields[key] = value.strip('"')
        pretty = fields.get("PRETTY_NAME")
        if pretty:
            return pretty
    except OSError:
        pass
    fallback = f"{platform.system()} {platform.release()}".strip()
    return fallback or None


def _system_uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _system_memory_gb() -> float | None:
    """Installed RAM, in GB. /proc/meminfo first (has a value even for memory
    reserved by the kernel); os.sysconf as a fallback for non-Linux POSIX."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kib = int(line.split()[1])
                    return round(kib / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return round((pages * page_size) / (1024**3), 1)
    except (ValueError, OSError, AttributeError):
        pass
    return None


def system_info() -> dict[str, Any]:
    """Live identity/hardware facts for the About tab — read from the host the
    server process is actually running on, not stored configuration. A field
    that can't be determined comes back as None; callers show that as unknown
    rather than a plausible-looking placeholder."""
    return {
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "hostname": socket.gethostname() or None,
        "os": _linux_os_pretty_name(),
        "kernel": platform.release() or None,
        "uptime_sec": _system_uptime_seconds(),
        "cpu_cores": os.cpu_count(),
        "memory_gb": _system_memory_gb(),
    }


def system_health() -> dict[str, Any]:
    """Per-component health. Always answers, including while a dependency is
    down — an operator polling this endpoint needs it to name the broken
    component, not to fail alongside it.

    Overall `status` is "ok" only while every component is; anything else
    degrades it, so a caller can branch on one field and read `components` for
    the detail."""
    database = database_status()
    components = {
        "api": "ok",
        "database": database,
        "repositories": "ok",
        "inventories": "ok",
        "scheduler": "ok",
        "task_pool": "ok",
    }
    # "not_configured" is the in-memory mode (no MONGODB_URI); it is a
    # deployment choice, not a fault, so it does not degrade the overall status.
    unhealthy = [
        name for name, state in components.items()
        if state not in ("ok", DATABASE_NOT_CONFIGURED)
    ]
    return {
        "status": "degraded" if unhealthy else "ok",
        "components": components,
        "counts": {
            "repositories": len(REPOSITORIES),
            "inventories": len(HYPERVISORS),
            "jobs": len(JOBS),
            "sessions": len(TASKS),
            "restore_points": len(BACKUPS),
        },
    }


def _read_env_defaults_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if key:
            values[key] = value

    return values


def _server_defaults_file_candidates() -> list[Path]:
    override = os.environ.get(SERVER_DEFAULTS_FILE_ENV_VAR)
    
    if override:
        return [Path(override)]

    return [Path("/etc/altastor-backup/server.conf"), DEFAULT_SERVER_DEFAULTS_FILE]


def snapshot_configuration() -> dict[str, str]:
    from_file: dict[str, str] = {}

    for candidate in _server_defaults_file_candidates():
        if candidate.is_file():
            from_file = _read_env_defaults_file(candidate)
            break

    configuration = {
        key: value for key, value in from_file.items() if key in CONFIGURATION_EXPORT_ALLOWLIST
    }

    configuration.update(
        {key: os.environ[key] for key in CONFIGURATION_EXPORT_ALLOWLIST if key in os.environ}
    )
    return configuration


def export_catalog_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(password, str) or not password:
        raise api_error(422, "invalid_argument", "password is required")

    catalog = snapshot_catalog()
    configuration = snapshot_configuration()
    manifest = {
        "format_version": CATALOG_SNAPSHOT_FORMAT_VERSION,
        "product": PRODUCT_NAME,
        "product_version": PRODUCT_VERSION,
        "exported_at": utc_now(),
        "counts": {table: len(records) for table, records in catalog.items() if table != "resources"},
        "configuration_keys": sorted(configuration.keys()),
    }
    archive = encrypt_payload(
        {"manifest": manifest, "catalog": catalog, "configuration": configuration},
        password,
    )

    return {
        "manifest": manifest,
        "archive_base64": base64.b64encode(archive).decode("ascii"),
    }


def import_catalog_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    password = payload.get("password") if isinstance(payload, dict) else None
    archive_base64 = payload.get("archive_base64") if isinstance(payload, dict) else None
    if (
        not isinstance(password, str)
        or not password
        or not isinstance(archive_base64, str)
        or not archive_base64
    ):
        raise api_error(422, "invalid_argument", "password and archive_base64 are required")

    try:
        archive = base64.b64decode(archive_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api_error(422, "invalid_export", "archive_base64 is not valid base64") from exc

    try:
        export_payload = decrypt_payload(archive, password)
    except WrongPasswordError as exc:
        raise api_error(422, "wrong_password", str(exc)) from exc
    except ArchiveError as exc:
        raise api_error(422, "invalid_export", str(exc)) from exc

    manifest = export_payload.get("manifest") if isinstance(export_payload, dict) else None
    catalog = export_payload.get("catalog") if isinstance(export_payload, dict) else None

    if not isinstance(manifest, dict) or not isinstance(catalog, dict):
        raise api_error(422, "invalid_export", "This is not system export payload")

    format_version = manifest.get("format_version")
    if format_version != CATALOG_SNAPSHOT_FORMAT_VERSION:
        raise api_error(
            422,
            "incompatible_export",
            f"Export format version {format_version!r} is not supported by this server "
            f"(expects {CATALOG_SNAPSHOT_FORMAT_VERSION}). Restore onto a matching version",
        )

    configuration = export_payload.get("configuration")
    if not isinstance(configuration, dict):
        configuration = {}

    counts = restore_catalog(catalog)
    return {"manifest": manifest, "restored": counts, "configuration": configuration}


def _verify_disk_artifact(
    point: dict[str, Any],
    disk: dict[str, Any],
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    disk_key = disk.get("disk_key") or disk.get("index")

    abr_backup_name = str(disk.get("abr_backup_name") or "").strip()
    abr_path = str(disk.get("abr_path") or "").strip()

    if not abr_backup_name:
        destination_path = str(disk.get("destination_path") or "").strip()

        if not destination_path:
            return {"disk_key": disk_key, "status": "failed", "message": "No backup artifact recorded for this disk"}
        
        if not Path(destination_path).is_file():
            return {"disk_key": disk_key, "status": "failed", "message": f"Backup file is missing: {destination_path}"}
        
        return {
            "disk_key": disk_key,
            "status": "ok",
            "message": "Backup file present (existence check only; disk is not stored in .abr format)",
        }

    repository = REPOSITORIES.get(str(disk.get("repository_id") or point.get("repository_id") or ""))
    if not isinstance(repository, dict):
        return {"disk_key": disk_key, "status": "failed", "message": "Repository not found for backup artifact"}

    # Cheap existence pre-check: skip the expensive block-level verify read
    # entirely when the artifact is provably absent. A listing failure is
    # inconclusive (not the same as "missing"), so it falls through to the
    # normal verify attempt rather than being reported as a failure here.
    try:
        names = list_disk_tool_backups(repository, abr_path, timeout=30)
    except StorageToolError:
        names = None
    if names is not None and abr_backup_name not in names:
        return {"disk_key": disk_key, "status": "failed", "message": "Backup artifact not found in storage"}

    try:
        verify_disk_tool_backup(
            repository,
            abr_path,
            abr_backup_name,
            timeout=VERIFY_ARTIFACT_TIMEOUT_SECONDS,
            on_progress=on_progress,
        )
    except StorageToolError as exc:
        return {"disk_key": disk_key, "status": "failed", "message": str(exc)}

    return {"disk_key": disk_key, "status": "ok", "message": "Verified"}


def _repository_latest_points(repository_id: str) -> list[dict[str, Any]]:
    latest_points: list[dict[str, Any]] = []

    for points in grouped_backup_points().values():
        if not points:
            continue

        latest = points[-1]
        if str(latest.get("repository_id") or "") == repository_id:
            latest_points.append(latest)

    return latest_points


def _verify_repository_backup_health(repository_id: str) -> dict[str, Any]:
    latest_points = _repository_latest_points(repository_id)
    work_items: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for point in latest_points:
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}

        for disk in artifact.get("disks", []):
            if isinstance(disk, dict):
                work_items.append((point, disk))

    if not work_items:
        return {
            "status": "skipped",
            "checked_chains": len(latest_points),
            "checked_artifacts": 0,
            "errors": [],
            "message": "No backup artifacts to verify",
        }

    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(VERIFY_CONCURRENCY, len(work_items))) as pool:
        futures = {pool.submit(_verify_disk_artifact, point, disk): 
                   point for point, disk in work_items}

        for future in as_completed(futures):
            point = futures[future]

            try:
                result = future.result()
            except Exception as exc:
                result = {"disk_key": None, "status": "failed", "message": str(exc)}

            if result["status"] != "ok":
                errors.append(
                    {
                        "chain_id": point.get("chain_id"),
                        "point_id": point.get("id"),
                        "disk_key": result.get("disk_key"),
                        "message": result["message"],
                    }
                )

    status = "failed" if errors else "ok"
    message = (
        f"{len(errors)} of {len(work_items)} backup artifact(s) failed verification"
        if errors
        else f"Verified {len(work_items)} backup artifact(s) across {len(latest_points)} chain(s)"
    )
    return {
        "status": status,
        "checked_chains": len(latest_points),
        "checked_artifacts": len(work_items),
        "errors": errors,
        "message": message,
    }
