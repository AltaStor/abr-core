# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from pyVmomi import vim, vmodl

from .vcenter import (
    _DISK_CONTROLLER_CLASS_BY_NAME,
    _SCSI_CONTROLLER_CLASS_BY_NAME,
    _disk_controller_type_name,
    _normalize_vm_firmware,
)
from .vcenter_backup import VCenterBackupError
from .vcenter_disk_types import describe_disk_placement, disk_controller_display_name

logger = logging.getLogger(__name__)

RESTORED_SUFFIX = re.compile(r"-r[0-9a-f]{8}$")
DISK_FILE_NAME_LIMIT = 106
HARDWARE_KEYS = ("num_cpu", "memory_mb")
HARDWARE_TEXT = {"num_cpu": "vCPU", "memory_mb": "MB memory"}
RECOVERY_LOCK = threading.Lock()
DELETE_RETRY_SECONDS = 10
RECOVERY_PATIENCE_SECONDS = 180
VSPHERE_FAILURES = (vmodl.MethodFault, RuntimeError, ValueError, VCenterBackupError)
GUARD_PREFIX = "abr-in-place-"
DISK_BACKINGS = (
    vim.vm.device.VirtualDisk.FlatVer2BackingInfo,
    vim.vm.device.VirtualDisk.SeSparseBackingInfo,
    vim.vm.device.VirtualDisk.SparseVer2BackingInfo,
)

Report = Callable[[int, str, str], None]


def split_path(path: str) -> tuple[str, str, str]:
    text = str(path or "").strip()
    if not text.startswith("[") or "]" not in text:
        raise VCenterBackupError(f"Unsupported datastore path format: {path}")
    datastore, relative = text[1:].split("]", 1)
    relative = relative.strip()
    folder = str(PurePosixPath(relative).parent)
    return datastore.strip(), "" if folder == "." else folder, PurePosixPath(relative).name


def find_original(node: Any, *, vm_id: str, instance_uuid: str, name: str) -> Any:
    vm = node._get_vm_by_moref(vm_id) if vm_id else None
    if vm is not None and instance_uuid and str(getattr(vm.config, "instanceUuid", "") or "").lower() != instance_uuid.lower():
        vm = None
    if vm is not None and not instance_uuid and str(vm.name) != name:
        vm = None
    if vm is None and instance_uuid:
        vm = node.si.RetrieveContent().searchIndex.FindByUuid(datacenter=None, uuid=instance_uuid, vmSearch=True, instanceUuid=True)
    if vm is None or getattr(vm, "config", None) is None:
        looked = " or ".join(part for part in (f"id {vm_id}" if vm_id else "", f"instance UUID {instance_uuid}" if instance_uuid else "") if part)
        raise VCenterBackupError(
            f"VM {name} is not on {node.host}, so it cannot be restored in place: no VM there has {looked or 'its id'}. "
            "Restore it to a new VM instead"
        )
    return vm


def _snapshot_items(vm: Any) -> list[Any]:
    items: list[Any] = []
    stack = list(getattr(getattr(vm, "snapshot", None), "rootSnapshotList", None) or [])
    while stack:
        item = stack.pop()
        items.append(item)
        stack.extend(item.childSnapshotList or [])
    return items


def _snapshot_names(vm: Any) -> list[str]:
    return sorted(str(item.name) for item in _snapshot_items(vm))


def _chain(backing: Any) -> list[str]:
    files: list[str] = []
    while backing is not None:
        files.append(str(getattr(backing, "fileName", "") or ""))
        backing = getattr(backing, "parent", None)
    return files


def _layout(vm: Any) -> Any:
    return getattr(vm, "layoutEx", None)


def _file_names(vm: Any) -> dict[int, str]:
    return {int(item.key): str(item.name) for item in getattr(_layout(vm), "file", None) or []}


def used_files(vm: Any) -> set[str]:
    return set(_file_names(vm).values())


def snapshot_disk_files(vm: Any) -> set[str]:
    names = _file_names(vm)
    held: set[str] = set()
    for snapshot in getattr(_layout(vm), "snapshot", None) or []:
        for disk in getattr(snapshot, "disk", None) or []:
            for unit in getattr(disk, "chain", None) or []:
                held.update(names[key] for key in getattr(unit, "fileKey", None) or [] if key in names)
    return held


