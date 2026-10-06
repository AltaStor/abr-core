# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import binascii
import calendar
import json
import logging
import math
import functools
import os
import platform
import re
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
from urllib.parse import urlencode, urlparse
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
    users_replaced_by_import,
)
from python.srv.core.server_defaults import env_int
from python.srv.core.subprocess_util import run_capped
from python.srv.core.secure_archive import (
    ArchiveError,
    WrongPasswordError,
    decrypt_payload,
    encrypt_payload,
)
from python.srv.core.storage_tool import (
    IMMUTABILITY_MODES,
    IMMUTABILITY_RETENTION_SCOPES,
    STORAGE_LAYOUT_BY_ID,
    STORAGE_LAYOUT_BY_NAME,
    StorageToolError,
    apply_repository_immutability,
    build_s3_location,
    condense_disk_tool_message,
    extract_s3_config,
    hydrate_manifest_via_disk_tool,
    immutability_is_enabled,
    immutability_rank,
    is_abr_repository,
    is_s3_repository,
    list_disk_tool_backups,
    SECRET_MASK,
    mask_credential_fields,
    normalize_immutability_config,
    normalize_immutability_mode,
    normalize_immutability_retention_scope,
    normalize_s3_prefix,
    probe_repository_access,
    probe_repository_immutability,
    read_storage_backup_info,
    read_storage_folder_info,
    remove_disk_tool_backup,
    s3_block_generation_days,
    s3_endpoint_connect_error,
    sanitize_s3_config,
    verify_disk_tool_backup,
)
from python.srv.core.utils import utc_now
from python.srv.services.inventory_management import (
    INVENTORY_KIND_BY_TYPE,
    EsxHostInClusterError,
    HyperVAlreadyRegisteredError,
    InventoryProviderRegistry,
    apply_inventory_updates,
    InventoryUnreachableError,
    assert_hyperv_identity_is_new,
    assert_inventory_registrable,
    connection_fields_changed,
    creatable_inventory_types,
    default_port_for_type,
    extract_host,
    inventory_references,
    inventory_type_from_record,
    normalize_inventory_type,
)
from python.srv.services.backup_mode import object_lock_actual_retention_days
from python.srv.plugins.vcenter.vcenter_backup import DEFAULT_COMPRESSION_LEVEL

from .api_errors import ApiError, api_error
from .auth_service import MIN_PASSWORD_LENGTH

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
        except Exception as exc:
            LOGGER.warning(
                "Background inventory discovery could not reach inventory %s: %s", inventory_id, exc
            )
            LOGGER.debug("Inventory discovery failure detail", exc_info=True)

    thread = threading.Thread(
        target=_run,
        name=f"inventory-discovery-{inventory_id}",
        daemon=True,
    )
    thread.start()
    return thread

PRODUCT_NAME = "AltaStor Backup & Replication"
PRODUCT_VERSION = "26.11"
SERVER_PACKAGE_NAME = "altastor-backup-server"
PACKAGE_QUERY_TIMEOUT_SECONDS = 10


