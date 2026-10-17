# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASK_LOCK,
    TASKS,
    catalog_entries,
    catalog_records,
    persist_backup,
    persist_job,
    persist_task,
)
from python.srv.core.storage_tool import (
    STORAGE_LAYOUT_BY_ID,
    extract_s3_config,
    hydrate_manifest_via_disk_tool,
    immutability_is_enabled,
    is_s3_repository,
    join_s3_prefix,
    list_disk_tool_backups,
    remove_disk_tool_backup,
    repository_cache_root,
    safe_path_component,
)
from python.srv.core.utils import utc_now
from python.srv.services.abr_v41 import (
    _append_task_log,
    apply_retention_to_chain,
    chain_clock_anomalies,
    extend_segment_object_lock,
    normalize_backup_point,
    normalize_job_record,
)
from python.srv.services.api_errors import ApiError

logger = logging.getLogger(__name__)


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _resolve_repository_ids(task: dict[str, Any]) -> list[str]:
    parameters = task.get("parameters")
    if isinstance(parameters, dict):
        ids = _coerce_string_list(parameters.get("repository_ids"))
        if not ids:
            single = parameters.get("repository_id")
            if isinstance(single, str) and single:
                ids = [single]
        if ids:
            return [repo_id for repo_id in ids if repo_id in REPOSITORIES]

    job_id = task.get("job_id")
    job = JOBS.get(str(job_id)) if isinstance(job_id, str) else None
    if isinstance(job, dict):
        normalized_job = normalize_job_record(job)
        repo_id = str(normalized_job.get("repo_id") or "")
        if repo_id and repo_id in REPOSITORIES:
            return [repo_id]
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if isinstance(metadata, dict):
        ids = _coerce_string_list(metadata.get("repository_ids"))
        if ids:
            return [repo_id for repo_id in ids if repo_id in REPOSITORIES]

        names = _coerce_string_list(metadata.get("repo"))
        if not names:
            names = _coerce_string_list(metadata.get("repository_names"))
        resolved: list[str] = []
        lower_name_map = {
            str(repository.get("name", "")).lower(): repository_id
            for repository_id, repository in catalog_entries(REPOSITORIES)
        }
        for name in names:
            repo_id = lower_name_map.get(name.lower())
            if repo_id:
                resolved.append(repo_id)
        if resolved:
            return resolved

    return []


def _resolve_point_encryption_key(
    task: dict[str, Any], normalized_job: dict[str, Any] | None, result: dict[str, Any]
) -> str | None:
    if not result.get("encryption_enabled"):
        return None
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    key = str(parameters.get("encryption_key") or "").strip()
    if key:
        return key
    job_parameters = normalized_job.get("task_parameters") if isinstance(normalized_job, dict) else None
    if isinstance(job_parameters, dict):
        return str(job_parameters.get("encryption_key") or "").strip() or None
    return None


def _resolve_point_encryption_key_id(
    normalized_job: dict[str, Any] | None, key: str | None
) -> int | None:
    if not key:
        return None
    job_parameters = normalized_job.get("task_parameters") if isinstance(normalized_job, dict) else None
    if not isinstance(job_parameters, dict):
        return None
    for entry in job_parameters.get("encryption_keys") or []:
        if isinstance(entry, dict) and str(entry.get("key") or "") == key:
            try:
                return int(entry.get("key_id"))
            except (TypeError, ValueError):
                return None
    return None


