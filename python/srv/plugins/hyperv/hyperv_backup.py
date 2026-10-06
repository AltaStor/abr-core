# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Back up a Hyper-V VM into a repository with abr-vhdx-tool.

The flow follows run_vcenter_backup step for step, in Hyper-V terms:

1. Check the VM can be checkpointed now (state, pending merges), and remove
   any recovery checkpoint an interrupted backup of it left behind.
2. Take a recovery checkpoint -- application consistent through the guest's
   VSS when it is running, crash consistent otherwise or as a fallback. The
   disks it refers to stay as they are from then on; the VM writes to new
   differencing disks.
3. Back each of the checkpoint's disks up with abr-vhdx-tool on the VM's node,
   which reads it through the Windows virtual disk service: in full, or, for
   an incremental, only what resilient change tracking (RCT) reports as
   changed since the RCT id the previous backup recorded for that disk. When
   RCT can no longer tell (a disk that was moved, merged or replaced), the run
   starts over as a full backup.
4. Turn the checkpoint into a reference point, which keeps the change tracking
   position the next incremental starts from; remove the reference point of
   the backup before; write the manifest.

A failed run removes the checkpoint and what it had written. The previous
backup's reference point stays, so the next incremental still has a position.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from ...core.mongo_db_wrapper import HYPERVISORS, REPOSITORIES, RESOURCES_BY_HV
from ...core.storage_tool import (
    StorageToolError,
    apply_repository_immutability,
    disk_tool_immutability_args,
    disk_tool_storage_args,
    ensure_private_directory,
    extract_s3_config,
    run_disk_tool,
)
from ...core.subprocess_util import ProcessCancelled
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.utils import utc_now
from ...services.backup_mode import encryption_changed_since_last_backup, resolve_job_backup_mode
from ..vcenter.vcenter_backup import (
    VCenterBackupError,
    _compression_ratio,
    _discard_task_backups,
    _disk_timeout_seconds,
    _disk_tool_progress_reporter,
    _emit_progress,
    _record_backup_snapshot,
    _resolve_backup_storage_layout,
    _resolve_compression_level,
    _resolve_encryption_settings,
    _resolve_immutability_plan,
    _resolve_parent_disk_entry,
    _resolve_parent_manifest,
    _resolve_repository,
    _resolve_tool_timeout_seconds,
    _sanitize_name,
    _stop_message,
)
from . import vhdx_tool
from .hyperv import QUIESCABLE_STATES, HyperVError, HyperVNode

logger = logging.getLogger(__name__)

WORKER_MODE = "hyperv-abr-vhdx-tool"
HYPERVISOR_KIND = "hyper-v"

ProgressCallback = Callable[[dict[str, object]], None] | None

CONSISTENCY_CHOICES = ("application", "crash")


class HyperVBackupError(RuntimeError):
    pass


class _ChangeTrackingLost(Exception):
    """abr-vhdx-tool refused an incremental: RCT no longer knows the id."""


