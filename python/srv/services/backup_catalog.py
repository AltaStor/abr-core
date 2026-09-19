# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    catalog_entries,
    catalog_records,
    persist_backup,
)
from python.srv.core.storage_tool import (
    extract_s3_config,
    hydrate_manifest_via_disk_tool,
    immutability_is_enabled,
)
from python.srv.core.utils import utc_now
from python.srv.services.abr_v41 import (
    _append_task_log,
    apply_retention_to_chain,
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
    return value if value > 0 else None


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
        if prior_points:
            parent_point_id = prior_points[-1].get("id")
            segment_id = str(prior_points[-1].get("segment_id") or prior_points[-1].get("id") or "")
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
    record = {
        "id": str(uuid4()),
        "status": "available",
        "backup_mode": result.get("backup_mode") or ("incremental" if result.get("incremental") else "full"),
        "size_bytes": _resolve_size_bytes(result),
        "logical_size_bytes": resolve_logical_size_bytes(result),
        "compression_ratio": resolve_compression_ratio(result),
        "compression_level": _resolve_compression_level(result),
        "encryption_enabled": bool(result.get("encryption_enabled")),
        "encryption_key": _resolve_point_encryption_key(task, normalized_job, result),
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
        },
        "placements": placements,
        "created_at": created_at,
    }
    normalized_record = normalize_backup_point(record)
    if not normalized_record.get("segment_id"):
        normalized_record["segment_id"] = normalized_record["id"]
    persist_backup(normalized_record)
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
                _record_session_warning(
                    task,
                    "Object Lock could not be extended to the new block generation for restore point "
                    f"{failure.get('restore_point_id')}; the backup it belongs to may lose protection before "
                    "the newer backups that depend on it",
                    **failure,
                )
    if normalized_job is not None:
        retention_vm_id = str(normalized_record.get("vm_id") or "")
        retention_repository_id = str(normalized_record.get("repository_id") or "") or None
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