def _build_placements(task: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    finished_at = result.get("finished_at") or utc_now()
    disk_paths = [
        str(disk.get("destination_path"))
        for disk in result.get("disks", [])
        if isinstance(disk, dict) and disk.get("destination_path")
    ]
    base_placement = {
        "status": "available",
        "present": True,
        "created_at": finished_at,
        "manifest_path": result.get("manifest_path"),
        "backup_root": result.get("backup_root"),
        "disk_paths": disk_paths,
    }

    repository_ids = _resolve_repository_ids(task)
    if repository_ids:
        placements: list[dict[str, Any]] = []
        for repository_id in repository_ids:
            repository = REPOSITORIES.get(repository_id)
            if not isinstance(repository, dict):
                continue
            placement = dict(base_placement)
            placement.update(
                {
                    "repository_id": repository_id,
                    "repository_name": repository.get("name"),
                    "repository_type": repository.get("type"),
                    "location_hint": repository.get("path"),
                }
            )
            placements.append(placement)
        if placements:
            return placements

    if result.get("backup_root") or result.get("manifest_path") or disk_paths:
        placement = dict(base_placement)
        placement.update(
            {
                "repository_id": None,
                "repository_name": None,
                "repository_type": "filesystem-path",
                "location_hint": result.get("backup_root") or result.get("manifest_path"),
            }
        )
        return [placement]

    return []


def _resolve_size_bytes(result: dict[str, Any]) -> int | None:
    # `transferred_bytes` is what the engine actually wrote for THIS backup
    # (the whole disk for a full, only the changed blocks for an incremental
    # -- see run_vcenter_backup). `processed_bytes` is the read-side estimate,
    # used as a fallback when the write-side figure wasn't tracked (e.g. an
    # engine that predates this field). Without this, the catalog fell back to
    # scanning `artifact.disks[].destination_path`, which is blank for `.abr`
    # stores and left every point -- full or incremental -- showing the same
    # near-empty manifest-only size.
    for field_name in ("transferred_bytes", "processed_bytes"):
        raw = result.get(field_name)
        if raw not in (None, ""):
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return None


# Public because the manifest-import path in abr_v41 resolves the same two
# fields from the same payload shape, and this module already owns the rule.
def resolve_logical_size_bytes(result: dict[str, Any]) -> int | None:
    """The real, uncompressed size behind this backup, as abr-vmdk-tool counted
    it (`raw_data_size`, empty blocks excluded). None on an engine that doesn't
    report it -- the UI shows the stored size alone rather than guessing."""
    raw = result.get("logical_bytes")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def resolve_compression_ratio(result: dict[str, Any]) -> float | None:
    """Stored / logical for this backup. Recomputed from the two sizes when the
    engine reported them but not the ratio, so an older manifest that carries
    only the byte counts still yields one."""
    raw = result.get("compression_ratio")
    if raw not in (None, ""):
        try:
            ratio = float(raw)
        except (TypeError, ValueError):
            ratio = None
        if ratio is not None and 0 < ratio <= 1:
            return round(ratio, 6)

    logical = resolve_logical_size_bytes(result)
    stored = _resolve_size_bytes(result)
    if logical and stored is not None and stored >= 0:
        return round(stored / logical, 6)
    return None


def _resolve_compression_level(result: dict[str, Any]) -> int | None:
    raw = result.get("compression_level")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _record_session_warning(
    task: dict[str, Any], message: str, *, component: str = "retention", **details: Any
) -> None:
    warnings = task.get("warnings") if isinstance(task.get("warnings"), list) else []
    warnings.append({"message": message, **{k: v for k, v in details.items() if v}})
    task["warnings"] = warnings
    _append_task_log(task, level="warn", message=message, component=component)


def _report_clock_anomalies(task: dict[str, Any], chain_id: str, retention_days: int) -> None:
    for anomaly in chain_clock_anomalies(chain_id):
        point_id = str(anomaly.get("point_id") or "")
        if anomaly.get("kind") == "ahead_of_server_clock":
            message = (
                f"Restore point {point_id} is dated {anomaly.get('created_at')}, which is ahead of this "
                f"server's time ({anomaly.get('server_time')}). Retention is measured from that date, so "
                f"the point can outlive the job's retention of {retention_days} day(s) until the clock "
                "catches up. Check the clock on this server."
            )
        else:
            message = (
                f"Restore point {point_id} is dated {anomaly.get('created_at')}, earlier than the restore "
                f"point it chains onto ({anomaly.get('parent_point_id')}, dated "
                f"{anomaly.get('parent_created_at')}). The chain no longer follows the dates, which "
                "happens when the server clock was ahead while a backup was written."
            )
        logger.warning("%s", message)
        _record_session_warning(task, message, component="catalog", **anomaly)


def _adopt_id_layout(job: dict[str, Any] | None, result: dict[str, Any]) -> None:
    if not isinstance(job, dict) or result.get("storage_layout") != STORAGE_LAYOUT_BY_ID:
        return
    if job.get("storage_layout") == STORAGE_LAYOUT_BY_ID:
        return
    stored = JOBS.get(str(job.get("id") or ""))
    if not isinstance(stored, dict):
        return
    persist_job(normalize_job_record({**stored, "storage_layout": STORAGE_LAYOUT_BY_ID, "updated_at": utc_now()}))
    logger.info(
        "Job %s now keeps its backups in folders named by job and VM id; its earlier backups stay where they are.",
        job.get("id"),
    )


def register_backup_from_completed_task(task: dict[str, Any]) -> dict[str, Any] | None:
    if task.get("task_type") != "backup" or task.get("status") != "completed":
        return None

    result = task.get("result")
    if not isinstance(result, dict):
        return None

    if result.get("stop_requested_after_completion"):
        _record_session_warning(
            task,
            "Stop requested after the backup data was already complete; the backup was kept "
            "rather than discarded.",
            component="engine",
        )

    existing = next(
        (
            backup
            for backup in catalog_records(BACKUPS)
            if isinstance(backup.get("origin"), dict)
            and backup["origin"].get("task_id") == task.get("id")
        ),
        None,
    )
    if existing is not None:
        return existing

    hypervisor_id = str(task.get("hypervisor_id") or "")
    hypervisor = HYPERVISORS.get(hypervisor_id, {})
    resource_type = str(task.get("resource_type") or "")
    resource_external_id = str(task.get("resource_external_id") or "")
    resource = RESOURCES_BY_HV.get(hypervisor_id, {}).get((resource_type, resource_external_id), {})
    job_id = str(task.get("job_id") or "") or None
    job = JOBS.get(job_id) if job_id else None

    created_at = result.get("finished_at") or task.get("updated_at") or utc_now()
    placements = _build_placements(task, result)
    normalized_job = normalize_job_record(job) if isinstance(job, dict) else None
    repository_id = None
    repository_name = None
    if placements:
        repository_id = placements[0].get("repository_id")
        repository_name = placements[0].get("repository_name")
    if normalized_job is not None:
        repository_id = repository_id or normalized_job.get("repo_id")
        repository_name = repository_name or normalized_job.get("repo_name")
    repository_type = placements[0].get("repository_type") if placements else None
    execution_type = str(
        result.get("resolved_backup_type")
        or ("Incremental" if result.get("incremental") else "ActiveFull")
    )
    execution_reason = str(
        result.get("resolved_backup_reason")
        or ("StandardIncremental" if execution_type == "Incremental" else "FirstRun")
    )
    full_backup = execution_type in {"ActiveFull", "SyntheticFull"}
    parent_point_id = None
    segment_id = None
    if normalized_job is not None:
        prior_points = [
            normalize_backup_point(point)
            for point in catalog_records(BACKUPS)
            if str((point.get("origin") or {}).get("job_id") if isinstance(point.get("origin"), dict) else point.get("job_id") or "") == str(normalized_job.get("id") or "")
            and str((point.get("source") or {}).get("vm_external_id") if isinstance(point.get("source"), dict) else point.get("vm_id") or "") == resource_external_id
            and str((point.get("placements") or [{}])[0].get("repository_id") if isinstance(point.get("placements"), list) and point.get("placements") else point.get("repository_id") or "") == str(repository_id or "")
        ]
        prior_points.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        base = next(
            (point for point in prior_points if point.get("id") and point.get("id") == result.get("parent_point_id")),
            prior_points[-1] if prior_points else None,
        )
        if base is not None:
            parent_point_id = base.get("id")
            segment_id = str(base.get("segment_id") or base.get("id") or "")
    if full_backup:
        segment_id = None
        parent_point_id = None

    generation = result.get("immutability_generation") if isinstance(result.get("immutability_generation"), dict) else {}
    if not generation and repository_type == "s3" and immutability_is_enabled(
        extract_s3_config(REPOSITORIES.get(str(repository_id or ""), {})).get("immutability")
    ):
        logger.warning(
            "Backup %s went to an immutable repository but reported no block generation, so its Object Lock "
            "cannot be kept in step with the rest of the chain",
            task.get("id"),
        )
    point_encryption_key = _resolve_point_encryption_key(task, normalized_job, result)
    record = {
        "id": str(uuid4()),
        "status": "available",
        "backup_mode": result.get("backup_mode") or ("incremental" if result.get("incremental") else "full"),
        "size_bytes": _resolve_size_bytes(result),
        "logical_size_bytes": resolve_logical_size_bytes(result),
        "compression_ratio": resolve_compression_ratio(result),
        "compression_level": _resolve_compression_level(result),
        "encryption_enabled": bool(result.get("encryption_enabled")),
        "consistency": result.get("consistency"),
        "consistency_reason": result.get("consistency_reason"),
        "encryption_key": point_encryption_key,
        "encryption_key_id": _resolve_point_encryption_key_id(normalized_job, point_encryption_key),
        "execution_type": execution_type,
        "execution_reason": execution_reason,
        "full_backup": full_backup,
        "parent_point_id": parent_point_id,
        "segment_id": segment_id,
        "hypervisor_kind": result.get("hypervisor_kind") or hypervisor.get("kind"),
        "chain_id": (
            f"backup::{normalized_job.get('id') if normalized_job else job_id or '-'}::"
            f"{result.get('vm_external_id') or resource_external_id or '-'}::"
            f"{repository_id or '-'}"
        ),
        "job_id": normalized_job.get("id") if normalized_job else job_id,
        "job_name": normalized_job.get("name") if normalized_job else None,
        "vm_id": result.get("vm_external_id") or resource_external_id,
        "vm_name": result.get("vm_name") or resource.get("name"),
        "inventory_id": hypervisor_id,
        "inventory_name": hypervisor.get("name"),
        "repository_id": repository_id,
        "repository_name": repository_name,
        "repository_type": repository_type,
        "gfs_marks": {"weekly": False, "monthly": False, "yearly": False},
        "gfs_protected": False,
        "immutability_generation_started_at": generation.get("generation_started_at"),
        "immutability_expires_at": generation.get("expires_at"),
        "source": {
            "hypervisor_id": hypervisor_id,
            "hypervisor_name": hypervisor.get("name"),
            "resource_type": resource_type,
            "resource_external_id": resource_external_id,
            "resource_name": resource.get("name"),
            "vm_name": result.get("vm_name") or resource.get("name"),
            "vm_external_id": result.get("vm_external_id") or resource_external_id,
        },
        "origin": {
            "task_id": task.get("id"),
            "job_id": job_id,
            "job_name": (
                job.get("metadata", {}).get("name")
                if isinstance(job, dict) and isinstance(job.get("metadata"), dict)
                else None
            ),
            "created_at": created_at,
        },
        "artifact": {
            "manifest_path": result.get("manifest_path"),
            "backup_root": result.get("backup_root"),
            "disk_count": result.get("disk_count"),
            "disks": result.get("disks", []),
            "skipped_disks": result.get("skipped_disks", []),
        },
        "placements": placements,
        "created_at": created_at,
    }
    normalized_record = normalize_backup_point(record)
    if not normalized_record.get("segment_id"):
        normalized_record["segment_id"] = normalized_record["id"]
    persist_backup(normalized_record)
    _adopt_id_layout(normalized_job, result)
    if generation.get("starts_new_generation") and generation.get("expires_at"):
        try:
            report = extend_segment_object_lock(
                str(normalized_record.get("segment_id") or ""),
                expires_at=str(generation["expires_at"]),
                skip_point_ids={str(normalized_record.get("id") or "")},
            )
        except (OSError, ApiError):
            logger.exception(
                "Could not extend the Object Lock of segment %s to the new block generation",
                normalized_record.get("segment_id"),
            )
        else:
            for failure in report["failures"]:
                reason = str(failure.get("message") or "unknown reason")
                logger.warning(
                    "Could not extend the Object Lock of restore point %s (%s) to the new block generation: %s",
                    failure.get("restore_point_id"),
                    failure.get("backup_name"),
                    reason,
                )
                _record_session_warning(
                    task,
                    "Object Lock could not be extended to the new block generation for restore point "
                    f"{failure.get('restore_point_id')} ({failure.get('backup_name')}: {reason}); the backup it "
                    "belongs to may lose protection before the newer backups that depend on it",
                    restore_point_id=failure.get("restore_point_id"),
                    backup_name=failure.get("backup_name"),
                    reason=reason,
                )
    if normalized_job is not None:
        retention_vm_id = str(normalized_record.get("vm_id") or "")
        retention_repository_id = str(normalized_record.get("repository_id") or "") or None
        try:
            _report_clock_anomalies(
                task,
                str(normalized_record.get("chain_id") or ""),
                int(normalized_job.get("retention_days") or 0),
            )
        except Exception:
            logger.exception(
                "Could not check the dates of chain %s against the server clock",
                normalized_record.get("chain_id"),
            )
        try:
            failures = apply_retention_to_chain(
                normalized_job, vm_id=retention_vm_id, repository_id=retention_repository_id
            )
        except (OSError, ApiError) as exc:
            logger.exception(
                "Retention pass failed for job=%s vm=%s repository=%s",
                normalized_job.get("id"),
                retention_vm_id,
                retention_repository_id,
            )
            _record_session_warning(
                task,
                f"Retention pass failed for this job and no expired restore point was removed: {exc}",
                job_id=str(normalized_job.get("id") or ""),
                vm_id=retention_vm_id,
                repository_id=retention_repository_id,
            )
        else:
            for failure in failures:
                _record_session_warning(
                    task,
                    "Retention could not remove expired restore point "
                    f"{failure.get('point_id')}; it is kept for retry",
                    **failure,
                )
    return normalized_record


_MANIFEST_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError)