def _emit(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    _emit_progress(progress_callback, stage, progress, message, worker_mode=WORKER_MODE, **extra)


def _warn(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    logger.warning("%s", message)
    _emit(progress_callback, stage, progress, message, log_level="warn", **extra)


def _gib(value: int | float | None) -> str:
    return "unknown" if value is None else f"{float(value) / 1024**3:.2f} GiB"


def _get_hypervisor(task: dict[str, Any]) -> dict[str, Any]:
    hypervisor_id = str(task.get("hypervisor_id", ""))
    hypervisor = HYPERVISORS.get(hypervisor_id)
    if not hypervisor:
        raise HyperVBackupError(f"Hypervisor not found: {hypervisor_id}")
    if hypervisor.get("kind") != HYPERVISOR_KIND:
        raise HyperVBackupError(
            f"Hyper-V backup needs a Hyper-V inventory, got {hypervisor.get('kind')!r}"
        )
    return hypervisor


def _resolve_backup_target(task: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hypervisor_id = str(task.get("hypervisor_id") or "")
    resource_type = str(task.get("resource_type") or "")
    external_id = str(task.get("resource_external_id") or "")
    resources = RESOURCES_BY_HV.get(hypervisor_id, {})
    resource = resources.get((resource_type, external_id))
    if not resource:
        raise HyperVBackupError(
            f"{resource_type} {external_id} is no longer in the inventory of this Hyper-V host. Run "
            "inventory rescan if it is still there; if it has been deleted, it cannot be backed up"
        )
    if resource_type == "virtual_machine":
        return resource, []
    if resource_type == "disk":
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        vm_id = str(payload.get("vm_id") or "")
        vm_resource = resources.get(("virtual_machine", vm_id))
        if not vm_resource:
            raise HyperVBackupError(
                f"The VM of disk {external_id} is no longer in the inventory; run inventory rescan"
            )
        return vm_resource, [resource]
    raise HyperVBackupError(f"Hyper-V backup does not support resource type {resource_type!r}")


def _choice(parameters: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = str(parameters.get(key) or default).strip().lower()
    if value not in allowed:
        raise HyperVBackupError(f"parameters.{key} must be one of: {', '.join(allowed)}")
    return value


def _flag(parameters: dict[str, Any], key: str, default: bool) -> bool:
    value = parameters.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def connection_settings(hypervisor: dict[str, Any]) -> dict[str, Any]:
    metadata = hypervisor.get("metadata") if isinstance(hypervisor.get("metadata"), dict) else {}
    credentials = metadata.get("credentials") if isinstance(metadata.get("credentials"), dict) else {}
    host = str(metadata.get("host") or "")
    username = str(credentials.get("username") or "")
    password = str(credentials.get("password") or "")
    if not host or not username or not password:
        raise HyperVBackupError("Saved Hyper-V registration is missing host or credentials")
    port = metadata.get("port")
    return {
        "host": host,
        "user": username,
        "password": password,
        "port": int(port) if port and metadata.get("port_explicit") else None,
        "verify_ssl": bool(metadata.get("verify_ssl", False)),
    }


def open_node(connection: dict[str, Any]) -> HyperVNode:
    return HyperVNode(
        connection["host"],
        connection["user"],
        connection["password"],
        port=connection["port"],
        verify_ssl=connection["verify_ssl"],
    )


def _remove_stranded_checkpoints(
    node: HyperVNode, task: dict[str, Any], vm_id: str, progress_callback: ProgressCallback
) -> None:
    from ...services.backup_catalog import forget_task_backup_snapshot, stranded_backup_snapshots

    try:
        pending = stranded_backup_snapshots(
            str(task.get("hypervisor_id") or ""), vm_id, exclude_task_id=str(task.get("id") or "")
        )
    except Exception:
        logger.exception("Could not look up checkpoints left by interrupted backups of VM %s", vm_id)
        return
    for task_id, checkpoint_id in pending:
        _emit(
            progress_callback,
            "snapshot",
            12,
            f"Removing recovery checkpoint {checkpoint_id} left behind by interrupted session {task_id}",
        )
        try:
            node.remove_snapshot(vm_id, checkpoint_id)
        except Exception as exc:
            _warn(
                progress_callback,
                "snapshot",
                12,
                f"Could not remove recovery checkpoint {checkpoint_id} left by interrupted session {task_id}: {exc}",
            )
            continue
        forget_task_backup_snapshot(task_id)


def _requested(disks: list[dict[str, Any]], requested_disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not requested_disks:
        return disks
    slots = set()
    paths = set()
    for resource in requested_disks:
        payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
        if payload.get("controller"):
            slots.add(str(payload["controller"]).lower())
        if payload.get("datastore_path"):
            paths.add(str(payload["datastore_path"]).lower())
    chosen = [
        disk
        for disk in disks
        if disk["slot"].lower() in slots
        or disk["checkpoint_path"].lower() in paths
        or any(path.lower() in paths for path in disk["chain_paths"])
    ]
    if not chosen:
        raise HyperVBackupError("The requested disk is no longer attached to the VM; run inventory rescan")
    return chosen


def _backup_name(index: int, disk: dict[str, Any]) -> str:
    """Stable from one backup of a disk to the next: a synthetic full pairs a
    disk's backups along the chain by this name."""
    stem = PureWindowsPath(disk["datastore_path"]).stem if disk["datastore_path"] else f"disk-{index}"
    return f"{index:02d}-{_sanitize_name(stem or f'disk-{index}')}.vhdx"


def _repository_label(repository: dict[str, Any], abr_path: str) -> str:
    name = str(repository.get("name") or repository.get("id") or "repository")
    bucket = str(extract_s3_config(repository).get("bucket") or "")
    return f"{name} (S3 bucket {bucket}, path {abr_path})"


def _previous_manifest(
    *, parameters: dict[str, Any], destination_root: Path, vm_name: str, task_root: Path, vm_id: str
) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        return _resolve_parent_manifest(
            parameters=parameters,
            destination_root=destination_root,
            vm_name=vm_name,
            task_root=task_root,
            vm_external_id=vm_id,
        )
    except VCenterBackupError:
        return None, None


def _change_ids(
    disks: list[dict[str, Any]], parent: dict[str, Any] | None
) -> tuple[dict[str, str], list[str]]:
    """Each disk's RCT id from the previous backup, and the disks that have
    none to chain onto."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for index, disk in enumerate(disks, start=1):
        entry = None
        if parent is not None:
            try:
                entry = _resolve_parent_disk_entry(
                    parent, datastore_path=disk["datastore_path"], uuid=disk["uuid"], index=index
                )
            except VCenterBackupError:
                entry = None
        change_id = str((entry or {}).get("change_id") or "").strip()
        if change_id:
            found[disk["slot"]] = change_id
        else:
            missing.append(disk["slot"])
    return found, missing


def run_hyperv_backup(
    task: dict[str, Any],
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    start_time = time.time()
    hypervisor = _get_hypervisor(task)
    connection = connection_settings(hypervisor)
    vm_resource, requested_disks = _resolve_backup_target(task)
    vm_id = str(vm_resource.get("external_id") or "")
    vm_name = str(vm_resource.get("name") or vm_id)
    vm_payload = vm_resource.get("payload") if isinstance(vm_resource.get("payload"), dict) else {}
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}

    consistency = _choice(parameters, "hyperv_consistency", CONSISTENCY_CHOICES, "application")
    consistency_fallback = _flag(parameters, "hyperv_consistency_fallback", True)
    try:
        compression_level = _resolve_compression_level(parameters)
        encryption_algorithm, encryption_key = _resolve_encryption_settings(parameters)
        tool_timeout_seconds = _resolve_tool_timeout_seconds(parameters)
    except VCenterBackupError as exc:
        raise HyperVBackupError(str(exc)) from exc
    existing_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    resolved_backup_type = str(
        parameters.get("resolved_backup_type") or existing_result.get("resolved_backup_type") or "ActiveFull"
    )
    resolved_backup_reason = str(
        parameters.get("resolved_backup_reason") or existing_result.get("resolved_backup_reason") or "FirstRun"
    )
    job_mode = resolve_job_backup_mode(task, parameters)
    incremental = job_mode == "incremental" if job_mode is not None else resolved_backup_type == "Incremental"
    if encryption_changed_since_last_backup(task, parameters):
        incremental = False
        resolved_backup_type, resolved_backup_reason = "ActiveFull", "EncryptionKeyChanged"

    repository = _resolve_repository(task)
    try:
        vhdx_tool.ensure_hyperv_repository(repository)
    except vhdx_tool.VhdxToolError as exc:
        raise HyperVBackupError(str(exc)) from exc
    destination_root, task_root, abr_path = _resolve_backup_storage_layout(
        task=task, vm_name=vm_name, parameters=parameters, repository=repository
    )
    ensure_private_directory(task_root)
    storage_args, storage_env = vhdx_tool.storage_invocation(repository, abr_path=abr_path)
    repository_label = _repository_label(repository, str(abr_path))
    inventory_name = str(hypervisor.get("name") or task.get("hypervisor_id") or "")
    previous_path, previous = _previous_manifest(
        parameters=parameters, destination_root=destination_root, vm_name=vm_name, task_root=task_root, vm_id=vm_id
    )
    warnings: list[str] = []

    def _fall_back_to_full(message: str) -> None:
        nonlocal incremental, resolved_backup_type, resolved_backup_reason
        _warn(progress_callback, "prepare", 22, message)
        warnings.append(message)
        incremental = False
        resolved_backup_type, resolved_backup_reason = "ActiveFull", "IncrementalFallbackToFull"

    logger.info(
        "Hyper-V backup session %s started: VM %s (%s) of inventory %s, host %s, %s (%s), to %s",
        task_id,
        vm_name,
        vm_id,
        inventory_name,
        vm_payload.get("host_name") or connection["host"],
        resolved_backup_type,
        resolved_backup_reason,
        repository_label,
    )
    _emit(
        progress_callback,
        "prepare",
        5,
        f"Hyper-V backup of {vm_name} ({vm_id}) from inventory {inventory_name}: "
        f"{'incremental' if incremental else 'full'} backup ({resolved_backup_type}, {resolved_backup_reason}) "
        f"to {repository_label}",
        vm_name=vm_name,
        backup_root=str(task_root),
    )

    checkpoint_id: str | None = None
    written_backup_names: list[str] = []
    stop_requested_after_completion = False
    disk_outputs: list[dict[str, Any]] = []

    try:
        with open_node(connection) as node:
            try:
                state = node.ensure_vm_host_connected(vm_id, hint=str(vm_payload.get("host_name") or "") or None)
            except HyperVError as exc:
                raise HyperVBackupError(str(exc)) from exc
            vm_name = str(state.get("name") or vm_name)
            source_node = str(state.get("computer") or "")
            service_type = f"Hyper-V {node.get_service_type().lower()}"
            _emit(
                progress_callback,
                "prepare",
                8,
                f"VM {vm_name} is {state.get('state')} on {source_node} (configuration version {state.get('version')})",
            )
            _remove_stranded_checkpoints(node, task, vm_id, progress_callback)
            tool_path = node.ensure_disk_tool(source_node)
            _emit(progress_callback, "prepare", 10, f"abr-vhdx-tool is ready on {source_node} at {tool_path}")

            # A checkpoint can only be application consistent when there is
            # a running guest to quiesce.
            achieved = consistency
            if str(state.get("state") or "") not in QUIESCABLE_STATES:
                achieved = "crash"
            _emit(
                progress_callback,
                "snapshot",
                15,
                f"Creating {achieved}-consistent recovery checkpoint of {vm_name} on {source_node}",
            )
            try:
                checkpoint = node.create_recovery_checkpoint(vm_id, achieved)
            except HyperVError as exc:
                if achieved != "application" or not consistency_fallback:
                    raise HyperVBackupError(f"Hyper-V could not checkpoint {vm_name}: {exc}") from exc
                message = (
                    f"Application-consistent checkpoint of {vm_name} failed ({exc}); taking a crash-consistent "
                    "one instead. Check the Backup (volume shadow copy) integration service in the guest"
                )
                _warn(progress_callback, "snapshot", 15, message)
                warnings.append(message)
                achieved = "crash"
                try:
                    checkpoint = node.create_recovery_checkpoint(vm_id, "crash")
                except HyperVError as retry_exc:
                    raise HyperVBackupError(f"Hyper-V could not checkpoint {vm_name}: {retry_exc}") from retry_exc
            checkpoint_id = str(checkpoint["checkpoint_id"])
            _record_backup_snapshot(progress_callback, vm_id, checkpoint_id)
            source_node = str(checkpoint.get("computer") or source_node)
            _emit(
                progress_callback,
                "snapshot",
                18,
                f"Recovery checkpoint {checkpoint_id} created ({achieved} consistent)",
            )

            all_disks = node.checkpoint_disks(vm_id, checkpoint_id)
            excluded = [disk for disk in all_disks if not disk["supported"]]
            for disk in excluded:
                message = (
                    f"Skipping disk {disk['slot']} of {vm_name} ({disk['checkpoint_path'] or 'no file'}): "
                    f"{disk['unsupported_reason']}"
                )
                _warn(progress_callback, "prepare", 20, message)
                warnings.append(message)
            disks = _requested([disk for disk in all_disks if disk["supported"]], requested_disks)
            if not disks:
                raise HyperVBackupError(
                    f"VM {vm_name} has no disk this backup can read"
                    + (f" ({'; '.join(d['unsupported_reason'] for d in excluded)})" if excluded else "")
                )
            vm_config = node.get_vm_restore_metadata(vm_id)

            change_ids: dict[str, str] = {}
            if incremental:
                if previous is None:
                    _fall_back_to_full(f"No earlier backup of {vm_name} to chain onto; taking a full backup")
                else:
                    change_ids, without = _change_ids(disks, previous)
                    if without:
                        _fall_back_to_full(
                            f"Disk(s) {', '.join(without)} of {vm_name} have no change tracking position from the "
                            "previous backup (a disk added since, a VHD disk, or a VM of configuration version "
                            "below 8.0); taking a full backup"
                        )
            _emit(
                progress_callback,
                "prepare",
                22,
                f"Backing up {len(disks)} disk(s) of {vm_name}: "
                + ", ".join(f"{disk['slot']} {disk['checkpoint_path']} ({_gib(disk['capacity_bytes'])})" for disk in disks),
            )

            # ---- transfer ---------------------------------------------------
            while True:
                try:
                    disk_outputs = _back_up_disks(
                        node,
                        source_node,
                        disks,
                        task_id=task_id,
                        vm_name=vm_name,
                        incremental=incremental,
                        change_ids=change_ids,
                        storage_args=storage_args,
                        storage_env=storage_env,
                        repository=repository,
                        abr_path=str(abr_path),
                        repository_label=repository_label,
                        compression_level=compression_level,
                        encryption_key=encryption_key,
                        tool_timeout_seconds=tool_timeout_seconds,
                        written_backup_names=written_backup_names,
                        progress_callback=progress_callback,
                    )
                    break
                except _ChangeTrackingLost as exc:
                    _discard_task_backups(repository, abr_path, written_backup_names, task_root)
                    written_backup_names.clear()
                    ensure_private_directory(task_root)
                    change_ids = {}
                    _fall_back_to_full(
                        f"Resilient change tracking of {vm_name} no longer knows the previous backup's position "
                        f"({exc}); taking a full backup instead"
                    )

            total_read_bytes = sum(int(disk.get("logical_bytes") or 0) for disk in disk_outputs)
            total_transferred_bytes = sum(int(disk.get("stored_bytes") or 0) for disk in disk_outputs)

            # ---- reference point --------------------------------------------
            reference_point_id = ""
            try:
                _emit(
                    progress_callback,
                    "finalize",
                    88,
                    f"Turning recovery checkpoint {checkpoint_id} of {vm_name} into a reference point",
                    processed_bytes=total_read_bytes,
                    transferred_bytes=total_transferred_bytes,
                )
            except TaskStopRequested:
                stop_requested_after_completion = True
            try:
                reference = node.convert_to_reference_point(vm_id, checkpoint_id)
                reference_point_id = str(reference.get("reference_point_id") or "")
            except HyperVError as exc:
                message = (
                    f"Recovery checkpoint {checkpoint_id} of {vm_name} could not be turned into a reference point "
                    f"({exc}); it is removed instead, and the next backup of {vm_name} is a full backup"
                )
                _warn(progress_callback, "finalize", 89, message)
                warnings.append(message)
                node.remove_snapshot(vm_id, checkpoint_id)
                for disk in disk_outputs:
                    disk["change_id"] = ""
            checkpoint_id = None
            _record_backup_snapshot(progress_callback, vm_id, None)

            previous_reference = str((previous or {}).get("reference_point_id") or "")
            same_job = str((previous or {}).get("job_id") or "") == str(task.get("job_id") or "")
            if reference_point_id and previous_reference and previous_reference != reference_point_id and same_job:
                try:
                    node.remove_reference_point(vm_id, previous_reference)
                except HyperVError as exc:
                    message = f"Could not remove the previous reference point {previous_reference} of {vm_name}: {exc}"
                    _warn(progress_callback, "finalize", 90, message)
                    warnings.append(message)

            # ---- manifest ---------------------------------------------------
            excluded_disks = [
                {"slot": disk["slot"], "path": disk["checkpoint_path"], "reason": disk["unsupported_reason"]}
                for disk in excluded
            ]
            compression_ratio = _compression_ratio(total_read_bytes, total_transferred_bytes)
            hyperv_details = {
                "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
                "consistency_requested": consistency,
                "consistency": achieved,
                "source_node": source_node,
                "cluster_name": node.cluster_name,
                "excluded_disks": excluded_disks,
                "reference_point_id": reference_point_id,
            }
            backup_mode = "incremental" if incremental else "full"
            manifest_path = task_root / "manifest.json"
            manifest_payload = {
                "format_version": 1,
                "operation": "vm-backup",
                "backup_mode": backup_mode,
                "resolved_backup_type": "Incremental" if incremental else "ActiveFull" if resolved_backup_type == "Incremental" else resolved_backup_type,
                "resolved_backup_reason": resolved_backup_reason,
                "hypervisor_kind": HYPERVISOR_KIND,
                "hypervisor_id": str(task.get("hypervisor_id") or ""),
                "job_id": str(task.get("job_id") or ""),
                "vm_name": vm_name,
                "vm_external_id": vm_id,
                "vm_config": vm_config,
                "compression_level": compression_level,
                "encryption_enabled": bool(encryption_algorithm),
                "encryption_algorithm": encryption_algorithm,
                "service_type": service_type,
                "incremental": incremental,
                "differential": False,
                "cbt_enabled": all(disk.get("change_id") for disk in disk_outputs),
                "parent_manifest_path": str(previous_path) if incremental and previous_path else "",
                "snapshot_name": hyperv_details["checkpoint_id"],
                "snapshot_moref": "",
                **hyperv_details,
                "disks": disk_outputs,
                "repository_id": str(repository.get("id") or ""),
                "repository_type": "s3",
                "storage_mode": "s3",
                "repository_storage_path": abr_path,
                "backup_root": str(task_root),
                "processed_bytes": total_read_bytes,
                "transferred_bytes": total_transferred_bytes,
                "logical_bytes": total_read_bytes,
                "compression_ratio": compression_ratio,
                "warnings": warnings,
                "created_at": utc_now(),
            }
            resolved_backup_type = manifest_payload["resolved_backup_type"]
            manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
            try:
                run_disk_tool(
                    [
                        "meta_save",
                        "--name",
                        "manifest",
                        *disk_tool_storage_args(repository, abr_path=abr_path),
                        "--source",
                        str(manifest_path),
                    ],
                    timeout=60,
                )
                written_backup_names.append("manifest")
                apply_repository_immutability(repository, abr_path, "manifest", timeout=120)
            except StorageToolError:
                logger.warning(
                    "Could not mirror the manifest of session %s into %s; the local copy at %s stays the reference",
                    task_id,
                    repository_label,
                    manifest_path,
                    exc_info=True,
                )

        if not stop_requested_after_completion and cancel_requested(task_id):
            stop_requested_after_completion = True
        duration = time.time() - start_time
        immutability_generation = (
            _resolve_immutability_plan(task, REPOSITORIES[str(repository.get("id") or "")])
            if str(repository.get("id") or "") in REPOSITORIES
            else None
        )
        if immutability_generation and immutability_generation.get("lock_days"):
            logger.info(
                "Hyper-V backup session %s: Object Lock holds its data for %s day(s), until %s",
                task_id,
                immutability_generation.get("lock_days"),
                immutability_generation.get("expires_at"),
            )
        summary = (
            f"Hyper-V backup of {vm_name} completed: {backup_mode} backup of {len(disk_outputs)} disk(s), "
            f"read {_gib(total_read_bytes)}, stored {_gib(total_transferred_bytes)}"
            + (f" (compression ratio {compression_ratio})" if compression_ratio is not None else "")
            + f" in {duration:.1f}s"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )
        logger.info("Session %s: %s", task_id, summary)
        _emit(progress_callback, "finalize", 98, summary)
        return {
            "operation": "backup",
            "worker_mode": WORKER_MODE,
            "hypervisor_kind": HYPERVISOR_KIND,
            "vm_name": vm_name,
            "vm_external_id": vm_id,
            "backup_mode": backup_mode,
            "resolved_backup_type": resolved_backup_type,
            "resolved_backup_reason": resolved_backup_reason,
            "incremental": incremental,
            "differential": False,
            "snapshot_removed": True,
            "stop_requested_after_completion": stop_requested_after_completion,
            **hyperv_details,
            "backup_root": str(task_root),
            "manifest_path": str(manifest_path),
            "repository_id": str(repository.get("id") or ""),
            "repository_type": "s3",
            "storage_mode": "s3",
            "repository_storage_path": abr_path,
            "immutability_generation": immutability_generation,
            "vm_config": vm_config,
            "disk_count": len(disk_outputs),
            "disks": disk_outputs,
            "compression_level": compression_level,
            "encryption_enabled": bool(encryption_algorithm),
            "encryption_algorithm": encryption_algorithm,
            "service_type": service_type,
            "processed_bytes": total_read_bytes,
            "transferred_bytes": total_transferred_bytes,
            "logical_bytes": total_read_bytes,
            "compression_ratio": compression_ratio,
            "duration_seconds": round(duration, 3),
            "warnings": warnings,
            "finished_at": utc_now(),
        }
    except ProcessCancelled as exc:
        logger.info("Hyper-V backup session %s of VM %s stopped; discarding what it had written.", task_id, vm_name)
        stranded = _discard_task_backups(repository, abr_path, written_backup_names, task_root)
        raise TaskStopRequested(_stop_message(stranded, abr_path, task_root)) from exc
    except TaskStopRequested:
        logger.info("Hyper-V backup session %s of VM %s stopped; discarding what it had written.", task_id, vm_name)
        stranded = _discard_task_backups(repository, abr_path, written_backup_names, task_root)
        raise TaskStopRequested(_stop_message(stranded, abr_path, task_root)) from None
    except Exception as exc:
        logger.exception(
            "Hyper-V backup session %s of VM %s (%s) failed after %.1fs: %s",
            task_id,
            vm_name,
            vm_id,
            time.time() - start_time,
            exc,
        )
        _discard_task_backups(repository, abr_path, written_backup_names, task_root)
        if isinstance(exc, (HyperVError, vhdx_tool.VhdxToolError)):
            raise HyperVBackupError(str(exc)) from exc
        raise
    finally:
        if checkpoint_id:
            _cleanup_checkpoint(connection, vm_id, vm_name, checkpoint_id, progress_callback)


def _back_up_disks(
    node: HyperVNode,
    source_node: str,
    disks: list[dict[str, Any]],
    *,
    task_id: str,
    vm_name: str,
    incremental: bool,
    change_ids: dict[str, str],
    storage_args: list[str],
    storage_env: dict[str, str],
    repository: dict[str, Any],
    abr_path: str,
    repository_label: str,
    compression_level: int,
    encryption_key: str,
    tool_timeout_seconds: int,
    written_backup_names: list[str],
    progress_callback: ProgressCallback,
) -> list[dict[str, Any]]:
    """Back every disk up with abr-vhdx-tool on the VM's node; raises
    _ChangeTrackingLost when an incremental cannot be taken."""
    outputs: list[dict[str, Any]] = []
    total_read = 0
    total_stored = 0
    for index, disk in enumerate(disks, start=1):
        disk_started = time.time()
        slot = disk["slot"]
        backup_name = _backup_name(index, disk)
        progress = min(25 + int((index - 1) * 60 / len(disks)), 85)
        span = max(2, min(25 + int(index * 60 / len(disks)), 85) - progress)
        change_id = change_ids.get(slot, "") if incremental else ""
        args = [
            "backup",
            "--json",
            "--name",
            backup_name,
            *storage_args,
            *disk_tool_immutability_args(repository),
            "--compression",
            str(compression_level),
            "--vhdx",
            disk["checkpoint_path"],
        ]
        if change_id:
            args.extend(["--type", "incremental", "--change-id", change_id])
        if encryption_key:
            args.extend(["--key", encryption_key])
        kind = f"incremental since {change_id}" if change_id else "full"
        _emit(
            progress_callback,
            "transfer",
            progress,
            f"Backing up disk {index}/{len(disks)} ({slot}, {disk['checkpoint_path']}) on {source_node}: {kind}, "
            f"as {backup_name} in {repository_label}",
            current_disk=disk["checkpoint_path"],
            processed_bytes=total_read,
        )
        report = _disk_tool_progress_reporter(
            progress_callback,
            base_progress=progress,
            progress_span=span,
            message_prefix=f"Backing up disk {index}/{len(disks)}",
            base_read_bytes=total_read,
            disk_total_bytes=0 if change_id else int(disk["capacity_bytes"] or 0),
            worker_mode=WORKER_MODE,
        )
        written_backup_names.append(backup_name)
        run = node.run_disk_tool(
            source_node,
            args,
            env=storage_env,
            on_progress=lambda percent: report(percent, 0),
            should_cancel=lambda: cancel_requested(task_id),
            timeout=_disk_timeout_seconds(tool_timeout_seconds, disk["capacity_gb"]),
        )
        result = run.get("result") or {}
        if run.get("exit_code") != 0 or result.get("status") not in (None, "ok"):
            detail = vhdx_tool.failure_message(result, run.get("lines") or [], run.get("exit_code"))
            if change_id and vhdx_tool.change_tracking_lost(detail):
                raise _ChangeTrackingLost(detail)
            raise HyperVBackupError(f"Backing up disk {slot} of {vm_name} failed: {detail}")
        logical = int(result.get("raw_data_size") or 0)
        stored = int(result.get("stored_data_size") or 0)
        total_read += logical
        total_stored += stored
        rct_id = str(result.get("rct_id") or "")
        outputs.append(
            {
                "index": index,
                "disk_label": slot,
                "slot": slot,
                "controller_type": disk["controller_type"],
                "controller_number": disk["controller_number"],
                "controller_location": disk["controller_location"],
                "datastore_path": disk["datastore_path"],
                "checkpoint_path": disk["checkpoint_path"],
                "chain_paths": disk["chain_paths"],
                "uuid": disk["uuid"] or None,
                "capacity_bytes": disk["capacity_bytes"],
                "capacity_gb": disk["capacity_gb"],
                "logical_sector_size": int(result.get("sector_size") or disk["logical_sector_size"]),
                "physical_sector_size": int(result.get("physical_sector_size") or disk["physical_sector_size"]),
                "block_size": disk["block_size"],
                "vhd_format": disk["vhd_format"],
                "vhd_type": disk["vhd_type"],
                "source_disk_type_code": disk["source_disk_type_code"],
                "source_disk_type_name": disk["source_disk_type_name"],
                "source_disk_eager_zeroed": disk["source_disk_eager_zeroed"],
                "change_id": rct_id,
                "previous_change_id": change_id,
                "store_mode": "incremental" if change_id else "full",
                "logical_bytes": logical,
                "stored_bytes": stored,
                "compression_ratio": _compression_ratio(logical, stored),
                "destination_path": "",
                "parent_path": "",
                "storage_mode": "s3",
                "repository_type": "s3",
                "repository_id": str(repository.get("id") or ""),
                "abr_path": abr_path,
                "abr_backup_name": backup_name,
            }
        )
        _emit(
            progress_callback,
            "transfer",
            progress + span,
            f"Disk {index}/{len(disks)} ({slot}) backed up: {_gib(logical)} read, {_gib(stored)} stored in "
            f"{time.time() - disk_started:.1f}s" + (f"; RCT id {rct_id}" if rct_id else "; no change tracking"),
            processed_bytes=total_read,
            transferred_bytes=total_stored,
        )
    return outputs


def _cleanup_checkpoint(
    connection: dict[str, Any],
    vm_id: str,
    vm_name: str,
    checkpoint_id: str,
    progress_callback: ProgressCallback,
) -> None:
    try:
        try:
            _emit(progress_callback, "cleanup", 96, f"Removing recovery checkpoint {checkpoint_id} of {vm_name}")
        except TaskStopRequested:
            pass
        with open_node(connection) as node:
            node.remove_snapshot(vm_id, checkpoint_id)
        _record_backup_snapshot(progress_callback, vm_id, None)
    except Exception as exc:
        logger.warning("Recovery checkpoint %s of VM %s could not be removed: %s", checkpoint_id, vm_name, exc)
        try:
            _emit(
                progress_callback,
                "cleanup",
                96,
                f"Recovery checkpoint {checkpoint_id} of {vm_name} could not be removed: {exc}. "
                "The next backup of this VM removes it",
                log_level="warn",
            )
        except TaskStopRequested:
            pass
