# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import errno
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from python.srv.core.credential_encryption import (
    is_encrypted,
    unreadable_encryption_key_message,
)
from python.srv.core.mongo_db_wrapper import (
    JOBS,
    REPOSITORIES,
    TASKS,
    catalog_records,
    persist_task,
)
from python.srv.core.time_zones import RENAMED_ZONES
from python.srv.core.storage_tool import (
    StorageToolError,
    chain_retention_till,
    copy_disk_tool_chain,
    ensure_private_directory,
    extract_s3_config,
    is_abr_repository,
    is_s3_repository,
    make_durable,
    s3_block_generation_days,
)
from python.srv.core.subprocess_util import ProcessCancelled
from python.srv.core.utils import utc_now
from python.srv.services.api_errors import api_error
from python.srv.services.backup_mode import (
    object_lock_expiry,
    object_lock_generation_plan,
)
from python.srv.services.share_mount import unmounted_share_message
from python.srv.services.synthetic_full import (
    FULL_EXECUTION_TYPES,
    _discard_partial_merge,
    _free_bytes_at_start,
    _hypervisor_kind,
    _no_room_detail,
    _out_of_space_message,
    _source_manifest,
    _write_synthetic_manifest,
    build_disk_merges,
)

logger = logging.getLogger(__name__)

COPY_MODES = ("periodic", "immediate")
DEFAULT_RETRIES = {"periodic": (5, 0), "immediate": (3, 10)}
MAX_RETRY_COUNT = 20
MAX_RETRY_INTERVAL_MINUTES = 24 * 60
AVAILABLE_STATUSES = frozenset({"available", "completed", "healthy"})
FINISHED_STATUSES = frozenset({"completed", "failed", "stopped", "canceled", "cancelled"})
WINDOW_TIME = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
WINDOW_STOP_REASON = "copy_window"


class BackupCopyError(RuntimeError):
    pass


class BackupCopyValidationError(BackupCopyError):
    pass


def is_backup_copy_task(task: dict[str, Any]) -> bool:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    return str(task.get("task_type") or "") == "backup" and bool(parameters.get("copy_job")) and not bool(
        parameters.get("synthetic_full")
    )


def retry_settings(job: dict[str, Any]) -> tuple[int, int]:
    mode = str(job.get("copy_mode") or "periodic")
    default_count, default_interval = DEFAULT_RETRIES.get(mode, DEFAULT_RETRIES["periodic"])
    count = job.get("retry_count")
    interval = job.get("retry_interval_minutes")
    return (
        default_count if count is None else int(count),
        default_interval if interval is None else int(interval),
    )


def source_job_ids(job: dict[str, Any]) -> list[str]:
    return [str(value) for value in job.get("source_job_ids") or [] if str(value or "").strip()]


def source_repository_ids(job: dict[str, Any]) -> list[str]:
    return [str(value) for value in job.get("source_repository_ids") or [] if str(value or "").strip()]


def _available(point: dict[str, Any]) -> bool:
    return str(point.get("status") or "") in AVAILABLE_STATUSES


def _point_order(point: dict[str, Any]) -> tuple[str, str]:
    return (str(point.get("created_at") or ""), str(point.get("id") or ""))


def _copy_source(point: dict[str, Any]) -> dict[str, Any]:
    value = point.get("copy_source")
    return value if isinstance(value, dict) else {}


def source_points(job: dict[str, Any]) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import normalized_backup_points

    copy_job_id = str(job.get("id") or "")
    jobs = set(source_job_ids(job))
    repositories = set(source_repository_ids(job))
    wanted_vms = {str(value) for value in job.get("vm_ids") or [] if str(value or "").strip()}

    selected = []
    for point in normalized_backup_points():
        point_job_id = str(point.get("job_id") or "")
        if not point_job_id or point_job_id == copy_job_id or not _available(point):
            continue
        if point_job_id not in jobs and str(point.get("repository_id") or "") not in repositories:
            continue
        if wanted_vms and str(point.get("vm_id") or "") not in wanted_vms:
            continue
        selected.append(point)

    selected.sort(key=_point_order)
    return selected


def copy_targets(job: dict[str, Any]) -> list[dict[str, str]]:
    targets: dict[str, dict[str, str]] = {}
    for point in source_points(job):
        vm_id = str(point.get("vm_id") or "")
        targets[vm_id] = {
            "vm_id": vm_id,
            "vm_name": str(point.get("vm_name") or vm_id),
            "inventory_id": str(point.get("inventory_id") or ""),
        }
    return sorted(targets.values(), key=lambda item: item["vm_name"].lower())


def destination_points(job: dict[str, Any], vm_id: str) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import _points_for_job_vm_repo

    points = _points_for_job_vm_repo(str(job.get("id") or ""), vm_id, str(job.get("repo_id") or "") or None)
    return [point for point in points if _available(point)]


