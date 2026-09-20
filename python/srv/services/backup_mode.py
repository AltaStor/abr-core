# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from python.srv.core.mongo_db_wrapper import BACKUPS, catalog_records
from python.srv.core.storage_tool import normalize_immutability_retention_scope


RequestedBackupMode = Literal["auto", "full"]
EffectiveBackupMode = Literal["full", "incremental"]


def resolve_requested_backup_mode(parameters: dict[str, Any]) -> RequestedBackupMode:
    value = parameters.get("backup_mode_request")
    if value in (None, ""):
        return "auto"
    normalized = str(value).strip().lower()
    if normalized not in {"auto", "full"}:
        raise ValueError("parameters.backup_mode_request must be 'auto' or 'full'")
    return normalized  # type: ignore[return-value]


def _normalized_execution_type(parameters: dict[str, Any]) -> str | None:
    value = parameters.get("resolved_backup_type")
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    mapping = {
        "incremental": "incremental",
        "activefull": "full",
        "active_full": "full",
        "active-full": "full",
        "syntheticfull": "full",
        "synthetic_full": "full",
        "synthetic-full": "full",
    }
    return mapping.get(normalized, None)


def has_previous_successful_backup(task: dict[str, Any]) -> bool:
    job_id = str(task.get("job_id") or "").strip()
    hypervisor_id = str(task.get("hypervisor_id") or "").strip()
    resource_type = str(task.get("resource_type") or "").strip()
    resource_external_id = str(task.get("resource_external_id") or "").strip()
    if not hypervisor_id or not resource_type or not resource_external_id:
        return False

    for backup in catalog_records(BACKUPS):
        if str(backup.get("status") or "") not in {"available", "completed", "healthy"}:
            continue
        origin = backup.get("origin") if isinstance(backup.get("origin"), dict) else {}
        source = backup.get("source") if isinstance(backup.get("source"), dict) else {}
        if job_id and str(origin.get("job_id") or "").strip() == job_id:
            return True
        if (
            str(source.get("hypervisor_id") or "").strip() == hypervisor_id
            and str(source.get("resource_type") or "").strip() == resource_type
            and str(source.get("resource_external_id") or "").strip() == resource_external_id
        ):
            return True
    return False


def _newest_point_of_chain(task: dict[str, Any]) -> dict[str, Any] | None:
    job_id = str(task.get("job_id") or "").strip()
    hypervisor_id = str(task.get("hypervisor_id") or "").strip()
    resource_type = str(task.get("resource_type") or "").strip()
    resource_external_id = str(task.get("resource_external_id") or "").strip()
    newest: dict[str, Any] | None = None
    newest_created = ""
    for backup in catalog_records(BACKUPS):
        if str(backup.get("status") or "") not in {"available", "completed", "healthy"}:
            continue
        origin = backup.get("origin") if isinstance(backup.get("origin"), dict) else {}
        source = backup.get("source") if isinstance(backup.get("source"), dict) else {}
        matches = bool(job_id) and str(origin.get("job_id") or "").strip() == job_id
        if not matches:
            matches = (
                str(source.get("hypervisor_id") or "").strip() == hypervisor_id
                and str(source.get("resource_type") or "").strip() == resource_type
                and str(source.get("resource_external_id") or "").strip() == resource_external_id
            )
        if not matches:
            continue
        created = str(backup.get("created_at") or "")
        if newest is None or created > newest_created:
            newest, newest_created = backup, created
    return newest


def encryption_changed_since_last_backup(task: dict[str, Any], parameters: dict[str, Any]) -> bool:
    newest = _newest_point_of_chain(task)
    if newest is None:
        return False
    previous_enabled = bool(newest.get("encryption_enabled"))
    previous_key = str(newest.get("encryption_key") or "").strip()
    current_key = str(parameters.get("encryption_key") or "").strip()
    current_enabled = bool(parameters.get("encryption_enabled")) and bool(current_key)
    if previous_enabled != current_enabled:
        return True
    if not current_enabled:
        return False
    if not previous_key:
        return False
    return previous_key != current_key