def _family(type_name: str) -> str:
    return "scsi" if type_name in _SCSI_CONTROLLER_CLASS_BY_NAME else type_name


def describe_vm(vm: Any) -> dict[str, Any]:
    config = vm.config
    devices = list(config.hardware.device)
    by_key = {device.key: device for device in devices}
    controllers = {}
    for device in devices:
        type_name = _disk_controller_type_name(device)
        if type_name is not None and getattr(device, "busNumber", None) is not None:
            controllers[f"{type_name}:{device.busNumber}"] = int(device.key)
    held = snapshot_disk_files(vm)
    disks = []
    for device in devices:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        controller = by_key.get(device.controllerKey)
        backing = device.backing
        chain = _chain(backing) if isinstance(backing, DISK_BACKINGS) else [str(getattr(backing, "fileName", "") or "")]
        if not isinstance(backing, DISK_BACKINGS):
            kind = "other"
        elif len(chain) == 1:
            kind = "plain"
        else:
            kind = "snapshot" if all(path in held for path in chain[1:]) else "linked"
        type_name = _disk_controller_type_name(controller) or ""
        disks.append(
            {
                "slot": f"{type_name}:{getattr(controller, 'busNumber', '')}:{device.unitNumber}",
                "position": f"{_family(type_name)}:{getattr(controller, 'busNumber', '')}:{device.unitNumber}",
                "controller": f"{type_name}:{getattr(controller, 'busNumber', '')}",
                "controller_key": int(device.controllerKey),
                "unit_number": int(device.unitNumber),
                "label": str(getattr(device.deviceInfo, "label", "") or ""),
                "path": chain[0],
                "base": chain[-1],
                "uuid": str(getattr(backing, "uuid", "") or ""),
                "kind": kind,
            }
        )
    home_datastore, home_folder, _ = split_path(config.files.vmPathName)
    return {
        "vm_id": str(vm._moId),
        "name": str(vm.name),
        "host": str(getattr(vm.runtime.host, "name", "") or ""),
        "powered_on": vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOff,
        "snapshots": _snapshot_names(vm),
        "consolidation_needed": bool(getattr(vm.runtime, "consolidationNeeded", False)),
        "controllers": controllers,
        "disks": disks,
        "home_datastore": home_datastore,
        "home_folder": home_folder,
        "host_datastores": sorted(str(getattr(item, "name", "") or "") for item in getattr(vm.runtime.host, "datastore", None) or []),
    }


def refusal(state: dict[str, Any]) -> str | None:
    if state["consolidation_needed"]:
        return (
            f"VM {state['name']} needs its disks consolidated (in vCenter: Snapshots > Consolidate). Consolidate them and "
            "start the restore again"
        )
    return None


def _slot_of(entry: dict[str, Any]) -> str | None:
    kind = str(entry.get("controller_type") or "").strip().lower()
    bus, unit = entry.get("controller_bus"), entry.get("unit_number")
    if not kind or not isinstance(bus, int) or not isinstance(unit, int):
        return None
    return f"{kind}:{bus}:{unit}"


def _file_name(entry: dict[str, Any], index: int, task_id: str) -> str:
    source = str(entry.get("datastore_path") or "")
    name = split_path(source)[2] if source.startswith("[") else ""
    stem = RESTORED_SUFFIX.sub("", PurePosixPath(name or f"disk-{index}.vmdk").stem)
    suffix = f"-r{task_id[:8]}.vmdk"
    return f"{stem[: DISK_FILE_NAME_LIMIT - len(suffix)]}{suffix}"


def _position(slot: str) -> str:
    kind, bus, unit = slot.split(":")
    return f"{_family(kind)}:{bus}:{unit}"


def _refuse_disk(state: dict[str, Any], current: dict[str, Any]) -> None:
    if current["kind"] == "other":
        raise VCenterBackupError(
            f"{current['label']} of VM {state['name']} ({current['path']}) is not a virtual disk file (a raw device mapping), "
            "so it cannot be replaced in place; restore to a new VM instead"
        )
    if current["kind"] == "linked":
        raise VCenterBackupError(
            f"{current['label']} of VM {state['name']} ({current['path']}) is a linked clone: it is built on {current['base']}, "
            "which is not one of this VM's snapshots, so it cannot be replaced in place; restore to a new VM instead"
        )