def _load_backup_manifest_payload(record: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    manifest_path = str(artifact.get("manifest_path") or "").strip()
    if not manifest_path:
        return {}
    path = Path(manifest_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        repository = REPOSITORIES.get(str(record.get("repository_id") or ""))
        abr_path = next(
            (
                str(disk.get("abr_path") or "")
                for disk in artifact.get("disks", [])
                if isinstance(disk, dict) and disk.get("abr_path")
            ),
            "",
        )
        if not (isinstance(repository, dict) and abr_path and hydrate_manifest_via_disk_tool(repository, abr_path, path)):
            logger.debug("Manifest not found for backup %s at %s; leaving details unresolved", record.get("id"), path)
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except _MANIFEST_READ_ERRORS:
            logger.warning("Recovered manifest for backup %s is not valid JSON", record.get("id"))
            return {}
    except _MANIFEST_READ_ERRORS:
        logger.warning("Unable to read manifest for backup %s from %s", record.get("id"), path)
        return {}
    return payload if isinstance(payload, dict) else {}


def backfill_backup_encryption_keys() -> int:
    updated = 0
    for record in catalog_records(BACKUPS):
        if not record.get("encryption_enabled"):
            continue
        if str(record.get("encryption_key") or "").strip():
            continue
        job = JOBS.get(str(record.get("job_id") or "")) or {}
        parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else {}
        key = str(parameters.get("encryption_key") or "").strip()
        if not key:
            continue
        enriched = dict(record)
        enriched["encryption_key"] = key
        persist_backup(enriched)
        updated += 1
    return updated


def backfill_job_encryption_keys() -> int:
    from python.srv.services.abr_v41 import _normalized_encryption_keys

    updated = 0
    for job in catalog_records(JOBS):
        parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else None
        if not isinstance(parameters, dict) or not parameters.get("encryption_enabled"):
            continue
        if _normalized_encryption_keys(parameters.get("encryption_keys")):
            continue
        key = str(parameters.get("encryption_key") or "").strip()
        if not key:
            continue
        enriched = dict(job)
        enriched["task_parameters"] = {
            **parameters,
            "encryption_keys": [{"key_id": 0, "key": key}],
            "encryption_key_id": 0,
        }
        persist_job(enriched)
        updated += 1
    return updated


def backfill_backup_encryption_key_ids() -> int:
    updated = 0
    for record in catalog_records(BACKUPS):
        key = str(record.get("encryption_key") or "").strip()
        if not key or record.get("encryption_key_id") is not None:
            continue
        job = JOBS.get(str(record.get("job_id") or "")) or {}
        key_id = _resolve_point_encryption_key_id({"task_parameters": job.get("task_parameters")}, key)
        if key_id is None:
            continue
        enriched = dict(record)
        enriched["encryption_key_id"] = key_id
        persist_backup(enriched)
        updated += 1
    return updated


def backfill_backup_detail_fields() -> int:
    updated = 0
    for record in catalog_records(BACKUPS):
        if (
            "compression_level" in record
            and "encryption_enabled" in record
            and "compression_ratio" in record
        ):
            continue
        artifact = record.get("artifact") if isinstance(record.get("artifact"), dict) else {}
        try:
            payload = _load_backup_manifest_payload(record, artifact)
            enriched = dict(record)
            enriched["compression_level"] = _resolve_compression_level(payload)
            enriched["encryption_enabled"] = bool(payload.get("encryption_enabled"))
            # Stays None for a point written before the mover reported sizes;
            # the manifest simply has nothing to read them from.
            enriched["logical_size_bytes"] = resolve_logical_size_bytes(payload)
            enriched["compression_ratio"] = resolve_compression_ratio(payload)
            persist_backup(enriched)
            updated += 1
        except Exception:
            logger.exception("Skipping detail backfill for backup %s due to an unexpected error", record.get("id"))
    return updated


def _recorded_chain_folder(task: dict[str, Any], repository: dict[str, Any]) -> tuple[str, ...] | None:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    recorded = Path(str(result.get("backup_root") or "").strip() or ".")
    if recorded.name != str(task.get("id") or "") or not recorded.parent.name:
        return None
    if is_s3_repository(repository):
        from python.srv.plugins.vcenter.vcenter_backup import _default_backup_root

        root = repository_cache_root(_default_backup_root(), repository)
    else:
        root = Path(str(repository.get("path") or "").strip() or ".")
    try:
        relative = recorded.parent.resolve().relative_to(root.resolve())
    except ValueError:
        return (recorded.parent.name,)
    return relative.parts if 1 <= len(relative.parts) <= 2 else (recorded.parent.name,)


def _interrupted_task_output_location(task: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    repository = REPOSITORIES.get(str(parameters.get("repository_id") or ""))
    if not isinstance(repository, dict):
        return None
    task_id = str(task.get("id") or "")
    if not task_id:
        return None
    vm_name = str(parameters.get("vm_name") or "").strip()
    if not vm_name:
        vm_id = str(task.get("resource_external_id") or "")
        for point in catalog_records(BACKUPS):
            if str(point.get("vm_id") or "") == vm_id and point.get("vm_name"):
                vm_name = str(point.get("vm_name"))
                break
    vm_component = safe_path_component(vm_name or task.get("resource_external_id"), fallback="vm")
    chain_parts = _recorded_chain_folder(task, repository) or (vm_component,)
    if is_s3_repository(repository):
        return repository, join_s3_prefix(extract_s3_config(repository).get("prefix"), *chain_parts, task_id)
    root = str(repository.get("path") or "").strip()
    if not root:
        return None
    return repository, str(Path(root).joinpath(*chain_parts) / task_id)


def _task_output_is_registered(task_id: str) -> bool:
    for point in catalog_records(BACKUPS):
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        if task_id in str(artifact.get("manifest_path") or ""):
            return True
        for disk in artifact.get("disks") or []:
            if isinstance(disk, dict) and task_id in str(disk.get("abr_path") or ""):
                return True
    return False


def _task_backup_snapshot(task: dict[str, Any]) -> tuple[str, str] | None:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    snapshot = result.get("backup_snapshot") if isinstance(result.get("backup_snapshot"), dict) else {}
    vm_name = str(snapshot.get("vm_name") or "").strip()
    snapshot_name = str(snapshot.get("name") or "").strip()
    if not vm_name or not snapshot_name:
        return None
    return vm_name, snapshot_name


def forget_task_backup_snapshot(task_id: str) -> None:
    record = TASKS.get(task_id)
    if not isinstance(record, dict):
        return
    result = record.get("result")
    if not isinstance(result, dict) or "backup_snapshot" not in result:
        return
    trimmed = dict(result)
    trimmed.pop("backup_snapshot", None)
    updated = dict(record)
    updated["result"] = trimmed
    updated["updated_at"] = utc_now()
    persist_task(updated)


def stranded_backup_snapshots(
    hypervisor_id: str,
    vm_name: str,
    exclude_task_id: str = "",
) -> list[tuple[str, str]]:
    pending: list[tuple[str, str]] = []
    for record in catalog_records(TASKS):
        task_id = str(record.get("id") or "")
        if not task_id or task_id == exclude_task_id:
            continue
        if str(record.get("status") or "") == "running":
            continue
        if str(record.get("hypervisor_id") or "") != hypervisor_id:
            continue
        snapshot = _task_backup_snapshot(record)
        if snapshot is None or snapshot[0] != vm_name:
            continue
        pending.append((task_id, snapshot[1]))
    return pending


_ACTIVE_TASK_STATES = {"queued", "running", "stopping"}


def task_access_lock(task: dict[str, Any]) -> tuple[str, str] | None:
    lock = task.get("access_lock") if isinstance(task.get("access_lock"), dict) else {}
    vm_name = str(lock.get("vm_name") or "").strip()
    vm_moref = str(lock.get("vm_moref") or "").strip()
    if not vm_name and not vm_moref:
        return None
    return vm_name, vm_moref


def _update_finished_task(task_id: str, change: Any) -> None:
    with TASK_LOCK:
        record = TASKS.get(task_id)
        if not isinstance(record, dict):
            return
        updated = dict(record)
        change(updated)
        updated["updated_at"] = utc_now()
        persist_task(updated)


def forget_task_access_lock(task_id: str, *, released_message: str | None = None) -> None:
    def change(record: dict[str, Any]) -> None:
        record["access_lock"] = None
        if released_message:
            _append_task_log(record, level="info", message=released_message, component="engine")

    _update_finished_task(task_id, change)


def _note_access_lock_retry(task_id: str, message: str) -> None:
    def change(record: dict[str, Any]) -> None:
        lock = dict(record.get("access_lock") or {})
        if lock.get("retry_noted"):
            return
        lock["retry_noted"] = True
        record["access_lock"] = lock
        _append_task_log(record, level="warn", message=message, component="engine")
        warnings = record.get("warnings") if isinstance(record.get("warnings"), list) else []
        record["warnings"] = [*warnings, {"message": message, "vm_name": lock.get("vm_name")}]

    _update_finished_task(task_id, change)


def access_lock_failure_reason(host: str, exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"vCenter {host} cannot be reached ({exc})"
    return str(exc)


def vm_in_use_by_another_task(hypervisor_id: str, vm_name: str, exclude_task_id: str = "") -> bool:
    for record in catalog_records(TASKS):
        if str(record.get("id") or "") == exclude_task_id:
            continue
        if str(record.get("status") or "") not in {"running", "stopping"}:
            continue
        if str(record.get("hypervisor_id") or "") != hypervisor_id:
            continue
        parameters = record.get("parameters") if isinstance(record.get("parameters"), dict) else {}
        names = {
            str(parameters.get(key) or "").strip()
            for key in ("vm_name", "target_vm_name", "source_vm_name")
        }
        if vm_name in names:
            return True
    return False


def release_task_access_lock(task: dict[str, Any]) -> bool:
    lock = task_access_lock(task)
    if lock is None:
        return False
    vm_name, vm_moref = lock
    task_id = str(task.get("id") or "")
    hypervisor_id = str(task.get("hypervisor_id") or "")
    hypervisor = HYPERVISORS.get(hypervisor_id)
    if not isinstance(hypervisor, dict):
        logger.warning(
            "Task %s left VM %r blocked from relocation, but its hypervisor is no longer registered, "
            "so the block has to be lifted in vCenter by hand.",
            task_id,
            vm_name,
        )
        forget_task_access_lock(task_id)
        return False
    if vm_in_use_by_another_task(hypervisor_id, vm_name, exclude_task_id=task_id):
        return False
    from python.srv.plugins import build_hypervisor_plugin

    try:
        with build_hypervisor_plugin(hypervisor) as node:
            release = getattr(node, "release_access_lock", None)
            lifted = bool(release(vm_name, vm_moref or None)) if callable(release) else False
    except Exception as exc:
        reason = access_lock_failure_reason(str((hypervisor.get("metadata") or {}).get("host") or ""), exc)
        logger.warning(
            "Could not lift the relocation block that task %s left on VM %r (%s); retrying later.",
            task_id,
            vm_name,
            reason,
        )
        _note_access_lock_retry(
            task_id,
            f"Could not lift the block on relocating VM {vm_name} that this session's data mover left "
            f"in place, so Storage vMotion and DRS cannot move the VM yet: {reason}. The server keeps "
            "retrying.",
        )
        return False
    forget_task_access_lock(
        task_id,
        released_message=(
            f"Lifted the block on relocating VM {vm_name} that this session's data mover left in place; "
            "Storage vMotion and DRS can move the VM again."
            if lifted
            else None
        ),
    )
    if lifted:
        logger.info("Lifted the relocation block that task %s left on VM %r.", task_id, vm_name)
    return lifted


def release_pending_access_locks() -> int:
    lifted = 0
    for record in catalog_records(TASKS):
        if str(record.get("status") or "") in _ACTIVE_TASK_STATES or task_access_lock(record) is None:
            continue
        try:
            if release_task_access_lock(dict(record)):
                lifted += 1
        except Exception:
            logger.exception("Could not lift the relocation block task %s left behind", record.get("id"))
    return lifted


def _remove_interrupted_task_snapshot(task: dict[str, Any]) -> None:
    snapshot = _task_backup_snapshot(task)
    if snapshot is None:
        return
    vm_name, snapshot_name = snapshot
    task_id = str(task.get("id") or "")
    hypervisor = HYPERVISORS.get(str(task.get("hypervisor_id") or ""))
    if not isinstance(hypervisor, dict):
        logger.warning(
            "Interrupted task %s left the snapshot %r on VM %r, but its hypervisor is no longer "
            "registered, so the snapshot has to be removed by hand.",
            task_id,
            snapshot_name,
            vm_name,
        )
        return
    from python.srv.plugins import build_hypervisor_plugin

    try:
        with build_hypervisor_plugin(hypervisor) as node:
            node.remove_snapshot(vm_name, snapshot_name)
    except Exception as exc:
        logger.warning(
            "Could not remove the snapshot %r that interrupted task %s left on VM %r (%s); "
            "the next backup of that VM will try again.",
            snapshot_name,
            task_id,
            vm_name,
            exc,
        )
        return
    forget_task_backup_snapshot(task_id)
    logger.info(
        "Removed the snapshot %r that interrupted task %s left on VM %r.",
        snapshot_name,
        task_id,
        vm_name,
    )


def _discard_interrupted_task_output(task: dict[str, Any]) -> None:
    task_id = str(task.get("id") or "")
    if _task_output_is_registered(task_id):
        logger.info(
            "Task %s left output that a restore point already references, so it was kept.",
            task_id,
        )
        return
    location = _interrupted_task_output_location(task)
    if location is None:
        return
    repository, abr_path = location
    try:
        names = list_disk_tool_backups(repository, abr_path)
    except Exception:
        logger.warning(
            "Could not list what task %s left at %s; its partial output stays until it is removed by hand.",
            task_id,
            abr_path,
        )
        return
    for name in sorted(names):
        try:
            remove_disk_tool_backup(repository, abr_path, name, timeout=120)
            logger.info("Removed the partial backup %r that task %s left at %s.", name, task_id, abr_path)
        except Exception:
            logger.warning(
                "Could not remove the partial backup %r that task %s left at %s.",
                name,
                task_id,
                abr_path,
            )
    if not is_s3_repository(repository):
        folder = Path(abr_path)
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass


def fail_interrupted_tasks() -> int:
    interrupted = [
        record
        for record in catalog_records(TASKS)
        if str(record.get("status") or "") == "running"
    ]
    for record in interrupted:
        task_id = str(record.get("id") or "")
        updated = dict(record)
        updated["status"] = "failed"
        result = dict(updated.get("result") if isinstance(updated.get("result"), dict) else {})
        result["message"] = (
            "The server stopped while this task was running, so it was abandoned; "
            "any partial output it had written was discarded"
        )
        result["failure_kind"] = "interrupted"
        result["finished_at"] = utc_now()
        updated["result"] = result
        updated["updated_at"] = utc_now()
        try:
            _append_task_log(
                updated,
                level="error",
                message="The server stopped while this task was running; the task was abandoned",
            )
        except Exception:
            logger.exception("Could not write the interruption log line for task %s", task_id)
        persist_task(updated)
        logger.warning("Task %s was still running when the server last stopped; marked as failed.", task_id)
    if interrupted:
        _start_interrupted_task_sweep([dict(record) for record in interrupted])
    return len(interrupted)


def _start_interrupted_task_sweep(tasks: list[dict[str, Any]]) -> None:
    threading.Thread(
        target=_sweep_interrupted_tasks,
        args=(tasks,),
        name="interrupted-task-sweep",
        daemon=True,
    ).start()


def _sweep_interrupted_tasks(tasks: list[dict[str, Any]]) -> None:
    for task in tasks:
        try:
            _remove_interrupted_task_snapshot(task)
        except Exception:
            logger.exception(
                "Could not remove the snapshot interrupted task %s left behind", task.get("id")
            )
        try:
            _discard_interrupted_task_output(task)
        except Exception:
            logger.exception(
                "Could not discard what interrupted task %s left behind", task.get("id")
            )
        try:
            release_task_access_lock(task)
        except Exception:
            logger.exception(
                "Could not lift the relocation block interrupted task %s left behind", task.get("id")
            )
