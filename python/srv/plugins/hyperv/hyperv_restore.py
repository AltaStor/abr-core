# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Restore a Hyper-V VM from a restore point, in place or as a new VM.

Out of place (``restore_mode`` "new_vm"): the disks are written as VHDX files
on the chosen host and storage, and a new VM with the backup's hardware,
firmware and network adapters (on the mapped virtual switches) is built around
them with create_vm_shell / attach_existing_disk -- the same interface the
vCenter plugin restores through. The source VM is not touched.

In place ("in_place"): the VM itself is kept -- its id and cluster role --
and its disks are replaced. The restored disks are written next to the
current ones under new names; once all are written the VM is turned off, set
back to the configuration the backup recorded, network adapters included, and
pointed at the new files, and only then are the files it used before deleted.
A VM with checkpoints is refused: replacing a disk under a checkpoint chain
would break the chain.

Each disk is written by abr-vhdx-tool on the target node, straight from the
repository: ``restore --vhdx`` creates a dynamic VHDX from the full backup and
applies the incrementals up to the restore point onto it, in order. A disk
asked for as fixed is converted on the host afterwards.

Everything is checked against the host before the first byte moves: the
target storage exists on the host and has room, the mapped virtual switches
exist, the VM name is free (or, in place, the VM is there and has no
checkpoints). A failure afterwards removes what the restore created and, in
place, points the VM back at its original disks.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from ...core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES, catalog_records
from ...core.server_defaults import env_int
from ...core.storage_tool import chain_source_names, is_s3_repository
from ...core.subprocess_util import ProcessCancelled
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.utils import utc_now
from ..vcenter.vcenter_backup import (
    VCenterBackupError,
    _disk_timeout_seconds,
    _disk_tool_progress_reporter,
    _emit_progress,
    _resolve_tool_timeout_seconds,
)
from ..vcenter.vcenter_restore import _resolve_abr_restore_layers
from . import vhdx_tool
from .hyperv import (
    RECONNECT_PAUSE_SECONDS,
    HyperVError,
    HyperVNode,
    HyperVUnreachableError,
    _slot_sort_key,
    reconnect_seconds,
)
from .hyperv_backup import WORKER_MODE, connection_settings, open_node

logger = logging.getLogger(__name__)

HYPERVISOR_KIND = "hyper-v"

ProgressCallback = Callable[[dict[str, object]], None] | None

_THIN_REQUESTS = {"thin", "dynamic", "thin-provisioned", "thin_provisioned"}
_THICK_REQUESTS = {"thick", "fixed", "thick_eager", "thick_lazy", "eagerzeroedthick", "lazyzeroedthick"}


class HyperVRestoreError(RuntimeError):
    pass