def plan_disks(state: dict[str, Any], disk_entries: list[dict[str, Any]], task_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_position = {disk["position"]: disk for disk in state["disks"]}
    by_uuid = {disk["uuid"].lower(): disk for disk in state["disks"] if disk["uuid"]}
    placed = all(_slot_of(entry) for entry in disk_entries)
    claimed = {by_uuid[str(entry.get("uuid") or "").lower()]["slot"] for entry in disk_entries if str(entry.get("uuid") or "").lower() in by_uuid}
    used: set[str] = set()
    planned = []
    for index, entry in enumerate(disk_entries, start=1):
        label = str(entry.get("disk_label") or f"Hard disk {index}")
        slot = _slot_of(entry) if placed else None
        if slot:
            if slot.rsplit(":", 2)[0] not in _DISK_CONTROLLER_CLASS_BY_NAME:
                raise VCenterBackupError(
                    f"{label} of the restore point sits on {describe_disk_placement(entry)}, which this restore cannot create; "
                    "restore to a new VM instead"
                )
            current = by_position.get(_position(slot))
        else:
            current = by_uuid.get(str(entry.get("uuid") or "").lower()) or next(
                (disk for disk in state["disks"] if disk["slot"] not in used and disk["slot"] not in claimed), None
            )
        if current is not None and current["slot"] in used:
            current = None
        if current is not None:
            _refuse_disk(state, current)
            used.add(current["slot"])
            datastore, folder, _ = split_path(current["path"])
            target_slot = slot or current["slot"]
        else:
            if not slot:
                raise VCenterBackupError(
                    f"{label} of the restore point has no disk of VM {state['name']} to replace and no recorded controller "
                    "to add it on; restore to a new VM instead"
                )
            source = str(entry.get("datastore_path") or "")
            source_datastore = split_path(source)[0] if source.startswith("[") else ""
            datastore = source_datastore if source_datastore in state.get("host_datastores", []) else state["home_datastore"]
            folder = state["home_folder"]
            target_slot = slot
        name = _file_name(entry, index, task_id)
        controller, unit = target_slot.rsplit(":", 1)
        planned.append(
            {
                "index": index,
                "label": label,
                "slot": target_slot,
                "controller": controller,
                "unit_number": int(unit),
                "old_controller_key": current["controller_key"] if current is not None else None,
                "old_unit_number": current["unit_number"] if current is not None else None,
                "datastore": datastore,
                "old_path": current["path"] if current is not None else "",
                "old_base": current["base"] if current is not None else "",
                "new_path": f"[{datastore}] {folder}/{name}" if folder else f"[{datastore}] {name}",
            }
        )
    kept = [disk for disk in state["disks"] if disk["slot"] not in used]
    return planned, kept


def _vm(node: Any, vm_id: str, name: str) -> Any:
    vm = node._get_vm_by_moref(vm_id)
    if vm is None:
        raise VCenterBackupError(f"VM {name} ({vm_id}) is no longer on {node.host}")
    return vm


def _disk_devices(vm: Any) -> dict[tuple[int, int], Any]:
    return {
        (int(device.controllerKey), int(device.unitNumber)): device
        for device in vm.config.hardware.device
        if isinstance(device, vim.vm.device.VirtualDisk)
    }


def attached_paths(node: Any, vm_id: str, name: str) -> set[str]:
    return {str(getattr(device.backing, "fileName", "") or "") for device in _disk_devices(_vm(node, vm_id, name)).values()}


def _removal(device: Any) -> Any:
    return vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.remove, device=device)


def _added(device: Any) -> Any:
    return vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=device)


def _edited(device: Any) -> Any:
    return vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=device)


def _new_controller(type_name: str, bus: int, key: int) -> Any:
    controller = _DISK_CONTROLLER_CLASS_BY_NAME[type_name]()
    controller.key = key
    controller.busNumber = bus
    if isinstance(controller, vim.vm.device.VirtualSCSIController):
        controller.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
    return controller


def _controller_name(type_name: str, bus: int) -> str:
    return f"{disk_controller_display_name(type_name)} {bus}"