def installed_package_versions() -> dict[str, str]:
    try:
        result = run_capped(
            ["dpkg-query", "-W", "-f=${Package} ${Version}\\n", "altastor-*"],
            timeout=PACKAGE_QUERY_TIMEOUT_SECONDS,
            nice=False,
        )
    except Exception:
        LOGGER.debug("Could not read the installed package versions", exc_info=True)
        return {}
    output = result.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    versions: dict[str, str] = {}
    for line in str(output or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("altastor"):
            versions[parts[0]] = parts[1]
    return versions


@functools.lru_cache(maxsize=1)
def product_version() -> str:
    installed = str(installed_package_versions().get(SERVER_PACKAGE_NAME) or "").strip()
    return installed or PRODUCT_VERSION

REPO_TYPES = ("local", "nfs", "smb", "s3")

PRODUCT_STORAGE_MODE = 0o750

EXECUTION_TYPES = ("Incremental", "ActiveFull", "SyntheticFull")
EXECUTION_REASONS = (
    "FirstRun",
    "ForceFull",
    "ForceSynthetic",
    "ActiveFullWindow",
    "SyntheticFullWindow",
    "ActiveOverridesSynthetic",
    "RepositorySyntheticUnsupportedEscalatedToActive",
    "StandardIncremental",
    "EncryptionKeyChanged",
    "IncrementalFallbackToFull",
    "VmConfigurationChanged",
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
# Page size assumed for a `page=`-style request that names no `per_page`.
SESSION_PAGE_DEFAULT = 100

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
MAX_IMMUTABILITY_RETENTION_DAYS = 36500
NFS_PATTERN = re.compile(r"^[^:/]+:.+$")
SMB_PATTERN = re.compile(r"^(//|\\\\)[^/\\\\]+[/\\\\].+$")
ZONEINFO_ROOT = Path("/usr/share/zoneinfo")

CATALOG_SNAPSHOT_FORMAT_VERSION = 1
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
        # What the refusal lists has to be what the parser takes. DAY_ALIASES
        # accepts the full day names and the short forms, in any case ("saturday"
        # is what the console sends), so a refusal naming only Mon-Sun told a
        # client its working request was invalid.
        raise api_error(
            422,
            "invalid_weekday",
            f"Unknown weekday {str(value)!r}: use a short (Mon-Sun) or full (monday-sunday) day name",
            value=value,
            valid_values=list(DAY_ORDER),
            accepted_values=sorted(DAY_ALIASES),
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
        value == value.strip(),
        400,
        code,
        f"{field} must not start or end with whitespace",
        field=field,
        value=value,
    )
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
    name_option: str = "--name",
    id_option: str = "--id",
) -> dict[str, Any]:
    resolved_by_name = _find_by_name(items, name, label=label) if name else None
    resolved_by_id = _find_by_id(items, record_id, label=label) if record_id else None
    if resolved_by_name is not None and resolved_by_id is not None:
        if str(resolved_by_name.get("id")) != str(resolved_by_id.get("id")):
            raise api_error(
                409,
                "selector_mismatch",
                f"{label} selector mismatch: {name_option} and {id_option} refer to different records",
                name=name,
                record_id=record_id,
            )
        return resolved_by_id
    if resolved_by_name is not None:
        return resolved_by_name
    if resolved_by_id is not None:
        return resolved_by_id
    raise api_error(422, "missing_selector", f"{label} selection requires {name_option} or {id_option}")


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
            candidate = datetime.combine(
                candidate_date, time(hour=hour, minute=minute, fold=local_after.fold), tzinfo=local_after.tzinfo
            )
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
            candidate = datetime(
                year, month, candidate_day, hour, minute, tzinfo=local_after.tzinfo, fold=local_after.fold
            )
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
        probe = (local_after.replace(second=0, microsecond=0) + timedelta(minutes=1)).replace(fold=local_after.fold)
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
            probe = (probe + timedelta(minutes=1)).replace(fold=local_after.fold)
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
    return is_abr_repository(repository)


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
    item["status_checked_at"] = item.get("status_checked_at")
    item["status_error"] = item.get("status_error")
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


def resolve_inventory(
    name: str | None = None,
    record_id: str | None = None,
    *,
    name_option: str = "--name",
    id_option: str = "--id",
) -> dict[str, Any]:
    return normalize_inventory_record(
        resolve_selector(
            list_inventory_records(),
            name=name,
            record_id=record_id,
            label="Inventory",
            name_option=name_option,
            id_option=id_option,
        )
    )


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


def _inventory_is_hyperv(inventory_id: Any) -> bool:
    record = HYPERVISORS.get(str(inventory_id or ""))
    return isinstance(record, dict) and inventory_type_from_record(record) == "hyper-v"


def _hyperv_vm_parts(
    resources: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """A Hyper-V inventory's discovered disks and network adapters, by VM id."""
    disks: dict[str, list[dict[str, Any]]] = {}
    nics: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        kind = str(resource.get("resource_type") or "")
        if kind not in {"disk", "network_adapter"}:
            continue
        payload = _resource_payload(resource)
        target = disks if kind == "disk" else nics
        target.setdefault(str(payload.get("vm_id") or ""), []).append(
            {"name": str(resource.get("name") or ""), **payload}
        )
    return disks, nics


def _hyperv_vm_details(
    payload: dict[str, Any], disks: list[dict[str, Any]], nics: list[dict[str, Any]]
) -> dict[str, Any]:
    """What the console shows of a Hyper-V VM beyond the common fields: its
    configuration, and each disk and adapter as backup and recovery see them."""
    return {
        "generation": _nullable_number(payload.get("generation")),
        "configuration_version": _nullable_str(payload.get("version")),
        "firmware": _nullable_str(payload.get("firmware")),
        "checkpoint_count": int(payload.get("checkpoints") or 0),
        "clustered": bool(payload.get("clustered")),
        "owner_node": _nullable_str(payload.get("host_name")),
        "config_path": _nullable_str(payload.get("config_path")),
        "disks": sorted(
            (
                {
                    "name": disk["name"],
                    "controller": _nullable_str(disk.get("controller")),
                    "path": _nullable_str(disk.get("datastore_path")),
                    "format": _nullable_str(disk.get("source_disk_type_code")),
                    "type": _nullable_str(disk.get("source_disk_type_name")),
                    "capacity_bytes": _capacity_bytes_from_disk_entry(disk),
                    "file_bytes": _nullable_number(disk.get("file_bytes")),
                    "has_parent": bool(disk.get("parent_path")),
                }
                for disk in disks
            ),
            key=lambda disk: str(disk["controller"] or disk["name"]),
        ),
        "network_adapters": [
            {
                "name": nic["name"],
                "switch": _nullable_str(nic.get("network")),
                "mac": _nullable_str(nic.get("mac")),
                "connected": bool(nic.get("connected")),
            }
            for nic in nics
        ],
    }


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
        hyperv_parts = _hyperv_vm_parts(resources) if _inventory_is_hyperv(inventory_id) else None

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
            if hyperv_parts is not None:
                vm_node["hyperv"] = _hyperv_vm_details(payload, *(part.get(vm_id, []) for part in hyperv_parts))
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
    name = str(payload.get("name") or "")
    address = str(payload.get("address") or "").strip()
    port = int(payload.get("port") or 0) if payload.get("port") is not None else None
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    token = str(payload.get("token") or "").strip()
    insecure = bool(payload.get("insecure", False))
    description = str(payload.get("description") or "")
    _ensure(bool(name.strip()), 422, "invalid_inventory", "Inventory name is required")
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
        "status_checked_at": None,
        "status_error": None,
        "vm_count": 0,
        "host_count": 0,
        "cluster_count": 0,
        "metadata": {
            "host": address.split("/", 1)[0].split(":", 1)[0],
            "port": effective_port,
            "port_explicit": port is not None,
            "verify_ssl": not insecure,
            "credentials": {
                "username": username or None,
                "password": password or None,
            },
            "token": token or None,
        },
    }
    try:
        proof = _proof_of_connection(assert_inventory_registrable(record))
    except EsxHostInClusterError as exc:
        raise api_error(
            409,
            "esx_host_in_cluster",
            str(exc),
            cluster=exc.cluster_name,
        ) from exc
    except InventoryUnreachableError as exc:
        raise api_error(
            422,
            "inventory_unreachable",
            str(exc),
            address=address,
            port=effective_port,
            reason=exc.reason,
        ) from exc
    try:
        record = _apply_inventory_identity(record, proof.get("identity") or {})
    except HyperVAlreadyRegisteredError as exc:
        raise _already_registered_error(exc) from exc
    if proof:
        checked_at = utc_now()
        record["status"] = "available"
        record["status_checked_at"] = checked_at
        record["status_error"] = None
        record["updated_at"] = checked_at
    persist_hypervisor(record)
    RESOURCES_BY_HV.setdefault(record["id"], {})
    if AUTO_DISCOVER_ON_INVENTORY_ADD:
        # Kick off discovery now so VM names are usable quickly, but keep `add`
        # responsive by doing it in the background. Lazy discovery in
        # `job create` still covers the case where this has not finished.
        schedule_inventory_discovery(record["id"])
    return normalize_inventory_record(record)


def _proof_of_connection(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {}


def _already_registered_error(exc: HyperVAlreadyRegisteredError) -> ApiError:
    return api_error(
        409,
        "inventory_already_registered",
        str(exc),
        scope=exc.scope,
        subject=exc.subject,
        existing_id=exc.existing_id,
        existing_name=exc.existing_name,
    )


def _record_inventory_identity(record: dict[str, Any], identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict) or not identity:
        return record
    updated = dict(record)
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    metadata["cluster_name"] = str(identity.get("cluster") or "") or None
    metadata["computer_name"] = str(identity.get("computer") or "") or None
    updated["metadata"] = metadata
    return updated


def _apply_inventory_identity(record: dict[str, Any], identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict) or not identity:
        return record
    assert_hyperv_identity_is_new(record, identity)
    return _record_inventory_identity(record, identity)


def _recheck_inventory_connection(record: dict[str, Any]) -> dict[str, Any]:
    updated = dict(record)
    checked_at = utc_now()
    try:
        proof = _proof_of_connection(assert_inventory_registrable(updated))
    except (EsxHostInClusterError, InventoryUnreachableError) as exc:
        updated["status"] = "unavailable"
        updated["status_error"] = str(exc)
        updated["status_checked_at"] = checked_at
        updated["updated_at"] = checked_at
        return updated
    except Exception as exc:
        LOGGER.warning(
            "Could not re-check inventory %s after an edit: %s", record.get("id"), exc
        )
        updated["status"] = "unavailable"
        updated["status_error"] = str(exc)
        updated["status_checked_at"] = checked_at
        updated["updated_at"] = checked_at
        return updated
    if not proof:
        return updated
    updated = _apply_inventory_identity(updated, proof.get("identity") or {})
    updated["status"] = "available"
    updated["status_error"] = None
    updated["status_checked_at"] = checked_at
    updated["updated_at"] = checked_at
    return updated


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
            new_name = str(updates["name"] or "")
            _ensure(bool(new_name.strip()), 422, "invalid_inventory", "Inventory name must not be empty")
            _ensure_ascii_name(new_name, field="Inventory name", code="invalid_inventory")
        normalized = apply_inventory_updates(normalized, updates)
    _ensure(any(key in payload for key in ("name", "address", "port", "username", "password", "description", "token", "insecure")), 422, "invalid_inventory", "inventory edit requires at least one editable option")
    if connection_fields_changed(payload):
        try:
            normalized = _recheck_inventory_connection(normalized)
        except HyperVAlreadyRegisteredError as exc:
            raise _already_registered_error(exc) from exc
    persist_hypervisor(normalized)
    return normalize_inventory_record(normalized)


def test_inventory(record_id: str) -> dict[str, Any]:
    record = _inventory_record_for_update(record_id)
    provider = InventoryProviderRegistry.resolve(record)
    checked_at = utc_now()
    try:
        identity = provider.test_connection(record)
    except EsxHostInClusterError as exc:
        update_hypervisor_fields(
            record["id"],
            {
                "status": "unavailable",
                "status_error": str(exc),
                "status_checked_at": checked_at,
                "updated_at": checked_at,
            },
        )
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
        update_hypervisor_fields(
            record["id"],
            {
                "status": "unavailable",
                "status_error": str(exc),
                "status_checked_at": checked_at,
                "updated_at": checked_at,
            },
        )
        return {
            "id": record["id"],
            "name": record.get("name"),
            "type": inventory_type_from_record(record),
            "status": "error",
            "message": "Unable to connect to virtualization environment",
            "error": str(exc),
        }
    learned = _record_inventory_identity(record, identity)
    update_hypervisor_fields(
        record["id"],
        {
            "status": "available",
            "status_error": None,
            "status_checked_at": checked_at,
            "updated_at": checked_at,
            "metadata": learned.get("metadata", record.get("metadata")),
        },
    )
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
        failed_at = utc_now()
        update_hypervisor_fields(
            record_id,
            {
                "status": "unavailable",
                "last_scan_error": str(exc),
                "status_error": str(exc),
                "status_checked_at": failed_at,
                "updated_at": failed_at,
            },
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
    updated["status_error"] = None
    updated["status_checked_at"] = discovered_at
    update_hypervisor_fields(
        record_id,
        {
            **counts,
            "status": "available",
            "last_scan_at": discovered_at,
            "updated_at": discovered_at,
            "last_scan_error": None,
            "status_error": None,
            "status_checked_at": discovered_at,
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
        item["immutability"] = s3_config.get("immutability")
        item["path"] = s3_config.get("location")
        item["location"] = item.get("path")
    else:
        item["immutability"] = None
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
    last_verify = item.get("last_verify") if isinstance(item.get("last_verify"), dict) else None
    if last_verify is not None:
        item["last_verify_at"] = last_verify.get("checked_at")
        if str(last_verify.get("status") or "") != "success":
            storage["available"] = False
            storage["error"] = str(last_verify.get("message") or "Repository verification failed")
            item["status"] = "unavailable"
            item["health"] = "unavailable"
    item["storage"] = storage
    item["warnings"] = warnings
    item["capacity_bytes"] = storage["capacity_bytes"]
    item["used_bytes"] = storage["used_bytes"]
    item["free_bytes"] = storage["free_bytes"]
    item["capabilities"] = {
        "supports_synthetic_full": repository_supports_synthetic(item),
        "supports_immutability": repo_type == "s3",
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

    merged_immutability = normalize_immutability_config(current.get("immutability"))
    if "immutability" in raw or "immutability" in payload:
        requested = _pick("immutability")
        requested = requested if isinstance(requested, dict) else {}
        if "mode" in requested:
            merged_immutability["mode"] = normalize_immutability_mode(requested.get("mode"))
            if merged_immutability["mode"] == "none" and "retention_days" not in requested:
                merged_immutability["retention_days"] = 0
        if "retention_days" in requested:
            merged_immutability["retention_days"] = requested.get("retention_days")
        if "retention_scope" in requested:
            scope = str(requested.get("retention_scope") or "").strip().lower().replace("-", "_")
            _ensure(
                scope in IMMUTABILITY_RETENTION_SCOPES,
                422,
                "invalid_repository",
                "S3 immutability retention scope must be one of: " + ", ".join(IMMUTABILITY_RETENTION_SCOPES),
                immutability_retention_scope=requested.get("retention_scope"),
            )
            merged_immutability["retention_scope"] = scope
    current["immutability"] = normalize_immutability_config(merged_immutability)

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

    immutability = normalize_immutability_config(config.get("immutability"))
    mode = immutability["mode"]
    retention_days = immutability["retention_days"]
    _ensure(
        mode in IMMUTABILITY_MODES,
        422,
        "invalid_repository",
        "S3 immutability mode must be one of: " + ", ".join(IMMUTABILITY_MODES),
        immutability_mode=mode,
    )
    if mode == "none":
        _ensure(
            retention_days == 0,
            422,
            "invalid_repository",
            "S3 immutability retention period requires an immutability mode of governance or compliance",
        )
    else:
        _ensure(
            retention_days >= 1,
            422,
            "invalid_repository",
            f"S3 immutability mode '{mode}' requires a retention period of at least 1 day",
        )
        _ensure(
            retention_days <= MAX_IMMUTABILITY_RETENTION_DAYS,
            422,
            "invalid_repository",
            f"S3 immutability retention period must not exceed {MAX_IMMUTABILITY_RETENTION_DAYS} days",
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


def _ensure_local_repository_share(server: Any, share: Any, username: Any, password: Any) -> None:
    """A local repository's server and share name the SMB share Hyper-V hosts
    reach its folder by (abr-vhdx-tool writes there from the host); its
    username and password are the account that opens it."""
    _ensure(
        bool(str(server or "").strip()) == bool(str(share or "").strip()),
        422,
        "invalid_repository",
        "A local repository's SMB share for Hyper-V hosts needs both --server and --share",
    )
    _ensure(
        not str(username or "").strip() or bool(str(password or "")),
        422,
        "invalid_repository",
        "A local repository's SMB share account needs --password with --username",
    )


def create_repository_record(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    repo_type = str(payload.get("type") or "").strip().lower()
    path = str(payload.get("path") or "").strip() or None
    address = str(payload.get("address") or "").strip() or None
    server = str(payload.get("server") or "").strip() or None
    share = str(payload.get("share") or "").strip() or None
    username = str(payload.get("username") or "").strip() or None
    password = str(payload.get("password") or "") or None
    description = str(payload.get("description") or "").strip() or None
    _ensure(bool(name.strip()), 422, "invalid_repository", "Repository name is required")
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
        _ensure_local_repository_share(server, share, username, password)
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
                    "immutability": s3_config["immutability"],
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
    create_bucket = bool(payload.get("create_bucket"))
    bucket_was_missing = (
        repo_type == "s3" and create_bucket and not s3_bucket_is_reachable(s3_config)
    )
    registration_check = _assert_repository_is_usable(record, create_bucket=create_bucket)
    if registration_check is not None:
        record["last_verify"] = registration_check
    if repo_type == "s3" and immutability_is_enabled(s3_config.get("immutability")):
        _assert_immutability_is_usable(s3_config)
    persist_repository(record)
    created = normalize_repository_record(record)
    if bucket_was_missing:
        created["warnings"] = [
            *created.get("warnings", []),
            f"S3 bucket '{s3_config['bucket']}' did not exist and was created. Object Lock cannot be "
            "turned on after a bucket is created, so this bucket can never hold immutable backups.",
        ]
    if repo_type == "s3":
        created["immutability_effect"] = _immutability_effect_report(
            str(record["id"]),
            previous={"mode": "none", "retention_days": 0},
            current=s3_config.get("immutability"),
        )
    return created


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
    previous_immutability: dict[str, Any] | None = None
    next_immutability: dict[str, Any] | None = None
    next_s3_config: dict[str, Any] | None = None
    if "name" in payload and payload["name"] is not None:
        new_name = str(payload["name"])
        _ensure(bool(new_name.strip()), 422, "invalid_repository", "Repository name must not be empty")
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
        for field_name in ("server", "share"):
            if field_name in payload and payload[field_name] is not None:
                metadata[field_name] = str(payload[field_name]).strip() or None
                changed = True
        _ensure_local_repository_share(
            metadata.get("server"),
            metadata.get("share"),
            payload.get("username", metadata.get("username")),
            payload.get("password", metadata.get("password")),
        )
    elif repo_type == "s3":
        current_s3 = extract_s3_config(updated)
        next_s3 = _normalize_s3_configuration(payload, existing=current_s3)
        _validate_s3_configuration(next_s3)
        previous_immutability = normalize_immutability_config(current_s3.get("immutability"))
        next_immutability = normalize_immutability_config(next_s3.get("immutability"))
        next_s3_config = next_s3
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
            "immutability": normalize_immutability_config(next_s3.get("immutability")),
            "description": description,
        }
        next_path = _build_s3_repository_path(next_s3)
        _reject_storage_location_change_if_repository_has_backups(
            record_id, original_path=original_path, new_path=next_path
        )
        comparable_metadata = dict(metadata)
        if isinstance(comparable_metadata.get("s3"), dict):
            comparable_s3 = dict(comparable_metadata["s3"])
            comparable_s3["immutability"] = normalize_immutability_config(comparable_s3.get("immutability"))
            comparable_metadata["s3"] = comparable_s3
        if next_path != updated.get("path") or next_metadata != comparable_metadata:
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
    registration_check = None
    if _repository_connection_identity(updated) != _repository_connection_identity(record):
        registration_check = _assert_repository_is_usable(
            updated, create_bucket=bool(payload.get("create_bucket"))
        )
        if registration_check is not None:
            updated["last_verify"] = registration_check
    if next_s3_config is not None and immutability_is_enabled(next_immutability):
        mode_changed = previous_immutability.get("mode") != next_immutability.get("mode")
        connection_changed = registration_check is not None
        # Only when the answer can have changed. The bucket probe reads the
        # bucket's own Object Lock configuration -- whether it has one, how
        # strong it is, what its default rule says -- and none of that depends
        # on the retention period this repository asks for. Re-reading the
        # bucket to change "lock for 1 day" into "lock for 5 days" put a
        # round trip to the object store (and its timeouts) in front of an edit
        # that only rewrites one integer in the catalog. Turning the lock on,
        # changing its mode or repointing the repository still probe, and
        # "Validate" re-runs the whole thing on demand.
        if mode_changed or connection_changed:
            _assert_immutability_is_usable(
                next_s3_config,
                write_probe=mode_changed or connection_changed,
            )
    updated["updated_at"] = utc_now()
    persist_repository(updated)
    edited = normalize_repository_record(updated)
    if next_immutability is not None:
        edited["immutability_effect"] = _immutability_effect_report(
            record_id, previous=previous_immutability or {}, current=next_immutability
        )
    return edited


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


def _verify_local_repository(repository: dict[str, Any], *, whole_chains: bool = False) -> dict[str, Any]:
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

    backup_health = _verify_repository_backup_health(str(repository.get("id") or ""), whole_chains=whole_chains)
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


def _s3_format_checks(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    checks: list[dict[str, Any]] = []
    bucket = str(config.get("bucket") or "").strip()
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

    return checks, None


def s3_bucket_is_reachable(config: dict[str, Any]) -> bool:
    try:
        read_storage_folder_info(
            {"type": "s3", "s3": config},
            str(config.get("prefix") or "").strip(),
            timeout=_s3_probe_timeout(),
        )
    except StorageToolError:
        return False
    return True


def _s3_connectivity_checks(
    config: dict[str, Any], *, create_bucket: bool = True
) -> tuple[list[dict[str, Any]], str | None]:
    checks, error = _s3_format_checks(config)
    if error is not None:
        return checks, error
    prefix = str(config.get("prefix") or "").strip()
    bucket = str(config.get("bucket") or "").strip()

    if not create_bucket and not s3_bucket_is_reachable(config):
        checks.append({"name": "bucket_exists", "status": "failed", "message": bucket})
        return checks, (
            f"S3 bucket '{bucket}' could not be opened. Check that it exists and that these "
            "credentials can use it; if it has not been created yet, pass --create-bucket. A bucket "
            "created that way cannot hold immutable backups, because Object Lock can only be turned "
            "on while a bucket is being created."
        )

    try:
        probe_repository_access(
            {"type": "s3", "s3": config},
            abr_path=prefix,
            backup_name=f"repo-probe-{uuid4().hex[:12]}",
            timeout=_s3_probe_timeout(),
            create_folder=create_bucket,
        )
    except StorageToolError as exc:
        checks.append({"name": "connectivity", "status": "failed", "message": str(exc)})
        return checks, "S3 repository validation failed"

    checks.append({"name": "connectivity", "status": "ok", "message": "Bucket/prefix access validated through native storage tool"})
    return checks, None


def _verify_s3_repository(repository: dict[str, Any], *, whole_chains: bool = False) -> dict[str, Any]:
    config = extract_s3_config(repository)
    checks, error = _s3_connectivity_checks(config)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}

    immutability = normalize_immutability_config(config.get("immutability"))
    if immutability_is_enabled(immutability):
        immutability_checks, immutability_error = _s3_immutability_checks(
            config,
            mode=immutability["mode"],
            retention_days=immutability["retention_days"],
            write_probe=False,
        )
        checks.extend(immutability_checks)
        if immutability_error is not None:
            return {"status": "error", "checks": checks, "message": immutability_error}
    else:
        checks.append(
            {
                "name": "immutability",
                "status": "skipped",
                "message": "Repository immutability is disabled; backups are written without Object Lock retention",
            }
        )

    backup_health = _verify_repository_backup_health(str(repository.get("id") or ""), whole_chains=whole_chains)
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

    return {
        "status": "success",
        "checks": checks,
        "message": f"S3 repository is available; immutability: {_immutability_summary(immutability)}",
    }


def _s3_probe_timeout() -> int:
    return env_int("ALTASTOR_S3_PROBE_TIMEOUT_SECONDS")


def _s3_immutability_info_timeout() -> int:
    return env_int("ALTASTOR_S3_IMMUTABILITY_INFO_TIMEOUT_SECONDS")


def _s3_immutability_probe_timeout() -> int:
    return env_int("ALTASTOR_S3_IMMUTABILITY_PROBE_TIMEOUT_SECONDS")


def repository_actual_retention_days(job: dict[str, Any] | None, immutability: dict[str, Any]) -> int:
    return object_lock_actual_retention_days(
        job, immutability, block_generation_days=s3_block_generation_days()
    )


OBJECT_LOCK_UNAVAILABLE_HINT = (
    "Either this storage does not implement Object Lock at all, or the bucket was created without it - "
    "Object Lock can only be turned on when a bucket is created, never afterwards - or the credentials "
    "are not allowed to read the bucket's Object Lock configuration."
)


def _s3_object_lock_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    folder = read_storage_folder_info(
        {"type": "s3", "s3": config},
        normalize_s3_prefix(config.get("prefix")),
        timeout=_s3_immutability_info_timeout(),
    )
    return {
        "supported_mode": normalize_immutability_mode(folder.get("immutability_supported")),
        "default_mode": normalize_immutability_mode(folder.get("immutability")),
        "default_retention_days": int(folder.get("retention_days") or 0),
    }


def _bucket_default_rule_text(default_mode: str, default_days: int) -> str:
    if default_mode == "none" or default_days <= 0:
        return "the bucket has no default retention rule"
    return f"the bucket default retention rule is {default_mode} for {default_days} day(s)"


def _s3_immutability_checks(
    config: dict[str, Any],
    *,
    mode: str,
    retention_days: int,
    write_probe: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    checks: list[dict[str, Any]] = []
    bucket = str(config.get("bucket") or "").strip()
    mode = normalize_immutability_mode(mode)
    retention_days = int(retention_days or 0)

    try:
        snapshot = _s3_object_lock_snapshot(config)
    except StorageToolError as exc:
        detail = condense_disk_tool_message(exc)
        checks.append({"name": "object_lock", "status": "failed", "message": detail})
        return checks, f"Could not read the Object Lock configuration of S3 bucket '{bucket}': {detail}"

    supported_mode = snapshot["supported_mode"]
    default_mode = snapshot["default_mode"]
    default_days = snapshot["default_retention_days"]
    default_rule = _bucket_default_rule_text(default_mode, default_days)

    if immutability_rank(supported_mode) <= 0:
        checks.append(
            {
                "name": "object_lock",
                "status": "failed",
                "message": f"S3 bucket '{bucket}' reports no Object Lock configuration. {OBJECT_LOCK_UNAVAILABLE_HINT}",
            }
        )
        return checks, f"S3 bucket '{bucket}' does not support Object Lock, so it cannot hold immutable backups"
    checks.append(
        {
            "name": "object_lock",
            "status": "ok",
            "message": f"Object Lock is enabled, up to '{supported_mode}' mode; {default_rule}",
        }
    )
    versioning_check = {
        "name": "versioning",
        "status": "skipped",
        "message": (
            "Not read directly: the storage tool does not report a bucket's versioning state. Object Lock needs a "
            "versioned bucket and S3 does not allow versioning to be suspended while Object Lock is on, so "
            "versioning is implied here rather than checked. The write probe confirms it by behaviour."
        ),
    }
    checks.append(versioning_check)

    if mode == "none":
        checks.append(
            {
                "name": "immutability_mode",
                "status": "skipped",
                "message": "Repository immutability is disabled; the bucket would support it if it is enabled",
            }
        )
        return checks, None

    if immutability_rank(supported_mode) < immutability_rank(mode):
        checks.append(
            {
                "name": "immutability_mode",
                "status": "failed",
                "message": f"S3 bucket '{bucket}' supports at most '{supported_mode}' immutability, but '{mode}' was requested",
            }
        )
        return checks, f"S3 bucket '{bucket}' does not support {mode} immutability"

    if immutability_rank(default_mode) > immutability_rank(mode):
        checks.append(
            {
                "name": "immutability_mode",
                "status": "failed",
                "message": (
                    f"S3 bucket '{bucket}' applies {default_mode} immutability to new objects through its default "
                    f"retention rule, which is stronger than the requested {mode}. Request {default_mode} instead, "
                    "or relax the bucket's default retention rule."
                ),
            }
        )
        return checks, f"S3 bucket '{bucket}' applies {default_mode} immutability, stronger than the requested {mode}"
    checks.append({"name": "immutability_mode", "status": "ok", "message": f"{mode} is available on this bucket"})

    if default_days > retention_days:
        checks.append(
            {
                "name": "retention_period",
                "status": "failed",
                "message": (
                    f"S3 bucket '{bucket}' retains new objects for {default_days} day(s) through its default retention "
                    f"rule, longer than the requested {retention_days} day(s). Set the repository retention to at least "
                    f"{default_days} day(s), or relax the bucket's default retention rule."
                ),
            }
        )
        return checks, (
            f"S3 bucket '{bucket}' retains new objects for {default_days} day(s), "
            f"longer than the requested {retention_days} day(s)"
        )
    checks.append(
        {
            "name": "retention_period",
            "status": "ok",
            "message": f"{retention_days} day(s) requested; {default_rule}",
        }
    )

    if not write_probe:
        return checks, None

    try:
        probe = probe_repository_immutability(
            {"type": "s3", "s3": config},
            prefix=config.get("prefix"),
            mode=mode,
            retention_days=max(default_days, 1),
            timeout=_s3_immutability_probe_timeout(),
        )
    except StorageToolError as exc:
        detail = condense_disk_tool_message(exc)
        checks.append({"name": "write_retention", "status": "failed", "message": detail})
        return checks, f"S3 bucket '{bucket}' rejected a test backup written under {mode} Object Lock: {detail}"
    checks.append(
        {
            "name": "write_retention",
            "status": "ok",
            "message": f"A test backup was written under {mode} Object Lock retention",
        }
    )

    if immutability_rank(probe["mode"]) < immutability_rank(mode):
        checks.append(
            {
                "name": "retention_readback",
                "status": "failed",
                "message": (
                    f"The test backup reads back as '{probe['mode']}' retention instead of the requested '{mode}'"
                ),
            }
        )
        return checks, f"S3 bucket '{bucket}' did not apply the requested {mode} Object Lock retention"
    checks.append(
        {
            "name": "retention_readback",
            "status": "ok",
            "message": f"Stored retention reads back as {probe['mode']} for {probe['retention_days']} day(s)",
        }
    )

    if not probe["delete_rejected"]:
        checks.append(
            {
                "name": "worm_enforcement",
                "status": "failed",
                "message": "The locked test backup could be deleted, so Object Lock is not enforced on this bucket",
            }
        )
        return checks, f"S3 bucket '{bucket}' did not enforce Object Lock on a locked test backup"
    checks.append(
        {
            "name": "worm_enforcement",
            "status": "ok",
            "message": f"Deleting the locked test backup was refused by the storage: {probe['delete_error']}",
        }
    )
    versioning_check["status"] = "ok"
    versioning_check["message"] = (
        "Confirmed by behaviour: the storage refused to delete the locked test backup, which only a versioned "
        "bucket enforcing Object Lock can do"
    )
    return checks, None


def _immutability_summary(immutability: dict[str, Any]) -> str:
    if not immutability_is_enabled(immutability):
        return "disabled, backups stay deletable"
    return f"{immutability['mode']} for {immutability['retention_days']} day(s)"


def validate_s3_immutability_config(config: dict[str, Any], *, write_probe: bool = True) -> dict[str, Any]:
    """Check a bucket can hold backups under the configured Object Lock, and
    log the outcome: every repository add, edit and verify comes through here."""
    result = _validate_s3_immutability_config(config, write_probe=write_probe)
    immutability = normalize_immutability_config(config.get("immutability"))
    LOGGER.log(
        logging.INFO if result.get("status") == "success" else logging.WARNING,
        "Object Lock validation of S3 bucket %s (endpoint %s, %s, write probe %s): %s - %s",
        config.get("bucket"),
        config.get("endpoint_url") or "AWS",
        _immutability_summary(immutability) if immutability_is_enabled(immutability) else "immutability off",
        "on" if write_probe else "off",
        result.get("status"),
        result.get("message"),
    )
    return result


def _validate_s3_immutability_config(config: dict[str, Any], *, write_probe: bool = True) -> dict[str, Any]:
    checks, error = _s3_format_checks(config)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}

    immutability = normalize_immutability_config(config.get("immutability"))
    immutability_checks, error = _s3_immutability_checks(
        config,
        mode=immutability["mode"],
        retention_days=immutability["retention_days"],
        write_probe=write_probe,
    )
    checks.extend(immutability_checks)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}
    if not immutability_is_enabled(immutability):
        return {
            "status": "success",
            "checks": checks,
            "message": "S3 bucket supports immutable backups; repository immutability is currently disabled",
        }
    return {
        "status": "success",
        "checks": checks,
        "message": f"S3 repository is validated for immutable backups: {_immutability_summary(immutability)}",
    }


def _immutability_job_retention_warnings(repository_id: str, immutability: dict[str, Any]) -> list[str]:
    if not immutability_is_enabled(immutability):
        return []
    configured_days = int(immutability["retention_days"])
    scope = normalize_immutability_retention_scope(immutability.get("retention_scope"))
    warnings: list[str] = []
    for job in normalized_job_records():
        if str(job.get("repo_id") or "") != repository_id:
            continue
        job_days = int(job.get("retention_days") or 0)
        if job_days <= 0:
            continue
        lock_days = repository_actual_retention_days(job, immutability)
        if configured_days > job_days:
            warnings.append(
                f"Job '{job.get('name')}' keeps restore points for {job_days} day(s), but its backups stay locked for "
                f"{lock_days} day(s); the storage is paid for the whole lock, so keep the immutability period at or "
                "below the job retention unless the lock is what you are after."
            )
        elif scope == "minimum_only" and lock_days < job_days:
            warnings.append(
                f"Job '{job.get('name')}' keeps restore points for {job_days} day(s), but they are locked for only "
                f"{lock_days} day(s); from day {lock_days} to day {job_days} its backups are still needed but can be "
                "deleted. Switch this repository to job_retention scope, or raise the immutability period."
            )
    return warnings


def _immutability_effect_report(
    repository_id: str,
    *,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    previous = normalize_immutability_config(previous)
    current = normalize_immutability_config(current)
    existing_points = len(
        [point for point in normalized_backup_points() if str(point.get("repository_id") or "") == repository_id]
    )

    effect: list[str] = []
    limitations: list[str] = []

    if immutability_is_enabled(current):
        if normalize_immutability_retention_scope(current.get("retention_scope")) == "job_retention":
            effect.append(
                f"Backups created from now on are written under {current['mode']} Object Lock retention. Every backup "
                "of one chain is held to the same date: its job's next full backup, plus the longer of that job's "
                f"retention and {current['retention_days']} day(s)."
            )
        else:
            effect.append(
                f"Backups created from now on are written under {current['mode']} Object Lock retention for "
                f"{current['retention_days']} day(s), counted from each backup's own creation time."
            )
    else:
        effect.append("Immutability is disabled: new backups are written without Object Lock retention and stay deletable.")

    if existing_points:
        limitations.append(
            f"{existing_points} restore point(s) are already stored in this repository. Changing this setting does "
            "not touch them; run 'repo apply-immutability' to put them under the current one."
        )
        if immutability_is_enabled(previous):
            limitations.append(
                f"Each keeps the {previous['mode']} retention it was written under, counted from its own creation "
                "and derived from the settings in force at the time. The resulting date is recorded on the restore "
                "point, and the storage holds the authoritative copy."
            )
        else:
            limitations.append(
                "They were written without Object Lock, so they stay deletable and are not protected by this configuration."
            )

    weaker_mode = immutability_rank(current["mode"]) < immutability_rank(previous["mode"])
    shorter_period = (
        immutability_is_enabled(current)
        and immutability_is_enabled(previous)
        and current["retention_days"] < previous["retention_days"]
    )
    if weaker_mode or shorter_period:
        limitations.append(
            "Object Lock retention that is already written can never be shortened or removed, so relaxing immutability "
            "only affects backups created from now on."
        )

    return {
        "previous": previous,
        "current": current,
        "changed": previous != current,
        "existing_restore_points": existing_points,
        "existing_data_protected": False,
        "effect": effect,
        "limitations": limitations,
        "warnings": _immutability_job_retention_warnings(repository_id, current),
    }


def _assert_repository_supports_immutability(repository: dict[str, Any]) -> None:
    _ensure(
        str(repository.get("type") or "").strip().lower() == "s3",
        422,
        "invalid_repository",
        "Immutability is only available for s3 repositories",
        repository_id=repository.get("id"),
        repository_type=repository.get("type"),
    )


def _assert_immutability_is_usable(config: dict[str, Any], *, write_probe: bool = True) -> None:
    result = validate_s3_immutability_config(config, write_probe=write_probe)
    if result.get("status") == "success":
        return
    raise api_error(
        422,
        "repository_immutability_unsupported",
        str(result.get("message") or "S3 repository cannot be configured for immutable backups"),
        checks=result.get("checks") or [],
    )


def _restore_point_abr_targets(point: dict[str, Any]) -> list[tuple[str, str]]:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    targets: list[tuple[str, str]] = []
    manifest_paths: set[str] = set()
    for disk in artifact.get("disks", []):
        if not isinstance(disk, dict):
            continue
        abr_path = str(disk.get("abr_path") or "").strip()
        backup_name = str(disk.get("abr_backup_name") or "").strip()
        if not abr_path or not backup_name:
            continue
        targets.append((abr_path, backup_name))
        manifest_paths.add(abr_path)
    targets.extend((abr_path, "manifest") for abr_path in sorted(manifest_paths))
    return targets


def extend_segment_object_lock(segment_id: str, *, expires_at: str, skip_point_ids: set[str] | None = None) -> dict[str, Any]:
    target = _parse_dt(expires_at)
    remaining_days = max(int(-(-(target - _now_utc()).total_seconds() // 86400)), 1)
    skip = skip_point_ids or set()
    extended: list[str] = []
    failures: list[dict[str, Any]] = []

    for point in normalized_backup_points():
        if str(point.get("segment_id") or "") != segment_id or str(point.get("id") or "") in skip:
            continue
        repository = REPOSITORIES.get(str(point.get("repository_id") or ""))
        if not isinstance(repository, dict) or not is_s3_repository(repository):
            continue
        config = normalize_immutability_config(extract_s3_config(repository).get("immutability"))
        if not immutability_is_enabled(config):
            continue
        stored_expiry = str(point.get("immutability_expires_at") or "").strip()
        if stored_expiry and _parse_dt(stored_expiry) >= target:
            continue
        point_failed = False
        for abr_path, backup_name in _restore_point_abr_targets(point):
            try:
                apply_repository_immutability(
                    repository,
                    abr_path,
                    backup_name,
                    retention_days=remaining_days,
                    timeout=_s3_immutability_probe_timeout(),
                )
            except StorageToolError as exc:
                point_failed = True
                failures.append(
                    {
                        "restore_point_id": str(point.get("id") or ""),
                        "backup_name": backup_name,
                        "message": condense_disk_tool_message(exc),
                    }
                )
        if point_failed:
            continue
        stored = BACKUPS.get(str(point.get("id") or ""))
        if isinstance(stored, dict):
            stored = dict(stored)
            stored["immutability_expires_at"] = expires_at
            persist_backup(stored)
        extended.append(str(point.get("id") or ""))

    return {"extended_restore_points": extended, "failures": failures, "retention_days": remaining_days}


def _applied_period_text(applied_days: set[int], config: dict[str, Any]) -> str:
    configured = int(config.get("retention_days") or 0)
    if not applied_days:
        return f"for {configured} day(s)"
    low, high = min(applied_days), max(applied_days)
    span = f"{low} day(s)" if low == high else f"{low}-{high} day(s)"
    return f"for {span} (its full-backup cycle plus the longer of the job retention and the {configured} configured)"


def _apply_immutability_to_existing_backups(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    _assert_repository_supports_immutability(repository)
    config = normalize_immutability_config(extract_s3_config(repository).get("immutability"))
    _ensure(
        immutability_is_enabled(config),
        422,
        "invalid_repository",
        "Repository immutability is disabled; enable it before applying it to stored backups",
        repository_id=record_id,
    )

    points = [point for point in normalized_backup_points() if str(point.get("repository_id") or "") == record_id]
    updated: list[str] = []
    failures: list[dict[str, Any]] = []
    applied_days: set[int] = set()
    attempted_days: set[int] = set()
    for point in points:
        point_failed = False
        job = JOBS.get(str(point.get("job_id") or ""))
        actual_days = repository_actual_retention_days(job if isinstance(job, dict) else None, config)
        attempted_days.add(actual_days)
        for abr_path, backup_name in _restore_point_abr_targets(point):
            try:
                apply_repository_immutability(
                    repository,
                    abr_path,
                    backup_name,
                    retention_days=actual_days,
                    timeout=_s3_immutability_probe_timeout(),
                )
            except StorageToolError as exc:
                point_failed = True
                failures.append(
                    {
                        "restore_point_id": str(point.get("id") or ""),
                        "backup_name": backup_name,
                        "message": condense_disk_tool_message(exc),
                    }
                )
        if not point_failed:
            updated.append(str(point.get("id") or ""))
            applied_days.add(actual_days)
            stored = BACKUPS.get(str(point.get("id") or ""))
            if isinstance(stored, dict) and actual_days > 0:
                stored = dict(stored)
                stored["immutability_expires_at"] = (_now_utc() + timedelta(days=actual_days)).isoformat()
                persist_backup(stored)

    result = {
        "id": record_id,
        "name": repository.get("name"),
        "immutability": config,
        "actual_retention_days": sorted(applied_days),
        "restore_point_count": len(points),
        "updated_restore_points": len(updated),
        "failed_restore_points": len(points) - len(updated),
        "failures": failures,
    }
    if not points:
        result["status"] = "success"
        result["message"] = "Repository holds no restore points; nothing to place under Object Lock"
        return result
    period = _applied_period_text(applied_days or attempted_days, config)
    if failures:
        result["status"] = "error"
        result["message"] = (
            f"{len(updated)} of {len(points)} restore point(s) were placed under {config['mode']} Object Lock "
            f"{period}; the storage refused the rest"
        )
        return result
    result["status"] = "success"
    result["message"] = (
        f"{len(updated)} restore point(s) are now held in {config['mode']} mode {period}, counted from now"
    )
    return result


def apply_immutability_to_existing_backups(record_id: str) -> dict[str, Any]:
    result = _apply_immutability_to_existing_backups(record_id)
    LOGGER.log(
        logging.INFO if result.get("status") == "success" else logging.WARNING,
        "Object Lock applied to existing backups of repository %s: %s of %s restore point(s) updated, "
        "%s refused - %s",
        result.get("name") or record_id,
        result.get("updated_restore_points"),
        result.get("restore_point_count"),
        result.get("failed_restore_points"),
        result.get("message"),
    )
    return result


def test_s3_immutability(payload: dict[str, Any]) -> dict[str, Any]:
    config = _normalize_s3_configuration(payload)
    _validate_s3_configuration(config)
    return validate_s3_immutability_config(config)


def verify_repository_immutability(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    _assert_repository_supports_immutability(repository)
    config = extract_s3_config(repository)
    result = validate_s3_immutability_config(config)
    payload = normalize_repository_record(repository)
    payload.update(result)
    immutability = normalize_immutability_config(config.get("immutability"))
    payload["immutability_effect"] = _immutability_effect_report(
        record_id, previous=immutability, current=immutability
    )
    return payload


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


def _repository_verification_result(repository: dict[str, Any]) -> dict[str, Any]:
    repo_type = str(repository.get("type") or "")
    if repo_type == "local":
        return _verify_local_repository(repository, whole_chains=True)
    if repo_type == "s3":
        return _verify_s3_repository(repository, whole_chains=True)
    return _verify_remote_repository(repository)


def _repository_connection_identity(record: dict[str, Any]) -> tuple[str, ...]:
    if str(record.get("type") or "") == "s3":
        config = extract_s3_config(record)
        return tuple(
            str(config.get(key) or "")
            for key in (
                "bucket",
                "prefix",
                "region",
                "endpoint_url",
                "access_key",
                "secret_key",
                "profile",
                "path_style",
                "verify_tls",
            )
        )
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return (
        str(record.get("path") or ""),
        str(metadata.get("server") or ""),
        str(metadata.get("share") or ""),
        str(metadata.get("username") or ""),
        str(metadata.get("password") or ""),
    )


def _verification_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(result.get("status") or "error"),
        "message": str(result.get("message") or ""),
        "checked_at": utc_now(),
    }


def _repository_registration_checks(
    record: dict[str, Any], *, create_bucket: bool = False
) -> dict[str, Any] | None:
    repo_type = str(record.get("type") or "")
    if repo_type == "local":
        return _verify_local_repository(record)
    if repo_type != "s3":
        return None
    config = extract_s3_config(record)
    if immutability_is_enabled(normalize_immutability_config(config.get("immutability"))):
        checks, error = _s3_format_checks(config)
        if error is not None:
            return {"status": "error", "checks": checks, "message": error}
        return {"status": "success", "checks": checks, "message": "S3 endpoint is reachable"}
    checks, error = _s3_connectivity_checks(config, create_bucket=create_bucket)
    if error is not None:
        return {"status": "error", "checks": checks, "message": error}
    return {
        "status": "success",
        "checks": checks,
        "message": "S3 repository is available; immutability: disabled, backups stay deletable",
    }


def _assert_bucket_creation_is_sensible(record: dict[str, Any], *, create_bucket: bool) -> None:
    if not create_bucket or str(record.get("type") or "") != "s3":
        return
    config = extract_s3_config(record)
    if not immutability_is_enabled(normalize_immutability_config(config.get("immutability"))):
        return
    raise api_error(
        422,
        "repository_bucket_cannot_be_created",
        "Object Lock can only be turned on while a bucket is being created, and --create-bucket "
        "cannot turn it on, so a bucket created that way could never hold immutable backups. Create "
        "the bucket with Object Lock enabled first, then add the repository without --create-bucket.",
        bucket=config.get("bucket"),
    )


def _assert_repository_is_usable(
    record: dict[str, Any], *, create_bucket: bool = False
) -> dict[str, Any] | None:
    _assert_bucket_creation_is_sensible(record, create_bucket=create_bucket)
    result = _repository_registration_checks(record, create_bucket=create_bucket)
    if result is None:
        return None
    if str(result.get("status") or "") != "success":
        raise api_error(
            422,
            "repository_validation_failed",
            str(result.get("message") or "Repository cannot be used for backups"),
            checks=result.get("checks") or [],
        )
    return _verification_summary(result)


def verify_repository(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    result = _repository_verification_result(repository)
    stored = _json_clone(repository)
    stored["last_verify"] = _verification_summary(result)
    persist_repository(stored)
    payload = normalize_repository_record(stored)
    payload.update(result)
    return payload


def rescan_repository(record_id: str) -> dict[str, Any]:
    repository = _find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
    reconciled = reconcile_repository_points(repository)
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
        "restore_points": reconciled,
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


DERIVED_FULL_SCHEDULE_TIME = "00:00"
DERIVED_FULL_SCHEDULE_TIMEZONE = "UTC"


def _time_of_day_in_schedule_timezone(created_at: Any, schedule: dict[str, Any]) -> str:
    moment = _parse_dt(created_at) if created_at else None
    if moment is None:
        return DERIVED_FULL_SCHEDULE_TIME
    return _localize(moment, schedule).strftime("%H:%M")


def _schedule_from_legacy_days(
    days: list[str], base_schedule: Any, created_at: Any = None
) -> dict[str, Any] | None:
    if not days:
        return None
    base = base_schedule if isinstance(base_schedule, dict) else None
    if base is None:
        time_value, timezone_name = DERIVED_FULL_SCHEDULE_TIME, DERIVED_FULL_SCHEDULE_TIMEZONE
    else:
        time_value = str(base.get("time") or "") or _time_of_day_in_schedule_timezone(created_at, base)
        timezone_name = base.get("timezone") or DERIVED_FULL_SCHEDULE_TIMEZONE
    return normalize_schedule_payload(
        {
            "type": "weekly",
            "days": list(days),
            "time": time_value,
            "timezone": timezone_name,
            "enabled": True,
        }
    )


def _normalize_execution_schedule(
    raw: Any, legacy_days: list[str], base_schedule: Any, created_at: Any
) -> dict[str, Any] | None:
    if raw in (None, ""):
        return _schedule_from_legacy_days(legacy_days, base_schedule, created_at)
    return normalize_schedule_payload(raw)


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
    item["active_full_schedule"] = _normalize_execution_schedule(
        item.get("active_full_schedule") or metadata.get("active_full_schedule"),
        item["active_full_days"],
        item["schedule"],
        item.get("created_at"),
    )
    item["synthetic_full_schedule"] = _normalize_execution_schedule(
        item.get("synthetic_full_schedule") or metadata.get("synthetic_full_schedule"),
        item["synthetic_full_days"],
        item["schedule"],
        item.get("created_at"),
    )
    runs = _job_is_enabled(item)
    for schedule_key, _days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
        if not runs:
            item[next_run_key] = None
        elif not item.get(next_run_key):
            item[next_run_key] = _execution_schedule_next_run(item.get(schedule_key))
    item["description"] = str(item.get("description") or metadata.get("description") or "")
    task_parameters = item.get("task_parameters")
    if not isinstance(task_parameters, dict):
        task_parameters = metadata.get("task_parameters") if isinstance(metadata.get("task_parameters"), dict) else {}
    item["task_parameters"] = dict(task_parameters)
    item["enabled"] = _job_is_enabled(item)
    item["schedule_state"] = "enabled" if item["schedule"] and item["enabled"] else "disabled" if item["schedule"] else "manual"
    item["vm_count"] = len(item["vm_ids"]) or len(item["vm_names"])
    if not item["enabled"]:
        item["status"] = "disabled"
    elif _job_has_a_schedule(item):
        item["status"] = "active"
    else:
        item["status"] = str(item.get("status") or "planned")
    item["created_at"] = str(item.get("created_at") or utc_now())
    item["updated_at"] = str(item.get("updated_at") or item["created_at"])
    stored_incremental = item.get("next_incremental_run_at") if "next_incremental_run_at" in item else item.get("next_run_at")
    item.pop("next_run_at", None)
    item["next_incremental_run_at"] = stored_incremental if item["enabled"] else None
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


MAX_RETENTION_DAYS = 3650
MAX_GFS_COUNTS = {"weekly": 520, "monthly": 120, "yearly": 99}


def _whole_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate_retention_days(value: Any) -> int:
    days = _whole_number(value)
    _ensure(days is not None, 422, "invalid_job", "retention-days must be a whole number", retention_days=value)
    _ensure(days > 0, 422, "invalid_job", "retention-days must be greater than 0", retention_days=value)
    _ensure(
        days <= MAX_RETENTION_DAYS,
        422,
        "invalid_job",
        f"retention-days must be at most {MAX_RETENTION_DAYS}",
        retention_days=value,
    )
    return days


def _validate_gfs_counts(gfs: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key in ("weekly", "monthly", "yearly"):
        value = gfs.get(key)
        count = 0 if value is None else _whole_number(value)
        _ensure(count is not None, 422, "invalid_job", "GFS counts must be integers", field=key)
        _ensure(count >= 0, 422, "invalid_job", "GFS counts must be >= 0", field=key)
        _ensure(
            count <= MAX_GFS_COUNTS[key],
            422,
            "invalid_job",
            f"GFS {key} count must be at most {MAX_GFS_COUNTS[key]}",
            field=key,
        )
        result[key] = count
    return result


def _ensure_known_timezone(schedule: dict[str, Any] | None) -> dict[str, Any] | None:
    name = str((schedule or {}).get("timezone") or "").strip()
    if not name:
        return schedule
    try:
        ZoneInfo(name)
        return schedule
    except (ZoneInfoNotFoundError, ValueError):
        pass
    _ensure(
        name.upper() in _server_timezone_aliases(),
        422,
        "invalid_job",
        f"Unknown time zone {name!r}: use an IANA time zone name such as Europe/Kyiv or UTC",
        timezone=name,
    )
    return schedule


def _schedule_from_request(raw: Any) -> dict[str, Any] | None:
    return _ensure_known_timezone(normalize_schedule_payload(raw))


def _ensure_unique_vms(vm_ids: list[str], vm_names: list[str]) -> None:
    seen: set[str] = set()
    for vm_id, vm_name in zip(vm_ids, vm_names):
        _ensure(
            vm_id not in seen,
            422,
            "invalid_job",
            f"VM {vm_name} is listed more than once",
            vm=vm_name,
        )
        seen.add(vm_id)


def _coerce_gfs_enabled(value: Any, *, default: bool = True) -> bool:
    """`enabled` suspends GFS protection without discarding the configured counts."""
    return default if value is None else bool(value)


def _ensure_encryption_key_strength(task_parameters: Any) -> None:
    if not isinstance(task_parameters, dict):
        return
    key = task_parameters.get("encryption_key")
    if not isinstance(key, str) or not key.strip():
        return
    _ensure(
        len(key.strip()) >= MIN_PASSWORD_LENGTH,
        422,
        "invalid_job",
        f"Encryption key must be at least {MIN_PASSWORD_LENGTH} characters.",
    )


def _normalized_encryption_keys(raw: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        try:
            key_id = int(item.get("key_id"))
        except (TypeError, ValueError):
            continue
        if key_id < 0 or key_id in seen:
            continue
        seen.add(key_id)
        entries.append({"key_id": key_id, "key": key})
    entries.sort(key=lambda entry: entry["key_id"])
    return entries


def _record_encryption_key(keys: list[dict[str, Any]], key: str) -> tuple[list[dict[str, Any]], int]:
    for entry in keys:
        if entry["key"] == key:
            return keys, int(entry["key_id"])
    next_id = max((int(entry["key_id"]) for entry in keys), default=-1) + 1
    return [*keys, {"key_id": next_id, "key": key}], next_id


TASK_PARAMETER_CHOICES = {
    "hyperv_consistency": ("application", "crash"),
}


def _validate_task_parameters(params: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(params)
    for key, allowed in TASK_PARAMETER_CHOICES.items():
        if result.get(key) is None:
            continue
        value = str(result[key]).strip().lower()
        _ensure(
            value in allowed,
            422,
            "invalid_job",
            f"task_parameters.{key} must be one of: {', '.join(allowed)}",
        )
        result[key] = value
    if "compression_level" in result and result["compression_level"] is not None:
        level = _whole_number(result["compression_level"])
        _ensure(
            level is not None and 0 <= level <= 9,
            422,
            "invalid_job",
            "task_parameters.compression_level must be an integer between 0 and 9",
        )
        result["compression_level"] = level

    if result.get("encryption_enabled"):
        key = result.get("encryption_key")
        known = _normalized_encryption_keys(
            result.get("encryption_keys")
            or (previous or {}).get("encryption_keys")
        )
        if not known:
            carried = str((previous or {}).get("encryption_key") or "").strip()
            if carried:
                known = [{"key_id": 0, "key": carried}]
        _ensure(
            (isinstance(key, str) and bool(key.strip())) or bool(known),
            422,
            "encryption_key_required",
            "Turning encryption on requires an encryption key: supply one with --encryption-key",
        )
        if isinstance(key, str) and key.strip():
            result["encryption_key"] = key.strip()
        else:
            result["encryption_key"] = str(known[-1]["key"])
        known, current_id = _record_encryption_key(known, str(result["encryption_key"]))
        result["encryption_keys"] = known
        result["encryption_key_id"] = current_id
    else:
        result.pop("encryption_keys", None)
        result.pop("encryption_key_id", None)
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
        "active_full_schedule": _schedule_dto(job.get("active_full_schedule")),
        "synthetic_full_schedule": _schedule_dto(job.get("synthetic_full_schedule")),
        "active_overrides_synthetic": True,
        "synthetic_requires_repository_support": True,
        "default": {"type": "Incremental", "reason": "StandardIncremental"},
    }


def stored_slot_next_run(job: dict[str, Any], next_run_key: str) -> Any:
    if next_run_key in job:
        return job.get(next_run_key)
    if next_run_key == "next_incremental_run_at":
        return job.get("next_run_at")
    return None


def _earliest_next_run(job: dict[str, Any]) -> str | None:
    earliest: tuple[datetime, str] | None = None
    for key in ("next_incremental_run_at", "active_full_next_run_at", "synthetic_full_next_run_at"):
        value = job.get(key)
        if not value:
            continue
        try:
            moment = _parse_dt(value)
        except (TypeError, ValueError):
            continue
        if earliest is None or moment < earliest[0]:
            earliest = (moment, str(value))
    return earliest[1] if earliest else None


def job_to_dto(job: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_job_record(job)
    active_sessions = _active_sessions_for_job(str(normalized["id"]))
    task_parameters = normalized.get("task_parameters") if isinstance(normalized.get("task_parameters"), dict) else {}
    compression_level = task_parameters.get("compression_level")
    if compression_level is None:
        compression_level = DEFAULT_COMPRESSION_LEVEL
    encryption_enabled = bool(task_parameters.get("encryption_enabled"))
    has_encryption_key = bool(str(task_parameters.get("encryption_key") or "").strip())
    encryption_keys = _normalized_encryption_keys(task_parameters.get("encryption_keys"))
    current_key_id = task_parameters.get("encryption_key_id")
    task_parameters_view = mask_credential_fields(dict(task_parameters))
    if "encryption_keys" in task_parameters_view:
        task_parameters_view["encryption_keys"] = [
            {"key_id": entry["key_id"], "key": SECRET_MASK} for entry in encryption_keys
        ]
    return {
        "id": normalized["id"],
        "name": normalized["name"],
        "job_type": normalized["job_type"],
        "description": normalized.get("description") or None,
        "inventory": {
            "id": normalized.get("inventory_id") or None,
            "name": normalized.get("inventory_name") or None,
        },
        "storage_layout": str(normalized.get("storage_layout") or STORAGE_LAYOUT_BY_NAME),
        "vm_ids": list(normalized.get("vm_ids") or []),
        "vm_names": _job_vm_current_names(normalized) if len(normalized.get("vm_ids") or []) == len(normalized.get("vm_names") or []) else list(normalized.get("vm_names") or []),
        "vm_count": int(normalized.get("vm_count") or 0),
        "vms": [
            {"id": vm_id or None, "name": vm_name or None}
            for vm_id, vm_name in zip(normalized.get("vm_ids") or [], _job_vm_current_names(normalized))
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
        "active_full_schedule": _schedule_dto(normalized.get("active_full_schedule")),
        "synthetic_full_schedule": _schedule_dto(normalized.get("synthetic_full_schedule")),
        "active_full_next_run_at": normalized.get("active_full_next_run_at"),
        "synthetic_full_next_run_at": normalized.get("synthetic_full_next_run_at"),
        "compression_level": compression_level,
        "encryption": {
            "enabled": encryption_enabled,
            "key_set": has_encryption_key,
            "key_id": current_key_id,
            "key_ids": [entry["key_id"] for entry in encryption_keys],
            "key_count": len(encryption_keys),
        },
        "task_parameters": task_parameters_view,
        "backup_options": task_parameters_view,
        "effective_execution_policy": job_effective_policy(normalized),
        "status": normalized.get("status"),
        "created_at": normalized.get("created_at"),
        "updated_at": normalized.get("updated_at"),
        "next_run_at": _earliest_next_run(normalized),
        "next_incremental_run_at": normalized.get("next_incremental_run_at"),
        "last_run_at": normalized.get("last_run_at"),
        "clock_anomalies": job_clock_anomalies(normalized),
        "last_session_id": normalized.get("last_session_id"),
        "last_run": job_run_summary(normalized),
        "active_sessions": active_sessions,
        "active_session_count": len(active_sessions),
        "can_delete": not active_sessions,
        "delete_disabled_reason": "Job has queued or running sessions" if active_sessions else None,
    }


def job_run_record(run_id: str, *, trigger: str, started_at: str, session_ids: list[str]) -> dict[str, Any] | None:
    if not session_ids:
        return None
    return {"id": run_id, "trigger": trigger, "started_at": started_at, "session_ids": list(session_ids)}


RUN_RESULTS_WORST_FIRST = ("running", "failed", "stopped", "warning", "success")


def _session_run_result(task: dict[str, Any]) -> str:
    state = _session_state(task)
    if state == "failed":
        return "failed"
    if state in {"stopped", "canceled"}:
        return "stopped"
    if state == "completed":
        return "warning" if task.get("warnings") else "success"
    return "running"


def _worse(first: str, second: str) -> str:
    return min(first, second, key=RUN_RESULTS_WORST_FIRST.index)


def job_run_summary(job: dict[str, Any]) -> dict[str, Any] | None:
    run = job.get("last_run")
    if not isinstance(run, dict) or not run.get("id"):
        return None
    vms: dict[str, dict[str, Any]] = {}
    finished: list[str] = []
    for session_id in run.get("session_ids") or []:
        task = TASKS.get(str(session_id))
        if not isinstance(task, dict):
            continue
        session = session_to_dto(task, include_logs=False)
        result = _session_run_result(task)
        vm_key = str(task.get("resource_external_id") or session_id)
        entry = vms.setdefault(
            vm_key,
            {"vm_id": session.get("vm_id"), "vm_name": session.get("vm_name"), "result": result, "session_ids": [], "message": None},
        )
        entry["session_ids"].append(str(session_id))
        entry["result"] = _worse(entry["result"], result)
        if result in {"failed", "stopped"} and not entry["message"]:
            errors = session.get("errors") or []
            entry["message"] = session.get("message") or (errors[0].get("message") if errors and isinstance(errors[0], dict) else None)
        if session.get("finished_at"):
            finished.append(str(session["finished_at"]))
    if not vms:
        return None
    counts = {name: sum(1 for vm in vms.values() if vm["result"] == name) for name in RUN_RESULTS_WORST_FIRST}
    result = "success"
    for vm in vms.values():
        result = _worse(result, vm["result"])
    return {
        "id": run["id"],
        "trigger": run.get("trigger"),
        "started_at": run.get("started_at"),
        "finished_at": max(finished) if result != "running" and finished else None,
        "result": result,
        "vm_count": len(vms),
        "succeeded": counts["success"] + counts["warning"],
        **{name: counts[name] for name in ("warning", "failed", "stopped", "running")},
        "vms": list(vms.values()),
    }


def job_clock_anomalies(job: dict[str, Any]) -> list[dict[str, Any]]:
    now = _now_utc()
    anomalies: list[dict[str, Any]] = []
    raw_last_run = str(job.get("last_run_at") or "")
    try:
        last_run = _parse_dt(raw_last_run) if raw_last_run else None
    except ValueError:
        last_run = None
    if last_run is not None and last_run - now > CLOCK_SKEW_TOLERANCE:
        anomalies.append(
            {
                "kind": "last_run_ahead_of_server_clock",
                "last_run_at": raw_last_run,
                "server_time": now.isoformat(),
                "message": (
                    f"The last run is dated {raw_last_run}, ahead of this server's time ({now.isoformat()}). "
                    "It was recorded while this server's clock was ahead, so the job shows a last run later "
                    "than its next run until the clock reaches that date. Check the clock on this server."
                ),
            }
        )
    return anomalies


def list_jobs() -> list[dict[str, Any]]:
    return [job_to_dto(job) for job in normalized_job_records()]


EXECUTION_SCHEDULE_FIELDS = (
    ("active_full_schedule", "active_full_days", "active_full_next_run_at"),
    ("synthetic_full_schedule", "synthetic_full_days", "synthetic_full_next_run_at"),
)


def _execution_schedule_next_run(schedule: dict[str, Any] | None) -> str | None:
    if not isinstance(schedule, dict) or not bool(schedule.get("enabled", True)):
        return None
    return schedule_next_run(schedule, after=_now_utc()).isoformat()


def _execution_schedule_state_key(schedule_key: str) -> str:
    return schedule_key.removesuffix("_schedule") + "_enabled"


def _requested_execution_schedule_state(payload: dict[str, Any], schedule_key: str) -> bool | None:
    value = payload.get(_execution_schedule_state_key(schedule_key))
    return None if value is None else bool(value)


def _execution_schedule_with_state(
    schedule: Any,
    state: bool | None,
    legacy_days: Any,
    base_schedule: Any,
    created_at: Any,
) -> Any:
    if state is None:
        return schedule
    if isinstance(schedule, dict):
        return dict(schedule, enabled=state)
    derived = _schedule_from_legacy_days(_normalize_day_list(legacy_days), base_schedule, created_at)
    return dict(derived, enabled=state) if isinstance(derived, dict) else schedule


def _has_full_backup_schedule(schedule: Any, legacy_days: Any) -> bool:
    if isinstance(schedule, dict):
        return bool(schedule.get("enabled", True))
    return bool(legacy_days)


def _ensure_full_backup_schedule(job: dict[str, Any]) -> None:
    enabled = any(
        _has_full_backup_schedule(job.get(schedule_key), job.get(days_key))
        for schedule_key, days_key, _next_run_key in EXECUTION_SCHEDULE_FIELDS
    )
    _ensure(
        enabled,
        422,
        "invalid_job",
        "A backup job requires at least one full backup schedule so that retention can free the chain: "
        "set an active full or a synthetic full schedule",
    )


def create_backup_job(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    vm_tokens = payload.get("vms")
    repo_name = payload.get("repo")
    repo_id = payload.get("repo_id")
    inventory_name = payload.get("inventory")
    inventory_id = payload.get("inventory_id")
    raw_schedule = payload.get("schedule")
    schedule = _schedule_from_request(raw_schedule) if raw_schedule not in (None, "") else None
    retention_days = _validate_retention_days(payload["retention_days"]) if "retention_days" in payload else 14
    raw_gfs = payload.get("gfs")
    gfs = _validate_gfs_counts(raw_gfs if isinstance(raw_gfs, dict) else payload)
    gfs["enabled"] = _coerce_gfs_enabled(raw_gfs.get("enabled") if isinstance(raw_gfs, dict) else None)
    active_full_days = _normalize_day_list(payload.get("active_full_days") or _split_csv(payload.get("active_full_days_csv")))
    synthetic_full_days = _normalize_day_list(payload.get("synthetic_full_days") or _split_csv(payload.get("synthetic_full_days_csv")))
    active_full_schedule = _schedule_from_request(payload.get("active_full_schedule"))
    synthetic_full_schedule = _schedule_from_request(payload.get("synthetic_full_schedule"))
    if "active_full_schedule" in payload and not _schedule_weekdays(active_full_schedule):
        active_full_days = []
    if "synthetic_full_schedule" in payload and not _schedule_weekdays(synthetic_full_schedule):
        synthetic_full_days = []
    now = utc_now()
    active_full_schedule = _execution_schedule_with_state(
        active_full_schedule,
        _requested_execution_schedule_state(payload, "active_full_schedule"),
        active_full_days,
        schedule,
        now,
    )
    synthetic_full_schedule = _execution_schedule_with_state(
        synthetic_full_schedule,
        _requested_execution_schedule_state(payload, "synthetic_full_schedule"),
        synthetic_full_days,
        schedule,
        now,
    )
    base_is_enabled = isinstance(schedule, dict) and bool(schedule.get("enabled", True))
    job_enabled = bool(payload.get("enabled", True)) if not isinstance(schedule, dict) else base_is_enabled
    description = str(payload.get("description") or "").strip()
    task_parameters = payload.get("task_parameters") if isinstance(payload.get("task_parameters"), dict) else payload.get("backup_options")
    if task_parameters is None:
        task_parameters = {}
    _ensure(isinstance(task_parameters, dict), 422, "invalid_job", "task_parameters must be an object")
    _ensure_encryption_key_strength(task_parameters)
    task_parameters = _validate_task_parameters(task_parameters)
    _ensure(bool(name.strip()), 422, "invalid_job", "Job name is required")
    _ensure_ascii_name(name, field="Job name", code="invalid_job")
    _ensure_ascii_text(description, field="Job description", code="invalid_job", max_length=MAX_LONG_TEXT_LENGTH)
    _ensure(retention_days > 0, 422, "invalid_job", "retention-days must be greater than 0")
    _ensure_full_backup_schedule(
        {
            "active_full_schedule": active_full_schedule,
            "active_full_days": active_full_days,
            "synthetic_full_schedule": synthetic_full_schedule,
            "synthetic_full_days": synthetic_full_days,
        }
    )
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
    _ensure_unique_vms(vm_ids, vm_names)
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
        "active_full_schedule": active_full_schedule,
        "synthetic_full_schedule": synthetic_full_schedule,
        "active_full_next_run_at": _execution_schedule_next_run(active_full_schedule) if base_is_enabled else None,
        "synthetic_full_next_run_at": _execution_schedule_next_run(synthetic_full_schedule) if base_is_enabled else None,
        "description": description,
        "task_parameters": dict(task_parameters),
        "storage_layout": STORAGE_LAYOUT_BY_ID,
        "enabled": job_enabled,
        "status": "active" if job_enabled else "disabled",
        "created_at": now,
        "updated_at": now,
        "next_incremental_run_at": schedule_next_run(schedule, after=_parse_dt(now)).isoformat() if schedule and bool(schedule.get("enabled", True)) else None,
        "last_run_at": None,
        "last_session_id": None,
    }
    return job_to_dto(_persist_canonical_job(job))


def create_copy_job(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    source_job = resolve_job(
        name=payload.get("source") or payload.get("source_job"),
        record_id=payload.get("source_id") or payload.get("source_job_id"),
    )
    repository_selector_name = payload.get("repo") or payload.get("repository")
    repository_selector_id = payload.get("repo_id") or payload.get("repository_id")
    repository = resolve_repository(name=repository_selector_name, record_id=repository_selector_id) if (repository_selector_name or repository_selector_id) else None
    raw_schedule = payload.get("schedule")
    schedule = _schedule_from_request(raw_schedule) if raw_schedule not in (None, "") else _json_clone(source_job.get("schedule"))
    enabled_override = payload.get("enabled")
    if isinstance(schedule, dict) and enabled_override is not None:
        schedule["enabled"] = bool(enabled_override)
    job_enabled = bool(enabled_override) if enabled_override is not None else (
        bool(schedule.get("enabled", True)) if isinstance(schedule, dict) else True
    )
    if "retention_days" in payload:
        retention_days = _validate_retention_days(payload["retention_days"])
    else:
        raw_retention_days = source_job.get("retention_days")
        retention_days = int(raw_retention_days) if raw_retention_days is not None else 14
    description = str(payload.get("description") or source_job.get("description") or "").strip()
    _ensure(bool(name.strip()), 422, "invalid_job", "Job name is required")
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
        "active_full_schedule": _json_clone(source_job.get("active_full_schedule")),
        "synthetic_full_schedule": _json_clone(source_job.get("synthetic_full_schedule")),
        "description": description,
        "task_parameters": dict(source_task_parameters),
        "storage_layout": STORAGE_LAYOUT_BY_ID,
        "enabled": job_enabled,
        "status": "active" if job_enabled else "disabled",
        "created_at": now,
        "updated_at": now,
        "next_incremental_run_at": schedule_next_run(schedule, after=_parse_dt(now)).isoformat() if schedule and bool(schedule.get("enabled", True)) else None,
        "last_run_at": None,
        "last_session_id": None,
    }
    if not job["source_job_id"]:
        job.pop("source_job_id", None)
        job.pop("source_job_name", None)
    return job_to_dto(_persist_canonical_job(job))


def _schedule_weekdays(schedule: Any) -> list[str]:
    if not isinstance(schedule, dict) or str(schedule.get("kind") or "") != "weekly":
        return []
    return _normalize_day_list(schedule.get("weekdays") or [])


def _retune_weekly_schedule(schedule: Any, days: list[str]) -> dict[str, Any] | None:
    if not isinstance(schedule, dict) or str(schedule.get("kind") or "") != "weekly":
        return schedule
    if not days:
        return None
    return dict(schedule, weekdays=list(days))


def _touches_full_backup_policy(payload: dict[str, Any]) -> bool:
    if payload.get("retention_days") is not None:
        return True
    for schedule_key, days_key, _next_run_key in EXECUTION_SCHEDULE_FIELDS:
        if schedule_key in payload:
            return True
        if payload.get(days_key) is not None:
            return True
        if payload.get(_execution_schedule_state_key(schedule_key)) is not None:
            return True
    return False


def _schedule_runs(schedule: Any) -> bool | None:
    if not isinstance(schedule, dict):
        return None
    return bool(schedule.get("enabled", True))


def _base_schedule_is_enabled(job: dict[str, Any]) -> bool:
    schedule = job.get("schedule")
    return isinstance(schedule, dict) and bool(schedule.get("enabled", True))


def _job_has_a_schedule(job: dict[str, Any]) -> bool:
    if isinstance(job.get("schedule"), dict):
        return True
    return any(
        isinstance(job.get(schedule_key), dict)
        for schedule_key, _days_key, _next_run_key in EXECUTION_SCHEDULE_FIELDS
    )


def _job_is_enabled(job: dict[str, Any]) -> bool:
    stored = job.get("enabled")
    if isinstance(stored, bool):
        return stored
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return bool(schedule.get("enabled", True))
    return True


def edit_job_record(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = resolve_job(record_id=job_id)
    updated = _json_clone(current)
    was_enabled = _job_is_enabled(updated)
    changed = False
    if "enabled" in payload and payload["enabled"] is not None:
        runs = bool(payload["enabled"])
        updated["enabled"] = runs
        changed = True
        if payload.get("schedule") is None and _schedule_runs(updated.get("schedule")) not in (None, runs):
            updated["schedule"] = dict(updated["schedule"], enabled=runs)
            updated["next_incremental_run_at"] = _execution_schedule_next_run(updated["schedule"])
    if payload.get("name"):
        new_name = str(payload["name"])
        _ensure(bool(new_name.strip()), 422, "invalid_job", "Job name must not be empty")
        _ensure_ascii_name(new_name, field="Job name", code="invalid_job")
        for existing_id, existing in catalog_entries(JOBS):
            if existing_id == job_id:
                continue
            if str(existing.get("name") or "").lower() == new_name.lower():
                raise api_error(409, "job_name_exists", "Job name already exists", name=new_name)
        updated["name"] = new_name
        changed = True
    if "schedule" in payload and payload["schedule"] is not None:
        previously_ran = _schedule_runs(updated.get("schedule"))
        updated["schedule"] = _schedule_from_request(payload["schedule"])
        updated["next_incremental_run_at"] = schedule_next_run(updated["schedule"], after=_now_utc()).isoformat() if bool(updated["schedule"].get("enabled", True)) else None
        if "enabled" not in payload and _schedule_runs(updated["schedule"]) != previously_ran:
            updated["enabled"] = bool(updated["schedule"].get("enabled", True))
        changed = True
    if "retention_days" in payload and payload["retention_days"] is not None:
        updated["retention_days"] = _validate_retention_days(payload["retention_days"])
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
    for schedule_key, days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
        if schedule_key not in payload:
            continue
        schedule_value = _schedule_from_request(payload[schedule_key])
        updated[schedule_key] = schedule_value
        updated[next_run_key] = _execution_schedule_next_run(schedule_value)
        updated[days_key] = _schedule_weekdays(schedule_value)
        changed = True
    for schedule_key, days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
        if days_key not in payload or payload[days_key] is None:
            continue
        days = _normalize_day_list(payload[days_key])
        updated[days_key] = days
        retuned = _retune_weekly_schedule(updated.get(schedule_key), days)
        if retuned != updated.get(schedule_key):
            updated[schedule_key] = retuned
            updated[next_run_key] = _execution_schedule_next_run(retuned)
        changed = True
    for schedule_key, days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
        state = _requested_execution_schedule_state(payload, schedule_key)
        if state is None:
            continue
        schedule_value = _execution_schedule_with_state(
            updated.get(schedule_key),
            state,
            updated.get(days_key),
            updated.get("schedule"),
            updated.get("created_at"),
        )
        if schedule_value == updated.get(schedule_key):
            continue
        updated[schedule_key] = schedule_value
        updated[next_run_key] = _execution_schedule_next_run(schedule_value)
        changed = True
    if "description" in payload:
        description = str(payload.get("description") or "").strip()
        _ensure_ascii_text(description, field="Job description", code="invalid_job", max_length=MAX_LONG_TEXT_LENGTH)
        updated["description"] = description
        changed = True
    if "task_parameters" in payload and payload["task_parameters"] is not None:
        _ensure(isinstance(payload["task_parameters"], dict), 422, "invalid_job", "task_parameters must be an object")
        _ensure_encryption_key_strength(payload["task_parameters"])
        stored_parameters = updated.get("task_parameters") if isinstance(updated.get("task_parameters"), dict) else {}
        merged = _merge_task_parameters(stored_parameters, payload["task_parameters"])
        updated["task_parameters"] = _validate_task_parameters(merged, previous=stored_parameters)
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
            _ensure_unique_vms(vm_ids, vm_names)
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
    if updated["job_type"] == "backup" and _touches_full_backup_policy(payload):
        _ensure_full_backup_schedule(updated)
    _ensure(changed, 422, "invalid_job", "job edit requires at least one editable option")
    updated["updated_at"] = utc_now()
    if updated.get("schedule"):
        updated["schedule_summary"] = _render_schedule_summary(updated["schedule"])
        if not bool(updated["schedule"].get("enabled", True)):
            updated["next_incremental_run_at"] = None
    else:
        updated["next_incremental_run_at"] = None
    is_enabled = _job_is_enabled(updated)
    if not is_enabled:
        updated["next_incremental_run_at"] = None
    if not is_enabled:
        updated["status"] = "disabled"
    elif _job_has_a_schedule(updated):
        updated["status"] = "active"
    else:
        updated["status"] = "planned"
    if was_enabled and not is_enabled:
        for _schedule_key, _days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
            updated[next_run_key] = None
    elif is_enabled and not was_enabled:
        for schedule_key, _days_key, next_run_key in EXECUTION_SCHEDULE_FIELDS:
            updated[next_run_key] = _execution_schedule_next_run(updated.get(schedule_key))
        updated["next_incremental_run_at"] = _execution_schedule_next_run(updated.get("schedule"))
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


def _encryption_differs_from_point(job: dict[str, Any], point: dict[str, Any]) -> bool:
    parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else {}
    current_key = str(parameters.get("encryption_key") or "").strip()
    current_enabled = bool(parameters.get("encryption_enabled")) and bool(current_key)
    previous_enabled = bool(point.get("encryption_enabled"))
    previous_key = str(point.get("encryption_key") or "").strip()
    if current_enabled != previous_enabled:
        return True
    if not current_enabled:
        return False
    if not previous_key:
        return False
    return current_key != previous_key


def _point_manifest(point: dict[str, Any]) -> dict[str, Any]:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    try:
        payload = json.loads(Path(str(artifact.get("manifest_path") or "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _chain_base_mismatches(job_id: str, vm_id: str, repository_id: str | None) -> list[str]:
    from python.srv.plugins.vcenter.vcenter_backup import VCenterBackupError, _resolve_parent_disk_entry

    segments = _segment_points(_points_for_job_vm_repo(job_id, vm_id, repository_id))
    points = segments[-1] if segments else []
    by_id = {str(point.get("id") or ""): point for point in points}
    mismatched: list[str] = []
    for point in points:
        parent = by_id.get(str(point.get("parent_point_id") or ""))
        if parent is None or str(point.get("execution_type") or "") != "Incremental":
            continue
        parent_manifest = _point_manifest(parent)
        for disk in _point_manifest(point).get("disks") or []:
            if not isinstance(disk, dict):
                continue
            base = str(disk.get("previous_change_id") or "").strip()
            try:
                parent_disk = _resolve_parent_disk_entry(
                    parent_manifest,
                    datastore_path=str(disk.get("datastore_path") or ""),
                    uuid=str(disk.get("uuid") or ""),
                    index=int(disk.get("index") or 0),
                )
            except (VCenterBackupError, TypeError, ValueError):
                parent_disk = None
            expected = str((parent_disk or {}).get("change_id") or (parent_disk or {}).get("next_change_id") or "").strip()
            if base and base != "*" and expected and base != expected:
                mismatched.append(str(point.get("id") or ""))
                break
    return mismatched


def _chain_base_mismatch_message(points: list[str]) -> str:
    return (
        "This job's backup chain holds incremental backups that were taken on another job's backup of the same "
        f"VM (restore points {', '.join(points)}), so they can miss changes and restore stale or damaged data. "
        "This run is an active full that starts a clean chain."
    )


def resolve_job_execution(
    job: dict[str, Any],
    *,
    vm_id: str,
    force_full: bool,
    force_synthetic: bool = False,
    now: datetime | None = None,
    scheduled_type: str | None = None,
) -> tuple[str, str]:
    current = now or _now_utc()
    repository = resolve_repository(record_id=str(job.get("repo_id") or "")) if job.get("repo_id") else {}
    if force_synthetic:
        _ensure(
            repository_supports_synthetic(repository),
            422,
            "invalid_job",
            f"Repository '{repository.get('name') or job.get('repo_name')}' cannot merge a backup chain, "
            "so a synthetic full cannot be run against it",
            repository_id=job.get("repo_id"),
        )
        return "SyntheticFull", "ForceSynthetic"
    if force_full:
        return "ActiveFull", "ForceFull"
    previous = _latest_successful_point(str(job["id"]), vm_id, str(job.get("repo_id") or "") or None)
    if previous is None:
        return "ActiveFull", "FirstRun"
    if _encryption_differs_from_point(job, previous):
        return "ActiveFull", "EncryptionKeyChanged"
    if _chain_base_mismatches(str(job["id"]), vm_id, str(job.get("repo_id") or "") or None):
        return "ActiveFull", "ChainBaseMismatch"
    if scheduled_type == "ActiveFull":
        return "ActiveFull", "ActiveFullSchedule"
    if scheduled_type == "SyntheticFull":
        if repository and not repository_supports_synthetic(repository):
            return "ActiveFull", "RepositorySyntheticUnsupportedEscalatedToActive"
        return "SyntheticFull", "SyntheticFullSchedule"
    if scheduled_type == "Incremental":
        return "Incremental", "StandardIncremental"
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


def refresh_queued_backup_decision(task: dict[str, Any]) -> dict[str, Any]:
    if str(task.get("task_type") or "") != "backup":
        return task
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    job_id = str(task.get("job_id") or "")
    if not job_id or job_id not in JOBS or parameters.get("copy_job") or parameters.get("requires_task_id"):
        return task
    queued_type = str(parameters.get("resolved_backup_type") or "")
    queued_reason = str(parameters.get("resolved_backup_reason") or "")
    if queued_reason in {"ForceFull", "ForceSynthetic"}:
        return task
    job = resolve_job(record_id=job_id)
    vm_id = str(task.get("resource_external_id") or "")
    has_point = _latest_successful_point(job_id, vm_id, str(job.get("repo_id") or "") or None) is not None
    if queued_reason == "FirstRun" and has_point:
        execution_type, execution_reason = resolve_job_execution(job, vm_id=vm_id, force_full=False)
        if execution_type == "SyntheticFull":
            execution_type, execution_reason = "Incremental", "StandardIncremental"
    elif queued_type in {"Incremental", "SyntheticFull"} and not has_point:
        execution_type, execution_reason = "ActiveFull", "FirstRun"
    else:
        execution_type, execution_reason = queued_type, queued_reason
    mismatched: list[str] = []
    if execution_type in {"Incremental", "SyntheticFull"} or execution_reason == "ChainBaseMismatch":
        mismatched = _chain_base_mismatches(job_id, vm_id, str(job.get("repo_id") or "") or None)
        if mismatched:
            execution_type, execution_reason = "ActiveFull", "ChainBaseMismatch"
    warnings = task.get("warnings") if isinstance(task.get("warnings"), list) else []
    if mismatched and not any(isinstance(item, dict) and item.get("code") == "chain_base_mismatch" for item in warnings):
        message = _chain_base_mismatch_message(mismatched)
        task["warnings"] = [*warnings, {"code": "chain_base_mismatch", "message": message, "restore_point_ids": mismatched}]
        _append_task_log(task, level="warn", message=message, component="session")
        LOGGER.warning("Backup session %s of job '%s', VM %s: %s", task.get("id"), job.get("name"), vm_id, message)
        if (execution_type, execution_reason) == (queued_type, queued_reason):
            persist_task(task)
    if (execution_type, execution_reason) == (queued_type, queued_reason):
        return task
    parameters.update(
        {
            "resolved_backup_type": execution_type,
            "resolved_backup_reason": execution_reason,
            "backup_mode_request": "full" if execution_type in {"ActiveFull", "SyntheticFull"} else "auto",
            "incremental": execution_type == "Incremental",
            "synthetic_full": execution_type == "SyntheticFull",
            "active_full": execution_type == "ActiveFull",
        }
    )
    task["parameters"] = parameters
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    result.update({"resolved_backup_type": execution_type, "resolved_backup_reason": execution_reason})
    task["result"] = result
    _append_task_log(
        task,
        level="info",
        message=(
            f"Runs as {execution_type} ({execution_reason}), decided when it started: it was queued as "
            f"{queued_type} ({queued_reason}), and the backup chain has changed since"
        ),
        component="session",
    )
    persist_task(task)
    return task

def bind_backup_parent(task: dict[str, Any]) -> dict[str, Any]:
    if str(task.get("task_type") or "") != "backup":
        return task
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    job_id = str(task.get("job_id") or "")
    if not job_id or job_id not in JOBS or parameters.get("copy_job"):
        return task
    binding: dict[str, Any] = {"parent_point_id": None, "parent_task_id": None, "parent_manifest_hint": None}
    previous = None
    if str(parameters.get("resolved_backup_type") or "") == "Incremental":
        job = resolve_job(record_id=job_id)
        previous = _latest_successful_point(
            job_id, str(task.get("resource_external_id") or ""), str(job.get("repo_id") or "") or None
        )
    if previous is not None:
        from python.srv.services.backup_catalog import _load_backup_manifest_payload

        origin = previous.get("origin") if isinstance(previous.get("origin"), dict) else {}
        artifact = previous.get("artifact") if isinstance(previous.get("artifact"), dict) else {}
        _load_backup_manifest_payload(previous, artifact)
        binding = {
            "parent_point_id": str(previous.get("id") or "") or None,
            "parent_task_id": str(origin.get("task_id") or "") or None,
            "parent_manifest_hint": str(artifact.get("manifest_path") or "") or None,
        }
    if all(parameters.get(key) == value for key, value in binding.items()):
        return task
    parameters.update(binding)
    task["parameters"] = parameters
    if previous is not None:
        _append_task_log(
            task,
            level="info",
            message=(
                f"Continues this job's backup chain from restore point {binding['parent_point_id']} "
                f"(session {binding['parent_task_id']})"
            ),
            component="session",
        )
    persist_task(task)
    return task


def _point_period_key(point: dict[str, Any], *, kind: str) -> str:
    created = _parse_dt(point.get("created_at"))
    if kind == "weekly":
        iso_year, iso_week, _ = created.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if kind == "monthly":
        return f"{created.year:04d}-{created.month:02d}"
    return f"{created.year:04d}"


_GFS_INELIGIBLE_REASONS = {"ForceFull", "ForceSynthetic"}


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
        if bool(point.get("full_backup"))
        and str(point.get("execution_reason") or "") not in _GFS_INELIGIBLE_REASONS
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
    storage_refused = False

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
                if _stored_backup_is_absent(repository, abr_path, abr_backup_name):
                    LOGGER.info(
                        "Backup artifact was already gone, counting it as deleted: point=%s path=%s name=%s",
                        point.get("id"),
                        abr_path,
                        abr_backup_name,
                    )
                else:
                    all_removed = False
                    storage_refused = True
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

    if storage_refused and is_s3_repository(repository):
        return False

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

    return _delete_local_point_paths(point, paths, storage_roots) and all_removed


def _delete_local_point_cache(point: dict[str, Any]) -> bool:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    repository = REPOSITORIES.get(str(point.get("repository_id") or ""))
    paths = [
        Path(value)
        for value in (artifact.get("backup_root"), artifact.get("manifest_path"))
        if isinstance(value, str) and value
    ]
    return _delete_local_point_paths(
        point, paths, _repository_storage_roots(repository if isinstance(repository, dict) else None)
    )


def _delete_local_point_paths(point: dict[str, Any], paths: list[Path], storage_roots: Any) -> bool:
    point_repository_id = str(point.get("repository_id") or "")
    all_removed = True
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


def _stored_backup_names(repository: dict[str, Any], abr_path: str) -> set[str] | None:
    if not is_s3_repository(repository):
        location = Path(abr_path)
        if not location.exists():
            return set()
        if not os.access(location, os.R_OK | os.X_OK):
            return None
    try:
        return list_disk_tool_backups(repository, abr_path, timeout=60)
    except StorageToolError:
        return None


def _stored_backup_is_absent(repository: dict[str, Any], abr_path: str, backup_name: str) -> bool:
    names = _stored_backup_names(repository, abr_path)
    return names is not None and backup_name not in names


def _point_artifact_locations(point: dict[str, Any]) -> list[tuple[str, str]]:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    locations: list[tuple[str, str]] = []
    for disk in artifact.get("disks", []):
        if not isinstance(disk, dict):
            continue
        abr_path = str(disk.get("abr_path") or "").strip()
        backup_name = str(disk.get("abr_backup_name") or "").strip()
        if abr_path and backup_name:
            locations.append((abr_path, backup_name))
    return locations


def reconcile_repository_points(repository: dict[str, Any]) -> dict[str, int]:
    repository_id = str(repository.get("id") or "")
    listings: dict[str, set[str] | None] = {}
    summary = {"checked": 0, "marked_missing": 0, "restored": 0, "unreadable": 0}
    for record in catalog_records(BACKUPS):
        point = normalize_backup_point(record)
        if str(point.get("repository_id") or "") != repository_id:
            continue
        locations = _point_artifact_locations(point)
        if not locations:
            continue
        summary["checked"] += 1
        absent = False
        unreadable = False
        for abr_path, backup_name in locations:
            if abr_path not in listings:
                listings[abr_path] = _stored_backup_names(repository, abr_path)
            names = listings[abr_path]
            if names is None:
                unreadable = True
            elif backup_name not in names:
                absent = True
        status = str(point.get("status") or "available")
        if unreadable:
            summary["unreadable"] += 1
            continue
        if absent and status != "missing":
            point["status"] = "missing"
            persist_backup(point)
            summary["marked_missing"] += 1
            LOGGER.warning(
                "Restore point has no stored data left; marking it missing: point=%s repository=%s",
                point.get("id"),
                repository_id,
            )
        elif not absent and status == "missing":
            point["status"] = "available"
            persist_backup(point)
            summary["restored"] += 1
    return summary


CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)


def _points_by_parent(points: list[dict[str, Any]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for point in points:
        parent_id = str(point.get("parent_point_id") or "")
        point_id = str(point.get("id") or "")
        if parent_id and point_id:
            children[parent_id].append(point_id)
    return children


def _descendant_ids(points: list[dict[str, Any]]) -> dict[str, set[str]]:
    children = _points_by_parent(points)
    descendants: dict[str, set[str]] = {}

    def collect(point_id: str) -> set[str]:
        known = descendants.get(point_id)
        if known is not None:
            return known
        descendants[point_id] = set()
        gathered: set[str] = set()
        for child_id in children.get(point_id, []):
            gathered.add(child_id)
            gathered |= collect(child_id)
        descendants[point_id] = gathered
        return gathered

    for point in points:
        collect(str(point.get("id") or ""))
    return descendants


def _retention_dates(points: list[dict[str, Any]]) -> dict[str, datetime]:
    by_id = {str(point.get("id") or ""): point for point in points}
    descendants = _descendant_ids(points)
    dates: dict[str, datetime] = {}
    for point_id, point in by_id.items():
        candidates = [_parse_dt(point.get("created_at"))]
        for child_id in descendants.get(point_id, set()):
            child = by_id.get(child_id)
            if child is not None:
                candidates.append(_parse_dt(child.get("created_at")))
        dates[point_id] = min(candidates)
    return dates


def _keep_points_others_still_need(
    to_delete: list[dict[str, Any]],
    points: list[dict[str, Any]],
    *,
    job_id: str,
) -> list[dict[str, Any]]:
    delete_ids = {str(point.get("id") or "") for point in to_delete}
    if not delete_ids:
        return to_delete
    descendants = _descendant_ids(points)
    needed = {
        point_id
        for point_id in delete_ids
        if any(child_id not in delete_ids for child_id in descendants.get(point_id, set()))
    }
    if not needed:
        return to_delete
    for point_id in sorted(needed):
        LOGGER.warning(
            "Retention kept expired restore point %s of job %s: a restore point that chains onto it is "
            "not expired. Its dates do not follow its chain, which happens when the server clock was "
            "ahead while it was written.",
            point_id,
            job_id,
        )
    return [point for point in to_delete if str(point.get("id") or "") not in needed]


def chain_clock_anomalies(chain_id: str) -> list[dict[str, Any]]:
    points = list(grouped_backup_points().get(str(chain_id or ""), []))
    if not points:
        return []
    now = _now_utc()
    by_id = {str(point.get("id") or ""): point for point in points}
    anomalies: list[dict[str, Any]] = []
    for point in points:
        point_id = str(point.get("id") or "")
        created = _parse_dt(point.get("created_at"))
        if created - now > CLOCK_SKEW_TOLERANCE:
            anomalies.append(
                {
                    "kind": "ahead_of_server_clock",
                    "point_id": point_id,
                    "created_at": str(point.get("created_at") or ""),
                    "server_time": now.isoformat(),
                }
            )
        parent = by_id.get(str(point.get("parent_point_id") or ""))
        if parent is None:
            continue
        parent_created = _parse_dt(parent.get("created_at"))
        if parent_created - created > CLOCK_SKEW_TOLERANCE:
            anomalies.append(
                {
                    "kind": "older_than_its_parent",
                    "point_id": point_id,
                    "created_at": str(point.get("created_at") or ""),
                    "parent_point_id": str(parent.get("id") or ""),
                    "parent_created_at": str(parent.get("created_at") or ""),
                }
            )
    return anomalies


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


_RETENTION_LOCK_REPORTED: set[str] = set()


def _point_object_lock(point: dict[str, Any], now: datetime) -> tuple[str, str] | None:
    repository = REPOSITORIES.get(str(point.get("repository_id") or ""))
    if not isinstance(repository, dict) or not is_s3_repository(repository):
        return None
    stored_expiry = str(point.get("immutability_expires_at") or "").strip()
    try:
        stored_until = _parse_dt(stored_expiry) if stored_expiry else None
    except (TypeError, ValueError):
        stored_until = None
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    disk = next(
        (
            item
            for item in artifact.get("disks") or []
            if isinstance(item, dict) and item.get("abr_path") and item.get("abr_backup_name")
        ),
        None,
    )
    if disk is not None:
        try:
            info = read_storage_backup_info(repository, str(disk["abr_path"]), str(disk["abr_backup_name"]), timeout=60)
        except StorageToolError:
            LOGGER.debug("Could not read the Object Lock of restore point %s before retention", point.get("id"), exc_info=True)
        else:
            days_left = int(info.get("retention_days") or 0) if isinstance(info, dict) else 0
            if isinstance(info, dict) and str(info.get("immutability") or "none") != "none" and days_left > 0:
                until = stored_expiry if stored_until is not None and stored_until > now else (now + timedelta(days=days_left)).isoformat()
                return until, f"for {days_left} more day(s)"
            return None
    if stored_until is not None and stored_until > now:
        return stored_expiry, f"until {stored_expiry}"
    return None


def _new_retention_report(job: dict[str, Any], vm_id: str, repository_id: str | None) -> dict[str, Any]:
    return {
        "job_id": str(job.get("id") or ""),
        "vm_id": vm_id,
        "vm_name": None,
        "repository_id": repository_id,
        "points": 0,
        "removed": [],
        "held": [],
        "failed": [],
        "kept_for_chain": [],
        "skipped_reason": None,
    }


def apply_retention_to_chain(
    job: dict[str, Any],
    *,
    vm_id: str,
    repository_id: str | None,
    report: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Remove the restore points of one chain that the job's retention lets go.

    A segment (a full and the incrementals on it) only goes once every point in
    it has expired and none is kept by GFS, newest point first, so what is left
    always restores. A point whose data is still under Object Lock is not even
    tried: it is reported as held, and so are the older points it depends on.

    ``report``, when given, is filled with what happened (or, with
    ``dry_run``, what would happen) point by point.
    """
    failures: list[dict[str, Any]] = []
    if report is None:
        report = _new_retention_report(job, vm_id, repository_id)
    points = _points_for_job_vm_repo(str(job["id"]), vm_id, repository_id)
    report["points"] = len(points)
    if not points:
        return failures
    report["vm_name"] = points[-1].get("vm_name")
    _apply_gfs_marks(points, dict(job.get("gfs") or {}))
    now = _now_utc()
    cutoff = now - timedelta(days=int(job.get("retention_days") or 0))
    retention_dates = _retention_dates(points)

    def expired(point: dict[str, Any]) -> bool:
        return retention_dates[str(point.get("id") or "")] < cutoff

    def keep(point: dict[str, Any]) -> None:
        if not dry_run:
            persist_backup(point)

    to_delete: list[dict[str, Any]] = []
    for segment in _segment_points(points):
        full_point = segment[0]
        other_points = segment[1:]
        expired_flags = [expired(point) for point in segment]
        all_expired = all(expired_flags)
        if full_point.get("gfs_protected"):
            if other_points and all(expired(point) and not point.get("gfs_protected") for point in other_points):
                to_delete.extend(other_points)
            continue
        if all_expired and not any(point.get("gfs_protected") for point in segment):
            to_delete.extend(segment)
    to_delete = _keep_points_others_still_need(to_delete, points, job_id=str(job.get("id") or ""))
    if not to_delete:
        for point in points:
            keep(point)
        return failures
    delete_ids = {point["id"] for point in to_delete}
    for segment in _segment_points(points):
        segment_to_delete = [point for point in segment if point["id"] in delete_ids]
        if not segment_to_delete:
            for point in segment:
                keep(point)
            continue

        blocked = False
        for point in reversed(segment_to_delete):
            point_id = str(point.get("id") or "")
            if blocked:
                report["kept_for_chain"].append(point_id)
                keep(point)
                continue
            held = _point_object_lock(point, now)
            if held:
                until, remaining = held
                if point_id not in _RETENTION_LOCK_REPORTED:
                    _RETENTION_LOCK_REPORTED.add(point_id)
                    LOGGER.info(
                        "Retention keeps expired restore point %s of job %s while Object Lock holds it %s; "
                        "it is removed on the first pass after the lock expires",
                        point_id,
                        job.get("id"),
                        remaining,
                    )
                report["held"].append({"point_id": point_id, "until": until})
                keep(point)
                blocked = True
                continue
            if dry_run:
                report["removed"].append(point_id)
                continue
            if _delete_backup_artifacts_from_point(point):
                delete_backup(point["id"])
                _RETENTION_LOCK_REPORTED.discard(point_id)
                report["removed"].append(point_id)
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
                        "point_id": point_id,
                        "job_id": str(job.get("id") or ""),
                        "vm_id": vm_id,
                        "repository_id": repository_id,
                    }
                )
                report["failed"].append(point_id)
                keep(point)
                blocked = True
        for point in segment:
            if point["id"] not in delete_ids:
                keep(point)
    LOGGER.info(
        "Retention%s for job %s, %s VM %s (%s) in repository %s: %d restore point(s), %s %d, "
        "held by Object Lock %d, kept for their chain %d, failed %d",
        " preview" if dry_run else "",
        job.get("id"),
        str(points[-1].get("hypervisor_kind") or "unknown") if points else "unknown",
        report.get("vm_name") or vm_id,
        vm_id,
        repository_id,
        len(points),
        "would remove" if dry_run else "removed",
        len(report["removed"]),
        len(report["held"]),
        len(report["kept_for_chain"]),
        len(report["failed"]),
    )
    return failures


def _job_chains(job_id: str) -> list[tuple[str, str | None]]:
    chains: dict[tuple[str, str | None], None] = {}
    for point in normalized_backup_points():
        if str(point.get("job_id") or "") != job_id:
            continue
        chains[(str(point.get("vm_id") or ""), point.get("repository_id") or None)] = None
    return list(chains)


def run_job_retention(job_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply (or preview) a backup job's retention to every chain it wrote.

    Retention also runs after each backup, for that backup's own chain; this
    covers all of them at once, including VMs that have since left the job.
    A chain with a backup of the same VM in flight is skipped, so that the
    point the running backup chains onto cannot go from under it.
    """
    job = resolve_job(record_id=job_id)
    _ensure(job.get("job_type") == "backup", 422, "invalid_job", "Retention applies to backup jobs")
    busy_vms = {
        str(task.get("resource_external_id") or "")
        for task in catalog_records(TASKS)
        if str(task.get("job_id") or "") == str(job["id"])
        and _session_state(task) in {"queued", "running", "stopping"}
    }
    chains = []
    for vm_id, repository_id in _job_chains(str(job["id"])):
        report = _new_retention_report(job, vm_id, repository_id)
        if vm_id in busy_vms:
            report["skipped_reason"] = "a backup of this VM is running"
            report["points"] = len(_points_for_job_vm_repo(str(job["id"]), vm_id, repository_id))
        else:
            apply_retention_to_chain(job, vm_id=vm_id, repository_id=repository_id, report=report, dry_run=dry_run)
        report["repository_name"] = _repository_name_for(str(repository_id or "")) if repository_id else None
        report["kept"] = report["points"] - len(report["removed"])
        chains.append(report)
    summary = {
        "chains": len(chains),
        "points": sum(item["points"] for item in chains),
        "removed": sum(len(item["removed"]) for item in chains),
        "held": sum(len(item["held"]) for item in chains),
        "failed": sum(len(item["failed"]) for item in chains),
        "skipped": sum(1 for item in chains if item["skipped_reason"]),
    }
    return {
        "job_id": str(job["id"]),
        "job_name": str(job.get("name") or ""),
        "dry_run": bool(dry_run),
        "retention_days": int(job.get("retention_days") or 0),
        "gfs": dict(job.get("gfs") or {}),
        "summary": summary,
        "chains": chains,
        "ran_at": utc_now(),
    }


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


def _repository_name_for(repository_id: str) -> str | None:
    record = REPOSITORIES.get(repository_id) if repository_id else None
    return str(record.get("name") or "") or None if isinstance(record, dict) else None


def _synthetic_full_report(result: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(result.get("synthetic_full")):
        return None
    merged_ids = result.get("merged_restore_point_ids")
    target_id = str(result.get("repository_id") or "")
    source_id = str(result.get("source_repository_id") or "") or target_id
    return {
        "merged_from": _safe_int(result.get("merged_from")),
        "merged_restore_point_ids": list(merged_ids) if isinstance(merged_ids, list) else [],
        "source_repository_id": source_id or None,
        "source_repository_name": _repository_name_for(source_id),
        "target_repository_id": target_id or None,
        "target_repository_name": _repository_name_for(target_id),
        "disk_count": _safe_int(result.get("disk_count")),
        "logical_bytes": _safe_int(result.get("logical_bytes")),
        "stored_bytes": _safe_int(result.get("transferred_bytes")),
    }


def session_to_dto(task: dict[str, Any], *, include_logs: bool = True) -> dict[str, Any]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    job = resolve_job(record_id=str(task.get("job_id") or "")) if task.get("job_id") and str(task.get("job_id")) in JOBS else None
    vm_name, source_vm_name = _session_vm_names(task)
    finished_at, message = _session_end(task, result)
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
        "finished_at": finished_at,
        "resolved_backup_type": result.get("resolved_backup_type"),
        "resolved_backup_reason": result.get("resolved_backup_reason"),
        # "schedule" only for tasks the JobScheduler enqueued on its own; every
        # other path (Run now/Run full backup, restores, backup-copy) is a
        # human clicking a button, so default to "manual" when unset.
        "triggered_by": task.get("trigger") or "manual",
        "run_id": task.get("run_id"),
        "progress": result.get("progress"),
        "message": message,
        # Best-effort byte counters (see run_vcenter_backup) -- absent/0 for
        # non-vCenter (mock) engines and for restores, which don't track them.
        "processed_bytes": result.get("processed_bytes"),
        "transferred_bytes": result.get("transferred_bytes"),
        "errors": error or ([] if not message or _session_state(task) == "completed" else [{"message": message}]),
        "warnings": task.get("warnings") if isinstance(task.get("warnings"), list) else [],
    }
    if result.get("failure_kind"):
        dto["failure_kind"] = result.get("failure_kind")
    if result.get("restore_point_id"):
        dto["restore_point_id"] = result.get("restore_point_id")
        dto["backup_id"] = result.get("backup_id")
    synthetic = _synthetic_full_report(result)
    if synthetic is not None:
        dto["synthetic_full"] = synthetic
    if str(task.get("task_type") or "") == "verify":
        dto["verify"] = result.get("verify")
    if str(task.get("task_type") or "") == "delete":
        dto["delete"] = result.get("delete")
    # A session's log is the bulk of its DTO (task_pool appends a line per
    # progress update), so list callers that only need the row can drop it and
    # read GET /sessions/{id}/log on demand instead.
    if include_logs:
        dto["logs"] = task.get("logs") if isinstance(task.get("logs"), list) else []
    return dto


SESSION_TYPES = ("backup", "restore", "verify", "delete")


def list_sessions(
    *,
    job_name: str | None = None,
    session_type: str | None = None,
    run_id: str | None = None,
    include_logs: bool = True,
) -> list[dict[str, Any]]:
    tasks = catalog_records(TASKS)
    if session_type:
        _ensure(
            session_type in SESSION_TYPES,
            422,
            "invalid_argument",
            f"type must be one of: {', '.join(SESSION_TYPES)}",
            type=session_type,
        )
        tasks = [task for task in tasks if str(task.get("task_type") or "") == session_type]
    if run_id:
        tasks = [task for task in tasks if str(task.get("run_id") or "") == run_id]
    sessions = [session_to_dto(task, include_logs=include_logs) for task in tasks]
    if job_name:
        lowered = job_name.lower()
        sessions = [
            session
            for session in sessions
            if isinstance(session.get("job"), dict) and str(session["job"].get("name") or "").lower() == lowered
        ]
    sessions.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("id") or "")), reverse=True)
    return sessions


SESSIONS_PATH = "/api/v1/sessions"


def _sessions_link(path_params: dict[str, Any], limit: int, offset: int) -> str:
    """A ready-to-follow URL for another page of the same query."""
    query = {key: value for key, value in path_params.items() if value not in (None, "")}
    query["limit"] = limit
    query["offset"] = offset
    return f"{SESSIONS_PATH}?{urlencode(query)}"


def _started_after(session: dict[str, Any], cutoff: datetime) -> bool:
    """Whether a session row starts at or after ``cutoff``.

    A row whose timestamp cannot be parsed is kept: a ``since`` filter is there
    to shrink a response, and silently dropping records the console would
    otherwise show is the one failure mode worse than sending too many.
    """
    try:
        return _parse_dt(session.get("started_at")) >= cutoff
    except (TypeError, ValueError):
        return True


def list_sessions_page(
    *,
    job_name: str | None = None,
    session_type: str | None = None,
    run_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    since: str | None = None,
) -> dict[str, Any]:
    """One newest-first page of the session list, with the unpaged match count.

    The whole task catalog lives in memory (see mongo_db_wrapper.TASKS), so the
    page is a slice, not a query -- what it saves is the response body, which
    had grown to ~1.8 MB at a thousand sessions. Items here carry no ``logs``
    (GET /sessions/{id}/log serves those per session).

    The envelope is what lets a client fetch only what it needs rather than
    walking the whole history in chunks: ``total`` is the unpaged match count,
    ``page``/``pages`` locate this slice, and ``next``/``prev`` are ready-made
    URLs for the neighbouring pages (null at either end). ``since`` narrows the
    match set itself -- the console asks for the window it actually renders
    instead of paging back through years of runs to build a 14-day chart.
    """
    sessions = list_sessions(job_name=job_name, session_type=session_type, run_id=run_id, include_logs=False)
    if since:
        cutoff = _parse_dt(since)
        sessions = [session for session in sessions if _started_after(session, cutoff)]

    total = len(sessions)
    start = max(0, offset)
    items = sessions[start:] if limit is None else sessions[start : start + limit]

    # With no limit the whole match set is one page -- saying "page 1 of 1" is
    # truer than reporting a page size the caller never asked for.
    per_page = limit if limit is not None else total
    pages = 1 if not limit else max(1, math.ceil(total / limit))
    page = 1 if not limit else (start // limit) + 1
    has_next = start + len(items) < total
    has_prev = start > 0

    link_params = {"job": job_name, "type": session_type, "run_id": run_id, "since": since}
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": start,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "has_next": has_next,
        "has_prev": has_prev,
        "next": _sessions_link(link_params, limit, start + len(items)) if (has_next and limit) else None,
        "prev": _sessions_link(link_params, limit, max(0, start - limit)) if (has_prev and limit) else None,
    }


def get_session(session_id: str) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    return session_to_dto(task)


def get_session_log(session_id: str) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    return {
        "id": task.get("id"),
        "logs": task.get("logs") if isinstance(task.get("logs"), list) else [],
    }


STOPPED_BEFORE_START_MESSAGE = "Stopped before execution started"
_TERMINAL_SESSION_STATES = {"completed", "failed", "stopped", "canceled"}


def _session_end(task: dict[str, Any], result: dict[str, Any]) -> tuple[Any, Any]:
    finished_at = result.get("finished_at")
    message = result.get("message")
    if _session_state(task) not in _TERMINAL_SESSION_STATES:
        return finished_at, message
    if not finished_at and task.get("updated_at"):
        try:
            finished_at = _parse_dt(task.get("updated_at")).isoformat()
        except ValueError:
            finished_at = task.get("updated_at")
    logs = task.get("logs") if isinstance(task.get("logs"), list) else []
    if not message and any(
        isinstance(entry, dict) and entry.get("message") == "Session stopped before execution started" for entry in logs
    ):
        message = STOPPED_BEFORE_START_MESSAGE
    return finished_at, message


def stop_session(session_id: str, *, force: bool = False) -> dict[str, Any]:
    task = _find_by_id(catalog_records(TASKS), session_id, label="Session")
    current_state = _session_state(task)
    if current_state in _TERMINAL_SESSION_STATES:
        return {
            "id": task["id"],
            "state": current_state,
            "message": "Session is already finished",
        }
    if current_state == "queued":
        task["status"] = "stopped"
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        task["result"] = {**result, "message": STOPPED_BEFORE_START_MESSAGE, "finished_at": utc_now()}
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
    unhealthy = next(
        (point for point in points if _normalize_backup_health(point.get("status")) == "invalid"),
        None,
    )
    restore_points = [
        {
            "id": point["id"],
            "created_at": point.get("created_at"),
            "execution_type": point.get("execution_type"),
            "execution_reason": point.get("execution_reason"),
            "gfs_marks": point.get("gfs_marks"),
            "gfs_protected": point.get("gfs_protected"),
            "immutability_expires_at": point.get("immutability_expires_at"),
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
            "skipped_disks": _restore_point_artifact(point).get("skipped_disks") or [],
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
        "status": (unhealthy or latest).get("status"),
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
    encryption_key: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    points = grouped_backup_points().get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)

    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    work_items: list[tuple[dict[str, Any], dict[str, Any], str]] = []

    for point in points:
        if point["execution_type"] == "Incremental" and not point.get("parent_point_id"):
            warnings.append({"point_id": point["id"], "message": "Incremental restore point does not have a recorded parent"})

        point_key = str(encryption_key or "").strip() or _resolve_restore_encryption_key(point) or ""
        if not point_key and _point_is_encrypted(point):
            errors.append(
                {
                    "point_id": point["id"],
                    "disk_key": None,
                    "message": (
                        "Restore point is encrypted and no decryption key is available for it, so it cannot be "
                        "restored; the data itself was not checked against a key"
                    ),
                }
            )
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        for disk in artifact.get("disks", []):
            if isinstance(disk, dict):
                work_items.append((point, disk, point_key))

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
                    encryption_key=point_key,
                    on_progress=_verify_artifact_progress_reporter(tracker, index),
                ): (point, index)
                for index, (point, disk, point_key) in enumerate(work_items)
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


def _update_session_result(task_id: str, update: dict[str, Any], *, status: str | None = None) -> None:
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


def _run_verify_session(backup_id: str, task_id: str, *, encryption_key: str | None = None) -> dict[str, Any]:
    try:
        report = verify_backup_chain(
            backup_id,
            encryption_key=encryption_key,
            progress_callback=lambda update: _update_session_result(
                task_id,
                {**update, "log_entry": False},
            ),
            should_cancel=lambda: _verify_session_cancel_requested(task_id),
        )
    except ApiError as exc:
        _update_session_result(
            task_id,
            {"progress": 100, "message": str(exc.message), "finished_at": utc_now()},
            status="failed",
        )
        raise
    except Exception as exc:
        LOGGER.exception("Verification of backup '%s' failed", backup_id)
        _update_session_result(
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
    _update_session_result(
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


def verify_backup_chain_request(
    backup_id: str, *, wait: bool = True, encryption_key: str | None = None
) -> dict[str, Any]:
    points = grouped_backup_points().get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)

    task = _create_verify_session(backup_id, points)
    task_id = str(task["id"])

    if wait:
        report = _run_verify_session(backup_id, task_id, encryption_key=encryption_key)
        return {**report, "session_id": task_id}

    thread = threading.Thread(
        target=_run_verify_session,
        args=(backup_id, task_id),
        kwargs={"encryption_key": encryption_key},
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


def _create_delete_session(backup_id: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    latest = points[-1] if points else {}
    task = {
        "id": str(uuid4()),
        "job_id": None,
        "task_type": "delete",
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
    _append_task_log(task, level="info", message=f"Started deletion of backup {backup_id}", component="session")
    persist_task(task)
    return task


def _run_delete_session(
    backup_id: str, task_id: str, *, segment_id: str | None, force: bool
) -> dict[str, Any]:
    try:
        report = delete_backup_chain(
            backup_id,
            segment_id=segment_id,
            force=force,
            progress_callback=lambda update: _update_session_result(task_id, {**update, "log_entry": False}),
        )
    except ApiError as exc:
        _update_session_result(
            task_id,
            {"progress": 100, "message": str(exc.message), "finished_at": utc_now()},
            status="failed",
        )
        raise
    except Exception as exc:
        LOGGER.exception("Deletion of backup '%s' failed", backup_id)
        _update_session_result(
            task_id, {"progress": 100, "message": str(exc), "finished_at": utc_now()}, status="failed"
        )
        raise

    _update_session_result(
        task_id,
        {
            "progress": 100,
            "message": report.get("message") or "Backup chain deleted",
            "finished_at": utc_now(),
            "delete": report,
        },
        status="completed" if report.get("deleted") else "failed",
    )
    return report


def delete_backup_chain_request(
    backup_id: str, *, segment_id: str | None = None, force: bool = False, wait: bool = True
) -> dict[str, Any]:
    chains = grouped_backup_points()
    points = chains.get(backup_id)
    if not points:
        raise api_error(404, "backup_not_found", "Backup chain not found", backup_id=backup_id)

    task = _create_delete_session(backup_id, points)
    task_id = str(task["id"])

    if wait:
        report = _run_delete_session(backup_id, task_id, segment_id=segment_id, force=force)
        return {**report, "session_id": task_id}

    thread = threading.Thread(
        target=_run_delete_session,
        args=(backup_id, task_id),
        kwargs={"segment_id": segment_id, "force": force},
        name=f"delete-{task_id}",
        daemon=True,
    )
    thread.start()
    return {
        "id": backup_id,
        "session_id": task_id,
        "state": _session_state(task),
        "deleted": False,
        "restore_point_count": len(points),
        "artifacts_removed": False,
    }


def _point_repository_holds_immutable_backups(point: dict[str, Any]) -> bool:
    repository_id = str(point.get("repository_id") or "")
    if not repository_id:
        return False
    try:
        repository = _find_by_id(catalog_records(REPOSITORIES), repository_id, label="Repository")
    except ApiError:
        return False
    if not is_s3_repository(repository):
        return False
    return immutability_is_enabled(extract_s3_config(repository).get("immutability"))


def delete_backup_chain(
    backup_id: str,
    *,
    segment_id: str | None = None,
    force: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
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
    retained_points: list[str] = []

    def _emit(progress: int, message: str) -> None:
        if progress_callback is not None:
            progress_callback({"progress": progress, "message": message})

    kept_for_chain: list[str] = []
    total_points = len(points)
    _emit(0, f"Deleting {total_points} restore point(s)")
    position = 0
    for segment in _segment_points(points):
        held_point_id: str | None = None
        for point in reversed(segment):
            position += 1
            _emit(
                int((position - 1) * 100 / total_points) if total_points else 100,
                f"Deleting restore point {position}/{total_points}",
            )
            point_id = str(point.get("id") or "")
            if held_point_id is not None:
                LOGGER.info(
                    "Keeping restore point %s: restore point %s, which needs it to restore, is still held "
                    "under Object Lock",
                    point_id,
                    held_point_id,
                )
                kept_for_chain.append(point_id)
                continue
            if _delete_backup_artifacts_from_point(point):
                delete_backup(point["id"])
                continue
            artifacts_removed = False
            if _point_repository_holds_immutable_backups(point):
                LOGGER.error(
                    "Object Lock refused the deletion of backup artifacts; keeping catalog entry for retry: "
                    "point=%s repository=%s",
                    point.get("id"),
                    point.get("repository_id"),
                )
                retained_points.append(point_id)
                held_point_id = point_id
                continue
            _delete_local_point_cache(point)
            delete_backup(point["id"])

    kept_points = len(retained_points) + len(kept_for_chain)
    _emit(100, f"Deleted {total_points - kept_points} of {total_points} restore point(s)")
    immutable_points = len(retained_points)
    result = {
        "id": backup_id,
        "deleted": not kept_points,
        "restore_point_count": len(points),
        "artifacts_removed": artifacts_removed,
    }
    if segment_id is not None:
        result["segment_id"] = segment_id
    if gfs_protected_point_ids:
        result["gfs_protection_overridden"] = True
    if not artifacts_removed:
        if immutable_points:
            result["immutable_restore_points"] = immutable_points
            result["retained_restore_point_ids"] = retained_points
        if kept_for_chain:
            result["kept_for_chain_restore_point_ids"] = kept_for_chain
        result["message"] = (
            (
                f"{immutable_points} restore point(s) are still held under S3 Object Lock and the storage refused "
                "to delete their data. They are kept in the catalog and retried once the lock expires, so nothing "
                "is left untracked in the bucket."
                + (
                    f" {len(kept_for_chain)} older restore point(s) they need to restore are kept with them."
                    if kept_for_chain
                    else ""
                )
            )
            if immutable_points
            else (
                "Catalog entry removed. Some backup files were outside this repository's storage root "
                "(e.g. an imported/reference chain) and were left untouched to avoid deleting another "
                "repository's data."
            )
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
        "skipped_disks": _restore_point_artifact(point).get("skipped_disks") or [],
        "immutability_expires_at": point.get("immutability_expires_at"),
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


_PLATFORM_LABELS = {"vsphere": "VMware", "hyper-v": "Hyper-V", "proxmox-ve": "Proxmox", "xcp-ng": "XCP-ng"}
_POINT_KIND_TO_INVENTORY_TYPE = {"vcenter": "vsphere", "hyper-v": "hyper-v"}


def _point_inventory_type(point: dict[str, Any]) -> str | None:
    kind = str(point.get("hypervisor_kind") or "").strip().lower()
    return _POINT_KIND_TO_INVENTORY_TYPE.get(kind)


def _point_platform_label(point: dict[str, Any]) -> str:
    return _PLATFORM_LABELS.get(_point_inventory_type(point) or "", "source platform")


def _inventory_platform_label(inventory: dict[str, Any]) -> str:
    return _PLATFORM_LABELS.get(inventory_type_from_record(inventory), str(inventory.get("type") or "another platform"))


def _restore_target_kind_matches(point: dict[str, Any], inventory: dict[str, Any]) -> bool:
    """A restore point goes back to the platform it came from: nothing here
    converts a VM between hypervisors. Points of unknown origin (imported,
    mock) keep the old behaviour and may go anywhere except Hyper-V, whose
    restore needs a Hyper-V manifest."""
    expected = _point_inventory_type(point)
    actual = inventory_type_from_record(inventory)
    if expected:
        return actual == expected
    return actual != "hyper-v"


_CSV_PATH = re.compile(r"^([A-Za-z]:\\ClusterStorage\\[^\\]+)(?:\\(.*))?$", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"^([A-Za-z]:)(?:\\(.*))?$")
_UNC_PATH = re.compile(r"^(\\\\[^\\]+\\[^\\]+)(?:\\(.*))?$")


def _parse_windows_storage_path(raw: str) -> tuple[str | None, str | None, str | None]:
    """A Hyper-V disk path as (datastore, folder, file name): the datastore is
    the Cluster Shared Volume, drive or share the file lives on, which is
    what Hyper-V hosts report as their storage."""
    value = raw.replace("/", "\\")
    for pattern in (_CSV_PATH, _UNC_PATH, _DRIVE_PATH):
        match = pattern.match(value)
        if not match:
            continue
        datastore = match.group(1)
        if pattern is not _UNC_PATH:
            datastore = datastore[0].upper() + datastore[1:]
        rest = (match.group(2) or "").strip("\\")
        if not rest:
            return datastore, None, None
        parts = rest.split("\\")
        folder = "\\".join(parts[:-1]) or None
        return datastore, folder, parts[-1] or None
    return None, None, None


def _parse_datastore_path_parts(datastore_path: str | None) -> tuple[str | None, str | None, str | None]:
    raw = str(datastore_path or "").strip()
    if not raw.startswith("["):
        return _parse_windows_storage_path(raw) if raw else (None, None, None)
    if "]" not in raw:
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
                "mac_address": str(
                    manifest_adapter.get("mac_address") or manifest_adapter.get("mac") or payload.get("mac") or ""
                ).strip()
                or None,
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
                "used_bytes": _restore_disk_used_bytes(manifest, disk_entry),
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
    inventories = [item for item in list_inventory_records() if _restore_target_kind_matches(point, item)]
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

    source_datastore_names: list[str] = []
    for disk in source_vm.get("disks", []):
        if not isinstance(disk, dict):
            continue
        name = _source_disk_datastore(disk)
        if name and name not in source_datastore_names:
            source_datastore_names.append(name)
    default_datastore_mappings = [
        {"source_datastore": name, "target_datastore_id": default_datastore_id}
        for name in source_datastore_names
    ]

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
            "datastore_mappings": default_datastore_mappings,
        },
        "capabilities": {
            "supports_in_place_restore": supports_in_place_restore,
            "supports_network_mapping": bool(source_vm.get("nics")),
            "supports_datastore_mapping": len(source_datastore_names) > 1,
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


# Hyper-V's "dynamic" VHDX (and a differencing chain, restored as one dynamic
# disk) grows with its data the way a thin VMDK does.
_THIN_PROVISIONING_REQUESTS = {"thin", "thin-provisioned", "thin_provisioned", "vmfs_thin", "dynamic"}

_THIN_SOURCE_DISK_TYPES = {"vmfs_thin", "vmfs_sparse", "monolithic_sparse", "split_sparse", "dynamic", "differencing"}

_WHOLE_DISK_BACKUP_MODES = {"full", "incremental-full-fallback"}


def _restore_disk_used_bytes(manifest: dict[str, Any], disk_entry: dict[str, Any]) -> int | None:
    store_mode = str(disk_entry.get("store_mode") or "").strip().lower()
    backup_mode = str(manifest.get("backup_mode") or "").strip().lower()
    holds_whole_disk = store_mode == "full" or (
        not store_mode and backup_mode in _WHOLE_DISK_BACKUP_MODES
    )
    if not holds_whole_disk:
        return None
    used = _safe_int(disk_entry.get("logical_bytes"))
    return used if used is not None and used >= 0 else None


def _restore_disk_target_is_thin(disk: dict[str, Any], disk_provisioning: str | None) -> bool:
    requested = str(disk_provisioning or "").strip().lower()
    if requested and requested != "original":
        return requested in _THIN_PROVISIONING_REQUESTS
    return str(disk.get("provisioning") or "").strip().lower() in _THIN_SOURCE_DISK_TYPES


def _gib(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / (1024 ** 3):.2f} GiB"


def _ensure_restore_target_capacity(
    disks: list[dict[str, Any]],
    datastore: dict[str, Any],
    restore_mode: str,
    disk_provisioning: str | None = None,
) -> None:
    """Hard-reject only when we KNOW the datastore can't fit the restore —
    unknown capacity (a datastore whose free_bytes hasn't been reported, or a
    source disk whose capacity wasn't recorded) is never enough evidence to
    block, since thin-provisioned disks routinely use far less than their
    declared capacity. In-place restore needs a full second copy of the VM's
    disks to coexist with the still-live original until the swap completes,
    so it's checked against 2x the required size rather than 1x — free_bytes
    already excludes the original's own footprint, so this is a deliberate
    safety margin (thin-disk growth during write, buffer before the original
    is destroyed), not a strict minimum.
    """
    requirements: list[int | None] = []
    thin_target = False
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        if _restore_disk_target_is_thin(disk, disk_provisioning):
            thin_target = True
            requirements.append(_safe_int(disk.get("used_bytes")))
        else:
            requirements.append(_safe_int(disk.get("capacity_bytes")))
    required = (
        sum(requirements) if requirements and all(value is not None for value in requirements) else None
    )
    required_total = None if required is None else required * (2 if restore_mode == "in_place" else 1)
    free_bytes = _safe_int(datastore.get("free_bytes"))
    _ensure(
        required_total is None or free_bytes is None or free_bytes >= required_total,
        409,
        "insufficient_datastore_space",
        f"Datastore '{datastore.get('name')}' does not have enough free space for the disks that land on it: "
        f"{_gib(required_total)} required, {_gib(free_bytes)} free"
        + (
            " — thin-provisioned disks are measured by the data the backup holds, "
            "not by their provisioned size"
            if thin_target
            else ""
        )
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


def _source_disk_datastore(disk: dict[str, Any]) -> str:
    datastore_name, _folder, _filename = _parse_datastore_path_parts(disk.get("datastore_path"))
    return str(datastore_name or "").strip()


def _collect_restore_mapping(
    mappings: dict[str, str], source: str, target: str, *, flag: str, case_sensitive: bool = False
) -> None:
    def same(left: str, right: str) -> bool:
        return left == right if case_sensitive else left.lower() == right.lower()

    for existing_source, existing_target in mappings.items():
        if not same(existing_source, source):
            continue
        _ensure(
            same(existing_target, target),
            422,
            "invalid_restore",
            f"{flag} maps the same source twice, to different targets",
            source=existing_source,
            targets=[existing_target, target],
        )
        return
    mappings[source] = target


def _parse_restore_identifier_map(value: Any, *, flag: str, source_key: str, target_key: str) -> dict[str, str]:
    form = f"Each {flag} value must use the form <source>=<target> or <source>:<target>"
    if isinstance(value, dict):
        pairs = list(value.items())
    elif not value:
        return {}
    elif isinstance(value, list):
        pairs = []
        for raw in value:
            if isinstance(raw, dict):
                pairs.append((raw.get(source_key), raw.get(target_key)))
                continue
            text = str(raw or "")
            left, separator, right = text.partition("=")
            if separator != "=":
                left, separator, right = text.partition(":")
            _ensure(separator in {"=", ":"} and bool(left.strip()) and bool(right.strip()), 422, "invalid_restore", form, value=text)
            pairs.append((left, right))
    else:
        raise api_error(422, "invalid_restore", form)
    mappings: dict[str, str] = {}
    for source, target in pairs:
        source_text = str(source or "").strip()
        target_text = str(target or "").strip()
        _ensure(bool(source_text) and bool(target_text), 422, "invalid_restore", form)
        _collect_restore_mapping(mappings, source_text, target_text, flag=flag)
    return mappings


def _restore_disk_identities(source_vm: dict[str, Any]) -> list[dict[str, Any]]:
    return [disk for disk in (source_vm.get("disks") or []) if isinstance(disk, dict)]


def _resolve_restore_disk_placement(
    source_vm: dict[str, Any],
    *,
    inventory_id: str,
    host: dict[str, Any],
    requested: Any,
    restore_mode: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    overrides = _parse_restore_identifier_map(
        requested, flag="--disk-datastore", source_key="source_disk", target_key="target_datastore"
    )
    if not overrides:
        return [], {}
    _ensure(
        restore_mode != "in_place",
        422,
        "invalid_restore",
        "In-place restore keeps every disk on its own datastore and cannot take a disk placement",
    )
    disks = _restore_disk_identities(source_vm)
    position_by_identifier: dict[str, int] = {}
    for position, disk in enumerate(disks, start=1):
        for identifier in (str(disk.get("id") or ""), str(disk.get("label") or "")):
            if identifier:
                position_by_identifier.setdefault(identifier.lower(), position)
    targets: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}
    for requested_disk, target_identifier in overrides.items():
        position = position_by_identifier.get(requested_disk.lower())
        _ensure(
            position is not None,
            422,
            "invalid_restore",
            "Disk placement names a disk this restore point does not carry",
            disk=requested_disk,
            disks=[{"id": disk.get("id"), "label": disk.get("label")} for disk in disks],
        )
        datastore = _resolve_restore_target_datastore(inventory_id, target_identifier, host=host)
        target_name = str(datastore.get("name") or "").strip()
        disk = disks[position - 1]
        targets.append(
            {
                "index": position,
                "disk_id": disk.get("id"),
                "label": disk.get("label"),
                "target_datastore": target_name,
            }
        )
        resolved[target_name] = datastore
    return sorted(targets, key=lambda item: int(item["index"])), resolved


def _restore_disk_target_name(
    disk: dict[str, Any],
    position: int,
    disk_targets: dict[int, str],
    placement: dict[str, str],
    default_name: str,
) -> str:
    override = str(disk_targets.get(position) or "").strip()
    if override:
        return override
    return str(placement.get(_source_disk_datastore(disk)) or default_name).strip() or default_name


def _resolve_restore_nic_network_mappings(
    source_vm: dict[str, Any],
    *,
    inventory_id: str,
    host: dict[str, Any] | None,
    requested: Any,
) -> list[dict[str, Any]]:
    overrides = _parse_restore_identifier_map(
        requested, flag="--nic-network", source_key="source_nic", target_key="target_network"
    )
    if not overrides:
        return []
    nics = [nic for nic in (source_vm.get("nics") or []) if isinstance(nic, dict)]
    nic_by_identifier: dict[str, dict[str, Any]] = {}
    for nic in nics:
        for identifier in (str(nic.get("id") or ""), str(nic.get("label") or "")):
            if identifier:
                nic_by_identifier.setdefault(identifier.lower(), nic)
    source_networks = [
        str(nic.get("source_network") or "").strip() for nic in nics if str(nic.get("source_network") or "").strip()
    ]
    mappings: list[dict[str, Any]] = []
    for requested_nic, target_identifier in overrides.items():
        nic = nic_by_identifier.get(requested_nic.lower())
        _ensure(
            nic is not None,
            422,
            "invalid_restore",
            "Adapter placement names an adapter this restore point does not carry",
            adapter=requested_nic,
            adapters=[{"id": item.get("id"), "label": item.get("label")} for item in nics],
        )
        network = _resolve_restore_target_network(
            inventory_id, target_identifier, source_networks=source_networks, host=host
        )
        mappings.append({"source_nic_id": nic.get("id"), "target_network_id": str(network.get("id") or "")})
    return mappings


def _resolve_restore_datastore_placement(
    source_vm: dict[str, Any],
    *,
    inventory_id: str,
    host: dict[str, Any],
    default_datastore: dict[str, Any],
    requested: Any,
    restore_mode: str,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    disks = [disk for disk in (source_vm.get("disks") or []) if isinstance(disk, dict)]
    source_names: list[str] = []
    for disk in disks:
        name = _source_disk_datastore(disk)
        if name and name not in source_names:
            source_names.append(name)

    default_name = str(default_datastore.get("name") or "").strip()
    placement = {name: default_name for name in source_names}
    resolved = {default_name: default_datastore} if default_name else {}

    overrides = _parse_restore_datastore_map(requested)
    if restore_mode == "in_place":
        _ensure(
            not overrides,
            422,
            "invalid_restore",
            "In-place restore keeps every disk on its own datastore and cannot take a datastore mapping",
        )
        overrides = {name: name for name in source_names}

    by_lowercase = {name.lower(): name for name in source_names}
    for requested_source, target_identifier in overrides.items():
        source_name = by_lowercase.get(requested_source.lower(), "")
        _ensure(
            bool(source_name),
            422,
            "invalid_restore",
            "Datastore mapping names a datastore this VM does not use",
            source_datastore=requested_source,
            source_datastores=source_names,
        )
        datastore = _resolve_restore_target_datastore(inventory_id, target_identifier, host=host)
        target_name = str(datastore.get("name") or "").strip()
        placement[source_name] = target_name
        resolved[target_name] = datastore

    return placement, resolved


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
        "datastore_map": payload.get("datastore_map") or payload.get("datastore_mappings"),
        "disk_datastore_map": payload.get("disk_datastore_map") or payload.get("disk_datastore_mappings"),
        "nic_network_map": payload.get("nic_network_map") or payload.get("nic_network_mappings"),
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
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Resolve where the restored VM's network adapters go.

    Returns the source-network-name map every path produces, plus -- for
    per-adapter ``mappings`` -- one entry per mapped adapter, by its position in
    the manifest's ``network_adapters``. The name map alone cannot tell two
    adapters on the same source network apart (a distributed switch's port
    groups used to be recorded under one name, too), so it sent them all to
    whichever target was listed last; the engine applies the per-adapter
    entries first.
    """
    source_vm = _build_restore_source_vm(point, manifest)
    source_nics = [item for item in source_vm.get("nics", []) if isinstance(item, dict)]
    if not source_nics:
        return _parse_restore_network_map(legacy_network_map), []

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

    legacy = _parse_restore_network_map(legacy_network_map)
    _ensure_restore_network_map_targets(
        legacy,
        source_networks=source_networks,
        target_networks=target_networks,
        off_host_names=off_host_names,
        target_inventory_id=target_inventory_id,
        host=host,
    )

    if mappings:
        resolved: dict[str, str] = {}
        adapter_targets: dict[int, dict[str, Any]] = {}
        # _source_network_fallbacks builds one NIC per manifest adapter, in the
        # manifest's order, so a NIC's position here is its adapter's position
        # in vm_config.network_adapters -- which is what the engine indexes.
        source_index = {str(item.get("id") or ""): (position, item) for position, item in enumerate(source_nics)}
        requested_targets: dict[str, str] = {}
        for entry in mappings:
            source_nic_id = str(entry.get("source_nic_id") or "").strip()
            target_network_id = str(entry.get("target_network_id") or "").strip()
            if not source_nic_id and not target_network_id:
                continue
            _ensure(bool(source_nic_id and target_network_id), 422, "invalid_restore", "Each network mapping requires source_nic_id and target_network_id")
            _collect_restore_mapping(requested_targets, source_nic_id, target_network_id, flag="--nic-network")
            position, source_nic = source_index.get(source_nic_id) or (None, None)
            _ensure(source_nic is not None, 422, "invalid_restore", "Source network adapter not found", source_nic_id=source_nic_id)
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
            target_network_name = str(target_network.get("name") or "")
            resolved[source_network] = target_network_name
            adapter_targets[position] = {
                "index": position,
                "label": source_nic.get("label"),
                "source_network": source_network,
                "target_network": target_network_name,
            }
        for position, item in enumerate(source_nics):
            if position in adapter_targets:
                continue
            source_network = str(item.get("source_network") or "").strip()
            target_network_name = legacy.get(source_network) or (source_network if source_network in target_network_names else "")
            if not source_network or not target_network_name:
                continue
            adapter_targets[position] = {
                "index": position,
                "label": item.get("label"),
                "source_network": source_network,
                "target_network": target_network_name,
            }
            resolved.setdefault(source_network, target_network_name)
        missing = [
            str(item.get("label") or item.get("id") or "network adapter")
            for position, item in enumerate(source_nics)
            if str(item.get("source_network") or "").strip() and position not in adapter_targets
        ]
        _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for all source adapters", adapters=missing)
        return resolved, [adapter_targets[position] for position in sorted(adapter_targets)]

    if legacy:
        legacy = {
            **{
                source_network: source_network
                for source_network in source_networks
                if source_network not in legacy and source_network in target_network_names
            },
            **legacy,
        }
        missing = [source_network for source_network in source_networks if source_network not in legacy]
        _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for all source adapters", networks=missing)
        return legacy, []

    automatic = {
        source_network: source_network
        for source_network in source_networks
        if source_network in target_network_names
    }
    missing = [source_network for source_network in source_networks if source_network and source_network not in automatic]
    _ensure(not missing or not target_networks, 422, "invalid_restore", "Network mappings are required for the selected restore target", networks=missing)
    return automatic, []


def _ensure_restore_network_map_targets(
    legacy: dict[str, str],
    *,
    source_networks: list[str],
    target_networks: list[dict[str, Any]],
    off_host_names: set[str],
    target_inventory_id: str,
    host: dict[str, Any] | None,
) -> None:
    source_network_set = set(source_networks)
    target_network_names = {str(item.get("name") or "") for item in target_networks}
    for source_network, target_network_name in legacy.items():
        _ensure(
            source_network in source_network_set,
            422,
            "invalid_restore",
            "Network map names a source network this restore point does not carry",
            source_network=source_network,
            networks=sorted(source_network_set),
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
        _collect_restore_mapping(mappings, source_name, target_name, flag="--network-map", case_sensitive=True)
    return mappings


def _parse_restore_datastore_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        pairs = [(source, target) for source, target in value.items()]
    elif not value:
        return {}
    elif isinstance(value, list):
        pairs = []
        for raw in value:
            if isinstance(raw, dict):
                pairs.append((raw.get("source_datastore"), raw.get("target_datastore") or raw.get("target_datastore_id")))
                continue
            text = str(raw or "")
            left, separator, right = text.partition("=")
            if separator != "=":
                left, separator, right = text.partition(":")
            _ensure(
                separator in {"=", ":"} and bool(left.strip()) and bool(right.strip()),
                422,
                "invalid_restore",
                "Each --datastore-map value must use the form <source>=<target> or <source>:<target>",
                value=text,
            )
            pairs.append((left, right))
    else:
        raise api_error(
            422,
            "invalid_restore",
            "Each --datastore-map value must use the form <source>=<target> or <source>:<target>",
        )

    mappings: dict[str, str] = {}
    for source, target in pairs:
        source_name = str(source or "").strip()
        target_name = str(target or "").strip()
        _ensure(
            bool(source_name) and bool(target_name),
            422,
            "invalid_restore",
            "Each datastore mapping requires a source datastore and a target datastore",
        )
        _collect_restore_mapping(mappings, source_name, target_name, flag="--datastore-map")
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


def _create_completed_copy_session(job: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
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
        "run_id": run_id,
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


def _create_backup_session(
    job: dict[str, Any],
    *,
    vm_id: str,
    vm_name: str,
    execution_type: str,
    execution_reason: str,
    target_repository: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    task_parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else {}
    parameters = dict(task_parameters)
    parameters.update(
        {
            "repository_id": (target_repository or {}).get("id") or job.get("repo_id"),
            "repository_name": (target_repository or {}).get("name") or job.get("repo_name"),
            "source_repository_id": job.get("repo_id") if target_repository else None,
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
        "run_id": run_id,
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
    _append_task_log(
        task,
        level="info",
        message=(
            f"Job {job.get('name')}: {execution_type} ({execution_reason}) of {vm_name} from inventory "
            f"{job.get('inventory_name')} to repository {parameters.get('repository_name')}"
        ),
        component="session",
    )
    LOGGER.info(
        "Backup session %s queued: job %s, VM %s (%s) of inventory %s, %s (%s), repository %s",
        task["id"],
        job.get("name"),
        vm_name,
        vm_id,
        job.get("inventory_name"),
        execution_type,
        execution_reason,
        parameters.get("repository_name"),
    )
    persist_task(task)
    return session_to_dto(task)


def _ensure_chain_can_be_merged(job: dict[str, Any]) -> None:
    from python.srv.services.synthetic_full import (
        SyntheticFullValidationError,
        resolve_source_chain,
        validate_chain,
    )

    repository_id = str(job.get("repo_id") or "") or None
    for vm_id, vm_name in zip(job.get("vm_ids") or [], job.get("vm_names") or []):
        try:
            validate_chain(resolve_source_chain(str(job["id"]), str(vm_id), repository_id))
        except SyntheticFullValidationError as exc:
            raise api_error(
                422,
                "chain_cannot_be_merged",
                str(exc),
                job_id=str(job.get("id") or ""),
                vm_id=str(vm_id),
                vm_name=str(vm_name),
            ) from exc
        mismatched = _chain_base_mismatches(str(job["id"]), str(vm_id), repository_id)
        _ensure(
            not mismatched,
            422,
            "chain_cannot_be_merged",
            f"The backup chain of {vm_name} holds incremental backups that were taken on another job's backup of "
            f"the same VM (restore points {', '.join(mismatched)}), so a synthetic full would merge their gaps. "
            "Run an active full instead (abr job start --full).",
            job_id=str(job.get("id") or ""),
            vm_id=str(vm_id),
            vm_name=str(vm_name),
            restore_point_ids=mismatched,
        )


def _job_vm_current_names(job: dict[str, Any]) -> list[str]:
    discovered = RESOURCES_BY_HV.get(str(job.get("inventory_id") or ""), {})
    names: list[str] = []
    for vm_id, stored_name in zip(job.get("vm_ids") or [], job.get("vm_names") or []):
        resource = discovered.get(("virtual_machine", str(vm_id)))
        names.append(str((resource or {}).get("name") or stored_name))
    return names


def start_job_record(
    job_id: str,
    *,
    force_full: bool = False,
    force_synthetic: bool = False,
    target_repository_id: str | None = None,
) -> dict[str, Any]:
    job = resolve_job(record_id=job_id)
    _ensure(
        not (force_full and force_synthetic),
        422,
        "invalid_job",
        "A run is either an active full or a synthetic full, not both",
    )
    target_repository = None
    if target_repository_id:
        _ensure(
            force_synthetic,
            422,
            "invalid_job",
            "A target repository is only used by a synthetic full",
        )
        target_repository = _find_by_id(catalog_records(REPOSITORIES), target_repository_id, label="Repository")
        _ensure(
            repository_supports_synthetic(normalize_repository_record(target_repository)),
            422,
            "invalid_job",
            f"Repository '{target_repository.get('name')}' cannot hold a merged backup",
            repository_id=target_repository_id,
        )
    if force_synthetic and job["job_type"] != "backup-copy":
        _ensure_chain_can_be_merged(job)
    sessions = []
    run_id = str(uuid4())
    run_started_at = utc_now()
    if job["job_type"] == "backup-copy":
        sessions.append(_create_completed_copy_session(job, run_id=run_id))
    else:
        for vm_id, vm_name in zip(job.get("vm_ids") or [], _job_vm_current_names(job)):
            execution_type, execution_reason = resolve_job_execution(
                job, vm_id=vm_id, force_full=force_full, force_synthetic=force_synthetic
            )
            sessions.append(
                _create_backup_session(
                    job,
                    vm_id=vm_id,
                    vm_name=vm_name,
                    execution_type=execution_type,
                    execution_reason=execution_reason,
                    target_repository=target_repository,
                    run_id=run_id,
                )
            )
    schedule = job.get("schedule")
    # A manual "Run now"/"Run full backup" must push next_incremental_run_at forward the
    # same way the scheduler does after its own dispatch (_dispatch_due_jobs
    # in job_scheduler.py) — otherwise a job whose scheduled time has already
    # arrived (or was already due when triggered manually) keeps a stale,
    # now-past next_incremental_run_at, and the scheduler's next poll (every 5s) sees it
    # as still due and fires it again right behind the manual run, which
    # looks like the job running twice.
    next_incremental_run_at = (
        schedule_next_run(schedule, after=_now_utc()).isoformat()
        if isinstance(schedule, dict) and bool(schedule.get("enabled", True))
        else job.get("next_incremental_run_at")
    )
    persisted = _persist_canonical_job(
        {
            **job,
            "last_run_at": utc_now(),
            "last_session_id": sessions[-1]["id"] if sessions else None,
            "last_run": job_run_record(run_id, trigger="manual", started_at=run_started_at, session_ids=[s["id"] for s in sessions]),
            "next_incremental_run_at": next_incremental_run_at,
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


def _point_is_encrypted(point: dict[str, Any]) -> bool:
    if point.get("encryption_enabled") is not None:
        return bool(point.get("encryption_enabled"))
    job_id = str(point.get("job_id") or "")
    if not job_id or job_id not in JOBS:
        return False
    task_parameters = resolve_job(record_id=job_id).get("task_parameters")
    return bool(isinstance(task_parameters, dict) and task_parameters.get("encryption_enabled"))


def _job_key_by_id(job_id: str, key_id: Any) -> str | None:
    if key_id is None or not job_id or job_id not in JOBS:
        return None
    source_job = resolve_job(record_id=job_id)
    task_parameters = source_job.get("task_parameters") if isinstance(source_job.get("task_parameters"), dict) else {}
    if not task_parameters.get("encryption_enabled"):
        return None
    try:
        wanted = int(key_id)
    except (TypeError, ValueError):
        return None
    for entry in task_parameters.get("encryption_keys") or []:
        if not isinstance(entry, dict):
            continue
        try:
            entry_id = int(entry.get("key_id"))
        except (TypeError, ValueError):
            continue
        if entry_id == wanted:
            return str(entry.get("key") or "").strip() or None
    return None


def _resolve_restore_encryption_key(point: dict[str, Any]) -> str | None:
    job_id = str(point.get("job_id") or "")
    by_id = _job_key_by_id(job_id, point.get("encryption_key_id"))
    if by_id:
        return by_id
    own_key = str(point.get("encryption_key") or "").strip()
    if own_key:
        return own_key
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

    target_inventory_id = str(request.get("target_inventory_id") or "").strip()
    target_inventory_name = str(request.get("target_inventory_name") or "").strip() or None
    if not target_inventory_id and not target_inventory_name:
        target_inventory_id = str(defaults.get("target_inventory_id") or "").strip()
    _ensure(bool(target_inventory_id or target_inventory_name), 422, "invalid_restore", "Restore target inventory is required")
    target_inventory = resolve_inventory(
        name=target_inventory_name,
        record_id=target_inventory_id or None,
        name_option="--target-inventory",
        id_option="--target-inventory-id",
    )
    _ensure(
        _restore_target_kind_matches(point, target_inventory),
        422,
        "restore_option_unsupported",
        f"A {_point_platform_label(point)} restore point can only be restored to a {_point_platform_label(point)} "
        f"inventory; '{target_inventory.get('name')}' is {_inventory_platform_label(target_inventory)}",
        inventory_id=target_inventory.get("id"),
    )
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
    datastore_placement, placement_datastores = _resolve_restore_datastore_placement(
        source_vm,
        inventory_id=str(target_inventory["id"]),
        host=host,
        default_datastore=datastore,
        requested=request.get("datastore_map"),
        restore_mode=restore_mode,
    )
    disk_targets, disk_target_datastores = _resolve_restore_disk_placement(
        source_vm,
        inventory_id=str(target_inventory["id"]),
        host=host,
        requested=request.get("disk_datastore_map"),
        restore_mode=restore_mode,
    )
    disk_target_by_index = {int(item["index"]): str(item["target_datastore"]) for item in disk_targets}
    disk_provisioning = str(request.get("disk_provisioning") or "").strip() or None
    for target_name, target_datastore in {**placement_datastores, **disk_target_datastores}.items():
        landing = [
            disk
            for position, disk in enumerate(source_vm.get("disks") or [], start=1)
            if isinstance(disk, dict)
            and _restore_disk_target_name(disk, position, disk_target_by_index, datastore_placement, target_datastore_name) == target_name
        ]
        if landing:
            _ensure_restore_target_capacity(landing, target_datastore, restore_mode, disk_provisioning)

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

    _ensure(
        restore_mode != "in_place"
        or not (
            request.get("network_mappings")
            or _parse_restore_network_map(request.get("legacy_network_map"))
            or request.get("nic_network_map")
        ),
        422,
        "invalid_restore",
        "In-place restore keeps every network adapter on its own network and cannot take a network mapping",
    )
    requested_network_mappings = [
        *(request.get("network_mappings") if isinstance(request.get("network_mappings"), list) else []),
        *_resolve_restore_nic_network_mappings(
            source_vm,
            inventory_id=str(target_inventory["id"]),
            host=host,
            requested=request.get("nic_network_map"),
        ),
    ]
    network_map, network_adapter_map = _resolve_restore_network_selection(
        point=point,
        manifest=manifest,
        target_inventory_id=str(target_inventory["id"]),
        mappings=requested_network_mappings,
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
    encryption_key = str(payload.get("encryption_key") or "").strip() or _resolve_restore_encryption_key(point)
    _ensure(
        bool(encryption_key) or not _point_is_encrypted(point),
        409,
        "encryption_key_unavailable",
        "This restore point is encrypted and no decryption key is stored for it. Repeat the restore with an "
        "explicit encryption key.",
        point_id=point_id,
    )
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
            "datastore_map": datastore_placement or None,
            "disk_datastore_map": disk_targets or None,
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
            "network_adapter_map": network_adapter_map or None,
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
    mode_label = "In-place" if restore_mode == "in_place" else "Out-of-place"
    placement_text = ", ".join(f"{source} -> {target}" for source, target in sorted((datastore_placement or {}).items()))
    network_text = ", ".join(f"{source} -> {target}" for source, target in sorted((network_map or {}).items()))
    restore_summary = (
        f"{mode_label} recovery of {point.get('vm_name') or point.get('vm_id')} from restore point {point_id} "
        f"({point.get('created_at')}, {_restore_point_type(point)}) to {target_vm_name} on "
        f"{target_inventory['name']}/{resolved_target_host_name}, storage {target_datastore_name}"
        + (f", datastore mapping {placement_text}" if placement_text and restore_mode != "in_place" else "")
        + (f", network mapping {network_text}" if network_text and restore_mode != "in_place" else "")
        + (", power on afterwards" if power_on else "")
    )
    _append_task_log(task, level="info", message=restore_summary, component="session")
    LOGGER.info("Restore session %s queued: %s", task["id"], restore_summary)
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
        "version": product_version(),
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
        "version": product_version(),
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


SECRET_CONFIGURATION_KEY_PARTS = ("PASSWORD", "SECRET", "TOKEN", "PASSPHRASE")
_URI_CREDENTIALS_PATTERN = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/]*@", re.IGNORECASE)


def configuration_without_secrets(configuration: dict[str, Any]) -> dict[str, Any]:
    shown: dict[str, Any] = {}
    for key, value in configuration.items():
        if any(part in str(key).upper() for part in SECRET_CONFIGURATION_KEY_PARTS):
            shown[key] = SECRET_MASK if value not in (None, "") else value
        elif isinstance(value, str):
            shown[key] = _URI_CREDENTIALS_PATTERN.sub(rf"\g<1>{SECRET_MASK}@", value)
        else:
            shown[key] = value
    return shown


def export_catalog_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(password, str) or not password:
        raise api_error(422, "invalid_argument", "password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise api_error(
            422,
            "invalid_argument",
            f"Export password must be at least {MIN_PASSWORD_LENGTH} characters. "
            "It is the only thing protecting the credentials and server configuration in the archive.",
        )

    catalog = snapshot_catalog()
    configuration = snapshot_configuration()
    manifest = {
        "format_version": CATALOG_SNAPSHOT_FORMAT_VERSION,
        "product": PRODUCT_NAME,
        "product_version": product_version(),
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

    replaced_users = sorted(str(user.get("name") or "") for user in users_replaced_by_import(catalog))
    counts = restore_catalog(catalog)
    return {
        "manifest": manifest,
        "restored": counts,
        "replaced_users": replaced_users,
        "configuration": configuration_without_secrets(configuration),
    }


def _verify_disk_artifact(
    point: dict[str, Any],
    disk: dict[str, Any],
    *,
    encryption_key: str = "",
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
            encryption_key=encryption_key,
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


def _repository_chain_points(repository_id: str) -> list[list[dict[str, Any]]]:
    chains: list[list[dict[str, Any]]] = []
    for points in grouped_backup_points().values():
        held = [point for point in points if str(point.get("repository_id") or "") == repository_id]
        if held:
            chains.append(held)
    return chains


def _verify_repository_backup_health(repository_id: str, *, whole_chains: bool = False) -> dict[str, Any]:
    if whole_chains:
        chains = _repository_chain_points(repository_id)
        checked_points = [point for chain in chains for point in chain]
        chain_count = len(chains)
    else:
        checked_points = _repository_latest_points(repository_id)
        chain_count = len(checked_points)
    work_items: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for point in checked_points:
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}

        for disk in artifact.get("disks", []):
            if isinstance(disk, dict):
                work_items.append((point, disk))

    if not work_items:
        return {
            "status": "skipped",
            "checked_chains": chain_count,
            "checked_restore_points": len(checked_points),
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
    covered = f"{len(checked_points)} restore point(s) across {chain_count} chain(s)"
    if not whole_chains:
        covered = f"the newest restore point of each of {chain_count} chain(s)"
    message = (
        f"{len(errors)} of {len(work_items)} backup artifact(s) of {covered} failed verification"
        if errors
        else f"Verified {len(work_items)} backup artifact(s) of {covered}"
    )
    return {
        "status": status,
        "checked_chains": chain_count,
        "checked_restore_points": len(checked_points),
        "checked_artifacts": len(work_items),
        "errors": errors,
        "message": message,
    }
