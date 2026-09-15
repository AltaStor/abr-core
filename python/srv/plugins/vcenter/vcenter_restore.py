# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from ...core.subprocess_util import ProcessCancelled, run_capped
from ...core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES, catalog_records
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.storage_tool import (
    build_s3_location,
    disk_tool_storage_args,
    extract_s3_config,
    redact_disk_tool_command,
)
from ...core.utils import utc_now

logger = logging.getLogger(__name__)
from .vcenter_backup import (
    VCenterBackupError,
    _build_vddk_args,
    _default_tool_path,
    _disk_tool_progress_reporter,
    _emit_progress,
    _format_vm_spec,
    _get_hypervisor,
    _get_resource,
    _get_vm_resource,
    _sanitize_name,
)
from .vcenter_disk_types import disk_type_name_from_code, normalize_disk_type_selection


_PLACEHOLDER_DISK_UUIDS = {"n/a", "none", "null", "unknown"}

ProgressCallback = Callable[[dict[str, object]], None] | None


def _resolve_restore_firmware(vm_config: dict[str, Any]) -> tuple[str | None, str]:
    raw_firmware = vm_config.get("firmware")
    normalized = str(raw_firmware or "").strip().lower()
    if not normalized:
        logger.warning(
            "Source firmware is missing from restore point metadata; falling back to default firmware 'bios'."
        )
        return None, "bios"
    if normalized not in {"bios", "efi"}:
        raise VCenterBackupError(
            f"Backup manifest contains unsupported firmware value: {raw_firmware!r}"
        )
    return normalized, normalized


def _parse_datastore_path(datastore_path: str) -> tuple[str, str, str]:
    if not datastore_path.startswith("[") or "]" not in datastore_path:
        raise VCenterBackupError(f"Unsupported datastore path format: {datastore_path}")
    datastore, relative_path = datastore_path.split("]", 1)
    datastore_name = datastore[1:].strip()
    relative_path = relative_path.strip()
    folder = str(Path(relative_path).parent)
    if folder == ".":
        folder = ""
    filename = Path(relative_path).name
    return datastore_name, folder, filename


VSPHERE_VM_NAME_LIMIT = 80

VSPHERE_DISK_FILE_NAME_LIMIT = 122

SNAPSHOT_DELTA_NAME_EXTRA = 16

RESTORED_DISK_FILE_NAME_LIMIT = VSPHERE_DISK_FILE_NAME_LIMIT - SNAPSHOT_DELTA_NAME_EXTRA


def _name_with_suffix(base: str, suffix: str, limit: int) -> str:
    room = max(limit - len(suffix), 0)
    return f"{base[:room]}{suffix}"


def _build_disk_file_name(index: int, source_name: str) -> str:
    source = Path(source_name)
    extension = source.suffix or ".vmdk"
    return _name_with_suffix(f"{index:02d}-{source.stem}", extension, RESTORED_DISK_FILE_NAME_LIMIT)


def _build_staging_vm_name(target_vm_name: str, task_id: str) -> str:
    return _name_with_suffix(
        target_vm_name, f"-restoring-{task_id[:8]}", VSPHERE_VM_NAME_LIMIT
    )


def _build_restore_target_path(original_datastore_path: str, task_id: str) -> str:
    datastore_name, folder, filename = _parse_datastore_path(original_datastore_path)
    source_path = Path(filename)
    extension = source_path.suffix or ".vmdk"
    restored_name = _name_with_suffix(
        source_path.stem,
        f"-restored-{task_id[:8]}{extension}",
        RESTORED_DISK_FILE_NAME_LIMIT,
    )
    relative = f"{folder}/{restored_name}" if folder else restored_name
    return f"[{datastore_name}] {relative}"


def _build_new_vm_disk_target_path(
    datastore_name: str,
    vm_name: str,
    source_path: str,
    index: int,
) -> str:
    source_name = Path(source_path).name or f"disk-{index}.vmdk"
    safe_vm_name = _sanitize_name(vm_name)
    return f"[{datastore_name}] {safe_vm_name}/{index:02d}-{source_name}"