def _disk_controllers(devices: list[Any]) -> dict[int, tuple[str, int, Any]]:
    controllers: dict[int, tuple[str, int, Any]] = {}
    for device in devices:
        type_name = _disk_controller_type_name(device)
        if type_name is not None and getattr(device, "busNumber", None) is not None:
            controllers[int(device.key)] = (type_name, int(device.busNumber), device)
    return controllers


def switch_spec(vm: Any, disks: list[dict[str, Any]]) -> tuple[list[Any], list[str]]:
    devices = list(vm.config.hardware.device)
    controllers = _disk_controllers(devices)
    attached: dict[int, list[Any]] = {}
    for device in devices:
        if getattr(device, "controllerKey", None) is not None:
            attached.setdefault(int(device.controllerKey), []).append(device)
    changes: list[Any] = []
    messages: list[str] = []
    key_for: dict[str, int] = {}
    moved: dict[int, int] = {}
    temporary = -200
    for wanted in dict.fromkeys(disk["controller"] for disk in disks):
        type_name, bus = wanted.rsplit(":", 1)[0], int(wanted.rsplit(":", 1)[1])
        existing = next((key for key, (kind, number, _device) in controllers.items() if kind == type_name and number == bus), None)
        if existing is not None:
            key_for[wanted] = existing
            continue
        if type_name == "ide":
            raise VCenterBackupError(f"VM {vm.name} has no IDE {bus} controller to put a disk on")
        temporary -= 1
        changes.append(_added(_new_controller(type_name, bus, temporary)))
        key_for[wanted] = temporary
        replaced = next(
            (key for key, (kind, number, _device) in controllers.items() if _family(kind) == _family(type_name) and number == bus), None
        )
        if replaced is None:
            messages.append(f"{_controller_name(type_name, bus)} added")
            continue
        changes.append(_removal(controllers[replaced][2]))
        moved[replaced] = temporary
        messages.append(f"{_controller_name(controllers[replaced][0], bus)} replaced by {_controller_name(type_name, bus)}")
    by_slot = {(int(d.controllerKey), int(d.unitNumber)): d for d in devices if isinstance(d, vim.vm.device.VirtualDisk)}
    touched: set[int] = set()
    for position, disk in enumerate(disks, start=1):
        target = key_for[disk["controller"]]
        old = None
        if disk.get("old_controller_key") is not None:
            old = by_slot.get((int(disk["old_controller_key"]), int(disk["old_unit_number"])))
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
            fileName=disk["new_path"], diskMode=str(getattr(getattr(old, "backing", None), "diskMode", "") or "persistent")
        )
        if old is not None:
            touched.add(int(old.key))
            old.controllerKey = target
            old.unitNumber = int(disk["unit_number"])
            old.backing = backing
            changes.append(_edited(old))
            continue
        added = vim.vm.device.VirtualDisk(key=-100 - position, controllerKey=target, unitNumber=int(disk["unit_number"]), capacityInKB=0, backing=backing)
        added.deviceInfo = vim.Description(label=str(disk.get("label") or f"Hard disk {position}"), summary=disk["new_path"])
        changes.append(_added(added))
    for old_key, new_key in moved.items():
        for device in attached.get(old_key, []):
            if int(device.key) not in touched:
                device.controllerKey = new_key
                changes.append(_edited(device))
                touched.add(int(device.key))
    return changes, messages


def release_disks(node: Any, vm_id: str) -> None:
    vm = node._get_vm_by_moref(vm_id)
    if vm is None:
        return
    removals = [_removal(device) for device in _disk_devices(vm).values()]
    if removals:
        node._wait_for_task(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=removals)))


def family_files(node: Any, base: str, *, tracking: bool = False) -> list[str]:
    datastore_name, folder, file_name = split_path(base)
    stem = file_name.removesuffix(".vmdk")
    pattern = re.compile(rf"^{re.escape(stem)}(-\d{{6}})?{'-ctk' if tracking else ''}\.vmdk$")
    datastore = node._get_datastore(datastore_name)
    if datastore is None:
        return [] if tracking else [base]
    spec = vim.host.DatastoreBrowser.SearchSpec(matchPattern=[f"{stem}*.vmdk"])
    try:
        found = node._wait_for_task(datastore.browser.SearchDatastore_Task(datastorePath=f"[{datastore_name}] {folder}", searchSpec=spec))
    except VSPHERE_FAILURES as exc:
        logger.warning("Could not list the files of %s: %s", base, exc)
        return [] if tracking else [base]
    names = [str(item.path) for item in getattr(found, "file", None) or [] if pattern.match(str(item.path))]
    paths = [f"[{datastore_name}] {folder}/{name}" if folder else f"[{datastore_name}] {name}" for name in sorted(names)]
    return paths if tracking or paths else [base]