def resolve_job_backup_mode(task: dict[str, Any], parameters: dict[str, Any]) -> EffectiveBackupMode | None:
    normalized_execution_type = _normalized_execution_type(parameters)
    if normalized_execution_type == "incremental":
        return "incremental"
    if normalized_execution_type == "full":
        return "full"

    if not task.get("job_id") and "backup_mode_request" not in parameters:
        return None

    requested_mode = resolve_requested_backup_mode(parameters)
    if not has_previous_successful_backup(task):
        return "full"
    if requested_mode == "full":
        return "full"
    if encryption_changed_since_last_backup(task, parameters):
        return "full"
    return "incremental"


def next_full_due_at(job: dict[str, Any] | None, *, now: datetime) -> datetime | None:
    schedule = (job or {}).get("active_full_schedule")
    if not isinstance(schedule, dict) or not bool(schedule.get("enabled", True)):
        return None
    from python.srv.services.abr_v41 import schedule_next_run

    try:
        due = schedule_next_run(schedule, after=now)
    except Exception:
        return None
    return due if due.tzinfo else due.replace(tzinfo=timezone.utc)


def days_to_next_full(job: dict[str, Any] | None, *, now: datetime) -> int | None:
    due = next_full_due_at(job, now=now)
    if due is None:
        return None
    return max(int(-(-((due - now).total_seconds() / 86400) // 1)), 1)


def _ceil_days(span: timedelta) -> int:
    return max(int(-(-(span.total_seconds() / 86400) // 1)), 1)


def object_lock_expiry(
    job: dict[str, Any] | None,
    immutability: dict[str, Any],
    *,
    block_generation_days: int,
    now: datetime,
) -> datetime | None:
    """When a backup taken at ``now`` stops having to be kept.

    Every point of one chain resolves to the same instant: the chain is only
    finished once its last increment has aged out, and that is the next full
    plus the job's retention. Deriving the instant rather than a span is what
    keeps two backups taken minutes apart on the same date, so neither rewrites
    the other's lock.
    """
    configured = int(immutability.get("retention_days") or 0)
    if configured < 1:
        return None
    floor = now + timedelta(days=configured)
    if normalize_immutability_retention_scope(immutability.get("retention_scope")) != "job_retention":
        return floor
    retention = int((job or {}).get("retention_days") or 0)
    due = next_full_due_at(job, now=now)
    if due is None:
        due = now + timedelta(days=max(int(block_generation_days), 0))
    return max(due + timedelta(days=retention), floor)


def object_lock_actual_retention_days(
    job: dict[str, Any] | None,
    immutability: dict[str, Any],
    *,
    block_generation_days: int,
    now: datetime | None = None,
) -> int:
    moment = now or datetime.now(timezone.utc)
    expiry = object_lock_expiry(
        job, immutability, block_generation_days=block_generation_days, now=moment
    )
    if expiry is None:
        return 0
    return _ceil_days(expiry - moment)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _chain_generation(chain_id: str) -> tuple[datetime | None, datetime | None]:
    newest: dict[str, Any] | None = None
    newest_created = ""
    for record in catalog_records(BACKUPS):
        if str(record.get("chain_id") or "") != chain_id:
            continue
        created = str(record.get("created_at") or "")
        if newest is None or created > newest_created:
            newest, newest_created = record, created
    if newest is None:
        return None, None
    return (
        _parse_timestamp(newest.get("immutability_generation_started_at")),
        _parse_timestamp(newest.get("immutability_expires_at")),
    )


def object_lock_generation_plan(
    chain_id: str,
    *,
    actual_retention_days: int,
    block_generation_days: int,
    now: datetime,
    starts_segment: bool = False,
    own_expiry: datetime | None = None,
) -> dict[str, Any]:
    if own_expiry is None:
        own_expiry = now + timedelta(days=int(actual_retention_days))
    if int(actual_retention_days) < 1:
        return {"lock_days": 0, "generation_started_at": None, "expires_at": None, "starts_new_generation": False}

    started, expires = (None, None) if starts_segment else _chain_generation(chain_id)
    if started is not None and expires is not None and expires >= own_expiry:
        return {
            "lock_days": _ceil_days(expires - now),
            "generation_started_at": started.isoformat(),
            "expires_at": expires.isoformat(),
            "starts_new_generation": False,
        }

    return {
        "lock_days": _ceil_days(own_expiry - now),
        "generation_started_at": now.isoformat(),
        "expires_at": own_expiry.isoformat(),
        "starts_new_generation": True,
    }
