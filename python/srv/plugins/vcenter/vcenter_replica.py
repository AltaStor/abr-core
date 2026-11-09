# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pyVmomi import vim, vmodl

from .vcenter import (
    VCenterNode,
    _boot_order_entries,
    _nic_network_name,
    _normalize_vm_firmware,
)
from .vcenter_backup import (
    _default_tool_path,
    _format_vm_spec,
    _unexpected_data_mover_end,
)
from .vcenter_restore import (
    _abr_restore_runs,
    _build_abr_restore_command,
    _redact_restore_command,
    _run_restore_layer,
)

PLATFORM = "vsphere"
RUNNING_TASK_STATES = ("queued", "running")

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _reauthenticated(operation: Callable[..., _T]) -> Callable[..., _T]:
    @functools.wraps(operation)
    def wrapper(node: VCenterNode, *args: Any, **kwargs: Any) -> _T:
        try:
            return operation(node, *args, **kwargs)
        except vim.fault.NotAuthenticated:
            logger.warning("vCenter session for %s expired before %s; signing in again and retrying", node.host, operation.__name__)
            node.reauthenticate()
            return operation(node, *args, **kwargs)

    return wrapper


def _snapshot_names(vm: Any) -> list[str]:
    names: list[str] = []
    stack = list(getattr(getattr(vm, "snapshot", None), "rootSnapshotList", None) or [])
    while stack:
        item = stack.pop()
        names.append(str(item.name))
        stack.extend(item.childSnapshotList or [])
    return names


def _disks(vm: Any) -> list[dict[str, Any]]:
    disks = []
    for device in getattr(getattr(vm.config, "hardware", None), "device", None) or []:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        backing = device.backing
        parent = getattr(backing, "parent", None)
        disks.append(
            {
                "slot": f"{device.controllerKey}:{device.unitNumber}",
                "path": str(getattr(backing, "fileName", "") or ""),
                "size_bytes": int(getattr(device, "capacityInBytes", 0) or 0),
                "parent_path": str(getattr(parent, "fileName", "") or "") if parent else "",
                "device_key": int(device.key),
                "controller_key": int(device.controllerKey),
                "unit_number": int(device.unitNumber),
            }
        )
    return disks


def _busy(vm: Any) -> list[str]:
    running = []
    for task in getattr(vm, "recentTask", None) or []:
        info = getattr(task, "info", None)
        if info is not None and str(getattr(info, "state", "")) in RUNNING_TASK_STATES:
            running.append(str(getattr(info, "descriptionId", "") or getattr(info, "name", "") or "task"))
    return running


def _fingerprint(vm: Any) -> str:
    parts = []
    for device in getattr(getattr(vm.config, "hardware", None), "device", None) or []:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        backing = device.backing
        identity = str(getattr(backing, "uuid", "") or "").lower() or str(getattr(backing, "fileName", "") or "").rsplit("/", 1)[-1]
        parts.append(f"{device.controllerKey}:{device.unitNumber}={identity}:{int(getattr(device, 'capacityInBytes', 0) or 0)}")
    return "disks:" + "|".join(sorted(parts))