def _segments(points: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    from python.srv.services.abr_v41 import _segment_points

    return _segment_points(points)


def _source_chain(job: dict[str, Any], vm_id: str, previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for point in source_points(job):
        if str(point.get("vm_id") or "") == vm_id:
            by_source.setdefault(f"{point.get('job_id')}::{point.get('repository_id')}", []).append(point)
    if not by_source:
        return []

    copied = _copy_source(previous or {})
    kept = by_source.get(f"{copied.get('job_id')}::{copied.get('repository_id')}") if copied else None
    if kept:
        return kept

    order = {job_id: position for position, job_id in enumerate(source_job_ids(job))}
    first_rank = min(order.get(str(points[-1].get("job_id") or ""), len(order)) for points in by_source.values())
    candidates = [
        points for points in by_source.values() if order.get(str(points[-1].get("job_id") or ""), len(order)) == first_rank
    ]
    return max(candidates, key=lambda points: _point_order(points[-1]))


def plan_copy(job: dict[str, Any], vm_id: str, *, requested_type: str = "Incremental") -> dict[str, Any] | None:
    previous_points = destination_points(job, vm_id)
    previous = previous_points[-1] if previous_points else None
    chain = _source_chain(job, vm_id, previous)
    if not chain:
        return None

    full_requested = requested_type in FULL_EXECUTION_TYPES
    copied_id = str(_copy_source(previous or {}).get("point_id") or "")
    position = {str(point.get("id") or ""): index for index, point in enumerate(chain)}
    if copied_id in position:
        pending = chain[position[copied_id] + 1 :]
    elif copied_id:
        copied_at = _point_order(_copy_source_point_stub(previous))
        pending = [point for point in chain if _point_order(point) > copied_at]
    else:
        pending = list(chain)

    if not pending:
        if not full_requested:
            return None
        pending = [chain[-1]]

    immediate = str(job.get("copy_mode") or "periodic") == "immediate"
    target = pending[0] if immediate and previous is not None and not full_requested else pending[-1]
    segment = next(points for points in _segments(chain) if any(point.get("id") == target.get("id") for point in points))
    target_index = next(index for index, point in enumerate(segment) if point.get("id") == target.get("id"))

    continues = (
        previous is not None
        and not full_requested
        and str(target.get("execution_type") or "") == "Incremental"
        and any(point.get("id") == copied_id for point in segment[:target_index])
    )
    if continues:
        start = next(index for index, point in enumerate(segment) if point.get("id") == copied_id) + 1
        points = segment[start : target_index + 1]
    else:
        points = segment[: target_index + 1]
        if str(points[0].get("execution_type") or "") not in FULL_EXECUTION_TYPES:
            return None

    return {
        "kind": "incremental" if continues else "full",
        "points": points,
        "target": target,
        "parent": previous if continues else None,
        "remaining": len(pending) - pending.index(target) - 1 if target in pending else 0,
    }


def _copy_source_point_stub(point: dict[str, Any] | None) -> dict[str, Any]:
    copied = _copy_source(point or {})
    return {"created_at": copied.get("created_at") or "", "id": copied.get("point_id") or ""}


def pending_copy(job: dict[str, Any], vm_id: str) -> bool:
    try:
        return plan_copy(job, vm_id) is not None
    except Exception:
        logger.exception("Could not work out what backup copy job %s has left to copy for VM %s", job.get("id"), vm_id)
        return False


def _session_open(job_id: str, vm_id: str, *, statuses: frozenset[str]) -> bool:
    return any(
        str(task.get("job_id") or "") == job_id
        and str(task.get("resource_external_id") or "") == vm_id
        and str(task.get("status") or "") in statuses
        for task in catalog_records(TASKS)
    )


def create_copy_sessions(
    job: dict[str, Any],
    *,
    trigger: str,
    run_id: str | None = None,
    vm_ids: list[str] | None = None,
    force_full: bool = False,
    force_synthetic: bool = False,
    scheduled_type: str | None = None,
    include_idle: bool = False,
) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import (
        _append_task_log,
        execution_phases,
        normalize_job_record,
        resolve_job_execution,
    )

    normalized = normalize_job_record(job)
    job_id = str(normalized.get("id") or "")
    wanted = {str(value) for value in vm_ids} if vm_ids else None
    run_id = run_id or str(uuid4())
    destination = REPOSITORIES.get(str(normalized.get("repo_id") or "")) or {}
    sessions: list[dict[str, Any]] = []

    for target in copy_targets(normalized):
        vm_id = target["vm_id"]
        if wanted is not None and vm_id not in wanted:
            continue
        if _session_open(job_id, vm_id, statuses=frozenset({"queued"})):
            logger.info("Backup copy job %s already has a session queued for VM %s; not adding another", job_id, vm_id)
            continue

        execution_type, execution_reason = resolve_job_execution(
            normalized,
            vm_id=vm_id,
            force_full=force_full,
            force_synthetic=force_synthetic,
            scheduled_type=scheduled_type,
        )
        if not include_idle and execution_type == "Incremental" and plan_copy(normalized, vm_id) is None:
            continue

        requires_task_id = None
        for phase_type, phase_reason in execution_phases(execution_type, execution_reason):
            synthetic = phase_type == "SyntheticFull"
            parameters = dict(normalized.get("task_parameters") or {})
            parameters.update(
                {
                    "copy_job": not synthetic,
                    "copy_mode": normalized.get("copy_mode"),
                    "repository_id": normalized.get("repo_id"),
                    "repository_name": normalized.get("repo_name"),
                    "vm_name": target["vm_name"],
                    "resolved_backup_type": phase_type,
                    "resolved_backup_reason": phase_reason,
                    "backup_mode_request": "full" if phase_type in FULL_EXECUTION_TYPES else "auto",
                    "incremental": phase_type == "Incremental",
                    "synthetic_full": synthetic,
                    "active_full": phase_type == "ActiveFull",
                }
            )
            if requires_task_id:
                parameters["requires_task_id"] = requires_task_id

            now = utc_now()
            task = {
                "id": str(uuid4()),
                "job_id": job_id,
                "task_type": "backup",
                "hypervisor_id": target["inventory_id"],
                "resource_type": "virtual_machine",
                "resource_external_id": vm_id,
                "status": "queued",
                "trigger": trigger,
                "run_id": run_id,
                "parameters": parameters,
                "result": {"progress": 0, "resolved_backup_type": phase_type, "resolved_backup_reason": phase_reason},
                "created_at": now,
                "updated_at": now,
                "logs": [],
            }

            what = "synthetic full of the copied chain" if synthetic else "backup copy"
            window = copy_window_state(normalized)
            if not window["open"]:
                _append_task_log(
                    task,
                    level="info",
                    message=(
                        f"Waits for the copy window ({describe_copy_window(normalized.get('copy_window') or [])}): "
                        f"it opens {_local_text(window['opens_at'], normalized)}"
                    ),
                    component="session",
                )
            _append_task_log(
                task,
                level="info",
                message=(
                    f"Job {normalized.get('name')}: {what} of {target['vm_name']} into repository "
                    f"{destination.get('name') or normalized.get('repo_name')} ({phase_type}, {phase_reason})"
                ),
                component="session",
            )
            persist_task(task)
            sessions.append(task)
            requires_task_id = task["id"]

    if sessions:
        logger.info(
            "Backup copy job %s (%s) queued %d session(s), trigger %s",
            normalized.get("name"),
            job_id,
            len(sessions),
            trigger,
        )
    return sessions


def _run_finished(task: dict[str, Any]) -> bool:
    run_id = str(task.get("run_id") or "")
    if not run_id:
        return True
    return all(
        str(item.get("status") or "") in FINISHED_STATUSES
        for item in catalog_records(TASKS)
        if str(item.get("run_id") or "") == run_id and str(item.get("job_id") or "") == str(task.get("job_id") or "")
    )


def _copy_jobs() -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import normalize_job_record

    jobs = [normalize_job_record(job) for job in catalog_records(JOBS)]
    return [job for job in jobs if job.get("job_type") == "backup-copy" and job.get("enabled")]


def _start_and_record(job: dict[str, Any], *, trigger: str, vm_ids: list[str] | None = None) -> list[dict[str, Any]]:
    from python.srv.core.mongo_db_wrapper import persist_job
    from python.srv.services.abr_v41 import job_run_record

    run_id = str(uuid4())
    sessions = create_copy_sessions(job, trigger=trigger, run_id=run_id, vm_ids=vm_ids)

    stored = JOBS.get(str(job.get("id") or ""))
    if sessions and isinstance(stored, dict):
        stored = dict(stored)
        stored["last_run_at"] = sessions[0]["created_at"]
        stored["last_session_id"] = sessions[-1]["id"]
        stored["last_task_id"] = sessions[-1]["id"]
        stored["last_run"] = job_run_record(
            run_id, trigger=trigger, started_at=sessions[0]["created_at"], session_ids=[task["id"] for task in sessions]
        )
        stored["updated_at"] = utc_now()
        persist_job(stored)
    return sessions


def on_session_finished(task: dict[str, Any]) -> None:
    if str(task.get("task_type") or "") != "backup":
        return
    job_id = str(task.get("job_id") or "")
    status = str(task.get("status") or "")
    vm_id = str(task.get("resource_external_id") or "")
    finished_job = JOBS.get(job_id) or {}

    if is_backup_copy_task(task) and status == "completed" and vm_id:
        from python.srv.services.abr_v41 import normalize_job_record

        copy_job = normalize_job_record(finished_job) if finished_job else {}
        if copy_job.get("enabled") and copy_job.get("copy_mode") == "immediate" and pending_copy(copy_job, vm_id):
            _start_and_record(copy_job, trigger="copy-backlog", vm_ids=[vm_id])
        return
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    if is_backup_copy_task(task) and status == "stopped" and result.get("stop_reason") == WINDOW_STOP_REASON:
        from python.srv.services.abr_v41 import normalize_job_record

        copy_job = normalize_job_record(finished_job) if finished_job else {}
        if copy_job and vm_id:
            _start_and_record(copy_job, trigger="copy-window", vm_ids=[vm_id])
        return
    if is_backup_copy_task(task):
        return

    for copy_job in _copy_jobs():
        if str(copy_job.get("id") or "") == job_id:
            continue
        sources_this = job_id in source_job_ids(copy_job) or str(result.get("repository_id") or "") in source_repository_ids(copy_job)
        if (
            copy_job.get("copy_mode") == "immediate"
            and sources_this
            and status == "completed"
            and result.get("restore_point_id")
            and vm_id
        ):
            _start_and_record(copy_job, trigger="immediate", vm_ids=[vm_id])
        elif (
            copy_job.get("copy_mode") == "periodic"
            and str(copy_job.get("run_after_job_id") or "") == job_id
            and _run_finished(task)
        ):
            _start_and_record(copy_job, trigger="after-job")


def _source_encryption_key(points: list[dict[str, Any]]) -> str:
    from python.srv.services.abr_v41 import _resolve_restore_encryption_key

    keys: list[str] = []
    for point in points:
        if not point.get("encryption_enabled"):
            keys.append("")
            continue
        key = str(_resolve_restore_encryption_key(point) or "")
        if is_encrypted(key):
            raise BackupCopyValidationError(unreadable_encryption_key_message(f"of restore point {point.get('id')}"))
        if not key:
            raise BackupCopyValidationError(
                f"Restore point {point.get('id')} is encrypted and no key is stored for it, so it cannot be copied"
            )
        keys.append(key)

    if len(set(keys)) > 1:
        raise BackupCopyValidationError(
            f"The restore points {', '.join(str(point.get('id')) for point in points)} of one chain are not all "
            "encrypted with the same key, so they cannot be read as one chain; run an active full backup of the "
            "source job to start a chain under one key"
        )
    return keys[0] if keys else ""


def _disk_entry(merge: dict[str, Any], result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    template = merge["template"]
    first = merge.get("first") or {}
    logical = int(result.get("raw_data_size") or 0)
    stored = int(result.get("stored_data_size") or 0)
    entry = dict(template)
    entry.update(
        {
            "logical_bytes": logical,
            "stored_bytes": stored,
            "compression_ratio": (round(stored / logical, 6) if logical else None),
            "store_mode": "full" if merge.get("kind") == "full" else "incremental",
            "copied": True,
            "merged_from": len(merge["source_names"]),
            "destination_path": "",
            "parent_path": "",
            "change_id": str(merge["newest"].get("change_id") or merge["newest"].get("next_change_id") or "").strip(),
            "previous_change_id": str(first.get("previous_change_id") or "").strip(),
            "utility_change_id": "",
        }
    )
    entry.update(extra)
    return entry


def _copy_once(
    task: dict[str, Any],
    job: dict[str, Any],
    plan: dict[str, Any],
    emit: Callable[[int, str], None],
) -> dict[str, Any]:
    from python.srv.core.task_executor import TaskStopRequested, cancel_requested
    from python.srv.plugins.vcenter.vcenter_backup import (
        _dir_size_bytes,
        _job_storage_layout,
        _resolve_backup_storage_layout,
        _resolve_compression_level,
        _resolve_encryption_settings,
        _resolve_tool_timeout_seconds,
    )
    from python.srv.services.abr_v41 import repository_actual_retention_days

    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    vm_id = str(task.get("resource_external_id") or "")
    points = plan["points"]
    target = plan["target"]
    kind = plan["kind"]
    repository_id = str(job.get("repo_id") or "")
    repository = REPOSITORIES.get(repository_id)
    source_repository_id = str(target.get("repository_id") or "")
    source_repository = REPOSITORIES.get(source_repository_id)

    if not isinstance(repository, dict):
        raise BackupCopyValidationError("The destination repository is no longer registered")
    if not isinstance(source_repository, dict):
        raise BackupCopyValidationError(
            f"Repository {target.get('repository_name') or source_repository_id} holding the restore point to copy is no longer registered"
        )
    if repository_id == source_repository_id:
        raise BackupCopyValidationError("The destination repository is the one the restore points are copied from")
    for candidate in (repository, source_repository):
        if not is_abr_repository(candidate):
            raise BackupCopyValidationError(
                f"Repository {candidate.get('name')} is of type '{candidate.get('type')}', which backup copy cannot read or write"
            )
        unmounted = unmounted_share_message(candidate)
        if unmounted:
            raise BackupCopyValidationError(unmounted)

    merges = build_disk_merges(points)
    for merge in merges:
        merge["kind"] = kind
        first_identity = merge["identity"]
        merge["first"] = next(
            (
                disk
                for position, disk in enumerate((points[0].get("artifact") or {}).get("disks") or [])
                if isinstance(disk, dict) and _identity_of(disk, position) == first_identity
            ),
            {},
        )

    _encryption_enabled, encryption_key = _resolve_encryption_settings(parameters)
    source_key = _source_encryption_key(points)
    if source_key and not encryption_key:
        raise BackupCopyValidationError(
            f"The restore points of {target.get('vm_name') or vm_id} are encrypted and backup copy job {job.get('name')} "
            "has no encryption key of its own; give it one so the copies stay encrypted"
        )

    compression_level = _resolve_compression_level(parameters)
    timeout_seconds = _resolve_tool_timeout_seconds(parameters)

    vm_name = str(target.get("vm_name") or parameters.get("vm_name") or vm_id)
    storage_layout = _job_storage_layout(task, starts_new_chain=kind == "full")
    destination_root, task_root, destination_path = _resolve_backup_storage_layout(
        task=task,
        vm_name=vm_name,
        parameters=parameters,
        repository=repository,
        vm_external_id=vm_id,
        layout=storage_layout,
    )

    free_at_start = _free_bytes_at_start(repository)
    if not is_s3_repository(repository):
        try:
            ensure_private_directory(task_root.parent)
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            raise BackupCopyError(_out_of_space_message(repository, free_at_start)) from exc

    retention_days = None
    retention_till = None
    generation: dict[str, Any] | None = None
    if is_s3_repository(repository):
        immutability = extract_s3_config(repository).get("immutability") or {}
        started_at = datetime.now(timezone.utc)
        computed = repository_actual_retention_days(job, immutability)
        retention_days = computed or None
        if retention_days:
            generation = object_lock_generation_plan(
                f"backup::{job.get('id') or '-'}::{vm_id or '-'}::{repository_id or '-'}",
                actual_retention_days=int(retention_days),
                block_generation_days=s3_block_generation_days(),
                now=started_at,
                starts_segment=kind == "full",
                own_expiry=object_lock_expiry(
                    job, immutability, block_generation_days=s3_block_generation_days(), now=started_at
                ),
            )
            retention_days = int(generation["lock_days"])
            retention_till = chain_retention_till(repository, generation.get("expires_at"))

    total = len(merges)
    written: list[str] = []
    disk_outputs: list[dict[str, Any]] = []
    logical_total = 0
    stored_total = 0
    for position, merge in enumerate(merges):
        backup_name = str(merge["template"].get("abr_backup_name") or "")
        base = int(position * 100 / total)
        span = max(int(100 / total), 1)
        emit(base, f"Copying disk {position + 1}/{total}")

        def _on_progress(percent: int, _block: int, base=base, span=span, position=position) -> None:
            emit(base + int(percent * span / 100), f"Copying disk {position + 1}/{total}: {percent}%")

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
                source_encryption_key=source_key,
                retention_days=retention_days,
                retention_till=retention_till,
                timeout=timeout_seconds,
                on_progress=_on_progress,
                should_cancel=lambda: cancel_requested(task_id) or _window_closed(job),
            )
        except ProcessCancelled as exc:
            _discard_partial_merge(repository, destination_path, written, task_root, in_flight=backup_name, below=destination_root)
            if not cancel_requested(task_id) and _window_closed(job):
                raise _stop_for_window(job, "while it copied; what it had written was discarded") from exc
            raise TaskStopRequested("Backup copy cancelled; what it had written was discarded") from exc
        except StorageToolError as exc:
            _discard_partial_merge(repository, destination_path, written, task_root, in_flight=backup_name, below=destination_root)
            detail = _no_room_detail(repository, points, free_at_start)
            raise BackupCopyError(f"Copying disk {position + 1}/{total} failed: {detail or exc}") from exc
        except OSError as exc:
            _discard_partial_merge(repository, destination_path, written, task_root, in_flight=backup_name, below=destination_root)
            if exc.errno == errno.ENOSPC:
                raise BackupCopyError(
                    f"Copying disk {position + 1}/{total} failed: "
                    + _out_of_space_message(repository, _free_bytes_at_start(repository))
                ) from exc
            raise BackupCopyError(f"Copying disk {position + 1}/{total} failed: {exc}") from exc
        written.append(backup_name)

        entry = _disk_entry(
            merge,
            result,
            abr_path=destination_path,
            abr_backup_name=backup_name,
            repository_id=repository_id,
            repository_type=str(repository.get("type") or "local"),
            storage_mode="s3" if is_s3_repository(repository) else "local",
        )
        logical_total += int(entry.get("logical_bytes") or 0)
        stored_total += int(entry.get("stored_bytes") or 0)
        disk_outputs.append(entry)

    repository_type = str(repository.get("type") or "local")
    if repository_type != "s3" and stored_total <= 0:
        stored_total = _dir_size_bytes(task_root)

    manifest_payload = _source_manifest(target)
    if not manifest_payload:
        _discard_partial_merge(repository, destination_path, written, task_root, below=destination_root)
        raise BackupCopyValidationError(
            f"The manifest of restore point {target.get('id')} could not be read, so its copy would not be restorable"
        )

    execution_type = "ActiveFull" if kind == "full" else "Incremental"
    requested_type = str(parameters.get("resolved_backup_type") or "Incremental")
    execution_reason = (
        "SourceNewChain"
        if kind == "full" and requested_type not in FULL_EXECUTION_TYPES
        else str(parameters.get("resolved_backup_reason") or "BackupCopy")
    )

    parent = plan.get("parent") or {}
    copy_source = {
        "job_id": target.get("job_id"),
        "job_name": target.get("job_name"),
        "repository_id": source_repository_id,
        "repository_name": source_repository.get("name"),
        "point_id": target.get("id"),
        "point_ids": [str(point.get("id") or "") for point in points],
        "execution_types": [str(point.get("execution_type") or "") for point in points],
        "created_at": target.get("created_at"),
    }

    manifest_payload.update(
        {
            "operation": "vm-backup",
            "backup_mode": "full" if kind == "full" else "incremental",
            "resolved_backup_type": execution_type,
            "resolved_backup_reason": execution_reason,
            "parent_point_id": parent.get("id"),
            "storage_layout": storage_layout,
            "incremental": kind != "full",
            "differential": False,
            "synthetic_full": False,
            "copy_source": copy_source,
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
        repository, task_root, destination_path, manifest_payload, retention_days=retention_days, retention_till=retention_till
    )
    try:
        make_durable(task_root, up_to=destination_root)
    except OSError as exc:
        _discard_partial_merge(repository, destination_path, written, task_root, below=destination_root)
        raise BackupCopyError(
            f"The copy was written but could not be flushed to stable storage in {task_root}: {exc}. "
            "It is not kept, so a crash cannot leave a restore point with empty files."
        ) from exc

    return {
        "operation": "backup",
        "worker_mode": "backup-copy",
        "hypervisor_kind": _hypervisor_kind(task, manifest_payload),
        "vm_name": vm_name,
        "vm_external_id": vm_id,
        "backup_mode": "full" if kind == "full" else "incremental",
        "resolved_backup_type": execution_type,
        "resolved_backup_reason": execution_reason,
        "parent_point_id": parent.get("id"),
        "storage_layout": storage_layout,
        "incremental": kind != "full",
        "synthetic_full": False,
        "copy_source": copy_source,
        "copied_restore_point_ids": copy_source["point_ids"],
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
        "consistency": manifest_payload.get("consistency"),
        "consistency_reason": manifest_payload.get("consistency_reason"),
        "logical_bytes": logical_total,
        "transferred_bytes": stored_total,
        "processed_bytes": logical_total,
    }


def _identity_of(disk: dict[str, Any], position: int) -> str:
    from python.srv.services.synthetic_full import _disk_identity

    return _disk_identity(disk, position)


def _wait_before_retry(task_id: str, minutes: int, job: dict[str, Any]) -> None:
    from python.srv.core.task_executor import TaskStopRequested, cancel_requested

    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if cancel_requested(task_id):
            raise TaskStopRequested("Backup copy cancelled while it waited to retry")
        if _window_closed(job):
            raise _stop_for_window(job, "while the copy waited to retry")
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def run_backup_copy(
    task: dict[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from python.srv.core.task_executor import TaskStopRequested, cancel_requested
    from python.srv.services.abr_v41 import normalize_job_record

    started = time.time()
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    vm_id = str(task.get("resource_external_id") or "")
    stored_job = JOBS.get(str(task.get("job_id") or ""))
    if not isinstance(stored_job, dict):
        raise BackupCopyValidationError("The backup copy job this session belongs to no longer exists")

    job = normalize_job_record(stored_job)
    destination = REPOSITORIES.get(str(job.get("repo_id") or "")) or {}
    retries, interval = retry_settings(job)
    vm_name = str(parameters.get("vm_name") or vm_id)

    def emit(progress: int, message: str, **extra: Any) -> None:
        if progress_callback is not None:
            progress_callback({"progress": max(0, min(100, progress)), "message": message, **extra})

    plan = plan_copy(job, vm_id, requested_type=str(parameters.get("resolved_backup_type") or "Incremental"))
    if plan is None:
        return {
            "operation": "backup",
            "worker_mode": "backup-copy",
            "nothing_copied": True,
            "message": f"Nothing new to copy for {vm_name}: its newest restore point is already in {destination.get('name')}",
            "vm_name": vm_name,
            "vm_external_id": vm_id,
        }

    if _window_closed(job):
        raise _stop_for_window(job, "before the copy could start")

    target = plan["target"]
    emit(
        0,
        f"Copying {vm_name}: {len(plan['points'])} restore point(s) of job {target.get('job_name')} "
        f"from repository {target.get('repository_name')} into repository {destination.get('name')} "
        f"as {'a full backup' if plan['kind'] == 'full' else 'an incremental backup'}",
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            result = _copy_once(task, job, plan, emit)
            break
        except (TaskStopRequested, BackupCopyValidationError):
            raise
        except BackupCopyError as exc:
            if attempt > retries or cancel_requested(task_id):
                raise
            wait = interval
            emit(
                0,
                f"Attempt {attempt}/{retries + 1} to copy {vm_name} failed: {exc}. "
                + (f"Retrying in {wait} minute(s)" if wait else "Retrying now"),
                log_level="warn",
            )
            if wait:
                _wait_before_retry(task_id, wait, job)

    duration = time.time() - started
    message = (
        f"Copied {vm_name}: restore point {target.get('id')} of job {target.get('job_name')} from repository "
        f"{target.get('repository_name')} into repository {destination.get('name')} as "
        f"{'a full backup' if plan['kind'] == 'full' else 'an incremental backup'}, "
        f"{result['logical_bytes']} logical / {result['transferred_bytes']} stored byte(s) in {duration:.1f} s"
        + (f" after {attempt} attempt(s)" if attempt > 1 else "")
    )

    logger.info("Backup copy session %s: %s", task_id, message)
    result["message"] = message
    result["copy_attempts"] = attempt
    result["copy_remaining"] = plan["remaining"]
    return result


def _tokens(value: Any) -> list[str]:
    from python.srv.services.abr_v41 import _split_csv

    if value is None:
        return []
    items = value if isinstance(value, list) else _split_csv(str(value))
    return [str(item).strip() for item in items if str(item or "").strip()]


def _resolve_sources(payload: dict[str, Any], current: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from python.srv.services.abr_v41 import (
        normalize_job_record,
        resolve_job,
        resolve_repository,
    )

    jobs_given = any(key in payload for key in ("source_jobs", "source_job_ids"))
    repositories_given = any(key in payload for key in ("source_repositories", "source_repository_ids"))
    if jobs_given:
        jobs = [resolve_job(name=token) for token in _tokens(payload.get("source_jobs"))]
        jobs += [resolve_job(record_id=token) for token in _tokens(payload.get("source_job_ids"))]
    else:
        jobs = [normalize_job_record(JOBS[job_id]) for job_id in source_job_ids(current) if job_id in JOBS]

    if repositories_given:
        repositories = [resolve_repository(name=token) for token in _tokens(payload.get("source_repositories"))]
        repositories += [resolve_repository(record_id=token) for token in _tokens(payload.get("source_repository_ids"))]
    else:
        repositories = [REPOSITORIES[repo_id] for repo_id in source_repository_ids(current) if repo_id in REPOSITORIES]

    unique_jobs = list({str(job["id"]): job for job in jobs}.values())
    unique_repositories = list({str(repo["id"]): repo for repo in repositories}.values())
    return unique_jobs, unique_repositories


def _known_vms(jobs: list[dict[str, Any]], repositories: list[dict[str, Any]]) -> dict[str, str]:
    from python.srv.services.abr_v41 import normalized_backup_points

    known: dict[str, str] = {}
    for job in jobs:
        for vm_id, vm_name in zip(job.get("vm_ids") or [], job.get("vm_names") or []):
            known[str(vm_id)] = str(vm_name)

    job_ids = {str(job["id"]) for job in jobs}
    repository_ids = {str(repo["id"]) for repo in repositories}
    for point in normalized_backup_points():
        if str(point.get("job_id") or "") in job_ids or str(point.get("repository_id") or "") in repository_ids:
            known.setdefault(str(point.get("vm_id") or ""), str(point.get("vm_name") or point.get("vm_id") or ""))
    known.pop("", None)
    return known


def _resolve_vms(tokens: list[str], known: dict[str, str]) -> tuple[list[str], list[str]]:
    from python.srv.services.abr_v41 import _ensure

    by_name: dict[str, list[str]] = {}
    for vm_id, vm_name in known.items():
        by_name.setdefault(vm_name.lower(), []).append(vm_id)

    vm_ids: list[str] = []
    for token in tokens:
        if token in known:
            vm_ids.append(token)
            continue
        matches = by_name.get(token.lower(), [])
        _ensure(
            len(matches) == 1,
            422,
            "invalid_job",
            (
                f"VM '{token}' is not backed up by the source jobs or repositories of this backup copy job"
                if not matches
                else f"VM name '{token}' matches several VMs of the sources; give its ID instead"
            ),
            vm=token,
        )
        vm_ids.append(matches[0])

    vm_ids = list(dict.fromkeys(vm_ids))
    return vm_ids, [known[vm_id] for vm_id in vm_ids]


def _validate_retries(payload: dict[str, Any], current: dict[str, Any]) -> tuple[int | None, int | None]:
    from python.srv.services.abr_v41 import _ensure

    values = []
    for key, upper in (("retry_count", MAX_RETRY_COUNT), ("retry_interval_minutes", MAX_RETRY_INTERVAL_MINUTES)):
        raw = payload.get(key, current.get(key))
        if raw is None or raw == "":
            values.append(None)
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = -1
        _ensure(
            0 <= number <= upper and not isinstance(raw, bool) and str(raw).strip().lstrip("+").isdigit(),
            422,
            "invalid_job",
            f"{key.replace('_', '-')} must be a whole number from 0 to {upper}",
            **{key: raw},
        )
        values.append(number)
    return values[0], values[1]


def _hyperv_sources(jobs: list[dict[str, Any]], repositories: list[dict[str, Any]], vm_ids: list[str]) -> bool:
    from python.srv.services.abr_v41 import (
        _inventory_is_hyperv,
        normalized_backup_points,
    )

    if any(_inventory_is_hyperv(job.get("inventory_id")) for job in jobs):
        return True

    repository_ids = {str(repo["id"]) for repo in repositories}
    wanted = set(vm_ids)
    return any(
        str(point.get("repository_id") or "") in repository_ids
        and (not wanted or str(point.get("vm_id") or "") in wanted)
        and str(point.get("hypervisor_kind") or "").lower().replace("_", "-") in {"hyperv", "hyper-v"}
        for point in normalized_backup_points()
    )


def _encrypted_sources(jobs: list[dict[str, Any]], repositories: list[dict[str, Any]], vm_ids: list[str]) -> bool:
    from python.srv.services.abr_v41 import normalized_backup_points

    if any((job.get("task_parameters") or {}).get("encryption_enabled") for job in jobs):
        return True

    repository_ids = {str(repo["id"]) for repo in repositories}
    wanted = set(vm_ids)
    return any(
        str(point.get("repository_id") or "") in repository_ids
        and (not wanted or str(point.get("vm_id") or "") in wanted)
        and point.get("encryption_enabled")
        for point in normalized_backup_points()
    )


def apply_copy_settings(job: dict[str, Any], payload: dict[str, Any], *, creating: bool) -> bool:
    from python.srv.services.abr_v41 import _ensure, resolve_job, resolve_repository

    current = dict(job)
    payload = dict(payload)
    if payload.get("source_job") is not None:
        payload.setdefault("source_jobs", payload["source_job"])
    if payload.get("source_job_id") is not None:
        payload.setdefault("source_job_ids", payload["source_job_id"])

    changed = False
    if creating or payload.get("copy_mode") is not None:
        mode = str(payload.get("copy_mode") or "periodic").strip().lower()
        _ensure(mode in COPY_MODES, 422, "invalid_job", "copy-mode must be periodic or immediate", copy_mode=payload.get("copy_mode"))
        job["copy_mode"] = mode
        changed = True

    sources_given = any(
        key in payload for key in ("source_jobs", "source_job_ids", "source_repositories", "source_repository_ids")
    )
    jobs, repositories = _resolve_sources(payload if sources_given else {}, current)

    if creating or sources_given:
        _ensure(
            bool(jobs or repositories),
            422,
            "invalid_job",
            "A backup copy job needs at least one source job or source repository",
        )
        for source in jobs:
            _ensure(
                str(source.get("id") or "") != str(job.get("id") or ""),
                422,
                "invalid_job",
                "A backup copy job cannot copy its own restore points",
            )
            _ensure(
                source.get("job_type") in ("backup", "backup-copy"),
                422,
                "invalid_job",
                f"Job {source.get('name')} cannot be a source of a backup copy job",
            )
        job["source_job_ids"] = [str(source["id"]) for source in jobs]
        job["source_job_names"] = [str(source["name"]) for source in jobs]
        job["source_repository_ids"] = [str(repo["id"]) for repo in repositories]
        job["source_repository_names"] = [str(repo["name"]) for repo in repositories]
        changed = True

    if creating or sources_given or payload.get("vms") is not None:
        tokens = _tokens(payload.get("vms")) if payload.get("vms") is not None else list(current.get("vm_ids") or [])
        vm_ids, vm_names = _resolve_vms(tokens, _known_vms(jobs, repositories)) if tokens else ([], [])
        job["vm_ids"] = vm_ids
        job["vm_names"] = vm_names
        changed = changed or payload.get("vms") is not None

    if creating or "repo" in payload or "repo_id" in payload:
        repository = resolve_repository(name=payload.get("repo"), record_id=payload.get("repo_id"))
        job["repo_id"] = repository["id"]
        job["repo_name"] = repository["name"]
        changed = True

    if creating or "run_after_job" in payload or "run_after_job_id" in payload:
        name = payload.get("run_after_job")
        record_id = payload.get("run_after_job_id")
        if name or record_id:
            other = resolve_job(name=name, record_id=record_id)
            _ensure(
                str(other.get("id") or "") != str(job.get("id") or ""),
                422,
                "invalid_job",
                "A backup copy job cannot run after itself",
            )
            job["run_after_job_id"] = str(other["id"])
            job["run_after_job_name"] = str(other["name"])
        else:
            job["run_after_job_id"] = ""
            job["run_after_job_name"] = ""
        changed = True

    if creating or "copy_window" in payload:
        job["copy_window"] = parse_copy_window(payload.get("copy_window"))
        changed = True

    if creating or "retry_count" in payload or "retry_interval_minutes" in payload:
        job["retry_count"], job["retry_interval_minutes"] = _validate_retries(payload, current)
        changed = True

    validate_copy_job(job, jobs, repositories)
    return changed


def validate_copy_job(job: dict[str, Any], jobs: list[dict[str, Any]], repositories: list[dict[str, Any]]) -> None:
    from python.srv.plugins.hyperv.vhdx_tool import (
        VhdxToolError,
        ensure_hyperv_repository,
    )
    from python.srv.services.abr_v41 import _ensure

    destination_id = str(job.get("repo_id") or "")
    destination = REPOSITORIES.get(destination_id) or {}
    destination_name = str(job.get("repo_name") or destination.get("name") or destination_id)
    clashing = [repo for repo in repositories if str(repo["id"]) == destination_id]
    clashing_jobs = [source for source in jobs if str(source.get("repo_id") or "") == destination_id]

    _ensure(
        not clashing and not clashing_jobs,
        422,
        "backup_copy_same_repository",
        (
            f"Repository {destination_name} is a source of this backup copy job"
            + (f" (job {clashing_jobs[0].get('name')} backs up into it)" if clashing_jobs else "")
            + "; the copies must go to another repository"
        ),
        repository=destination_name,
    )
    _ensure(
        is_abr_repository(destination),
        422,
        "invalid_job",
        f"Repository {destination_name} is of type '{destination.get('type')}', which cannot hold backup copies",
    )

    mode = str(job.get("copy_mode") or "periodic")
    if mode == "immediate":
        _ensure(
            not job.get("schedule"),
            422,
            "invalid_job",
            "An immediate backup copy job copies every new restore point as soon as it appears, so it takes no schedule; "
            "use copy-mode periodic to copy on a schedule",
        )
        _ensure(
            not job.get("run_after_job_id"),
            422,
            "invalid_job",
            "An immediate backup copy job already runs after every backup of its sources; run-after-job is for periodic ones",
        )

    if _hyperv_sources(jobs, repositories, list(job.get("vm_ids") or [])):
        try:
            ensure_hyperv_repository(destination)
        except VhdxToolError:
            raise api_error(
                422,
                "hyperv_repository_unreachable",
                f"The sources hold Hyper-V VMs, and a Hyper-V restore reads its disks from the Hyper-V host, which "
                f"cannot reach {destination_name}, a folder of this appliance. Copy them to an S3 repository, or an "
                "SMB repository with its share",
                repository=destination_name,
            ) from None

    parameters = job.get("task_parameters") if isinstance(job.get("task_parameters"), dict) else {}
    keyed = bool(parameters.get("encryption_enabled")) and bool(str(parameters.get("encryption_key") or "").strip())
    _ensure(
        keyed or not _encrypted_sources(jobs, repositories, list(job.get("vm_ids") or [])),
        422,
        "invalid_job",
        "The source restore points are encrypted; give the backup copy job an encryption key of its own "
        "(set-encryption) so the copies stay encrypted",
    )


def _named(ids: list[str], names: list[Any], store: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    stored_names = [str(name or "") for name in names]
    return [
        {
            "id": record_id,
            "name": (store.get(record_id) or {}).get("name") or (stored_names[index] if index < len(stored_names) else None),
        }
        for index, record_id in enumerate(ids)
    ]


def copy_job_view(job: dict[str, Any]) -> dict[str, Any]:
    count, interval = retry_settings(job)
    run_after_id = str(job.get("run_after_job_id") or "")
    return {
        "copy_mode": job.get("copy_mode") or "periodic",
        "source_jobs": _named(source_job_ids(job), list(job.get("source_job_names") or []), JOBS),
        "source_repositories": _named(
            source_repository_ids(job), list(job.get("source_repository_names") or []), REPOSITORIES
        ),
        "run_after_job": _named([run_after_id], [job.get("run_after_job_name")], JOBS)[0] if run_after_id else None,
        "retry": {"count": count, "interval_minutes": interval},
        "copy_window": list(job.get("copy_window") or []),
        "copy_window_summary": describe_copy_window(list(job.get("copy_window") or [])),
    }


def _window_error(message: str, **details: Any) -> Exception:
    return api_error(422, "invalid_copy_window", message, **details)


def _window_days(tokens: list[str], rule: str) -> list[str]:
    from python.srv.services.abr_v41 import DAY_ORDER, _title_day

    days: list[str] = []
    for token in tokens:
        if "-" in token:
            first, _, last = token.partition("-")
            begin = DAY_ORDER.index(_title_day(first))
            span = (DAY_ORDER.index(_title_day(last)) - begin) % len(DAY_ORDER)
            covered = sorted((begin + step) % len(DAY_ORDER) for step in range(span + 1))
            listed = ",".join(DAY_ORDER[position].lower() for position in covered)
            raise _window_error(
                f"Copy window rule '{rule}' gives days as a range ({token}); list them separated by commas, as in {listed}",
                rule=rule,
            )
        day = _title_day(token)
        if day in days:
            raise _window_error(f"{day} is given more than once in copy window rule '{rule}'", rule=rule, day=day)
        if days and DAY_ORDER.index(day) < DAY_ORDER.index(days[-1]):
            in_order = ",".join(item.lower() for item in sorted([*days, day], key=DAY_ORDER.index))
            raise _window_error(
                f"Days in copy window rule '{rule}' must go in week order, Monday to Sunday, as in {in_order}",
                rule=rule,
            )
        days.append(day)

    return days or list(DAY_ORDER)


def _days_label(days: list[str]) -> str:
    from python.srv.services.abr_v41 import DAY_ORDER

    return "every day" if len(days) == len(DAY_ORDER) else ", ".join(days)


def _window_minutes(value: str, rule: str) -> int:
    match = WINDOW_TIME.match(value.strip())
    if not match or (match.group(1) == "24" and match.group(2) != "00"):
        raise _window_error(f"'{value}' in copy window rule '{rule}' is not a time from 00:00 to 24:00", rule=rule)
    return int(match.group(1)) * 60 + int(match.group(2))


def _window_rule(rule: Any) -> dict[str, Any]:
    from python.srv.services.abr_v41 import _server_timezone_name

    if isinstance(rule, dict):
        raw_days = rule.get("days")
        tokens = raw_days if isinstance(raw_days, list) else _tokens(raw_days)
        start_text, end_text = str(rule.get("start") or ""), str(rule.get("end") or "")
        text = f"{','.join(str(token) for token in tokens)} {start_text}-{end_text}".strip()
    else:
        text = str(rule or "").strip()
        parts = text.split()
        if len(parts) not in (1, 2) or parts[-1].count("-") != 1:
            raise _window_error(
                f"Copy window rule '{text}' is not '[days] HH:MM-HH:MM', for example 'mon-fri 22:00-06:00'", rule=text
            )
        tokens = [token for token in parts[0].split(",") if token] if len(parts) == 2 else []
        start_text, end_text = parts[-1].split("-")

    start, end = _window_minutes(start_text, text), _window_minutes(end_text, text)
    if start == 24 * 60 or start == end:
        raise _window_error(
            f"Copy window rule '{text}' must start before 24:00 and end at another time; use 00:00-24:00 for a whole day",
            rule=text,
        )

    days = _window_days([str(token).strip() for token in tokens], text)
    return {
        "days": days,
        "label": _days_label(days),
        "start": start_text.strip(),
        "end": end_text.strip(),
        "timezone": (
            # By its current name, as schedules are: "Europe/Kiev" from a
            # browser is kept as "Europe/Kyiv".
            RENAMED_ZONES.get(str(rule["timezone"]), str(rule["timezone"]))
            if isinstance(rule, dict) and rule.get("timezone")
            else _server_timezone_name()
        ),
    }


def parse_copy_window(value: Any) -> list[dict[str, Any]]:
    rules = value if isinstance(value, list) else [value]
    rules = [rule for rule in rules if rule not in (None, "")]
    if len(rules) == 1 and isinstance(rules[0], str) and rules[0].strip().lower() == "none":
        return []
    return [_window_rule(rule) for rule in rules]


def _window_intervals(rules: list[dict[str, Any]], around: datetime) -> list[tuple[datetime, datetime]]:
    from python.srv.services.abr_v41 import DAY_ORDER, _load_timezone

    intervals = []
    for rule in rules:
        zone = _load_timezone(rule.get("timezone"))
        local = around.astimezone(zone)
        start = _window_minutes(str(rule["start"]), "")
        end = _window_minutes(str(rule["end"]), "")
        for offset in range(-1, 9):
            day = (local + timedelta(days=offset)).date()
            if DAY_ORDER[day.weekday()] not in rule["days"]:
                continue
            midnight = datetime(day.year, day.month, day.day, tzinfo=zone)
            opens = midnight + timedelta(minutes=start)
            closes = midnight + timedelta(minutes=end if end > start else end + 24 * 60)
            intervals.append((opens.astimezone(timezone.utc), closes.astimezone(timezone.utc)))
    return sorted(intervals)


def copy_window_state(job: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    rules = [rule for rule in job.get("copy_window") or [] if isinstance(rule, dict)]
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not rules:
        return {"open": True, "closes_at": None, "opens_at": None}

    intervals = _window_intervals(rules, moment)
    current = [interval for interval in intervals if interval[0] <= moment < interval[1]]
    if not current:
        upcoming = [opens for opens, _closes in intervals if opens > moment]
        return {"open": False, "closes_at": None, "opens_at": min(upcoming) if upcoming else None}

    closes = max(closes for _opens, closes in current)
    extended = True
    while extended:
        extended = False
        for opens, ends in intervals:
            if opens <= closes < ends:
                closes, extended = ends, True
    return {"open": True, "closes_at": closes, "opens_at": None}


def describe_copy_window(rules: list[dict[str, Any]]) -> str:
    if not rules:
        return "any time"
    return "; ".join(f"{rule.get('label') or ', '.join(rule.get('days') or [])} {rule.get('start')}-{rule.get('end')}" for rule in rules)


def _local_text(moment: datetime | None, job: dict[str, Any]) -> str:
    from python.srv.services.abr_v41 import _load_timezone

    if moment is None:
        return "never"
    rules = job.get("copy_window") or [{}]
    zone = _load_timezone(rules[0].get("timezone"))
    return moment.astimezone(zone).strftime("%a %Y-%m-%d %H:%M %Z")


def waits_for_window(task: dict[str, Any]) -> bool:
    from python.srv.services.abr_v41 import normalize_job_record

    stored = JOBS.get(str(task.get("job_id") or ""))
    if str(task.get("task_type") or "") != "backup" or not isinstance(stored, dict):
        return False
    job = normalize_job_record(stored)
    return job.get("job_type") == "backup-copy" and not copy_window_state(job)["open"]


def _window_closed(job: dict[str, Any]) -> bool:
    return not copy_window_state(job)["open"]


def _stop_for_window(job: dict[str, Any], detail: str) -> Exception:
    from python.srv.core.task_executor import TaskStopRequested

    state = copy_window_state(job)
    stop = TaskStopRequested(
        f"The copy window ({describe_copy_window(job.get('copy_window') or [])}) closed {detail}; "
        f"the copy is taken up again when it opens, {_local_text(state.get('opens_at'), job)}"
    )
    stop.reason = WINDOW_STOP_REASON
    return stop
