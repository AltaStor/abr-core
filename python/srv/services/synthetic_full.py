# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from datetime import datetime, timezone

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from python.srv.core.mongo_db_wrapper import REPOSITORIES
from python.srv.core.storage_tool import (
    StorageToolError,
    remove_disk_tool_backup,
    apply_repository_immutability,
    copy_disk_tool_chain,
    disk_tool_storage_args,
    ensure_private_directory,
    is_abr_repository,
    join_s3_prefix,
    run_disk_tool,
    s3_block_generation_days,
)
from python.srv.core.subprocess_util import ProcessCancelled
from python.srv.core.utils import utc_now
from python.srv.services.backup_mode import object_lock_expiry, object_lock_generation_plan

logger = logging.getLogger(__name__)

FULL_EXECUTION_TYPES = frozenset({"ActiveFull", "SyntheticFull"})


class SyntheticFullError(RuntimeError):
    pass


class SyntheticFullValidationError(SyntheticFullError):
    pass


def is_synthetic_full_task(task: dict[str, Any]) -> bool:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    if bool(parameters.get("synthetic_full")):
        return True
    return str(parameters.get("resolved_backup_type") or "") == "SyntheticFull"


def _disk_identity(disk: dict[str, Any], position: int) -> str:
    for key in ("abr_backup_name", "uuid", "datastore_path"):
        value = str(disk.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return f"index:{disk.get('index', position)}"


def _artifact_disks(point: dict[str, Any]) -> list[dict[str, Any]]:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    return [disk for disk in artifact.get("disks", []) if isinstance(disk, dict)]


def _common_prefix(paths: list[str]) -> str:
    if not paths:
        return ""
    absolute = all(path.startswith("/") for path in paths)
    split = [path.strip("/").split("/") for path in paths]
    common: list[str] = []
    for segments in zip(*split):
        if len(set(segments)) != 1:
            break
        common.append(segments[0])
    joined = "/".join(common)
    return f"/{joined}" if absolute and joined else joined


def _relative_to(path: str, prefix: str) -> str:
    stripped = path.strip("/")
    base = prefix.strip("/")
    if base and stripped.startswith(base):
        stripped = stripped[len(base):]
    return stripped.strip("/")


def resolve_source_chain(job_id: str, vm_id: str, repository_id: str | None) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import _points_for_job_vm_repo, _segment_points

    points = _points_for_job_vm_repo(job_id, vm_id, repository_id)
    segments = _segment_points(points)
    return list(segments[-1]) if segments else []


def validate_chain(points: list[dict[str, Any]]) -> None:
    if not points:
        raise SyntheticFullValidationError(
            "No backup chain was found for this VM in this repository; run a full backup first"
        )
    head = points[0]
    head_type = str(head.get("execution_type") or "")
    if head_type not in FULL_EXECUTION_TYPES:
        raise SyntheticFullValidationError(
            f"The chain starts with a '{head_type or 'unknown'}' restore point; a synthetic full needs it to start with a full backup"
        )
    for point in points[1:]:
        point_type = str(point.get("execution_type") or "")
        if point_type != "Incremental":
            raise SyntheticFullValidationError(
                f"Restore point {point.get('id')} is '{point_type or 'unknown'}'; every backup after the full one must be incremental"
            )
    if len(points) < 2:
        raise SyntheticFullValidationError(
            "The chain holds only a full backup with no incrementals, so a synthetic full would copy it unchanged"
        )

    identities = None
    for point in points:
        disks = _artifact_disks(point)
        if not disks:
            raise SyntheticFullValidationError(
                f"Restore point {point.get('id')} records no backup artifacts to merge"
            )
        current = {_disk_identity(disk, position) for position, disk in enumerate(disks)}
        if identities is None:
            identities = current
            continue
        if current != identities:
            raise SyntheticFullValidationError(
                f"Restore point {point.get('id')} describes different disks than the full backup it chains onto"
            )


def build_disk_merges(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: dict[str, list[dict[str, Any]]] = {}
    template: dict[str, dict[str, Any]] = {}
    for point in points:
        for position, disk in enumerate(_artifact_disks(point)):
            identity = _disk_identity(disk, position)
            ordered.setdefault(identity, []).append(disk)
            merged = dict(template.get(identity) or {})
            merged.update({key: value for key, value in disk.items() if value not in (None, "")})
            template[identity] = merged

    merges: list[dict[str, Any]] = []
    for identity, disks in ordered.items():
        paths = [str(disk.get("abr_path") or "") for disk in disks]
        names = [str(disk.get("abr_backup_name") or "") for disk in disks]
        if not all(paths) or not all(names):
            raise SyntheticFullValidationError(
                f"Disk {identity} is missing stored backup locations, so its chain cannot be merged"
            )
        source_path = _common_prefix(paths)
        qualified: list[str] = []
        for path, name in zip(paths, names):
            relative = _relative_to(path, source_path)
            qualified.append(f"{relative}/{name}" if relative else name)
        merges.append(
            {
                "identity": identity,
                "source_path": source_path,
                "source_names": qualified,
                "template": template[identity],
                "newest": disks[-1],
            }
        )
    return merges


def _merged_change_id(newest_disk: dict[str, Any]) -> str:
    return str(newest_disk.get("change_id") or newest_disk.get("next_change_id") or "").strip()


def _discard_partial_merge(
    repository: dict[str, Any],
    destination_path: str | None,
    written: list[str],
    task_root: Path,
    in_flight: str | None = None,
) -> None:
    names = list(written)
    if in_flight and in_flight not in names:
        names.append(in_flight)
    stranded: list[str] = []
    for backup_name in names:
        try:
            remove_disk_tool_backup(repository, destination_path, backup_name, timeout=120)
        except StorageToolError:
            if backup_name == in_flight and backup_name not in written:
                continue
            stranded.append(backup_name)
    if stranded:
        logger.error(
            "Synthetic full left %d merged disk(s) behind at %s: %s. Object Lock keeps them until their "
            "retention expires; remove them once it does.",
            len(stranded),
            destination_path or task_root,
            ", ".join(stranded),
        )
    try:
        if task_root.exists() and not any(task_root.iterdir()):
            task_root.rmdir()
    except OSError:
        pass


def _source_manifest(point: dict[str, Any]) -> dict[str, Any]:
    from python.srv.services.backup_catalog import _load_backup_manifest_payload

    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    return _load_backup_manifest_payload(point, artifact)


def _write_synthetic_manifest(
    repository: dict[str, Any],
    task_root: Path,
    destination_path: str | None,
    payload: dict[str, Any],
    retention_days: int | None = None,
) -> Path:
    ensure_private_directory(task_root)
    manifest_path = task_root / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        run_disk_tool(
            [
                "meta_save",
                "--name",
                "manifest",
                *disk_tool_storage_args(repository, abr_path=destination_path),
                "--source",
                str(manifest_path),
            ],
            timeout=60,
        )
        apply_repository_immutability(
            repository, destination_path, "manifest", retention_days=retention_days, timeout=120
        )
    except StorageToolError:
        logger.debug("Ignoring manifest metadata mirror failure for %s", manifest_path, exc_info=True)
    return manifest_path


def run_synthetic_full(
    task: dict[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from python.srv.plugins.vcenter.vcenter_backup import (
        _dir_size_bytes,
        _resolve_backup_storage_layout,
        _resolve_compression_level,
        _resolve_encryption_settings,
        _resolve_tool_timeout_seconds,
    )
    from python.srv.services.abr_v41 import repository_actual_retention_days
    from python.srv.core.mongo_db_wrapper import JOBS
    from python.srv.core.task_executor import cancel_requested, task_status
    from python.srv.core.storage_tool import extract_s3_config, is_s3_repository

    start_time = time.time()
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    job_id = str(task.get("job_id") or "")
    vm_id = str(task.get("resource_external_id") or "")
    required_task_id = str(parameters.get("requires_task_id") or "")
    if required_task_id:
        prerequisite = task_status(required_task_id)
        if prerequisite != "completed":
            raise SyntheticFullValidationError(
                "The backup that was to supply this run's newest changes finished as "
                f"'{prerequisite or 'unknown'}', so merging now would publish a restore point "
                "that is missing them"
            )

    repository_id = str(parameters.get("repository_id") or "")
    source_repository_id = str(parameters.get("source_repository_id") or "") or repository_id
    repository = REPOSITORIES.get(repository_id)
    source_repository = REPOSITORIES.get(source_repository_id)
    if not isinstance(repository, dict):
        raise SyntheticFullValidationError("The destination repository is no longer registered")
    if not isinstance(source_repository, dict):
        raise SyntheticFullValidationError("The repository holding the backup chain is no longer registered")
    for candidate in (repository, source_repository):
        if not is_abr_repository(candidate):
            raise SyntheticFullValidationError(
                f"Synthetic full backups need a local or s3 repository; '{candidate.get('type')}' cannot merge a chain"
            )

    points = resolve_source_chain(job_id, vm_id, source_repository_id or None)
    validate_chain(points)
    merges = build_disk_merges(points)

    vm_name = str(points[-1].get("vm_name") or parameters.get("vm_name") or vm_id)
    _destination_root, task_root, destination_path = _resolve_backup_storage_layout(
        task=task, vm_name=vm_name, parameters=parameters, repository=repository
    )
    if not is_s3_repository(repository):
        ensure_private_directory(task_root.parent)
    source_paths = {merge["source_path"] for merge in merges}
    if repository_id == source_repository_id and destination_path is not None and destination_path in source_paths:
        raise SyntheticFullValidationError(
            "The synthetic full would be written where the chain it reads from is stored"
        )

    compression_level = _resolve_compression_level(parameters)
    _encryption_enabled, encryption_key = _resolve_encryption_settings(parameters)
    timeout_seconds = _resolve_tool_timeout_seconds(parameters)
    job = JOBS.get(job_id) if job_id else None
    retention_days = None
    generation: dict[str, Any] | None = None
    if is_s3_repository(repository):
        immutability = extract_s3_config(repository).get("immutability") or {}
        started_at = datetime.now(timezone.utc)
        computed = repository_actual_retention_days(job if isinstance(job, dict) else None, immutability)
        retention_days = computed or None
        if retention_days:
            generation = object_lock_generation_plan(
                f"backup::{job_id or '-'}::{vm_id or '-'}::{repository_id or '-'}",
                actual_retention_days=int(retention_days),
                block_generation_days=s3_block_generation_days(),
                now=started_at,
                starts_segment=True,
                own_expiry=object_lock_expiry(
                    job if isinstance(job, dict) else None,
                    immutability,
                    block_generation_days=s3_block_generation_days(),
                    now=started_at,
                ),
            )
            retention_days = int(generation["lock_days"])

    total = len(merges)
    disk_outputs: list[dict[str, Any]] = []
    written_names: list[str] = []
    logical_total = 0
    stored_total = 0

    def _emit(progress: int, message: str) -> None:
        if progress_callback is not None:
            progress_callback({"progress": max(0, min(100, progress)), "message": message})

    target_name = str(repository.get("name") or repository_id)
    source_name = str(source_repository.get("name") or source_repository_id)
    where = (
        f"in '{target_name}'"
        if source_name == target_name
        else f"from '{source_name}' into '{target_name}'"
    )
    source_clause = "" if source_name == target_name else f" from '{source_name}'"
    _emit(
        0,
        f"Merging {len(points)} restore point(s){source_clause} "
        f"into a synthetic full in '{target_name}'",
    )

    for position, merge in enumerate(merges):
        template = merge["template"]
        backup_name = str(template.get("abr_backup_name") or "")
        base = int(position * 100 / total)
        span = max(int(100 / total), 1)
        _emit(base, f"Merging disk {position + 1}/{total}")

        def _on_progress(percent: int, _block: int, base=base, span=span, position=position) -> None:
            _emit(base + int(percent * span / 100), f"Merging disk {position + 1}/{total}: {percent}%")

        try:
            result = copy_disk_tool_chain(
                source_repository=source_repository,
                source_abr_path=merge["source_path"],
                source_names=merge["source_names"],
                destination_repository=repository,
                destination_abr_path=destination_path,
                backup_name=backup_name,
                compression_level=compression_level,
                encryption_key=encryption_key,
                source_encryption_key=encryption_key,
                retention_days=retention_days,
                timeout=timeout_seconds,
                on_progress=_on_progress,
                should_cancel=lambda: cancel_requested(task_id),
            )
        except ProcessCancelled as exc:
            from python.srv.core.task_executor import TaskStopRequested

            _discard_partial_merge(
                repository, destination_path, written_names, task_root, in_flight=backup_name
            )
            raise TaskStopRequested("Synthetic full cancelled; the merged backup was discarded") from exc
        except StorageToolError as exc:
            _discard_partial_merge(
                repository, destination_path, written_names, task_root, in_flight=backup_name
            )
            raise SyntheticFullError(f"Merging disk {position + 1}/{total} failed: {exc}") from exc

        written_names.append(backup_name)

        logical = int(result.get("raw_data_size") or 0)
        stored = int(result.get("stored_data_size") or 0)
        logical_total += logical
        stored_total += stored
        entry = dict(template)
        entry.update(
            {
                "abr_path": destination_path,
                "abr_backup_name": backup_name,
                "logical_bytes": logical,
                "stored_bytes": stored,
                "compression_ratio": (round(stored / logical, 6) if logical else None),
                "store_mode": "synthetic-full",
                "merged_from": result.get("merged_from") or len(merge["source_names"]),
                "repository_id": repository_id,
                "repository_type": str(repository.get("type") or "local"),
                "storage_mode": "s3" if is_s3_repository(repository) else "local",
                "destination_path": "",
                "parent_path": "",
                "change_id": _merged_change_id(merge["newest"]),
                "previous_change_id": "",
                "utility_change_id": "",
            }
        )
        disk_outputs.append(entry)

    repository_type = str(repository.get("type") or "local")
    if repository_type != "s3" and stored_total <= 0:
        stored_total = _dir_size_bytes(task_root)
    manifest_payload = _source_manifest(points[-1])
    if not manifest_payload:
        _discard_partial_merge(repository, destination_path, written_names, task_root)
        raise SyntheticFullError(
            f"The manifest of restore point {points[-1].get('id')} could not be read, so the merged backup "
            "would not be restorable"
        )
    manifest_payload.update(
        {
            "operation": "vm-backup",
            "backup_mode": "full",
            "resolved_backup_type": "SyntheticFull",
            "resolved_backup_reason": str(parameters.get("resolved_backup_reason") or "SyntheticFullWindow"),
            "incremental": False,
            "differential": False,
            "synthetic_full": True,
            "merged_from": len(points),
            "merged_restore_point_ids": [str(point.get("id") or "") for point in points],
            "snapshot_name": "",
            "snapshot_moref": "",
            "disks": disk_outputs,
            "compression_level": compression_level,
            "encryption_enabled": bool(encryption_key),
            "repository_id": repository_id,
            "repository_type": repository_type,
            "storage_mode": "s3" if repository_type == "s3" else "local",
            "repository_storage_path": destination_path if repository_type == "s3" else str(task_root),
            "backup_root": str(task_root),
            "logical_bytes": logical_total,
            "transferred_bytes": stored_total,
            "created_at": utc_now(),
        }
    )
    manifest_path = _write_synthetic_manifest(
        repository, task_root, destination_path, manifest_payload, retention_days=retention_days
    )
    _emit(
        100,
        f"Synthetic full written to '{target_name}' from {len(points)} restore point(s): "
        f"{logical_total} logical / {stored_total} stored byte(s)",
    )
    logger.info(
        "Synthetic full for task %s merged %d restore point(s) %s across %d disk(s), "
        "%d logical / %d stored byte(s), in %.2f seconds",
        task_id,
        len(points),
        where,
        total,
        logical_total,
        stored_total,
        time.time() - start_time,
    )
    return {
        "operation": "backup",
        "worker_mode": "synthetic-full",
        "hypervisor_kind": str(task.get("hypervisor_kind") or "vcenter"),
        "vm_name": vm_name,
        "vm_external_id": vm_id,
        "backup_mode": "full",
        "resolved_backup_type": "SyntheticFull",
        "resolved_backup_reason": str(parameters.get("resolved_backup_reason") or "SyntheticFullWindow"),
        "incremental": False,
        "synthetic_full": True,
        "merged_restore_point_ids": [str(point.get("id") or "") for point in points],
        "merged_from": len(points),
        "source_repository_id": source_repository_id,
        "immutability_generation": generation,
        "backup_root": str(task_root),
        "manifest_path": str(manifest_path),
        "vm_config": manifest_payload.get("vm_config"),
        "repository_id": repository_id,
        "repository_type": repository_type,
        "storage_mode": "s3" if repository_type == "s3" else "local",
        "repository_storage_path": destination_path if repository_type == "s3" else str(task_root),
        "disk_count": total,
        "disks": disk_outputs,
        "compression_level": compression_level,
        "encryption_enabled": bool(encryption_key),
        "logical_bytes": logical_total,
        "transferred_bytes": stored_total,
        "processed_bytes": logical_total,
        "finished_at": utc_now(),
    }