def rebuild_location(state: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    datastore = next((disk["path"][1:].split("]", 1)[0] for disk in state["disks"] if disk["path"].startswith("[") and "]" in disk["path"]), "")
    return state["host"] or settings["target_host"], datastore or settings["target_datastore"]


def _state(vm: Any) -> dict[str, Any]:
    runtime = vm.runtime
    host = getattr(runtime, "host", None)
    busy = _busy(vm)
    return {
        "vm_id": str(vm._moId),
        "name": str(vm.name),
        "host": str(getattr(host, "name", "") or ""),
        "powered_on": runtime.powerState != vim.VirtualMachinePowerState.poweredOff,
        "marker_text": str(getattr(vm.config, "annotation", "") or ""),
        "snapshots": _snapshot_names(vm),
        "disks": _disks(vm),
        "fingerprint": _fingerprint(vm),
        "busy": bool(busy),
        "busy_tasks": busy,
        "consolidation_needed": bool(getattr(runtime, "consolidationNeeded", False)),
        "start_marker": None,
    }


@_reauthenticated
def find_replica(node: VCenterNode, vm_id: str) -> dict[str, Any] | None:
    vm = node._get_vm_by_moref(vm_id)
    return _state(vm) if vm is not None and getattr(vm, "config", None) is not None else None


@_reauthenticated
def replicas_with_marker(node: VCenterNode, marker_prefix: str) -> list[dict[str, Any]]:
    found = []
    for vm in node._list_vms():
        config = getattr(vm, "config", None)
        if config is not None and marker_prefix in str(getattr(config, "annotation", "") or ""):
            found.append(_state(vm))
    return found


@_reauthenticated
def destroy_replica(node: VCenterNode, state: dict[str, Any]) -> list[str]:
    node.destroy_vm(state["name"], vm_moref=state["vm_id"])
    return []


class VCenterReplicaError(RuntimeError):
    pass


def _vm(node: VCenterNode, state: dict[str, Any]) -> Any:
    vm = node._get_vm_by_moref(state["vm_id"])
    if vm is None:
        raise VCenterReplicaError(f"Replica {state['name']} ({state['vm_id']}) is no longer on {node.host}")
    return vm


def _snapshot(vm: Any, name: str) -> Any:
    stack = list(getattr(getattr(vm, "snapshot", None), "rootSnapshotList", None) or [])
    while stack:
        item = stack.pop()
        if str(item.name) == name:
            return item.snapshot
        stack.extend(item.childSnapshotList or [])
    return None


@_reauthenticated
def create_snapshot(node: VCenterNode, state: dict[str, Any], name: str, timeout_seconds: int) -> dict[str, Any]:
    vm = _vm(node, state)
    if _snapshot(vm, name) is None:
        node._wait_for_task(vm.CreateSnapshot_Task(name=name, description="AltaStor replica update", memory=False, quiesce=False))
    created = find_replica(node, state["vm_id"])
    if created is None or _snapshot(_vm(node, state), name) is None:
        raise VCenterReplicaError(f"The snapshot {name} of {state['name']} was not created")
    if any(not disk["parent_path"] for disk in created["disks"]):
        raise VCenterReplicaError(f"The snapshot {name} of {state['name']} left a disk without a delta disk over it")
    return created


def _remove(node: VCenterNode, state: dict[str, Any], name: str) -> None:
    snapshot = _snapshot(_vm(node, state), name)
    if snapshot is None:
        return
    try:
        node._wait_for_task(snapshot.RemoveSnapshot_Task(removeChildren=False))
    except vim.fault.NotAuthenticated:
        raise
    except Exception:
        if _snapshot(_vm(node, state), name) is not None:
            raise
        logger.info("Snapshot %s of %s was already removed by an earlier attempt", name, state["name"])


@_reauthenticated
def revert_snapshot(node: VCenterNode, state: dict[str, Any], name: str, timeout_seconds: int) -> None:
    snapshot = _snapshot(_vm(node, state), name)
    if snapshot is None:
        return
    node._wait_for_task(snapshot.RevertToSnapshot_Task())
    _remove(node, state, name)


@_reauthenticated
def remove_snapshot(node: VCenterNode, state: dict[str, Any], name: str, timeout_seconds: int) -> None:
    _remove(node, state, name)
    vm = _vm(node, state)
    if getattr(vm.runtime, "consolidationNeeded", False):
        node._wait_for_task(vm.ConsolidateVMDisks_Task())


@_reauthenticated
def consolidate(node: VCenterNode, state: dict[str, Any], timeout_seconds: int) -> None:
    node._wait_for_task(_vm(node, state).ConsolidateVMDisks_Task())


@_reauthenticated
def powered_on_since(node: VCenterNode, state: dict[str, Any], since: str) -> bool:
    if not since:
        return False
    try:
        begin = datetime.fromisoformat(since)
    except ValueError:
        return False
    vm = _vm(node, state)
    spec = vim.event.EventFilterSpec(
        entity=vim.event.EventFilterSpec.ByEntity(entity=vm, recursion="self"),
        eventTypeId=["VmPoweredOnEvent", "VmStartingEvent"],
        time=vim.event.EventFilterSpec.ByTime(beginTime=begin),
    )
    manager = node.si.content.eventManager
    try:
        return bool(manager.QueryEvents(spec))
    except vmodl.fault.NotImplemented:
        return _has_run_logs(node, vm) or _collected_events(manager, spec)


def _collected_events(manager: Any, spec: Any) -> bool:
    collector = manager.CreateCollectorForEvents(spec)
    try:
        return bool(collector.latestPage)
    finally:
        collector.DestroyCollector()


def _has_run_logs(node: VCenterNode, vm: Any) -> bool:
    files = vm.config.files
    folder = str(files.logDirectory or str(files.vmPathName).rsplit("/", 1)[0]).rstrip("/")
    datastore = next((item for item in vm.datastore or [] if folder.startswith(f"[{item.name}]")), None)
    if datastore is None:
        raise VCenterReplicaError(f"Cannot check whether {vm.name} was powered on: no datastore holds its folder {folder}")
    spec = vim.host.DatastoreBrowser.SearchSpec(matchPattern=["vmware*.log"])
    task = datastore.browser.SearchDatastore_Task(datastorePath=folder, searchSpec=spec)
    node._wait_for_task(task)
    return bool(getattr(task.info.result, "file", None))


@_reauthenticated
def _connection_details(node: VCenterNode) -> tuple[str, str]:
    return node.get_service_type(), node.get_thumbprint()


def write_increments(
    node: VCenterNode,
    state: dict[str, Any],
    disk: dict[str, Any],
    layers: list[dict[str, Any]],
    repository: dict[str, Any],
    encryption_key: str,
    *,
    report: Any,
    should_cancel: Any,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    tool_path = Path(_default_tool_path()).expanduser()
    service_type, thumbprint = _connection_details(node)
    written = 0
    for layer in _abr_restore_runs(layers, tool_path=tool_path, repository=repository):
        command = _build_abr_restore_command(
            tool_path=tool_path,
            repository=repository,
            abr_path=str(layer["abr_path"]),
            backup_name=str(layer["abr_backup_name"]),
            remote_datastore_path=disk["path"],
            host=node.host,
            port=node.port,
            username=node.user,
            password=node.password,
            thumbprint=thumbprint,
            vm_moref=_format_vm_spec(state["vm_id"], service_type),
            transport_mode="nbdssl" if service_type == "VirtualCenter" else None,
            encryption_key=encryption_key,
            force=True,
            source_names=layer.get("source_names"),
        )
        completed = _run_restore_layer(
            command,
            check=False,
            capture_output=True,
            text=True,
            should_cancel=should_cancel,
            on_progress=report,
        )
        if completed.returncode != 0:
            detail = _unexpected_data_mover_end(completed) or (completed.stderr or completed.stdout or "").strip()
            raise VCenterReplicaError(
                f"Updating disk {disk['path']} of {state['name']} from {layer.get('source')} failed: {detail} "
                f"({' '.join(_redact_restore_command(command)[:3])})"
            )
    return {"written_bytes": written}


def _disk_devices(vm: Any) -> dict[str, Any]:
    return {
        f"{device.controllerKey}:{device.unitNumber}": device
        for device in vm.config.hardware.device
        if isinstance(device, vim.vm.device.VirtualDisk)
    }


def _removal(device: Any) -> Any:
    spec = vim.vm.device.VirtualDeviceSpec()
    spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
    spec.device = device
    return spec


def _addition(template: Any, controller_key: int, unit_number: int, path: str) -> Any:
    disk = vim.vm.device.VirtualDisk()
    disk.key = -100 - unit_number - controller_key
    disk.controllerKey = controller_key
    disk.unitNumber = unit_number
    disk.capacityInKB = 0
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    backing.fileName = path
    backing.diskMode = getattr(template.backing, "diskMode", None) or "persistent"
    backing.thinProvisioned = getattr(template.backing, "thinProvisioned", None)
    backing.eagerlyScrub = getattr(template.backing, "eagerlyScrub", None)
    disk.backing = backing
    spec = vim.vm.device.VirtualDeviceSpec()
    spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    spec.device = disk
    return spec


@_reauthenticated
def release_staging_disks(node: VCenterNode, staging: dict[str, Any]) -> None:
    vm = _vm(node, staging)
    removals = [_removal(device) for device in _disk_devices(vm).values()]
    if removals:
        node._wait_for_task(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=removals)))


