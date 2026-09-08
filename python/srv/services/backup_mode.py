# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any, Literal

from python.srv.core.mongo_db_wrapper import BACKUPS, catalog_records


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
    return "incremental"


_WEEKDAY_INDEX = {name: index for index, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


def full_backup_cycle_days(job: dict[str, Any], *, default_days: int) -> int:
    indexes: set[int] = set()
    for key in ("active_full_days", "synthetic_full_days"):
        for value in job.get(key) or ():
            index = _WEEKDAY_INDEX.get(str(value).strip().lower()[:3])
            if index is not None:
                indexes.add(index)
    if not indexes:
        return max(int(default_days), 1)
    ordered = sorted(indexes)
    gaps = [ordered[position + 1] - ordered[position] for position in range(len(ordered) - 1)]
    gaps.append(ordered[0] + 7 - ordered[-1])
    return max(max(gaps), 1)


def object_lock_actual_retention_days(
    job: dict[str, Any] | None,
    immutability: dict[str, Any],
    *,
    default_generation_days: int,
) -> int:
    configured = int(immutability.get("retention_days") or 0)
    if configured < 1:
        return 0
    return configured + full_backup_cycle_days(job or {}, default_days=default_generation_days)