def _build_disk_layout(disk_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    layout: list[dict[str, Any]] = []
    for index, disk in enumerate(disk_entries, start=1):
        source_path = str(disk.get("datastore_path") or disk.get("destination_path") or "")
        source_name = Path(source_path).name or f"disk-{index}.vmdk"
        capacity_value = disk.get("capacity_gb")
        try:
            capacity_gb = float(capacity_value) if capacity_value not in (None, "") else 1.0
        except (TypeError, ValueError):
            capacity_gb = 1.0
        layout.append(
            {
                "label": str(disk.get("disk_label") or f"Hard disk {index}"),
                "capacity_gb": max(capacity_gb, 1.0),
                "file_name": _build_disk_file_name(index, source_name),
                "disk_uuid": disk.get("uuid"),
            }
        )
    return layout


def _disk_capacity_gb(disk_entry: dict[str, Any]) -> float:
    capacity_value = disk_entry.get("capacity_gb")
    try:
        capacity_gb = float(capacity_value) if capacity_value not in (None, "") else 1.0
    except (TypeError, ValueError):
        capacity_gb = 1.0
    return max(capacity_gb, 1.0)


def _abr_backup_files_size_bytes(abr_path: str, abr_backup_name: str) -> int:
    """Best-effort on-disk size of one local .abr backup's files (written
    directly under the repository folder as <name>.abri/.abrd/.abrm -- see
    storage_tool_common_args). Used to approximate bytes READ from the
    repository during restore; S3 has no cheap equivalent, so callers fall
    back to the target disk's capacity there instead (mirroring the backup
    side's fallback in run_vcenter_backup). Never raises."""
    total = 0
    try:
        for entry in Path(abr_path).glob(f"{abr_backup_name}.*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _load_manifest(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if not path.exists():
        raise VCenterBackupError(f"Backup manifest not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VCenterBackupError(f"Backup manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise VCenterBackupError(f"Backup manifest must contain a JSON object: {path}")
    return payload


def _derive_manifest_datastore_name(disk_entries: list[dict[str, Any]]) -> str:
    for disk in disk_entries:
        datastore_path = str(disk.get("datastore_path") or "").strip()
        if datastore_path:
            datastore_name, _, _ = _parse_datastore_path(datastore_path)
            if datastore_name:
                return datastore_name
    return ""


def _resolve_manifest_disk_entry_for_resource(
    manifest: dict[str, Any],
    *,
    resource_payload: dict[str, Any],
) -> dict[str, Any]:
    disks = manifest.get("disks")
    if not isinstance(disks, list) or not disks:
        raise VCenterBackupError("Backup manifest does not contain any disks")

    uuid = str(resource_payload.get("uuid") or "").strip()
    if uuid:
        for entry in disks:
            if isinstance(entry, dict) and str(entry.get("uuid") or "").strip() == uuid:
                return entry

    datastore_path = str(resource_payload.get("datastore_path") or "").strip()
    if datastore_path:
        for entry in disks:
            if isinstance(entry, dict) and str(entry.get("datastore_path") or "").strip() == datastore_path:
                return entry

    if len(disks) == 1 and isinstance(disks[0], dict):
        return disks[0]

    raise VCenterBackupError(
        "Backup manifest does not contain a matching disk entry for the selected disk resource"
    )


def _resolve_task_repository(task: dict[str, Any]) -> dict[str, Any] | None:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    repository_id = str(parameters.get("repository_id") or "").strip()
    if repository_id and repository_id in REPOSITORIES:
        repository = REPOSITORIES.get(repository_id)
        if isinstance(repository, dict):
            return repository
    return None


def _backup_point_by_id(point_id: str) -> dict[str, Any] | None:
    point = BACKUPS.get(point_id)
    if isinstance(point, dict):
        return point
    return next(
        (
            candidate
            for candidate in catalog_records(BACKUPS)
            if isinstance(candidate, dict) and str(candidate.get("id") or "") == point_id
        ),
        None,
    )


def _disk_storage_mode(disk_entry: dict[str, Any], repository: dict[str, Any] | None) -> str:
    mode = str(disk_entry.get("storage_mode") or "").strip().lower()
    if mode:
        return mode
    repository_type = str((repository or {}).get("type") or "").strip().lower()
    return "s3" if repository_type == "s3" else "local"


def _resolve_point_chain(selected_point: dict[str, Any]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current = selected_point
    seen: set[str] = set()
    while isinstance(current, dict):
        point_id = str(current.get("id") or "").strip()
        if not point_id:
            raise VCenterBackupError("Backup chain contains a point without an id")
        if point_id in seen:
            raise VCenterBackupError(f"Backup chain contains a cycle at restore point {point_id}")
        seen.add(point_id)
        chain.append(current)
        parent_id = str(current.get("parent_point_id") or "").strip()
        if not parent_id:
            break
        parent = _backup_point_by_id(parent_id)
        if parent is None:
            raise VCenterBackupError(
                f"Backup chain is incomplete: missing parent restore point {parent_id} for {point_id}"
            )
        current = parent
    chain.reverse()
    return chain


def _manifest_path_for_point(point: dict[str, Any]) -> str:
    artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
    manifest_path = str(artifact.get("manifest_path") or "").strip()
    if not manifest_path:
        raise VCenterBackupError(f"Restore point {point.get('id')} is missing manifest_path metadata")
    return manifest_path


def _load_manifest_for_point(point: dict[str, Any]) -> dict[str, Any]:
    return _load_manifest(_manifest_path_for_point(point))


def _resolve_chain_disk_entry(
    manifest: dict[str, Any],
    *,
    selected_disk: dict[str, Any],
    disk_index: int,
) -> dict[str, Any] | None:
    """Find this disk's entry in an older manifest along the restore chain.

    Returns ``None`` (rather than raising) when the manifest simply predates
    the disk -- e.g. a disk added to the VM partway through the backup
    chain, present in the newer full backup but absent from earlier points.
    The caller treats that as "this point doesn't contribute to this disk's
    restore" rather than a corrupt chain.
    """
    disks = manifest.get("disks")
    if not isinstance(disks, list) or not disks:
        return None

    selected_uuid = str(selected_disk.get("uuid") or "").strip()
    if selected_uuid and selected_uuid.lower() not in _PLACEHOLDER_DISK_UUIDS:
        for entry in disks:
            if isinstance(entry, dict) and str(entry.get("uuid") or "").strip() == selected_uuid:
                return entry

    selected_datastore_path = str(selected_disk.get("datastore_path") or "").strip()
    if selected_datastore_path:
        for entry in disks:
            if isinstance(entry, dict) and str(entry.get("datastore_path") or "").strip() == selected_datastore_path:
                return entry

    for entry in disks:
        if isinstance(entry, dict) and int(entry.get("index") or 0) == disk_index:
            return entry

    if len(disks) == 1 and isinstance(disks[0], dict):
        return disks[0]

    return None


def _resolve_abr_restore_layers(
    *,
    selected_point: dict[str, Any],
    selected_disk: dict[str, Any],
    disk_index: int,
    repository: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve the full -> incremental chain of .abr layers to replay.

    Works for both local-file and S3 repositories; the mover applies each layer
    in order onto the same restore target.
    """
    repository_type = str((repository or {}).get("type") or "").strip().lower()
    layers: list[dict[str, Any]] = []
    for point in _resolve_point_chain(selected_point):
        manifest = _load_manifest_for_point(point)
        disk_entry = _resolve_chain_disk_entry(
            manifest,
            selected_disk=selected_disk,
            disk_index=disk_index,
        )
        if disk_entry is None:
            continue
        store_mode = str(disk_entry.get("store_mode") or "").strip().lower()
        if bool(disk_entry.get("query_changed_areas_only")) or store_mode in {"query-only", "metadata-fallback"}:
            raise VCenterBackupError(
                f"Restore point {point.get('id')} only contains metadata for disk index {disk_index} and cannot be replayed"
            )

        abr_path = str(disk_entry.get("abr_path") or "").strip()
        abr_backup_name = str(disk_entry.get("abr_backup_name") or "").strip()
        if not abr_path or not abr_backup_name:
            raise VCenterBackupError(
                f"Restore point {point.get('id')} is missing .abr artifact metadata for disk index {disk_index}"
            )

        if repository_type == "s3":
            source = build_s3_location(
                str(extract_s3_config(repository).get("bucket") or ""),
                f"{abr_path}/{abr_backup_name}",
            )
        else:
            source = f"{abr_path}/{abr_backup_name}"

        if store_mode != "incremental":
            layers = []

        layers.append(
            {
                "point_id": str(point.get("id") or ""),
                "manifest_path": _manifest_path_for_point(point),
                "abr_path": abr_path,
                "abr_backup_name": abr_backup_name,
                "store_mode": "incremental" if store_mode == "incremental" else "full",
                "source": source,
            }
        )
    return layers


def _build_abr_restore_command(
    *,
    tool_path: Path,
    repository: dict[str, Any],
    abr_path: str,
    backup_name: str,
    remote_datastore_path: str,
    host: str,
    port: int,
    username: str,
    password: str,
    thumbprint: str,
    vm_moref: str | None,
    transport_mode: str | None,
    encryption_key: str = "",
    force: bool = False,
) -> list[str]:
    command = [
        str(tool_path),
        "restore",
        "--name",
        backup_name,
        *disk_tool_storage_args(repository, abr_path=abr_path),
    ]
    if force:
        command.append("--force")
    if encryption_key:
        command.extend(["--key", encryption_key])
    command.extend(
        _build_vddk_args(
            vmdk_path=remote_datastore_path,
            host=host,
            port=port,
            username=username,
            password=password,
            thumbprint=thumbprint,
            vm_spec=vm_moref,
            transport_mode=transport_mode,
        )
    )
    return command


def _run_restore_layer(command: list[str], **kwargs: Any):
    try:
        return run_capped(command, **kwargs)
    except ProcessCancelled as exc:
        raise TaskStopRequested("Session stop requested") from exc


def _redact_restore_command(command: list[str]) -> list[str]:
    return redact_disk_tool_command(command)


def _normalize_restore_disk_type(value: Any, field_name: str) -> str | int:
    try:
        return normalize_disk_type_selection(value)
    except ValueError as exc:
        raise VCenterBackupError(f"{field_name} {exc}") from exc


def _resolve_source_disk_type_code(disk_entry: dict[str, Any], field_name: str) -> int:
    source_code = disk_entry.get("source_disk_type_code")
    if source_code in (None, ""):
        raise VCenterBackupError(f"{field_name} requires stored source_disk_type_code metadata")
    return _normalize_restore_disk_type(source_code, field_name)  # type: ignore[return-value]


def _resolve_requested_disk_type(
    *,
    parameters: dict[str, Any],
    disk_entry: dict[str, Any],
    disk_index: int | None = None,
) -> tuple[str | int, int]:
    has_global_choice = "disk_type" in parameters and parameters.get("disk_type") not in (None, "")
    requested = parameters.get("disk_type") if has_global_choice else None
    disk_types = parameters.get("disk_types")
    if disk_index is not None and isinstance(disk_types, dict):
        override = disk_types.get(str(disk_index))
        if override is not None:
            requested = override

    if requested is None:
        return "original", _resolve_source_disk_type_code(disk_entry, "default disk type")

    normalized = _normalize_restore_disk_type(requested, "parameters.disk_type")
    if normalized == "original":
        return normalized, _resolve_source_disk_type_code(disk_entry, "parameters.disk_type='original'")
    return normalized, normalized


def _disk_type_details(
    requested: str | int,
    resolved_code: int,
) -> dict[str, Any]:
    return {
        "requested_disk_type": requested,
        "resolved_disk_type_code": resolved_code,
        "resolved_disk_type_name": disk_type_name_from_code(resolved_code),
    }


def run_vcenter_restore(
    task: dict[str, Any],
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    start_time = time.time()

    try:
        from .vcenter import VCenterNode
    except ModuleNotFoundError as exc:
        logger.error("Missing dependency for restore: %s", exc)
        if exc.name in {"pyVim", "pyVmomi"}:
            raise VCenterBackupError(
                "vCenter restore support requires pyVmomi/pyVim to be installed"
            ) from exc
        raise

    hypervisor = _get_hypervisor(task)
    if task.get("source_resource_available", True):
        resource = _get_resource(task)
    else:
        resource = {
            "resource_type": task.get("resource_type"),
            "external_id": task.get("resource_external_id"),
            "name": None,
        }

    logger.info(
        "Starting vCenter restore task %s for hypervisor=%s resource=%s/%s",
        task.get("id"),
        task.get("hypervisor_id"),
        resource.get("resource_type"),
        resource.get("external_id"),
    )

    parameters = task.get("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}
    encryption_key = str(parameters.get("encryption_key") or "").strip()
    task_id = str(task.get("id") or "")
    selected_point_id = str(parameters.get("point_id") or "").strip()
    selected_point = _backup_point_by_id(selected_point_id) if selected_point_id else None
    repository = _resolve_task_repository(task)
    repository_type = str((repository or {}).get("type") or "").strip().lower()
    # Every repository now uses the .abr format. With no repository record,
    # treat it as local-file; abr_path in each manifest is an absolute path so
    # the file backend can locate the data. S3-backed disks still require an
    # explicit S3 repository (guarded below).
    if repository is None:
        repository = {"type": "local", "id": ""}
        repository_type = "local"

    metadata = hypervisor.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    credentials = metadata.get("credentials", {})
    if not isinstance(credentials, dict):
        credentials = {}

    host = str(metadata.get("host") or "")
    port = int(metadata.get("port", 443))
    verify_ssl = bool(metadata.get("verify_ssl", False))
    username = str(credentials.get("username") or "")
    password = str(credentials.get("password") or "")
    if not host or not username or not password:
        raise VCenterBackupError("Saved vCenter registration is missing host or credentials")

    tool_path = Path(str(parameters.get("tool_path") or _default_tool_path())).expanduser()
    logger.debug(
        "Restoring with host=%s port=%s verify_ssl=%s tool=%s",
        host,
        port,
        verify_ssl,
        tool_path,
    )
    if not tool_path.exists():
        raise VCenterBackupError(f"Restore tool not found: {tool_path}")

    transport_mode_value = parameters.get("transport_mode")
    if transport_mode_value is None or transport_mode_value == "":
        transport_mode = "nbdssl"
    else:
        requested_transport_mode = str(transport_mode_value).strip()
        transport_mode = None if requested_transport_mode.lower() in {"auto", "none", "omit"} else requested_transport_mode

    resource_type = str(resource.get("resource_type") or "")
    if resource_type == "virtual_machine":
        is_instant_recovery = bool(parameters.get("instant_recovery"))
        is_in_place = str(parameters.get("restore_mode") or "").strip().lower() == "in_place"
        if is_in_place and is_instant_recovery:
            raise VCenterBackupError("restore_mode 'in_place' is not compatible with instant_recovery")
        recovery_label = "In-place VM restore" if is_in_place else ("Instant VM recovery" if is_instant_recovery else "VM restore")
        restore_mode = "in-place" if is_in_place else ("instant-vm" if is_instant_recovery else "new-vm")

        manifest_path_value = parameters.get("manifest_path")
        if not manifest_path_value:
            raise VCenterBackupError(f"{recovery_label} requires parameters.manifest_path")
        manifest = _load_manifest(str(manifest_path_value))
        vm_config = manifest.get("vm_config", {})
        if not isinstance(vm_config, dict):
            raise VCenterBackupError("Backup manifest is missing vm_config")
        vm_config = dict(vm_config)
        source_firmware, requested_firmware = _resolve_restore_firmware(vm_config)
        firmware_override = str(parameters.get("firmware") or "").strip().lower()
        if firmware_override:
            if firmware_override not in {"bios", "efi"}:
                raise VCenterBackupError(f"Unsupported restore firmware override: {firmware_override!r}")
            requested_firmware = firmware_override
        vm_config["firmware"] = requested_firmware
        disk_entries = manifest.get("disks", [])
        if not isinstance(disk_entries, list) or not disk_entries:
            raise VCenterBackupError("Backup manifest does not contain any disks")

        network_map = parameters.get("network_map")
        connect_nics = bool(parameters.get("connect_nics", True))
        if isinstance(network_map, dict) or not connect_nics:
            remapped_adapters: list[dict[str, Any]] = []
            for adapter in vm_config.get("network_adapters", []) or []:
                if not isinstance(adapter, dict):
                    continue
                adapter_copy = dict(adapter)
                source_network = str(adapter_copy.get("network") or "")
                if isinstance(network_map, dict) and source_network and source_network in network_map:
                    adapter_copy["network"] = str(network_map[source_network])
                if not connect_nics:
                    adapter_copy["connected"] = False
                remapped_adapters.append(adapter_copy)
            vm_config["network_adapters"] = remapped_adapters

        source_vm_name = str(manifest.get("vm_name") or resource.get("name") or "")
        logger.debug(
            "Loaded VM restore manifest %s for VM %s with %d disk entries",
            manifest_path_value,
            source_vm_name,
            len(disk_entries),
        )
        logger.info(
            "Loaded restore firmware metadata for VM %s: source firmware=%s requested firmware=%s",
            source_vm_name,
            source_firmware or "missing",
            requested_firmware,
        )
        default_target_vm_name = f"{source_vm_name}-ir" if is_instant_recovery else f"{source_vm_name}-restored"
        target_vm_name = str(parameters.get("target_vm_name") or default_target_vm_name).strip()
        target_host_name = str(parameters.get("target_host_name") or vm_config.get("source_host") or "").strip()
        target_datastore_name = str(
            parameters.get("target_datastore_name") or _derive_manifest_datastore_name(disk_entries)
        ).strip()
        logger.info(
            "Preparing %s from %s to target VM %s on host %s datastore %s",
            recovery_label.lower(),
            source_vm_name,
            target_vm_name,
            target_host_name,
            target_datastore_name,
        )
        if not target_vm_name or not target_host_name or not target_datastore_name:
            raise VCenterBackupError(
                f"{recovery_label} requires target_vm_name, target_host_name, and target_datastore_name"
            )

        # In-place restore builds a fully separate, fully verified VM under a
        # staging name first — target_vm_name is the ORIGINAL VM's own name
        # (abr_v41 resolves/forces it that way for in-place), so the shell and
        # every disk write must land under a different name, in its own
        # datastore folder, never touching the still-live original until the
        # swap below. For every other restore mode create_vm_name is simply
        # target_vm_name — zero behavior change.
        create_vm_name = (
            _build_staging_vm_name(target_vm_name, str(task.get("id") or ""))
            if is_in_place
            else target_vm_name
        )

        _emit_progress(
            progress_callback,
            "prepare",
            5,
            f"Preparing {recovery_label} for {target_vm_name}",
            worker_mode="vcenter-abr-vmdk-tool",
            source_vm_name=source_vm_name,
            target_vm_name=target_vm_name,
            target_host_name=target_host_name,
            target_datastore_name=target_datastore_name,
        )

        with VCenterNode(host, username, password, port=port, verify_ssl=verify_ssl) as node, node.session_keepalive():
            service_type = node.get_service_type()
            is_virtual_center = service_type == "VirtualCenter"
            for index, disk in enumerate(disk_entries, start=1):
                if not isinstance(disk, dict):
                    raise VCenterBackupError("Backup manifest contains an invalid disk entry")
                if _disk_storage_mode(disk, repository) == "s3" and repository_type != "s3":
                    raise VCenterBackupError("Restore point requires an S3 repository selection")
                if selected_point is None:
                    raise VCenterBackupError("Restore session is missing point_id for .abr restore")
                _resolve_abr_restore_layers(
                    selected_point=selected_point,
                    selected_disk=disk,
                    disk_index=index,
                    repository=repository,
                )
                _resolve_requested_disk_type(
                    parameters=parameters,
                    disk_entry=disk,
                    disk_index=index,
                )

            disk_layout = _build_disk_layout(disk_entries) if is_virtual_center else None
            created_vm = node.create_vm_shell(
                vm_name=create_vm_name,
                host_name=target_host_name,
                datastore_name=target_datastore_name,
                config_metadata=vm_config,
                disk_layout=disk_layout,
            )
            logger.info(
                "Created VM shell %s on host %s datastore %s with resulting firmware %s, "
                "SCSI controller type %s, Secure Boot requested=%s",
                create_vm_name,
                target_host_name,
                target_datastore_name,
                created_vm.get("restored_firmware") or "unknown",
                created_vm.get("requested_scsi_controller_type") or "unknown",
                created_vm.get("requested_secure_boot"),
            )
            original_destroyed = False
            try:
                thumbprint = node.get_thumbprint()
                created_disk_paths = created_vm.get("disk_paths", [])
                if not isinstance(created_disk_paths, list):
                    created_disk_paths = []

                restored_disks: list[dict[str, Any]] = []
                # Best-effort byte counters surfaced to the Activity page's Read/Sent
                # columns, mirroring run_vcenter_backup's approach in reverse: READ is
                # the .abr layer files' actual size in the repository (or, for S3 where
                # that's not cheap to measure, the target capacity estimate), and SENT
                # is the full decompressed size written to the datastore.
                total_read_bytes = 0
                total_sent_bytes = 0
                for index, disk in enumerate(disk_entries, start=1):
                    disk_start_time = time.time()
                    if not isinstance(disk, dict):
                        raise VCenterBackupError("Backup manifest contains an invalid disk entry")
                    if _disk_storage_mode(disk, repository) == "s3" and repository_type != "s3":
                        raise VCenterBackupError("Restore point requires an S3 repository selection")
                    if selected_point is None:
                        raise VCenterBackupError("Restore session is missing point_id for .abr restore")
                    abr_layers = _resolve_abr_restore_layers(
                        selected_point=selected_point,
                        selected_disk=disk,
                        disk_index=index,
                        repository=repository,
                    )
                    disk_capacity_gb = _disk_capacity_gb(disk)
                    # What THIS disk contributes, kept separate from the running
                    # totals so the per-layer reporter below can interpolate
                    # across it live. The totals are banked only once the disk is
                    # actually written (see "Successfully restored disk") --
                    # adding them up front made both counters jump to their
                    # finished value before a single block moved, which left the
                    # console with two samples for the whole run and so no rate
                    # to derive: the restore showed no throughput at all.
                    base_read_bytes = total_read_bytes
                    base_sent_bytes = total_sent_bytes
                    disk_sent_bytes = int(disk_capacity_gb * 1024**3)
                    disk_read_bytes = 0
                    if abr_layers:
                        if repository_type != "s3":
                            disk_read_bytes = sum(
                                _abr_backup_files_size_bytes(str(layer["abr_path"]), str(layer["abr_backup_name"]))
                                for layer in abr_layers
                            )
                        else:
                            disk_read_bytes = int(disk_capacity_gb * 1024**3)
                    elif legacy_backup_path is not None:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
                        try:
                            disk_read_bytes = legacy_backup_path.stat().st_size  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
                        except OSError:
                            disk_read_bytes = int(disk_capacity_gb * 1024**3)

                    source_datastore_path = str(disk.get("datastore_path") or "")
                    restore_target = (
                        str(created_disk_paths[index - 1])
                        if is_virtual_center and len(created_disk_paths) >= index
                        else _build_new_vm_disk_target_path(
                            target_datastore_name,
                            create_vm_name,
                            source_datastore_path or f"disk-{index}.vmdk",
                            index,
                        )
                    )
                    logger.info(
                        "Restoring VM disk %d/%d from %s to %s",
                        index,
                        len(disk_entries),
                        abr_layers[-1]["source"] if abr_layers else "(none)",
                        restore_target,
                    )
                    requested_disk_type, resolved_disk_type_code = _resolve_requested_disk_type(
                        parameters=parameters,
                        disk_entry=disk,
                        disk_index=index,
                    )

                    attachment: dict[str, Any] | None = None
                    if not is_virtual_center:
                        node.create_managed_disk(
                            datastore_name=target_datastore_name,
                            disk_path=restore_target,
                            capacity_gb=disk_capacity_gb,
                            disk_type_code=resolved_disk_type_code,
                        )
                        attachment = node.attach_existing_disk(
                            create_vm_name,
                            restore_target,
                            disk_label=str(disk.get("disk_label") or f"Hard disk {index}"),
                            disk_uuid=disk.get("uuid"),
                        )
                    progress = min(20 + int((index - 1) * 55 / max(len(disk_entries), 1)), 80)
                    progress_span = max(1, min(20 + int(index * 55 / max(len(disk_entries), 1)), 80) - progress)
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        progress,
                        f"Restoring disk {index}/{len(disk_entries)} to {restore_target}",
                        worker_mode="vcenter-abr-vmdk-tool",
                        current_disk=restore_target,
                        processed_bytes=base_read_bytes,
                        transferred_bytes=base_sent_bytes,
                    )
                    if abr_layers:
                        layer_count = len(abr_layers)
                        for layer_index, layer in enumerate(abr_layers, start=1):
                            # A disk's .abr chain restores as several sequential
                            # subprocess runs (one per full/incremental layer),
                            # each reporting its own independent 0-100%. Map
                            # every layer onto its own slice of the disk's
                            # [progress, progress+progress_span) band instead of
                            # the whole band — otherwise each new layer's tool
                            # process restarts at 0% and gets remapped back to
                            # the start of the same band the previous layer just
                            # finished, so the bar visibly snaps backward every
                            # time a layer completes and the next one begins.
                            layer_base = progress + int((layer_index - 1) * progress_span / layer_count)
                            layer_span = max(
                                1,
                                progress + int(layer_index * progress_span / layer_count) - layer_base,
                            )
                            # Split this disk's byte budget across its layers the
                            # same way its progress band is split, so the counters
                            # and the bar advance together instead of each layer
                            # replaying the whole disk's bytes from the start.
                            layer_read_base = base_read_bytes + int((layer_index - 1) * disk_read_bytes / layer_count)
                            layer_read_bytes = (
                                base_read_bytes + int(layer_index * disk_read_bytes / layer_count) - layer_read_base
                            )
                            layer_sent_base = base_sent_bytes + int((layer_index - 1) * disk_sent_bytes / layer_count)
                            layer_sent_bytes = (
                                base_sent_bytes + int(layer_index * disk_sent_bytes / layer_count) - layer_sent_base
                            )
                            command = _build_abr_restore_command(
                                tool_path=tool_path,
                                repository=repository,
                                abr_path=str(layer["abr_path"]),
                                backup_name=str(layer["abr_backup_name"]),
                                remote_datastore_path=restore_target,
                                host=host,
                                port=port,
                                username=username,
                                password=password,
                                thumbprint=thumbprint,
                                vm_moref=_format_vm_spec(str(created_vm["vm_moref"]), service_type),
                                transport_mode=transport_mode if is_virtual_center else None,
                                encryption_key=encryption_key,
                                force=str(layer.get("store_mode") or "").strip().lower() != "full",
                            )
                            logger.info(
                                ".abr restore command preview for task %s disk %d/%d layer %d/%d: %s",
                                task.get("id"),
                                index,
                                len(disk_entries),
                                layer_index,
                                len(abr_layers),
                                _redact_restore_command(command),
                            )
                            completed = _run_restore_layer(
                                command,
                                check=False,
                                capture_output=True,
                                text=True,
                                should_cancel=lambda: cancel_requested(task_id),
                                on_progress=_disk_tool_progress_reporter(
                                    progress_callback,
                                    base_progress=layer_base,
                                    progress_span=layer_span,
                                    message_prefix=f"Restoring disk {index}/{len(disk_entries)}",
                                    base_read_bytes=layer_read_base,
                                    disk_total_bytes=layer_read_bytes,
                                    base_sent_bytes=layer_sent_base,
                                    disk_sent_bytes=layer_sent_bytes,
                                ),
                            )
                            if completed.returncode != 0:
                                stderr = (completed.stderr or completed.stdout or "").strip()
                                logger.error(
                                    ".abr disk restore command failed for %s layer %s: %s",
                                    restore_target,
                                    layer.get("source"),
                                    stderr or f"exit code {completed.returncode}",
                                )
                                raise VCenterBackupError(
                                    f".abr disk restore failed to {restore_target} from {layer.get('source')}: "
                                    f"{stderr or f'exit code {completed.returncode}'}"
                                )
                    else:
                        raise VCenterBackupError(
                            f"No .abr restore layers resolved for disk index {index}"
                        )
                    disk_duration = time.time() - disk_start_time
                    logger.info("Successfully restored disk %d in %.2f seconds.", index, disk_duration)
                    total_read_bytes = base_read_bytes + disk_read_bytes
                    total_sent_bytes = base_sent_bytes + disk_sent_bytes

                    restored_disks.append(
                        {
                            "index": index,
                            "source_backup_path": abr_layers[-1]["source"],
                            "storage_mode": _disk_storage_mode(disk, repository),
                            "restore_layers": abr_layers,
                            "restored_datastore_path": restore_target,
                            "backup_mode": manifest.get("backup_mode"),
                            "source_disk_type_code": disk.get("source_disk_type_code"),
                            "source_disk_type_name": disk.get("source_disk_type_name"),
                            "attachment": attachment,
                            **_disk_type_details(requested_disk_type, resolved_disk_type_code),
                        }
                    )

                if is_in_place:
                    # Every disk is now fully written and verified on the
                    # staging VM — only now is it safe to touch the original.
                    # The original is never powered off or destroyed before
                    # this point, no matter what fails above.
                    _emit_progress(
                        progress_callback,
                        "finalize",
                        85,
                        f"Retiring original VM {target_vm_name} to complete in-place restore",
                        worker_mode="vcenter-abr-vmdk-tool",
                        target_vm_name=target_vm_name,
                    )
                    logger.info(
                        "In-place restore: all disks verified written to staging VM '%s'; retiring original VM '%s'.",
                        create_vm_name,
                        target_vm_name,
                    )
                    node.power_off_vm(target_vm_name)
                    node.destroy_vm(target_vm_name)
                    original_destroyed = True  # point of no return: the original is gone
                    node.rename_vm(create_vm_name, target_vm_name, vm_moref=str(created_vm.get("vm_moref") or ""))
                    try:
                        from ...services.abr_v41 import schedule_inventory_discovery

                        schedule_inventory_discovery(str(task["hypervisor_id"]), force=True)
                    except Exception:
                        logger.warning(
                            "Failed to schedule post-restore inventory rescan for hypervisor %s; "
                            "run `inventory rescan` manually so jobs pick up %s's new id.",
                            task.get("hypervisor_id"),
                            target_vm_name,
                            exc_info=True,
                        )

                power_state: dict[str, Any] | None = None
                if bool(parameters.get("power_on")):
                    _emit_progress(
                        progress_callback,
                        "finalize",
                        90,
                        f"Powering on recovered VM {target_vm_name}",
                        worker_mode="vcenter-abr-vmdk-tool",
                        target_vm_name=target_vm_name,
                    )
                    power_state = node.power_on_vm(
                        target_vm_name,
                        vm_moref=str(created_vm.get("vm_moref") or ""),
                    )
            except Exception as exc:
                if is_in_place and original_destroyed:
                    # Every disk was confirmed written and the original was
                    # already retired — only the rename (or the power-on
                    # after it) failed. The fully-restored VM survives intact
                    # under the staging name; nothing is lost, but it is NOT
                    # automatically renamed. Do not guess at remediation here
                    # (retrying blind against an unknown failure mode risks
                    # compounding it) — fail loudly and leave it for a human.
                    logger.critical(
                        "In-place restore for '%s' failed AFTER the original VM was destroyed but BEFORE "
                        "the rename completed. The fully-restored VM survives under the staging name '%s' "
                        "on host '%s' datastore '%s' with all data intact — it is NOT automatically renamed. "
                        "The original VM has already been destroyed and is not recoverable. "
                        "Manual intervention required: rename '%s' to '%s' in vCenter to restore normal operation.",
                        target_vm_name,
                        create_vm_name,
                        target_host_name,
                        target_datastore_name,
                        create_vm_name,
                        target_vm_name,
                    )
                    raise
                logger.warning(
                    "Restore %s for VM shell %s; destroying created VM and cleaning up.",
                    "stopped" if isinstance(exc, TaskStopRequested) else "failed",
                    create_vm_name,
                )
                try:
                    node.destroy_vm(create_vm_name, vm_moref=str(created_vm.get("vm_moref") or ""))
                except Exception:
                    logger.exception("Failed to destroy VM %s during restore cleanup.", create_vm_name)
                raise

        total_duration = time.time() - start_time
        logger.info(
            "%s completed for %s with %d restored disk(s) in %.2f seconds.",
            recovery_label,
            target_vm_name,
            len(restored_disks),
            total_duration,
        )
        return {
            "operation": "restore",
            "worker_mode": "vcenter-abr-vmdk-tool",
            "hypervisor_kind": "vcenter",
            "restore_mode": restore_mode,
            "source_vm_name": source_vm_name,
            "target_vm_name": target_vm_name,
            "target_host_name": target_host_name,
            "target_datastore_name": target_datastore_name,
            "cache_datastore_name": parameters.get("cache_datastore_name") if is_instant_recovery else None,
            "reason": parameters.get("reason"),
            "manifest_path": str(Path(str(manifest_path_value)).expanduser().resolve()),
            "vm_config": vm_config,
            "source_firmware": source_firmware,
            "requested_firmware": requested_firmware,
            "restored_firmware": created_vm.get("restored_firmware"),
            "requested_scsi_controller_type": created_vm.get("requested_scsi_controller_type"),
            "requested_secure_boot": created_vm.get("requested_secure_boot"),
            "disk_count": len(restored_disks),
            "disks": restored_disks,
            "disk_type": parameters.get("disk_type"),
            "disk_types": parameters.get("disk_types") if isinstance(parameters.get("disk_types"), dict) else None,
            "network_map": network_map if isinstance(network_map, dict) else None,
            "transport_mode": transport_mode,
            "service_type": service_type,
            "power_on": bool(parameters.get("power_on")),
            "power_state": power_state,
            "processed_bytes": total_read_bytes,
            "transferred_bytes": total_sent_bytes,
            "finished_at": utc_now(),
        }

    if resource_type != "disk":
        raise VCenterBackupError("vCenter restore currently supports disk resources only")

    payload = resource.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    vm_id = str(payload.get("vm_id") or "")
    original_datastore_path = str(payload.get("datastore_path") or "")
    if not vm_id or not original_datastore_path:
        raise VCenterBackupError("Selected disk resource is missing vm_id or datastore_path")

    vm_resource = _get_vm_resource(str(task["hypervisor_id"]), vm_id)
    vm_name = str(vm_resource.get("name") or "")
    if not vm_name:
        raise VCenterBackupError(f"Virtual machine name not found for {vm_id}")

    manifest_path_value = parameters.get("manifest_path")
    manifest_disk_entry: dict[str, Any] | None = None
    source_manifest_path = ""
    abr_layers: list[dict[str, Any]] = []
    if manifest_path_value:
        manifest = _load_manifest(str(manifest_path_value))
        manifest_disk_entry = _resolve_manifest_disk_entry_for_resource(
            manifest,
            resource_payload=payload,
        )
        if _disk_storage_mode(manifest_disk_entry, repository) == "s3" and repository_type != "s3":
            raise VCenterBackupError("Restore point requires an S3 repository selection")
        if selected_point is None:
            raise VCenterBackupError("Restore session is missing point_id for .abr restore")
        abr_layers = _resolve_abr_restore_layers(
            selected_point=selected_point,
            selected_disk=manifest_disk_entry,
            disk_index=int(manifest_disk_entry.get("index") or 1),
            repository=repository,
        )
        source_manifest_path = str(Path(str(manifest_path_value)).expanduser().resolve())
    else:
        # abr-vmdk-tool restores .abr backups only; there is no clone command
        # to push a bare VMDK at a datastore.
        raise VCenterBackupError(
            "Disk restore requires parameters.manifest_path; restoring from a plain "
            "parameters.backup_path VMDK is no longer supported"
        )

    disk_type_source = manifest_disk_entry if manifest_disk_entry is not None else payload
    requested_disk_type, resolved_disk_type_code = _resolve_requested_disk_type(
        parameters=parameters,
        disk_entry=disk_type_source,
    )
    logger.debug(
        "Resolved disk restore types: requested=%s resolved=%s",
        requested_disk_type,
        resolved_disk_type_code,
    )

    attach_to_vm = bool(parameters.get("attach_to_vm", True))
    restored_datastore_path = str(
        parameters.get("restored_datastore_path")
        or _build_restore_target_path(original_datastore_path, str(task["id"]))
    )

    logger.info(
        "Preparing disk restore for VM %s to datastore path %s",
        vm_name,
        restored_datastore_path,
    )
    _emit_progress(
        progress_callback,
        "prepare",
        5,
        f"Preparing VMware disk restore for {vm_name}",
        worker_mode="vcenter-abr-vmdk-tool",
        vm_name=vm_name,
        backup_path=str(abr_layers[-1]["source"]),
        restore_target=restored_datastore_path,
    )

    with VCenterNode(host, username, password, port=port, verify_ssl=verify_ssl) as node, node.session_keepalive():
        disk_start_time = time.time()
        service_type = node.get_service_type()
        is_virtual_center = service_type == "VirtualCenter"
        vm = node._get_vm(vm_name)
        if not vm:
            raise VCenterBackupError(f"VM '{vm_name}' not found on {host}.")
        vm_moref = vm._moId
        thumbprint = node.get_thumbprint()
        logger.debug("Found VM %s with MoRef %s", vm_name, vm_moref)

        restore_target_datastore_name, _, _ = _parse_datastore_path(restored_datastore_path)
        capacity_gb = _disk_capacity_gb(disk_type_source)
        node.create_managed_disk(
            datastore_name=restore_target_datastore_name,
            disk_path=restored_datastore_path,
            capacity_gb=capacity_gb,
            disk_type_code=resolved_disk_type_code,
        )

        try:
            attachment: dict[str, Any] | None = None
            # The VM spec stays set even for a detached restore: abr-vmdk-tool
            # needs --vm to open a VMDK through a host.
            restore_vm_spec = _format_vm_spec(vm_moref, service_type)
            if attach_to_vm:
                attachment = node.attach_existing_disk(
                    vm_name,
                    restored_datastore_path,
                    disk_uuid=(manifest_disk_entry or {}).get("uuid"),
                )

            # Best-effort byte counters (see the full-VM restore path above for the
            # same approach): READ is the .abr layer files' actual repository size
            # (or the capacity estimate for S3 / a direct backup_path clone), SENT
            # is the full decompressed size written to the datastore.
            sent_bytes = int(capacity_gb * 1024**3)
            if abr_layers:
                if repository_type != "s3":
                    read_bytes = sum(
                        _abr_backup_files_size_bytes(str(layer["abr_path"]), str(layer["abr_backup_name"]))
                        for layer in abr_layers
                    )
                else:
                    read_bytes = sent_bytes
            elif backup_path is not None:  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
                try:
                    read_bytes = backup_path.stat().st_size  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
                except OSError:
                    read_bytes = sent_bytes
            else:
                read_bytes = sent_bytes

            _emit_progress(
                progress_callback,
                "transfer",
                30,
                f"Restoring backup VMDK to {restored_datastore_path}",
                worker_mode="vcenter-abr-vmdk-tool",
                current_disk=restored_datastore_path,
                # Nothing has moved yet -- the per-layer reporter below interpolates
                # up to read_bytes/sent_bytes as the transfer runs. Reporting the
                # finished figures here left the console with no delta to derive a
                # rate from, so a disk restore showed no throughput.
                processed_bytes=0,
                transferred_bytes=0,
            )

            if abr_layers:
                # Same fix as the full-VM restore path above: a multi-layer .abr
                # chain runs as several sequential subprocesses, each reporting
                # its own independent 0-100% — give each layer its own slice of
                # the [30, 80) band instead of remapping every layer onto the
                # whole band, which snaps the bar back to 30% each time a layer
                # finishes and the next one starts.
                layer_count = len(abr_layers)
                for layer_index, layer in enumerate(abr_layers, start=1):
                    layer_base = 30 + int((layer_index - 1) * 50 / layer_count)
                    layer_span = max(1, 30 + int(layer_index * 50 / layer_count) - layer_base)
                    # Byte budget split across layers the same way the progress band
                    # is, so counters and bar advance together.
                    layer_read_base = int((layer_index - 1) * read_bytes / layer_count)
                    layer_read_bytes = int(layer_index * read_bytes / layer_count) - layer_read_base
                    layer_sent_base = int((layer_index - 1) * sent_bytes / layer_count)
                    layer_sent_bytes = int(layer_index * sent_bytes / layer_count) - layer_sent_base
                    command = _build_abr_restore_command(
                        tool_path=tool_path,
                        repository=repository,
                        abr_path=str(layer["abr_path"]),
                        backup_name=str(layer["abr_backup_name"]),
                        remote_datastore_path=restored_datastore_path,
                        host=host,
                        port=port,
                        username=username,
                        password=password,
                        thumbprint=thumbprint,
                        vm_moref=restore_vm_spec,
                        transport_mode=transport_mode if is_virtual_center else None,
                        encryption_key=encryption_key,
                        force=str(layer.get("store_mode") or "").strip().lower() != "full",
                    )
                    logger.info(
                        ".abr disk restore command preview for task %s layer %d/%d: %s",
                        task.get("id"),
                        layer_index,
                        len(abr_layers),
                        _redact_restore_command(command),
                    )
                    completed = _run_restore_layer(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        should_cancel=lambda: cancel_requested(task_id),
                        on_progress=_disk_tool_progress_reporter(
                            progress_callback,
                            base_progress=layer_base,
                            progress_span=layer_span,
                            message_prefix=f"Restoring backup VMDK to {restored_datastore_path}",
                            base_read_bytes=layer_read_base,
                            disk_total_bytes=layer_read_bytes,
                            base_sent_bytes=layer_sent_base,
                            disk_sent_bytes=layer_sent_bytes,
                        ),
                    )
                    if completed.returncode != 0:
                        stderr = (completed.stderr or completed.stdout or "").strip()
                        logger.error(
                            ".abr disk restore command failed for %s layer %s: %s",
                            restored_datastore_path,
                            layer.get("source"),
                            stderr or f"exit code {completed.returncode}",
                        )
                        raise VCenterBackupError(
                            f".abr disk restore failed to {restored_datastore_path} from {layer.get('source')}: "
                            f"{stderr or f'exit code {completed.returncode}'}"
                        )
            else:
                raise VCenterBackupError(
                    f"No .abr restore layers resolved for {restored_datastore_path}"
                )
            disk_duration = time.time() - disk_start_time
            logger.info("Disk cloning to %s completed in %.2f seconds.", restored_datastore_path, disk_duration)

            if attach_to_vm:
                logger.info("Attached restored disk %s to VM %s before clone", restored_datastore_path, vm_name)
                _emit_progress(
                    progress_callback,
                    "attach",
                    80,
                    f"Attached restored disk to {vm_name}",
                    worker_mode="vcenter-abr-vmdk-tool",
                    current_disk=restored_datastore_path,
                    processed_bytes=read_bytes,
                    transferred_bytes=sent_bytes,
                )
        except Exception:
            logger.warning(
                "Disk restore to %s did not finish; detaching and deleting the disk it created.",
                restored_datastore_path,
            )
            try:
                node.detach_disk(vm_name, restored_datastore_path)
                node.delete_managed_disk(restored_datastore_path)
            except Exception:
                logger.exception(
                    "Failed to remove the restored disk %s during cleanup.",
                    restored_datastore_path,
                )
            raise

        total_duration = time.time() - start_time
        logger.info(
            "Disk restore completed for VM %s attached=%s in %.2f seconds.",
            vm_name,
            attach_to_vm,
            total_duration
        )

        return {
            "operation": "restore",
            "worker_mode": "vcenter-abr-vmdk-tool",
            "hypervisor_kind": "vcenter",
            "vm_name": vm_name,
            "vm_external_id": vm_id,
            "source_backup_path": str(abr_layers[-1]["source"]),
            "storage_mode": _disk_storage_mode(disk_type_source, repository),
            "restore_layers": abr_layers if abr_layers else None,
            "manifest_path": source_manifest_path or None,
            "original_datastore_path": original_datastore_path,
            "restored_datastore_path": restored_datastore_path,
            "attached_to_vm": attach_to_vm,
            "attachment": attachment,
            "source_disk_type_code": disk_type_source.get("source_disk_type_code"),
            "source_disk_type_name": disk_type_source.get("source_disk_type_name"),
            **_disk_type_details(requested_disk_type, resolved_disk_type_code),
            "disk_type": requested_disk_type,
            "transport_mode": transport_mode,
            "service_type": service_type,
            "processed_bytes": read_bytes,
            "transferred_bytes": sent_bytes,
            "finished_at": utc_now(),
        }