def _emit(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    _emit_progress(progress_callback, stage, progress, message, worker_mode=WORKER_MODE, **extra)


def _warn(progress_callback: ProgressCallback, stage: str, progress: int, message: str) -> None:
    logger.warning("%s", message)
    _emit(progress_callback, stage, progress, message, log_level="warn")


def _gib(value: int | float | None) -> str:
    return "unknown" if value is None else f"{float(value) / 1024**3:.2f} GiB"


def _load_manifest(parameters: dict[str, Any]) -> dict[str, Any]:
    from ...services.backup_catalog import _load_backup_manifest_payload

    point_id = str(parameters.get("point_id") or "")
    point = BACKUPS.get(point_id) if point_id else None
    point = point if isinstance(point, dict) else {"id": point_id, "repository_id": parameters.get("repository_id")}
    artifact = dict(point.get("artifact") or {}) if isinstance(point.get("artifact"), dict) else {}
    if parameters.get("manifest_path"):
        artifact["manifest_path"] = parameters["manifest_path"]
    manifest = _load_backup_manifest_payload(point, artifact)
    if not manifest:
        manifest_path = str(parameters.get("manifest_path") or artifact.get("manifest_path") or "")
        if manifest_path:
            try:
                manifest = json.loads(Path(manifest_path).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
    if not isinstance(manifest, dict) or not manifest:
        raise HyperVRestoreError(f"The manifest of restore point {point_id} cannot be read")
    if str(manifest.get("hypervisor_kind") or "") != HYPERVISOR_KIND:
        raise HyperVRestoreError(f"Restore point {point_id} was not written by a Hyper-V backup")
    return manifest


def _provisioning(disk: dict[str, Any], requested: str | None) -> str:
    value = str(requested or "").strip().lower()
    if value in _THIN_REQUESTS:
        return "dynamic"
    if value in _THICK_REQUESTS:
        return "fixed"
    return "fixed" if str(disk.get("vhd_type") or "").lower() == "fixed" else "dynamic"


def _restore_layers(
    point: dict[str, Any] | None, disk: dict[str, Any], index: int, repository: dict[str, Any]
) -> list[dict[str, Any]]:
    """The backups that make up this disk at the restore point -- its full
    backup, then each incremental up to the point -- with what each holds."""
    if point is None:
        layers = [
            {"abr_path": str(disk.get("abr_path") or ""), "abr_backup_name": str(disk.get("abr_backup_name") or "")}
        ]
    else:
        layers = _resolve_abr_restore_layers(
            selected_point=point, selected_disk=disk, disk_index=int(disk.get("index") or index), repository=repository
        )
    if not layers or not all(layer["abr_path"] and layer["abr_backup_name"] for layer in layers):
        raise HyperVRestoreError(f"The restore point records no stored backup for disk {disk.get('slot') or index}")
    for layer in layers:
        layer["logical_bytes"] = _layer_logical_bytes(layer)
    if point is None:
        layers[-1]["logical_bytes"] = int(disk.get("logical_bytes") or 0)
    return layers


def restored_estimate(
    disk: dict[str, Any], layers: list[dict[str, Any]], *, later_allocated: int = 0, source_allocated: int = 0
) -> tuple[int, str]:
    recorded = int(disk.get("allocated_bytes") or 0)
    if recorded:
        return recorded, "what the disk took when this backup was made"
    if later_allocated:
        return later_allocated, "what the disk took at a later backup of it"
    if source_allocated:
        return source_allocated, "what the source VM's disk takes now"
    bounds = [sum(int(layer.get("logical_bytes") or 0) for layer in layers), int(disk.get("capacity_bytes") or 0)]
    return min([value for value in bounds if value] or [0]), "the data of the chain's backups, at most the disk's capacity"


def restored_bytes(disk: dict[str, Any], layers: list[dict[str, Any]], source_allocated: int = 0) -> int:
    return restored_estimate(disk, layers, source_allocated=source_allocated)[0]


def later_allocated_bytes(point: dict[str, Any] | None, disk: dict[str, Any]) -> int:
    if not isinstance(point, dict) or not point.get("vm_id"):
        return 0
    created = str(point.get("created_at") or "")
    later = sorted(
        (
            record
            for record in catalog_records(BACKUPS)
            if str(record.get("vm_id") or "") == str(point["vm_id"]) and str(record.get("created_at") or "") > created
        ),
        key=lambda record: str(record.get("created_at") or ""),
    )
    for record in later:
        artifact = record.get("artifact") if isinstance(record.get("artifact"), dict) else {}
        size = source_allocated_bytes(disk, [item for item in artifact.get("disks") or [] if isinstance(item, dict)])
        if size:
            return size
    return 0


def source_vm_disks(
    point: dict[str, Any] | None, source_vm_id: str, target_inventory_id: str, node: HyperVNode
) -> list[dict[str, Any]]:
    source_inventory_id = str((point or {}).get("inventory_id") or "")
    if not source_inventory_id or source_inventory_id == target_inventory_id:
        try:
            return node.get_vm_disks(source_vm_id)
        except HyperVError:
            return []
    hypervisor = HYPERVISORS.get(source_inventory_id) or {}
    if hypervisor.get("kind") != HYPERVISOR_KIND:
        return []
    found: list[dict[str, Any]] = []

    def ask() -> None:
        try:
            with open_node(connection_settings(hypervisor)) as source:
                found.extend(source.get_vm_disks(source_vm_id))
        except Exception:
            logger.info("Could not ask inventory %s for the disks of VM %s", hypervisor.get("name"), source_vm_id, exc_info=True)

    worker = threading.Thread(target=ask, name="source-vm-disks", daemon=True)
    worker.start()
    worker.join(env_int("ALTASTOR_HYPERV_SOURCE_SIZE_SECONDS"))
    return list(found) if not worker.is_alive() else []


def source_allocated_bytes(disk: dict[str, Any], source_disks: list[dict[str, Any]]) -> int:
    identifier = str(disk.get("uuid") or "").lower()
    for live in source_disks:
        live_identifier = str(live.get("uuid") or "").lower()
        if identifier and live_identifier:
            if live_identifier == identifier:
                return int(live.get("allocated_bytes") or 0)
        elif str(live.get("slot") or "") == _disk_slot(disk):
            return int(live.get("allocated_bytes") or 0)
    return 0


def _layer_logical_bytes(layer: dict[str, Any]) -> int:
    """What one backup of the disk holds, from the manifest it was recorded in."""
    try:
        manifest = json.loads(Path(str(layer.get("manifest_path") or "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    for entry in manifest.get("disks") or []:
        if isinstance(entry, dict) and entry.get("abr_backup_name") == layer.get("abr_backup_name"):
            return int(entry.get("logical_bytes") or 0)
    return 0


def _restore_runs(layers: list[dict[str, Any]], repository: dict[str, Any]) -> list[dict[str, Any]]:
    names = [str(layer["abr_backup_name"]) for layer in layers]
    if len(layers) < 2 or any("," in name for name in names) or not vhdx_tool.restores_chains():
        return layers
    source_path, source_names = chain_source_names([str(layer["abr_path"]) for layer in layers], names)
    if not is_s3_repository(repository):
        if not source_path:
            return layers
        source_names = [name.replace("/", "\\") for name in source_names]
    return [
        {
            "abr_path": source_path,
            "abr_backup_name": f"{names[0]} and {len(layers) - 1} incremental(s) on it",
            "source_names": source_names,
        }
    ]


def _source_datastore(path: str) -> str:
    from ...services.abr_v41 import _parse_datastore_path_parts

    datastore, _folder, _name = _parse_datastore_path_parts(path)
    return str(datastore or "")


def vm_base_folder(datastore: str) -> str:
    """Where a restored VM's folder goes on a target datastore: under
    Hyper-V on a drive, at the root of a Cluster Shared Volume or share."""
    root = datastore.rstrip("\\")
    if len(root) == 2 and root.endswith(":"):
        return f"{root}\\Hyper-V"
    return root


def _disk_slot(disk: dict[str, Any]) -> str:
    return str(disk.get("slot") or disk.get("disk_label") or "")


def _disk_placements(parameters: dict[str, Any], manifest_disks: list[dict[str, Any]]) -> dict[str, str]:
    placements: dict[str, str] = {}
    entries = parameters.get("disk_datastore_map")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        position = entry.get("index")
        target = str(entry.get("target_datastore") or "").strip()
        if not isinstance(position, int) or isinstance(position, bool) or not target:
            continue
        if 1 <= position <= len(manifest_disks):
            placements[_disk_slot(manifest_disks[position - 1])] = target
    return placements


def _disk_file_name(disk: dict[str, Any], index: int) -> str:
    name = PureWindowsPath(str(disk.get("datastore_path") or "")).name or f"disk-{index}.vhdx"
    stem = PureWindowsPath(name).stem
    return f"{stem}.vhdx"


def _adapter_targets(
    vm_config: dict[str, Any], parameters: dict[str, Any]
) -> list[dict[str, Any]]:
    """Each network adapter of the backup with the virtual switch it lands on."""
    adapters = [item for item in vm_config.get("network_adapters") or [] if isinstance(item, dict)]
    by_index = {
        int(entry.get("index")): str(entry.get("target_network") or "")
        for entry in parameters.get("network_adapter_map") or []
        if isinstance(entry, dict) and entry.get("index") is not None
    }
    by_name = parameters.get("network_map") if isinstance(parameters.get("network_map"), dict) else {}
    connect = parameters.get("connect_nics", True) is not False
    planned = []
    for index, adapter in enumerate(adapters):
        source = str(adapter.get("network") or "")
        target = by_index.get(index) or str(by_name.get(source) or "")
        planned.append(
            {
                **adapter,
                "source_network": source,
                "target_switch": target if connect else "",
            }
        )
    return planned


def run_hyperv_restore(
    task: dict[str, Any],
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    start_time = time.time()
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    hypervisor = HYPERVISORS.get(str(task.get("hypervisor_id") or "")) or {}
    if hypervisor.get("kind") != HYPERVISOR_KIND:
        raise HyperVRestoreError("A Hyper-V restore needs a Hyper-V target inventory")
    connection = connection_settings(hypervisor)
    manifest = _load_manifest(parameters)
    vm_config = manifest.get("vm_config") if isinstance(manifest.get("vm_config"), dict) else {}
    manifest_disks = [disk for disk in manifest.get("disks") or [] if isinstance(disk, dict)]
    disks = sorted(manifest_disks, key=lambda disk: _slot_sort_key(_disk_slot(disk)))
    if not disks:
        raise HyperVRestoreError("The restore point records no disks to restore")
    repository = REPOSITORIES.get(str(parameters.get("repository_id") or "")) or {"type": "local", "id": ""}
    try:
        repository = vhdx_tool.for_hyperv_host(repository, connection["host"])
        vhdx_tool.ensure_hyperv_repository(repository)
    except vhdx_tool.VhdxToolError as exc:
        raise HyperVRestoreError(str(exc)) from exc
    encryption_key = str(parameters.get("encryption_key") or "").strip()
    tool_timeout_seconds = _resolve_tool_timeout_seconds(parameters)

    mode = "in_place" if str(parameters.get("restore_mode") or "") == "in_place" else "new_vm"
    point_id = str(parameters.get("point_id") or "")
    point = BACKUPS.get(point_id) if point_id else None
    point_created = str((point or {}).get("created_at") or manifest.get("created_at") or "")
    source_vm_name = str(manifest.get("vm_name") or parameters.get("source_vm_name") or "")
    source_vm_id = str(manifest.get("vm_external_id") or "")
    target_vm_name = str(parameters.get("target_vm_name") or source_vm_name)
    requested_host = str(parameters.get("target_host_name") or parameters.get("target_host") or "")
    default_datastore = str(parameters.get("target_datastore_name") or "")
    datastore_map = parameters.get("datastore_map") if isinstance(parameters.get("datastore_map"), dict) else {}
    disk_placements = _disk_placements(parameters, manifest_disks)
    power_on = bool(parameters.get("power_on"))
    mode_label = "In-place" if mode == "in_place" else "Out-of-place"
    adapters = _adapter_targets(vm_config, parameters)

    logger.info(
        "Hyper-V restore session %s started: %s recovery of %s (%s) from restore point %s (%s, repository %s) "
        "to inventory %s, host %s, VM name %s",
        task_id,
        mode_label.lower(),
        source_vm_name,
        source_vm_id,
        point_id,
        point_created,
        repository.get("name") or repository.get("id"),
        hypervisor.get("name"),
        requested_host,
        target_vm_name,
    )
    _emit(
        progress_callback,
        "prepare",
        3,
        f"{mode_label} recovery of {source_vm_name} from restore point {point_id} taken {point_created}"
        + (
            f" as new VM {target_vm_name} on {requested_host}"
            if mode == "new_vm"
            else f", replacing the disks of {source_vm_name} where it runs"
        ),
    )
    if mode == "new_vm":
        mapping_text = ", ".join(f"{source} -> {target}" for source, target in sorted(datastore_map.items())) or (
            f"all disks -> {default_datastore}"
        )
        _emit(progress_callback, "prepare", 4, f"Datastore mapping: {mapping_text}")
        if disk_placements:
            placement_text = ", ".join(f"{slot} -> {target}" for slot, target in sorted(disk_placements.items()))
            _emit(progress_callback, "prepare", 4, f"Disk placement: {placement_text}")
        network_text = ", ".join(
            f"{adapter.get('label')} ({adapter['source_network'] or 'not connected'}) -> "
            f"{adapter['target_switch'] or 'not connected'}"
            for adapter in adapters
        ) or "the VM has no network adapters"
        _emit(progress_callback, "prepare", 5, f"Network mapping: {network_text}")

    warnings: list[str] = []
    written_files: list[str] = []
    created_folders: list[str] = []
    created_vm_id: str | None = None
    swapped: list[dict[str, Any]] = []
    node: HyperVNode | None = None
    node_name = ""
    restored: list[dict[str, Any]] = []
    total_read = 0
    total_sent = 0
    target_vm_id = ""

    try:
        with open_node(connection) as node:
            # ---- validate the target ------------------------------------
            in_place_state: dict[str, Any] = {}
            live_disks: dict[str, dict[str, Any]] = {}
            current_config: dict[str, Any] | None = None
            if mode == "in_place":
                try:
                    in_place_state = node.locate_vm(source_vm_id)
                except HyperVError as exc:
                    raise HyperVRestoreError(
                        f"In-place recovery needs {source_vm_name} ({source_vm_id}) where it was backed up: {exc}"
                    ) from exc
                node_name = str(in_place_state.get("computer") or "")
                checkpoints = int(in_place_state.get("checkpoint_count") or 0)
                if checkpoints:
                    raise HyperVRestoreError(
                        f"{source_vm_name} has {checkpoints} checkpoint(s); replacing its disks would break "
                        "the checkpoint chain. Delete its checkpoints, or restore it as a new VM"
                    )
                target_vm_id = source_vm_id
                target_vm_name = str(in_place_state.get("name") or source_vm_name)
                live_disks = {disk["slot"]: disk for disk in node.get_vm_disks(source_vm_id)}
                try:
                    current_config = node.get_vm_restore_metadata(source_vm_id)
                except HyperVError as exc:
                    logger.warning("Could not read the configuration of %s before the in-place restore: %s", target_vm_name, exc)
            else:
                try:
                    node_name = node.resolve_node(requested_host)
                except HyperVError as exc:
                    raise HyperVRestoreError(str(exc)) from exc
            resources = node.host_resources(node_name)
            _emit(
                progress_callback,
                "validate",
                7,
                f"Target host {node_name}: {len(resources['volumes'])} storage location(s), "
                f"{len(resources['switches'])} virtual switch(es)",
            )

            later_sizes = [
                0 if int(disk.get("allocated_bytes") or 0) else later_allocated_bytes(point, disk) for disk in disks
            ]
            source_disks = list(live_disks.values())
            if not source_disks and source_vm_id and not all(
                int(disk.get("allocated_bytes") or 0) or later for disk, later in zip(disks, later_sizes)
            ):
                source_disks = source_vm_disks(point, source_vm_id, str(hypervisor.get("id") or ""), node)

            plan = []
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            for index, disk in enumerate(disks, start=1):
                slot = str(disk.get("slot") or disk.get("disk_label") or "")
                provisioning = _provisioning(disk, parameters.get("disk_type"))
                layers = _restore_layers(point, disk, index, repository)
                if mode == "in_place":
                    current = live_disks.get(slot, {})
                    current_path = str(current.get("current_datastore_path") or "")
                    folder = str(PureWindowsPath(current_path or str(disk.get("datastore_path") or "")).parent)
                    target_path = f"{folder}\\{PureWindowsPath(_disk_file_name(disk, index)).stem}-restored-{stamp}.vhdx"
                    target_datastore = _source_datastore(target_path)
                else:
                    source_datastore = _source_datastore(str(disk.get("datastore_path") or ""))
                    target_datastore = str(
                        disk_placements.get(slot)
                        or datastore_map.get(source_datastore)
                        or default_datastore
                        or source_datastore
                    )
                    folder = f"{vm_base_folder(target_datastore)}\\{target_vm_name}\\Virtual Hard Disks"
                    target_path = f"{folder}\\{_disk_file_name(disk, index)}"
                    current_path = ""
                written, basis = restored_estimate(
                    disk,
                    layers,
                    later_allocated=later_sizes[index - 1],
                    source_allocated=source_allocated_bytes(disk, source_disks),
                )
                needed = int(disk.get("capacity_bytes") or 0) if provisioning == "fixed" else written
                _emit(
                    progress_callback,
                    "validate",
                    7,
                    f"Disk {slot} needs {_gib(needed)} on {target_datastore} ("
                    + ("its full capacity, as a fixed VHDX" if provisioning == "fixed" else basis)
                    + ")",
                )
                plan.append(
                    {
                        "index": index,
                        "slot": slot,
                        "disk": disk,
                        "layers": layers,
                        "written_bytes": written,
                        "provisioning": provisioning,
                        "datastore": target_datastore,
                        "folder": folder,
                        "path": target_path,
                        "previous_path": current_path,
                        "needed_bytes": needed,
                    }
                )

            # Storage: every target location is on this host and has room.
            volumes = {str(item.get("name") or "").lower(): item for item in resources["volumes"]}
            needs: dict[str, int] = {}
            for item in plan:
                needs[item["datastore"]] = needs.get(item["datastore"], 0) + item["needed_bytes"] + (
                    # A fixed disk is written dynamic first and converted, so
                    # both exist for a moment.
                    item["written_bytes"] if item["provisioning"] == "fixed" else 0
                )
            for datastore, needed in needs.items():
                volume = volumes.get(datastore.lower())
                if volume is None and datastore.startswith("\\\\"):
                    # An SMB share: the host does not report its free space.
                    _emit(progress_callback, "validate", 8, f"Storage location {datastore} is a share; its free space is not checked")
                    continue
                if volume is None:
                    raise HyperVRestoreError(
                        f"Storage location {datastore} does not exist on host {node_name}; it has "
                        + (", ".join(str(item.get("name")) for item in resources["volumes"]) or "none")
                    )
                free = int(volume.get("free") or 0)
                if free < needed:
                    raise HyperVRestoreError(
                        f"Storage location {datastore} on {node_name} has {_gib(free)} free, but the disks "
                        f"restored there need {_gib(needed)}"
                    )
                _emit(
                    progress_callback,
                    "validate",
                    8,
                    f"Storage location {datastore} on {node_name}: {_gib(free)} free, {_gib(needed)} needed",
                )

            if mode == "in_place":
                switches = {str(item.get("name") or "") for item in resources["switches"]}
                missing = sorted(
                    {
                        str(adapter.get("network") or "")
                        for adapter in vm_config.get("network_adapters") or []
                        if isinstance(adapter, dict) and str(adapter.get("network") or "") not in switches | {""}
                    }
                )
                if missing:
                    raise HyperVRestoreError(
                        f"Virtual switch(es) {', '.join(missing)} the VM was connected to do not exist on {node_name}; "
                        "it has " + (", ".join(sorted(switches)) or "none")
                    )
            if mode == "new_vm":
                if target_vm_name.lower() in {name.lower() for name in resources["vm_names"]}:
                    raise HyperVRestoreError(f"A VM named {target_vm_name} already exists on {node_name}")
                switches = {str(item.get("name") or "") for item in resources["switches"]}
                missing = sorted({a["target_switch"] for a in adapters if a["target_switch"] and a["target_switch"] not in switches})
                if missing:
                    raise HyperVRestoreError(
                        f"Virtual switch(es) {', '.join(missing)} do not exist on {node_name}; it has "
                        + (", ".join(sorted(switches)) or "none")
                    )
                unconnected = [a.get("label") for a in adapters if not a["target_switch"]]
                if unconnected:
                    message = (
                        f"Network adapter(s) {', '.join(str(label) for label in unconnected)} will not be connected "
                        "to a virtual switch"
                    )
                    _warn(progress_callback, "validate", 9, message)
                    warnings.append(message)
                if vm_config.get("tpm_enabled"):
                    message = (
                        "The VM had a virtual TPM. The restored VM gets a new one, so secrets sealed to the old "
                        "TPM (BitLocker keys, for one) have to be recovered with their recovery keys"
                    )
                    _warn(progress_callback, "validate", 9, message)
                    warnings.append(message)

            existing, created_folders = node.create_folders(
                node_name, sorted({item["folder"] for item in plan}), [item["path"] for item in plan]
            )
            if existing:
                raise HyperVRestoreError(
                    f"{', '.join(existing)} already exist(s) on {node_name}; the restore does not overwrite files"
                )
            _emit(progress_callback, "validate", 10, "Target checks passed")

            # ---- restore the disks --------------------------------------
            tool_path = node.ensure_disk_tool(node_name)
            _emit(progress_callback, "restore", 11, f"abr-vhdx-tool is ready on {node_name} at {tool_path}")
            for item in plan:
                disk = item["disk"]
                index = item["index"]
                slot = item["slot"]
                layers = item["layers"]
                progress = 12 + int((index - 1) * 70 / len(plan))
                span = max(2, int(70 / len(plan)))
                target = item["path"] if item["provisioning"] == "dynamic" else item["path"][:-5] + ".dynamic.vhdx"
                _emit(
                    progress_callback,
                    "restore",
                    progress,
                    f"Restoring disk {index}/{len(plan)} ({slot}) from repository {repository.get('name') or ''} "
                    f"to {target} on {node_name}: "
                    + ("full backup" if len(layers) == 1 else f"full backup and {len(layers) - 1} incremental(s)"),
                    processed_bytes=total_read,
                )
                written_files.append(target)
                runs = _restore_runs(layers, repository)
                for position, layer in enumerate(runs):
                    storage_args, storage_env = vhdx_tool.storage_invocation(repository, abr_path=layer["abr_path"])
                    command = [
                        "restore",
                        "--json",
                        *(
                            ["--source-names", ",".join(layer["source_names"])]
                            if layer.get("source_names")
                            else ["--name", layer["abr_backup_name"]]
                        ),
                        *storage_args,
                        "--vhdx",
                        target,
                    ]
                    if position:
                        # An incremental goes onto the disk its parent left.
                        command.append("--force")
                    if encryption_key:
                        command.extend(["--key", encryption_key])
                    layer_base = progress + int(position * span / len(runs))
                    report = _disk_tool_progress_reporter(
                        progress_callback,
                        base_progress=layer_base,
                        progress_span=max(1, int(span / len(runs))),
                        message_prefix=(
                            f"Restoring disk {index}/{len(plan)}"
                            if layer.get("source_names")
                            else f"Restoring disk {index}/{len(plan)}, backup {position + 1}/{len(runs)}"
                        ),
                        worker_mode=WORKER_MODE,
                    )
                    run = node.run_disk_tool(
                        node_name,
                        command,
                        env=storage_env,
                        on_progress=lambda percent, report=report: report(percent, 0),
                        should_cancel=lambda: cancel_requested(task_id),
                        timeout=_disk_timeout_seconds(
                            tool_timeout_seconds, int(disk.get("capacity_bytes") or 0) / 1024**3
                        ),
                        on_notice=lambda message, layer_base=layer_base: _warn(progress_callback, "transfer", layer_base, message),
                    )
                    result = run.get("result") or {}
                    if run.get("exit_code") != 0 or result.get("status") not in (None, "ok"):
                        if vhdx_tool.ended_unexpectedly(run.get("result"), run.get("lines") or [], run.get("exit_code")):
                            raise HyperVRestoreError(
                                vhdx_tool.unexpected_end_message(
                                    node_name, run["exit_code"], run.get("lines") or [], f"restoring disk {slot} of {target_vm_name}"
                                )
                            )
                        detail = vhdx_tool.failure_message(result, run.get("lines") or [], run.get("exit_code"))
                        raise HyperVRestoreError(
                            f"Restoring disk {slot} from {layer['abr_backup_name']} failed: {detail}"
                        )
                total_read += item["written_bytes"]
                total_sent += item["written_bytes"]
                if item["provisioning"] == "fixed":
                    _emit(
                        progress_callback,
                        "transfer",
                        progress + span - 1,
                        f"Converting disk {slot} to a fixed VHDX at {item['path']}",
                    )
                    node.convert_disk(node_name, target, item["path"], "Fixed")
                    written_files.remove(target)
                    written_files.append(item["path"])
                restored.append(
                    {
                        "slot": slot,
                        "path": item["path"],
                        "previous_path": item["previous_path"] or None,
                        "provisioning": item["provisioning"],
                        "capacity_bytes": int(disk.get("capacity_bytes") or 0),
                        "data_bytes": item["written_bytes"],
                        "datastore": item["datastore"],
                        "restored_from": [layer["abr_backup_name"] for layer in layers],
                    }
                )
                _emit(
                    progress_callback,
                    "transfer",
                    progress + span,
                    f"Disk {index}/{len(plan)} ({slot}) restored to {item['path']} ({item['provisioning']}, "
                    f"about {_gib(item['written_bytes'])})",
                    processed_bytes=total_read,
                    transferred_bytes=total_sent,
                )

            # ---- build or update the VM -------------------------------------
            # A generation 2 "Drive" boot entry is a DVD drive as often as a
            # hard disk; only a disk this restore wrote can be booted from.
            restored_slots = {str(item["slot"]).upper(): str(item["slot"]) for item in plan}
            boot_slot = next(
                (
                    restored_slots[str(entry.get("device") or "").upper()]
                    for entry in vm_config.get("boot_order") or []
                    if isinstance(entry, dict)
                    and entry.get("boot_type") == "Drive"
                    and str(entry.get("device") or "").upper() in restored_slots
                ),
                plan[0]["slot"],
            )
            if mode == "new_vm":
                primary = plan[0]["datastore"]
                _emit(
                    progress_callback,
                    "configure",
                    85,
                    f"Creating VM {target_vm_name} on {node_name} (generation {vm_config.get('generation')}, "
                    f"{vm_config.get('num_cpu')} vCPU, {vm_config.get('memory_mb')} MB)",
                )
                created = node.create_vm_shell(
                    vm_name=target_vm_name,
                    host_name=node_name,
                    datastore_name=vm_base_folder(primary),
                    config_metadata={**vm_config, "network_adapters": adapters},
                    disk_layout=[
                        {"controller_type": item["disk"].get("controller_type"), "controller_number": item["disk"].get("controller_number")}
                        for item in plan
                    ],
                )
                created_vm_id = str(created["vm_id"])
                target_vm_id = created_vm_id
                for item in plan:
                    node.attach_existing_disk(created_vm_id, item["path"], item["slot"])
                node.set_boot_disk(created_vm_id, boot_slot, vm_config.get("boot_order") or [])
            else:
                _emit(
                    progress_callback,
                    "configure",
                    85,
                    f"Turning off {target_vm_name} to replace its disks",
                )
                previous_state = node.power_off_vm(target_vm_id)
                logger.info("VM %s was %s before the in-place restore", target_vm_name, previous_state.get("previous_state"))
                changed = node.set_vm_hardware(target_vm_id, vm_config)
                if changed:
                    _emit(
                        progress_callback,
                        "configure",
                        86,
                        f"{target_vm_name} set back to the backup's " + ", ".join(_setting_change(item) for item in changed),
                    )
                swapped = node.replace_vm_disks(
                    target_vm_id,
                    [
                        {
                            "controller_type": item["disk"].get("controller_type"),
                            "controller_number": item["disk"].get("controller_number"),
                            "controller_location": item["disk"].get("controller_location"),
                            "path": item["path"],
                        }
                        for item in plan
                    ],
                )
                written_files = []
                replaced = [
                    str(entry.get("previous_path") or "")
                    for entry in swapped
                    if entry.get("previous_path") and str(entry.get("previous_path")).lower() != str(entry.get("path")).lower()
                ]
                _emit(
                    progress_callback,
                    "configure",
                    90,
                    f"{target_vm_name} now uses the restored disks; removing {len(replaced)} replaced disk file(s)",
                )
                # The swap is done: from here a failure must not point the VM
                # back at files that are being deleted.
                swapped = []
                failed = node.remove_files(node_name, replaced)
                if failed:
                    message = f"Could not delete the replaced disk file(s) {', '.join(failed)}; remove them by hand"
                    _warn(progress_callback, "configure", 91, message)
                    warnings.append(message)

            # The VM is complete; nothing below undoes it.
            created_vm_id = None
            written_files = []
            created_folders = []
            if mode == "in_place":
                _restore_adapters(node, target_vm_id, target_vm_name, vm_config, warnings, progress_callback)
                boot_restored = _restore_boot_order(
                    node, target_vm_id, target_vm_name, vm_config, restored_slots, warnings, progress_callback
                )
                kept = _kept_as_they_are(current_config, vm_config, boot_restored)
                if kept:
                    message = (
                        f"{target_vm_name} keeps what an in-place restore does not change, and it differs from the "
                        "backup: " + "; ".join(kept)
                    )
                    _warn(progress_callback, "configure", 93, message)
                    warnings.append(message)
            power_state = "Off"
            if power_on:
                _emit(progress_callback, "finalize", 94, f"Starting {target_vm_name}")
                try:
                    power_state = str(node.power_on_vm(target_vm_id).get("state") or "Running")
                except HyperVError as exc:
                    message = f"{target_vm_name} is restored but did not start: {exc}"
                    _warn(progress_callback, "finalize", 95, message)
                    warnings.append(message)

        try:
            from ...services.abr_v41 import schedule_inventory_discovery

            schedule_inventory_discovery(str(task["hypervisor_id"]), force=True)
        except Exception:
            logger.warning(
                "Failed to schedule the inventory rescan after restore session %s; run `abr inventory rescan` "
                "so the inventory shows %s",
                task_id,
                target_vm_name,
                exc_info=True,
            )
        duration = time.time() - start_time
        summary = (
            f"{mode_label} recovery of {source_vm_name} completed: {target_vm_name} on {node_name}, "
            f"{len(restored)} disk(s), about {_gib(total_read)}, in {duration:.1f}s"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )
        logger.info("Session %s: %s", task_id, summary)
        _emit(progress_callback, "finalize", 98, summary, processed_bytes=total_read, transferred_bytes=total_sent)
        return {
            "operation": "restore",
            "worker_mode": WORKER_MODE,
            "hypervisor_kind": HYPERVISOR_KIND,
            "restore_mode": mode,
            "restore_point_id": point_id,
            "restore_point_created_at": point_created,
            "identity_preserved": mode == "in_place",
            "source_vm_name": source_vm_name,
            "source_vm_id": source_vm_id,
            "target_vm_name": target_vm_name,
            "target_vm_id": target_vm_id,
            "target_host_name": node_name,
            "target_datastore_name": plan[0]["datastore"] if plan else default_datastore,
            "datastore_map": datastore_map or None,
            "disk_placements": disk_placements or None,
            "network_adapters": [
                {"label": a.get("label"), "source_network": a["source_network"], "target_switch": a["target_switch"]}
                for a in adapters
            ]
            if mode == "new_vm"
            else None,
            "disk_count": len(restored),
            "disks": restored,
            "vm_config": vm_config,
            "power_on": power_on,
            "power_state": power_state,
            "processed_bytes": total_read,
            "transferred_bytes": total_sent,
            "duration_seconds": round(duration, 3),
            "warnings": warnings,
            "finished_at": utc_now(),
        }
    except (ProcessCancelled, TaskStopRequested) as exc:
        logger.info("Hyper-V restore session %s stopped; undoing what it had created", task_id)
        _undo(
            connection, node_name, created_vm_id, swapped, written_files, created_folders, target_vm_id, progress_callback
        )
        raise TaskStopRequested("Session stop requested; the restore was undone") from exc
    except Exception as exc:
        logger.exception(
            "Hyper-V restore session %s (%s recovery of %s from restore point %s) failed after %.1fs: %s",
            task_id,
            mode_label.lower(),
            source_vm_name,
            point_id,
            time.time() - start_time,
            exc,
        )
        left = _undo(
            connection, node_name, created_vm_id, swapped, written_files, created_folders, target_vm_id, progress_callback
        )
        if isinstance(exc, (HyperVError, vhdx_tool.VhdxToolError, VCenterBackupError)):
            raise HyperVRestoreError(f"{exc}. {left}" if left else str(exc)) from exc
        raise


def _undo(
    connection: dict[str, Any],
    node_name: str,
    created_vm_id: str | None,
    swapped: list[dict[str, Any]],
    written_files: list[str],
    created_folders: list[str],
    target_vm_id: str,
    progress_callback: ProgressCallback,
) -> str:
    """Leave the host as the restore found it, as far as that can be done."""
    left = {
        "vm": created_vm_id or "",
        "swapped": list(swapped),
        "files": list(written_files),
        "tool": list(written_files),
        "folders": sorted(created_folders, key=len, reverse=True),
    }
    if not (left["vm"] or left["swapped"] or left["files"] or left["folders"]):
        return ""
    window = reconnect_seconds()
    deadline = time.monotonic() + window
    waiting = False
    while True:
        try:
            with open_node(connection) as node:
                _undo_on(node, node_name, left, target_vm_id, progress_callback)
            return ""
        except HyperVUnreachableError as exc:
            if time.monotonic() >= deadline:
                message = (
                    f"Could not reach the Hyper-V host within {window} s to undo the failed restore ({exc}); "
                    f"left there: {_left_behind(left)}. Remove them by hand"
                )
                _undo_note(progress_callback, True, "cleanup", 98, message)
                return message
            if not waiting:
                waiting = True
                _undo_note(
                    progress_callback,
                    True,
                    "cleanup",
                    97,
                    f"The Hyper-V host does not answer ({exc}); trying to undo the restore again for up to {window} s",
                )
            time.sleep(RECONNECT_PAUSE_SECONDS)
        except Exception as exc:
            logger.warning("Undoing the failed Hyper-V restore failed: %s", exc)
            message = f"Undoing the failed restore failed: {exc}; left on the host: {_left_behind(left)}"
            _undo_note(progress_callback, True, "cleanup", 98, message)
            return message


def _undo_on(
    node: HyperVNode, node_name: str, left: dict[str, Any], target_vm_id: str, progress_callback: ProgressCallback
) -> None:
    if node_name:
        node_name = node.resolve_node(node_name)
    if left["vm"]:
        try:
            node.destroy_vm(left["vm"])
            _undo_note(progress_callback, False, "cleanup", 97, f"Removed the VM {left['vm']} the restore had created")
        except HyperVUnreachableError:
            raise
        except Exception as exc:
            _undo_note(progress_callback, True, "cleanup", 97, f"Could not remove the VM {left['vm']} the restore had created: {exc}")
        left["vm"] = ""
    if left["swapped"]:
        back = [
            {
                "controller_type": entry.get("controller_type"),
                "controller_number": entry.get("controller_number"),
                "controller_location": entry.get("controller_location"),
                "path": entry.get("previous_path") or "",
            }
            for entry in left["swapped"]
        ]
        try:
            node.replace_vm_disks(target_vm_id, back)
            _undo_note(progress_callback, False, "cleanup", 97, "Pointed the VM back at its original disks")
        except HyperVUnreachableError:
            raise
        except Exception as exc:
            _undo_note(progress_callback, True, "cleanup", 97, f"Could not point the VM back at its original disks: {exc}")
        left["swapped"] = []
    if left["tool"] and node_name:
        stopped = node.stop_disk_tool(node_name, left["tool"])
        if stopped:
            _undo_note(progress_callback, False, "cleanup", 98, f"Stopped {', '.join(stopped)}, still writing what the failed restore had started")
        left["tool"] = []
    if left["files"] and node_name:
        failed = node.remove_files(node_name, left["files"])
        if failed:
            _undo_note(progress_callback, True, "cleanup", 98, f"Could not remove {', '.join(failed)}; delete by hand")
        else:
            _undo_note(progress_callback, False, "cleanup", 98, f"Removed {len(left['files'])} partially restored file(s)")
        left["files"] = []
    if left["folders"] and node_name:
        kept = node.remove_empty_folders(node_name, left["folders"])
        if kept:
            _undo_note(progress_callback, True, "cleanup", 98, f"Kept {', '.join(kept)}: not empty; delete by hand")
        else:
            _undo_note(progress_callback, False, "cleanup", 98, f"Removed {len(left['folders'])} folder(s) the restore had created")
        left["folders"] = []


def _left_behind(left: dict[str, Any]) -> str:
    parts = []
    if left["vm"]:
        parts.append(f"the VM {left['vm']} the restore had created")
    if left["swapped"]:
        parts.append(
            "the VM still points at the restored disks "
            + ", ".join(str(entry.get("path") or "") for entry in left["swapped"])
            + " instead of "
            + ", ".join(str(entry.get("previous_path") or "") for entry in left["swapped"])
        )
    if left["files"]:
        tool = " (abr-vhdx-tool may still be writing them)" if left["tool"] else ""
        parts.append(f"the file(s) {', '.join(left['files'])}{tool}")
    if left["folders"]:
        parts.append(f"the folder(s) {', '.join(left['folders'])}")
    return "; ".join(parts) or "nothing"


def _setting_change(item: dict[str, Any]) -> str:
    setting = str(item.get("setting") or "")
    if setting == "notes":
        return "notes"
    return f"{setting} ({item.get('before') or 'none'} -> {item.get('after') or 'none'})"


def _restore_adapters(
    node: HyperVNode,
    vm_id: str,
    vm_name: str,
    vm_config: dict[str, Any],
    warnings: list[str],
    progress_callback: ProgressCallback,
) -> None:
    recorded = vm_config.get("network_adapters")
    if not isinstance(recorded, list):
        message = f"The backup does not record the network adapters of {vm_name}, so they are left as they are"
        _warn(progress_callback, "configure", 92, message)
        warnings.append(message)
        return
    wanted = [
        {
            "label": str(adapter.get("label") or ""),
            "switch": str(adapter.get("network") or ""),
            "vlan_mode": str(adapter.get("vlan_mode") or ""),
            "vlan_id": int(adapter.get("vlan_id") or 0),
            "adapter_id": str(adapter.get("adapter_id") or ""),
            "mac": str(adapter.get("mac") or ""),
            "dynamic_mac": adapter.get("dynamic_mac") is not False,
            "legacy": bool(adapter.get("legacy")),
        }
        for adapter in recorded
        if isinstance(adapter, dict)
    ]
    try:
        rows = node.restore_vm_adapters(vm_id, wanted)
    except HyperVError as exc:
        message = f"{vm_name} is restored, but its network adapters could not be set back to the backup's: {exc}"
        _warn(progress_callback, "configure", 92, message)
        warnings.append(message)
        return
    for row in rows:
        label = row.get("label") or "network adapter"
        status = row.get("status")
        if status == "removed":
            _emit(
                progress_callback,
                "configure",
                92,
                f"Network adapter {label} on {row.get('previous') or 'no switch'} removed from {vm_name}: the backup "
                "does not have it",
            )
        elif status == "created":
            static = next(
                (
                    item["mac"]
                    for item in wanted
                    if item["label"] == row.get("label") and not item["dynamic_mac"] and item["mac"].strip("0")
                ),
                "",
            )
            _emit(
                progress_callback,
                "configure",
                92,
                f"Network adapter {label} of {vm_name} recreated on {row.get('switch') or 'no switch'}"
                + (f" with its MAC address {static}" if static else "")
                + ", as in the backup",
            )
        elif status == "reconnected":
            _emit(
                progress_callback,
                "configure",
                92,
                f"Network adapter {label} of {vm_name} reconnected from {row.get('previous') or 'no switch'} "
                f"to {row.get('switch') or 'no switch'}, as in the backup",
            )
        if row.get("vlan_changed"):
            wanted_vlan = next((item for item in wanted if item["label"] == row.get("label")), {})
            vlan_text = (
                f"VLAN {wanted_vlan.get('vlan_id')}" if wanted_vlan.get("vlan_mode") == "Access" else "untagged"
            )
            _emit(
                progress_callback,
                "configure",
                92,
                f"Network adapter {label} of {vm_name} set back from {row.get('previous_vlan') or 'its VLAN'} "
                f"to {vlan_text}, as in the backup",
            )


def _first_boot_entry(config: dict[str, Any]) -> dict[str, Any]:
    return next((entry for entry in config.get("boot_order") or [] if isinstance(entry, dict)), {})


def _boot_entry_text(entry: dict[str, Any]) -> str:
    kind = str(entry.get("boot_type") or "")
    device = str(entry.get("device") or "")
    return f"{kind} {device}".strip() or "nothing"


def _restore_boot_order(
    node: HyperVNode,
    vm_id: str,
    vm_name: str,
    vm_config: dict[str, Any],
    restored_slots: dict[str, str],
    warnings: list[str],
    progress_callback: ProgressCallback,
) -> bool:
    boot_order = [entry for entry in vm_config.get("boot_order") or [] if isinstance(entry, dict)]
    if not boot_order:
        return False
    first = boot_order[0]
    generation = int(vm_config.get("generation") or (2 if vm_config.get("firmware") == "efi" else 1))
    slot = ""
    if generation >= 2:
        if first.get("boot_type") != "Drive" or str(first.get("device") or "").upper() not in restored_slots:
            return False
        slot = restored_slots[str(first.get("device") or "").upper()]
    try:
        node.set_boot_disk(vm_id, slot, boot_order)
    except HyperVError as exc:
        message = f"{vm_name} is restored, but its boot order could not be set back to the backup's: {exc}"
        _warn(progress_callback, "configure", 93, message)
        warnings.append(message)
        return False
    return True


def _slot_paths(entries: Any) -> dict[str, str]:
    return {
        str(entry.get("slot") or "").upper(): str(entry.get("path") or "")
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("slot")
    }


def _kept_as_they_are(
    current: dict[str, Any] | None, backup: dict[str, Any], boot_restored: bool
) -> list[str]:
    if not current:
        return ["the rest of its configuration, which could not be read to compare it with the backup"]
    kept: list[str] = []
    now_secure, then_secure = current.get("secure_boot_enabled"), backup.get("secure_boot_enabled")
    if now_secure is not None and then_secure is not None and bool(now_secure) != bool(then_secure):
        kept.append(f"Secure Boot is {'on' if now_secure else 'off'} (the backup: {'on' if then_secure else 'off'})")
    elif now_secure and then_secure:
        now_template = str(current.get("secure_boot_template") or "")
        then_template = str(backup.get("secure_boot_template") or "")
        if now_template and then_template and now_template != then_template:
            kept.append(f"the Secure Boot template is {now_template} (the backup: {then_template})")
    if "tpm_enabled" in backup and bool(current.get("tpm_enabled")) != bool(backup.get("tpm_enabled")):
        kept.append(
            f"the virtual TPM is {'on' if current.get('tpm_enabled') else 'off'} "
            f"(the backup: {'on' if backup.get('tpm_enabled') else 'off'})"
        )
    if "dvd_drives" in backup:
        now_dvd, then_dvd = _slot_paths(current.get("dvd_drives")), _slot_paths(backup.get("dvd_drives"))
        for slot in sorted(set(now_dvd) | set(then_dvd)):
            if slot not in then_dvd:
                kept.append(f"the DVD drive {slot} was added after the backup")
            elif slot not in now_dvd:
                kept.append(f"the DVD drive {slot} of the backup is gone")
            elif now_dvd[slot].lower() != then_dvd[slot].lower():
                kept.append(f"the DVD drive {slot} holds {now_dvd[slot] or 'no media'} (the backup: {then_dvd[slot] or 'no media'})")
    if "hard_drives" in backup:
        then_disks = _slot_paths(backup.get("hard_drives"))
        for slot, path in sorted(_slot_paths(current.get("hard_drives")).items()):
            if slot not in then_disks:
                kept.append(f"the hard disk {slot} ({path}) was attached after the backup")
    now_version, then_version = str(current.get("version") or ""), str(backup.get("version") or "")
    if now_version and then_version and now_version != then_version:
        kept.append(f"the configuration version is {now_version} (the backup: {then_version})")
    if not boot_restored:
        now_first, then_first = _first_boot_entry(current), _first_boot_entry(backup)
        if then_first and _boot_entry_text(now_first).lower() != _boot_entry_text(then_first).lower():
            kept.append(f"it boots first from {_boot_entry_text(now_first)} (the backup: {_boot_entry_text(then_first)})")
    return kept


def _undo_note(progress_callback: ProgressCallback, warn: bool, stage: str, progress: int, message: str) -> None:
    try:
        (_warn if warn else _emit)(progress_callback, stage, progress, message)
    except TaskStopRequested:
        pass