def delete_files(node: Any, paths: list[str]) -> list[str]:
    content = node.si.RetrieveContent()
    failed = []
    for path in paths:
        datastore = node._get_datastore(split_path(path)[0])
        try:
            node._wait_for_task(content.fileManager.DeleteDatastoreFile_Task(name=path, datacenter=node._get_datacenter(datastore) if datastore is not None else None))
        except VSPHERE_FAILURES as exc:
            if isinstance(exc, vim.fault.FileNotFound) or "not found" in str(exc).lower():
                continue
            logger.warning("Could not delete file %s: %s", path, exc)
            failed.append(path)
    return failed


def delete_disks(node: Any, paths: list[str], *, patience: float = 0) -> list[str]:
    deadline = time.monotonic() + patience
    left = list(paths)
    while True:
        failed = []
        for path in left:
            try:
                node.delete_managed_disk(path)
            except (vmodl.MethodFault, RuntimeError, ValueError) as exc:
                if isinstance(exc, vim.fault.FileNotFound) or "not found" in str(exc).lower():
                    continue
                logger.warning("Could not delete disk file %s: %s", path, exc)
                failed.append(path)
        if not failed or time.monotonic() >= deadline:
            return failed
        left = failed
        time.sleep(DELETE_RETRY_SECONDS)


def _remove_staging(node: Any, plan: dict[str, Any]) -> str | None:
    staging = node._get_vm_by_moref(plan["staging_vm_id"]) if plan.get("staging_vm_id") else None
    if staging is None:
        return None
    try:
        node.destroy_vm(plan["staging_name"], vm_moref=plan["staging_vm_id"])
    except VSPHERE_FAILURES as exc:
        logger.warning("Could not remove the temporary VM %s: %s", plan["staging_name"], exc)
        return f"the temporary VM {plan['staging_name']} could not be removed ({exc}); remove it by hand"
    return None


def _guard(vm: Any, plan: dict[str, Any]) -> Any:
    name = str(plan.get("guard_snapshot") or "")
    return next((item for item in _snapshot_items(vm) if name and str(item.name) == name), None)


def take_guard(node: Any, plan: dict[str, Any]) -> None:
    vm = _vm(node, plan["vm_id"], plan["name"])
    node._wait_for_task(
        vm.CreateSnapshot_Task(
            name=plan["guard_snapshot"],
            description="AltaStor in-place restore: reverted if the restore does not complete, removed when it does",
            memory=False,
            quiesce=False,
        )
    )


def drop_guard(node: Any, plan: dict[str, Any], *, revert: bool) -> bool:
    guard = _guard(_vm(node, plan["vm_id"], plan["name"]), plan)
    if guard is None:
        return False
    if revert:
        node._wait_for_task(guard.snapshot.RevertToSnapshot_Task())
        guard = _guard(_vm(node, plan["vm_id"], plan["name"]), plan)
    node._wait_for_task(guard.snapshot.RemoveSnapshot_Task(removeChildren=False, consolidate=True))
    return True


def rollback(node: Any, plan: dict[str, Any], *, patience: float = 0) -> list[str]:
    problems = []
    name = plan["name"]
    try:
        guarded = drop_guard(node, plan, revert=True)
    except VSPHERE_FAILURES as exc:
        logger.exception("Could not take VM %s back to its snapshot %s", name, plan.get("guard_snapshot"))
        plan["put_back_failed"] = True
        return [
            (
                f"VM {name} could not be taken back to its snapshot {plan.get('guard_snapshot')} ({exc}); it is left as it is, "
                "and the snapshot is kept so that it can be reverted in vCenter"
            )
        ]
    staging = node._get_vm_by_moref(plan["staging_vm_id"]) if plan.get("staging_vm_id") else None
    holds_disks = staging is not None and bool(_disk_devices(staging))
    problem = _remove_staging(node, plan)
    if problem:
        problems.append(problem)
    if not holds_disks:
        left = delete_disks(node, [disk["new_path"] for disk in plan["disks"] if disk["new_path"]], patience=patience)
        if left:
            problems.append(f"the restored disk file(s) {', '.join(left)} could not be removed; delete them by hand")
    if guarded:
        problems.extend(clean_old_disks(node, plan, patience=patience)[0])
    if plan.get("was_powered_on"):
        try:
            node.power_on_vm(name, vm_moref=plan["vm_id"])
        except VSPHERE_FAILURES as exc:
            problems.append(f"VM {name} could not be powered on again ({exc})")
    return problems