@_reauthenticated
def swap_disks(node: VCenterNode, replica: dict[str, Any], staging: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    vm = _vm(node, replica)
    devices = _disk_devices(vm)
    current = {disk["path"] for disk in find_replica(node, replica["vm_id"])["disks"]}
    if all((not change["new_path"] or change["new_path"] in current) and (change["new_path"] or change["slot"] not in devices) for change in changes):
        return
    templates = {disk["slot"]: disk for disk in staging["disks"]}
    spec_changes = []
    for change in changes:
        old = devices.get(change["slot"])
        if old is not None:
            spec_changes.append(_removal(old))
        if change["new_path"]:
            source = templates.get(change["staging_slot"]) or {}
            template = old if old is not None else None
            controller_key = int(old.controllerKey) if old is not None else int(source.get("controller_key") or 0)
            unit_number = int(old.unitNumber) if old is not None else int(source.get("unit_number") or 0)
            if template is None:
                template = vim.vm.device.VirtualDisk(backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode="persistent", thinProvisioned=True))
            spec_changes.append(_addition(template, controller_key, unit_number, change["new_path"]))
    node._wait_for_task(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=spec_changes)))


@_reauthenticated
def delete_disk_files(node: VCenterNode, host: str, paths: list[str]) -> list[str]:
    left = []
    for path in paths:
        try:
            node.delete_managed_disk(path)
        except (vmodl.MethodFault, RuntimeError, ValueError) as exc:
            if "not found" in str(exc).lower() or "FileNotFound" in type(exc).__name__:
                continue
            logger.warning("Could not delete disk file %s: %s", path, exc)
            left.append(path)
    return left


def remove_staging(node: VCenterNode, staging: dict[str, Any]) -> list[str]:
    return destroy_replica(node, staging)


@_reauthenticated
def discard_staging_shell(node: VCenterNode, staging: dict[str, Any]) -> None:
    vm = node._get_vm_by_moref(staging["vm_id"])
    if vm is None:
        return
    if _disk_devices(vm):
        raise VCenterReplicaError(f"The staging VM {staging['name']} still holds disks; it is not removed")
    node.destroy_vm(staging["name"], vm_moref=staging["vm_id"])


@_reauthenticated
def remove_folder(node: VCenterNode, host: str, folder: str) -> None:
    datastore_name = folder.split("]", 1)[0].lstrip("[")
    datastore = node._get_datastore(datastore_name)
    datacenter = node._get_datacenter(datastore) if datastore is not None else None
    try:
        node._wait_for_task(node.si.content.fileManager.DeleteDatastoreFile_Task(name=folder, datacenter=datacenter))
    except (vmodl.MethodFault, RuntimeError) as exc:
        logger.warning("Could not remove the empty folder %s: %s", folder, exc)


HARDWARE_KEYS = ("num_cpu", "memory_mb")


def _adapters(vm: Any) -> list[dict[str, Any]]:
    adapters = []
    for device in getattr(getattr(vm.config, "hardware", None), "device", None) or []:
        if not isinstance(device, vim.vm.device.VirtualEthernetCard):
            continue
        connectable = getattr(device, "connectable", None)
        adapters.append(
            {
                "id": str(device.key),
                "label": str(getattr(device.deviceInfo, "label", "") or ""),
                "network": str(_nic_network_name(vm, device) or ""),
                "connected": bool(getattr(connectable, "startConnected", False)),
                "vlan_id": 0,
            }
        )
    return adapters


@_reauthenticated
def read_config(node: VCenterNode, state: dict[str, Any]) -> dict[str, Any]:
    vm = _vm(node, state)
    config = vm.config
    secure_boot = getattr(getattr(config, "bootOptions", None), "efiSecureBootEnabled", None)
    return {
        "num_cpu": int(getattr(config.hardware, "numCPU", 0) or 0),
        "memory_mb": int(getattr(config.hardware, "memoryMB", 0) or 0),
        "firmware": _normalize_vm_firmware(getattr(config, "firmware", None)),
        "secure_boot_enabled": bool(secure_boot) if secure_boot is not None else None,
        "boot_order": _boot_order_entries(config),
        "network_adapters": _adapters(vm),
    }


def _host_and_datacenter(node: VCenterNode, vm: Any) -> tuple[Any, Any]:
    host = vm.runtime.host
    return host, node._get_datacenter(host)


def _on_host(network: Any, host: Any) -> bool:
    if network is None or not getattr(getattr(network, "summary", None), "accessible", True):
        return False
    host_id = getattr(host, "_moId", None)
    return any(getattr(item, "_moId", None) == host_id for item in getattr(network, "host", None) or [])