def clean_old_disks(node: Any, plan: dict[str, Any], *, patience: float = 0) -> tuple[list[str], list[str]]:
    vm = _vm(node, plan["vm_id"], plan["name"])
    used = used_files(vm)
    held = snapshot_disk_files(vm)
    stale, kept, tracking = [], [], []
    for base in dict.fromkeys(str(path) for path in plan.get("old_bases") or [] if path):
        for path in family_files(node, base):
            if path not in used:
                stale.append(path)
            elif path in held:
                kept.append(path)
        tracking.extend(path for path in family_files(node, base, tracking=True) if path.replace("-ctk.vmdk", ".vmdk") not in used and path not in used)
    left = delete_disks(node, stale, patience=patience) + delete_files(node, tracking)
    problems = [f"the replaced disk file(s) {', '.join(left)} could not be removed; delete them by hand"] if left else []
    return problems, kept


def finish(node: Any, plan: dict[str, Any], *, patience: float = 0) -> tuple[list[str], list[str]]:
    problems, kept = clean_old_disks(node, plan, patience=patience)
    problem = _remove_staging(node, plan)
    if problem:
        problems.append(problem)
    return problems, kept


def held_note(name: str, snapshots: list[str], kept: list[str], restored: list[str]) -> str:
    text = (
        f"VM {name} keeps its snapshot(s) {', '.join(snapshots)}. Reverting to one of them brings back the disks it was taken "
        f"with and leaves the restored disk(s) {', '.join(restored)} unused"
    )
    if kept:
        text += f"; deleting the snapshots instead leaves the disk(s) the VM had before the restore, which they keep: {', '.join(kept)}"
    return text + ". vCenter does not remove such files, so delete them by hand then"


def _held(node: Any, plan: dict[str, Any], kept: list[str]) -> str:
    snapshots = _snapshot_names(_vm(node, plan["vm_id"], plan["name"]))
    if not snapshots:
        return ""
    return held_note(plan["name"], snapshots, kept, [disk["new_path"] for disk in plan["disks"]])


def switch(
    node: Any, plan: dict[str, Any], wanted: dict[str, Any], point_id: str, report: Report, save: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    name = plan["name"]
    try:
        if plan["was_powered_on"]:
            report(85, f"Turning off {name} to replace its disks", "info")
        node.power_off_vm(name, vm_moref=plan["vm_id"])
        release_disks(node, plan["staging_vm_id"])
        take_guard(node, plan)
        report(85, f"Took snapshot {plan['guard_snapshot']} of {name}, so that it goes back to it if the restore does not complete", "info")
        vm = _vm(node, plan["vm_id"], name)
        changes, controller_messages = switch_spec(vm, plan["disks"])
        node._wait_for_task(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=changes)))
        messages, notes = settle_config(node, plan["vm_id"], name, wanted, point_id)
        drop_guard(node, plan, revert=False)
    except BaseException as exc:
        logger.warning("Replacing the disks of %s failed (%s); taking it back to its snapshot", name, exc)
        problems = rollback(node, plan)
        plan["undone"] = not plan.pop("put_back_failed", False)
        report(
            86,
            (
                f"Replacing the disks of {name} failed; it is back as it was"
                + ("" if not plan["was_powered_on"] else " and powered on again")
                if plan["undone"]
                else f"Replacing the disks of {name} failed"
            )
            + (f". {'; '.join(problems)}" if problems else ""),
            "warn" if problems else "info",
        )
        raise
    plan["committed"] = True
    save(plan)
    report(86, f"{name} now uses the restored disks and snapshot {plan['guard_snapshot']} is removed; removing the disk files nothing uses any more", "info")
    problems, kept = finish(node, plan)
    return {
        "problems": problems,
        "changes": [*controller_messages, *messages],
        "notes": notes,
        "held": _held(node, plan, kept),
    }


def _pairs(wanted: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[int, str]:
    paired: dict[int, str] = {}
    free = [adapter["id"] for adapter in current]
    by_label = {}
    for adapter in current:
        by_label.setdefault(adapter["label"].strip().lower(), adapter["id"])
    for position, adapter in enumerate(wanted):
        found = by_label.get(str(adapter.get("label") or "").strip().lower())
        if found in free:
            paired[position] = found
            free.remove(found)
    for position in range(len(wanted)):
        if position not in paired and free:
            paired[position] = free.pop(0)
    return paired


def settle_config(node: Any, vm_id: str, name: str, wanted: dict[str, Any], point_id: str) -> tuple[list[str], list[str]]:
    from .vcenter_replica import apply_config, networks, read_config

    state = {"vm_id": vm_id, "name": name}
    current = read_config(node, state)
    messages: list[str] = []
    warnings: list[str] = []
    hardware = {
        key: int(wanted[key])
        for key in HARDWARE_KEYS
        if wanted.get(key) is not None and int(current.get(key) or 0) != int(wanted[key])
    }
    wanted_adapters = [adapter for adapter in wanted.get("network_adapters") or [] if isinstance(adapter, dict)]
    current_adapters = list(current.get("network_adapters") or [])
    names = sorted({str(adapter.get("network") or "") for adapter in wanted_adapters} - {""})
    available = networks(node, state, names) if names else set()
    paired = _pairs(wanted_adapters, current_adapters)
    by_id = {adapter["id"]: adapter for adapter in current_adapters}
    change, add = [], []
    for position, adapter in enumerate(wanted_adapters):
        label = str(adapter.get("label") or f"Network adapter {position + 1}")
        source = str(adapter.get("network") or "")
        network = source if source in available else ""
        if source and not network:
            warnings.append(f"{label} of restore point {point_id} is on {source}, which is not on host {current_host(node, vm_id, name)}; the adapter is left disconnected")
        desired = {"label": label, "network": network, "source_network": source, "connected": bool(network) and adapter.get("connected", True) is not False}
        have = by_id.get(paired.get(position, ""))
        if have is None:
            add.append(desired)
            messages.append(f"{label} added on {network or 'no network'}{'' if desired['connected'] else ', disconnected'}")
            continue
        if (network and have.get("network") != network) or bool(have.get("connected")) != desired["connected"]:
            change.append({"id": have["id"], **desired})
            messages.append(
                f"{have['label']} set to {network or have.get('network') or 'no network'}{'' if desired['connected'] else ', disconnected'} "
                f"(it was on {have.get('network') or 'no network'}{'' if have.get('connected') else ', disconnected'})"
            )
    remove = [adapter["id"] for adapter in current_adapters if adapter["id"] not in paired.values()]
    messages.extend(f"{by_id[adapter_id]['label']} on {by_id[adapter_id].get('network') or 'no network'} removed: restore point {point_id} has no such adapter" for adapter_id in remove)
    if hardware:
        messages.insert(0, ", ".join(f"{value} {HARDWARE_TEXT[key]} (was {current.get(key)})" for key, value in hardware.items()))
    if hardware or change or add or remove:
        apply_config(node, state, hardware, remove=remove, change=change, add=add)
    messages.extend(_set_firmware(node, vm_id, current, wanted))
    boot_order = [entry for entry in wanted.get("boot_order") or [] if isinstance(entry, dict)]
    if boot_order:
        report = node.apply_boot_order(
            name, boot_order, network_adapters=[{"label": adapter.get("label"), "attached": True} for adapter in wanted_adapters], vm_moref=vm_id
        )
        missing = [item["device"] for item in report if not item.get("restored")]
        if missing:
            warnings.append(f"VM {name} does not boot from {', '.join(missing)} as in restore point {point_id}: it has no such device")
    return messages, warnings


def _set_firmware(node: Any, vm_id: str, current: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    now_firmware, then_firmware = str(current.get("firmware") or ""), str(_normalize_vm_firmware(wanted.get("firmware")) or "")
    now_secure, then_secure = current.get("secure_boot_enabled"), wanted.get("secure_boot_enabled")
    target = then_firmware or now_firmware
    spec = vim.vm.ConfigSpec()
    messages = []
    if then_firmware and now_firmware != then_firmware:
        spec.firmware = then_firmware
        messages.append(f"{then_firmware} firmware (was {now_firmware or 'not set'})")
    if target == "efi" and then_secure is not None and bool(now_secure) != bool(then_secure):
        spec.bootOptions = vim.vm.BootOptions(efiSecureBootEnabled=bool(then_secure))
        messages.append(f"Secure Boot {'on' if then_secure else 'off'} (was {'on' if now_secure else 'off'})")
    elif target != "efi" and now_secure:
        spec.bootOptions = vim.vm.BootOptions(efiSecureBootEnabled=False)
    if spec.firmware is None and spec.bootOptions is None:
        return []
    vm = node._get_vm_by_moref(vm_id)
    if vm is None:
        raise VCenterBackupError(f"VM {vm_id} is no longer on {node.host}")
    node._wait_for_task(vm.ReconfigVM_Task(spec=spec))
    return messages


def current_host(node: Any, vm_id: str, name: str) -> str:
    vm = node._get_vm_by_moref(vm_id)
    return str(getattr(getattr(getattr(vm, "runtime", None), "host", None), "name", "") or "its host") if vm is not None else "its host"


def recover(node: Any, result: dict[str, Any], staging_name: str) -> tuple[str | None, bool, bool]:
    with RECOVERY_LOCK:
        plan = result.get("swap_plan") if isinstance(result.get("swap_plan"), dict) else {}
        if plan:
            return _recover_plan(node, dict(plan))
        staging_id = str(result.get("staging_vm_id") or "")
        staging = node._get_vm_by_moref(staging_id) if staging_id else None
        if staging is None and staging_name:
            staging = node._get_vm(staging_name)
        found = str(staging.name) if staging is not None else ""
        if staging is not None:
            node.destroy_vm(found, vm_moref=str(staging._moId))
        written = [str(path) for path in result.get("in_place_new_paths") or [] if path]
        left = delete_disks(node, written, patience=RECOVERY_PATIENCE_SECONDS)
        if not found and not written:
            return None, True, False
        text = (
            f"Removed the temporary VM {found} with the disks the interrupted restore had written"
            if found
            else "Removed the disks the interrupted restore had written"
        ) + "; the VM itself was not touched"
        return text + (f". The disk file(s) {', '.join(left)} could not be removed; delete them by hand" if left else ""), True, False


def _recover_plan(node: Any, plan: dict[str, Any]) -> tuple[str, bool, bool]:
    name = plan["name"]
    guard = plan.get("guard_snapshot")
    completed = (
        f"VM {name} already used the restored disks and its snapshot {guard} was removed, so the restore was completed "
        "instead of rolled back"
    )
    if plan.get("committed"):
        problems, kept = finish(node, plan, patience=RECOVERY_PATIENCE_SECONDS)
        return completed + _tail(problems, name, node, plan, kept), True, True
    vm = _vm(node, plan["vm_id"], name)
    if _guard(vm, plan) is None and any(disk["new_path"] in attached_paths(node, plan["vm_id"], name) for disk in plan["disks"]):
        problems, kept = finish(node, plan, patience=RECOVERY_PATIENCE_SECONDS)
        return completed + _tail(problems, name, node, plan, kept), True, True
    had_guard = _guard(vm, plan) is not None
    problems = rollback(node, plan, patience=RECOVERY_PATIENCE_SECONDS)
    if plan.pop("put_back_failed", False):
        return f"VM {name} could not be taken back: {'; '.join(problems)}", False, False
    powered = " and powered on again" if plan.get("was_powered_on") else ""
    text = (
        f"VM {name} is back as it was before the restore{powered}: its snapshot {guard} was reverted and removed"
        if had_guard
        else f"VM {name} was not changed{powered}"
    ) + "; the restored disks and the temporary VM were removed"
    return text + (f". {'; '.join(problems)}" if problems else ""), True, False


def _tail(problems: list[str], name: str, node: Any, plan: dict[str, Any], kept: list[str]) -> str:
    parts = [*problems, _held(node, plan, kept)]
    parts = [part for part in parts if part]
    return f". {'; '.join(parts)}" if parts else ""