@_reauthenticated
def networks(node: VCenterNode, state: dict[str, Any], names: list[str]) -> set[str]:
    host, datacenter = _host_and_datacenter(node, _vm(node, state))
    return {name for name in names if _on_host(node._find_network(datacenter, host, name), host)}


def _backing(node: VCenterNode, datacenter: Any, host: Any, item: dict[str, Any]) -> Any:
    if item["network"]:
        found = _on_host(node._find_network(datacenter, host, item["network"]), host)
        spec = node._build_nic_spec(datacenter, host, {"network": item["network"], "label": item["label"]}) if found else None
        if spec is None:
            raise VCenterReplicaError(f"Network {item['network']} is not on host {getattr(host, 'name', '')}")
        return spec.device.backing
    return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=item["source_network"] or "")


def _connectable(connected: bool) -> Any:
    return vim.vm.device.VirtualDevice.ConnectInfo(startConnected=connected, connected=False, allowGuestControl=True)


@_reauthenticated
def apply_config(
    node: VCenterNode,
    state: dict[str, Any],
    hardware: dict[str, Any],
    *,
    remove: list[str],
    change: list[dict[str, Any]],
    add: list[dict[str, Any]],
) -> list[str]:
    vm = _vm(node, state)
    host, datacenter = _host_and_datacenter(node, vm)
    devices = {str(device.key): device for device in vm.config.hardware.device}
    before = set(devices)
    spec = vim.vm.ConfigSpec()
    if "num_cpu" in hardware:
        spec.numCPUs = int(hardware["num_cpu"])
    if "memory_mb" in hardware:
        spec.memoryMB = int(hardware["memory_mb"])
    operations = vim.vm.device.VirtualDeviceSpec.Operation
    changes = []
    for key in remove:
        if key in devices:
            changes.append(vim.vm.device.VirtualDeviceSpec(operation=operations.remove, device=devices[key]))
    for item in change:
        device = devices[item["id"]]
        if item["network"]:
            device.backing = _backing(node, datacenter, host, item)
        device.connectable = _connectable(item["connected"])
        changes.append(vim.vm.device.VirtualDeviceSpec(operation=operations.edit, device=device))
    for position, item in enumerate(add, start=1):
        nic = vim.vm.device.VirtualVmxnet3()
        nic.key = -100 - position
        nic.addressType = "generated"
        nic.wakeOnLanEnabled = True
        nic.backing = _backing(node, datacenter, host, item)
        nic.connectable = _connectable(item["connected"])
        changes.append(vim.vm.device.VirtualDeviceSpec(operation=operations.add, device=nic))
    if changes:
        spec.deviceChange = changes
    node._wait_for_task(vm.ReconfigVM_Task(spec=spec))
    vm = _vm(node, state)
    return [adapter["id"] for adapter in sorted(_adapters(vm), key=lambda item: int(item["id"])) if adapter["id"] not in before]


@_reauthenticated
def rename(node: VCenterNode, state: dict[str, Any], new_name: str) -> None:
    node.rename_vm(state["name"], new_name, vm_moref=state["vm_id"])


def _first_boot(config: dict[str, Any]) -> str:
    entry = next((item for item in config.get("boot_order") or [] if isinstance(item, dict)), {})
    kind = str(entry.get("type") or "")
    if kind == "disk" and entry.get("controller_type"):
        return f"disk on {entry.get('controller_type')} {entry.get('controller_bus')}:{entry.get('unit_number')}"
    return kind or "the default order"


def unsynced(current: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    kept = []
    now_firmware, then_firmware = str(current.get("firmware") or ""), str(wanted.get("firmware") or "")
    if now_firmware and then_firmware and now_firmware != then_firmware:
        kept.append(f"its firmware is {now_firmware} (the source VM: {then_firmware})")
    now_secure, then_secure = current.get("secure_boot_enabled"), wanted.get("secure_boot_enabled")
    if now_secure is not None and then_secure is not None and bool(now_secure) != bool(then_secure):
        kept.append(f"Secure Boot is {'on' if now_secure else 'off'} (the source VM: {'on' if then_secure else 'off'})")
    if wanted.get("boot_order") and _first_boot(current) != _first_boot(wanted):
        kept.append(f"it boots first from {_first_boot(current)} (the source VM: {_first_boot(wanted)})")
    return kept
