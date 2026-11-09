# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any

from pypsrp.exceptions import WinRMTransportError

from . import vhdx_tool
from .hyperv import CONNECTION_ERRORS, HyperVNode, HyperVUnreachableError
from .hyperv_restore import _restore_runs, vm_base_folder

PLATFORM = "hyper-v"


def _state(row: dict[str, Any]) -> dict[str, Any]:
    disks = [
        {
            "slot": str(disk.get("slot") or ""),
            "path": str(disk.get("path") or ""),
            "size_bytes": int(disk.get("size_bytes") or 0),
            "parent_path": str(disk.get("parent_path") or ""),
            "last_write_utc": str(disk.get("last_write_utc") or ""),
        }
        for disk in row.get("disks") or []
        if isinstance(disk, dict)
    ]
    checkpoints = [str(item.get("name") or item.get("id") or "") for item in row.get("checkpoints") or [] if isinstance(item, dict)]
    status = str(row.get("status") or "")
    return {
        "vm_id": str(row.get("vm_id") or ""),
        "name": str(row.get("name") or ""),
        "host": str(row.get("computer") or ""),
        "powered_on": str(row.get("state") or "").lower() not in ("off", ""),
        "marker_text": str(row.get("notes") or ""),
        "snapshots": checkpoints,
        "disks": disks,
        "fingerprint": "|".join(f"{disk['slot']}={disk['last_write_utc']}" for disk in disks),
        "busy": "merg" in status.lower(),
        "consolidation_needed": not checkpoints and any(disk["path"].lower().endswith(".avhdx") for disk in disks),
        "configuration_location": str(row.get("configuration_location") or ""),
        "start_marker": int(row["last_start_record"]) if row.get("last_start_record") is not None else None,
    }


class HyperVReplicaError(RuntimeError):
    pass


DROPPED_CONNECTION = (WinRMTransportError, HyperVUnreachableError, *CONNECTION_ERRORS)


def _script(node: HyperVNode, name: str, script_args: dict[str, Any], host: str) -> list[dict[str, Any]]:
    try:
        return node._run_script(name, script_args, host)
    except DROPPED_CONNECTION:
        node.disconnect()
        node.connect()
        return node._run_script(name, script_args, host)


def _each_node(node: HyperVNode, script_args: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reached = False
    for name in node.nodes:
        try:
            rows.extend(row for row in _script(node, "replica_state.ps1", script_args, name) if isinstance(row, dict))
            reached = True
        except HyperVUnreachableError:
            continue
    if not reached:
        raise HyperVUnreachableError(node.host, [], "no node of the Hyper-V inventory answered")
    return rows


def find_replica(node: HyperVNode, vm_id: str) -> dict[str, Any] | None:
    rows = _each_node(node, {"vm_id": vm_id})
    return _state(rows[0]) if rows else None


def replicas_with_marker(node: HyperVNode, marker_prefix: str) -> list[dict[str, Any]]:
    return [_state(row) for row in _each_node(node, {"marker": marker_prefix})]


def replica_folder(datastore: str, replica_name: str) -> str:
    return f"{vm_base_folder(datastore)}\\{replica_name}"


def tagged_files(node: HyperVNode, host: str, folder: str, tag: str, *, subfolders: str = "") -> list[dict[str, Any]]:
    rows = _script(node, "replica_state.ps1", {"folder": folder, "tag": tag, "subfolders": subfolders}, node.resolve_node(host))
    return [
        {"path": str(row.get("path") or ""), "size_bytes": int(row.get("size_bytes") or 0), "attached": bool(row.get("attached"))}
        for row in rows
        if isinstance(row, dict) and row.get("path")
    ]


def rebuild_location(state: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    return state["host"] or settings["target_host"], settings["target_datastore"]


def staging_root(datastore: str) -> str:
    return vm_base_folder(datastore)


def detached_staging_files(node: HyperVNode, host: str, datastore: str, tag: str) -> list[str]:
    return [
        item["path"]
        for item in tagged_files(node, host, staging_root(datastore), tag, subfolders="*-staging-*")
        if not item["attached"]
    ]


def remove_files(node: HyperVNode, host: str, paths: list[str]) -> list[str]:
    return node.remove_files(node.resolve_node(host), paths)


def destroy_replica(node: HyperVNode, state: dict[str, Any]) -> list[str]:
    computer = state["host"]
    _script(node, "remove_vm.ps1", {"vm_id": state["vm_id"]}, computer)
    paths = [disk["path"] for disk in state["disks"] if disk["path"]]
    left = node.remove_files(computer, paths)
    folders = sorted({str(PureWindowsPath(path).parent) for path in paths}, key=len, reverse=True)
    if state.get("configuration_location"):
        folders.append(state["configuration_location"])
    node.remove_empty_folders(computer, folders)
    return left


def _checkpoint(node: HyperVNode, state: dict[str, Any], action: str, name: str, timeout_seconds: int) -> list[str]:
    rows = _script(
        node,
        "replica_checkpoint.ps1",
        {"vm_id": state["vm_id"], "action": action, "name": name, "timeout_seconds": int(timeout_seconds)},
        state["host"],
    )
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    return [str(path) for path in row.get("disks") or []]


def create_snapshot(node: HyperVNode, state: dict[str, Any], name: str, timeout_seconds: int) -> dict[str, Any]:
    _checkpoint(node, state, "create", name, timeout_seconds)
    created = find_replica(node, state["vm_id"])
    if created is None or not all(disk["path"].lower().endswith(".avhdx") for disk in created["disks"]):
        raise HyperVReplicaError(f"The checkpoint {name} of {state['name']} did not move its disks to differencing disks")
    return created


def revert_snapshot(node: HyperVNode, state: dict[str, Any], name: str, timeout_seconds: int) -> None:
    _checkpoint(node, state, "revert", name, timeout_seconds)


def remove_snapshot(node: HyperVNode, state: dict[str, Any], name: str, timeout_seconds: int) -> None:
    _checkpoint(node, state, "remove", name, timeout_seconds)


def consolidate(node: HyperVNode, state: dict[str, Any], timeout_seconds: int) -> None:
    raise HyperVReplicaError(
        f"The disks of {state['name']} are differencing disks ({', '.join(disk['path'] for disk in state['disks'])}) "
        "without a checkpoint over them: Hyper-V did not finish merging them. Merge them in Hyper-V Manager "
        "(Edit Disk) or create a new Replication Job"
    )


def write_increments(
    node: HyperVNode,
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
    computer = state["host"]
    written = 0
    for layer in _restore_runs(layers, repository):
        storage_args, storage_env = vhdx_tool.storage_invocation(repository, abr_path=layer["abr_path"])
        command = [
            "restore",
            "--json",
            *(["--source-names", ",".join(layer["source_names"])] if layer.get("source_names") else ["--name", layer["abr_backup_name"]]),
            *storage_args,
            "--vhdx",
            disk["path"],
            "--force",
        ]
        if encryption_key:
            command.extend(["--key", encryption_key])
        run = node.run_disk_tool(
            computer,
            command,
            env=storage_env,
            on_progress=lambda percent: report(percent, 0),
            should_cancel=should_cancel,
            timeout=timeout_seconds,
        )
        result = run.get("result") or {}
        if run.get("exit_code") != 0 or result.get("status") not in (None, "ok"):
            if vhdx_tool.ended_unexpectedly(run.get("result"), run.get("lines") or [], run.get("exit_code")):
                raise HyperVReplicaError(
                    vhdx_tool.unexpected_end_message(
                        computer, run["exit_code"], run.get("lines") or [], f"updating disk {disk['slot']} of {state['name']}"
                    )
                )
            detail = vhdx_tool.failure_message(result, run.get("lines") or [], run.get("exit_code"))
            raise HyperVReplicaError(f"Updating disk {disk['slot']} of {state['name']} from {layer['abr_backup_name']} failed: {detail}")
        written += int(result.get("written_bytes") or result.get("bytes_written") or 0)
    return {"written_bytes": written}




def release_staging_disks(node: HyperVNode, staging: dict[str, Any]) -> None:
    _script(node, "remove_vm.ps1", {"vm_id": staging["vm_id"]}, staging["host"])


def swap_disks(node: HyperVNode, replica: dict[str, Any], staging: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    disks = []
    for change in changes:
        controller, _, location = change["slot"].partition(":")
        kind = "".join(ch for ch in controller if ch.isalpha())
        number = controller[len(kind):]
        disks.append(
            {
                "controller_type": kind,
                "controller_number": int(number or 0),
                "controller_location": int(location or 0),
                "path": change["new_path"],
            }
        )
    _script(node, "replace_disks.ps1", {"vm_id": replica["vm_id"], "disks": disks}, replica["host"])


def put_back_disks(node: HyperVNode, replica: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    swap_disks(node, replica, {}, [{**change, "new_path": change["old_path"]} for change in changes])


def delete_disk_files(node: HyperVNode, host: str, paths: list[str]) -> list[str]:
    computer = node.resolve_node(host)
    left = node.remove_files(computer, paths)
    node.remove_empty_folders(computer, sorted({str(PureWindowsPath(path).parent) for path in paths}, key=len, reverse=True))
    return left


def remove_staging(node: HyperVNode, staging: dict[str, Any]) -> list[str]:
    return destroy_replica(node, staging)


def discard_staging_shell(node: HyperVNode, staging: dict[str, Any]) -> None:
    current = find_replica(node, staging["vm_id"])
    if current is not None:
        if current["disks"]:
            raise HyperVReplicaError(f"The staging VM {staging['name']} still holds disks; it is not removed")
        _script(node, "remove_vm.ps1", {"vm_id": staging["vm_id"]}, current["host"])
    folder = str(staging.get("configuration_location") or "")
    if folder:
        node.remove_empty_folders(node.resolve_node(staging["host"]), [folder])


def remove_folder(node: HyperVNode, host: str, folder: str) -> None:
    node.remove_empty_folders(node.resolve_node(host), [folder.replace("/", "\\")])


HARDWARE_KEYS = ("num_cpu", "memory_mb", "dynamic_memory", "memory_minimum_mb", "memory_maximum_mb", "memory_buffer")


def read_config(node: HyperVNode, state: dict[str, Any]) -> dict[str, Any]:
    rows = _script(node, "vm_config.ps1", {"vm_id": state["vm_id"]}, state["host"])
    if not rows or not isinstance(rows[0], dict):
        raise HyperVReplicaError(f"Hyper-V returned no configuration for replica {state['name']}")
    config = dict(rows[0])
    config["network_adapters"] = [
        {
            "id": str(adapter.get("adapter_id") or "").upper(),
            "label": str(adapter.get("label") or ""),
            "network": str(adapter.get("network") or ""),
            "connected": bool(adapter.get("network")),
            "vlan_mode": str(adapter.get("vlan_mode") or ""),
            "vlan_id": int(adapter.get("vlan_id") or 0),
            "legacy": bool(adapter.get("legacy")),
        }
        for adapter in config.get("network_adapters") or []
        if isinstance(adapter, dict) and adapter.get("adapter_id")
    ]
    return config


def networks(node: HyperVNode, state: dict[str, Any], names: list[str]) -> set[str]:
    switches = {str(item.get("name") or "") for item in node.host_resources(node.resolve_node(state["host"]))["switches"]}
    return {name for name in names if name in switches}


def apply_config(
    node: HyperVNode,
    state: dict[str, Any],
    hardware: dict[str, Any],
    *,
    remove: list[str],
    change: list[dict[str, Any]],
    add: list[dict[str, Any]],
) -> list[str]:
    if hardware:
        _script(node, "set_hardware.ps1", {"vm_id": state["vm_id"], **hardware}, state["host"])
    if not (remove or change or add):
        return []
    rows = _script(
        node,
        "replica_config.ps1",
        {"vm_id": state["vm_id"], "remove": list(remove), "change": list(change), "add": list(add)},
        state["host"],
    )
    added = (rows[0] if rows and isinstance(rows[0], dict) else {}).get("added") or []
    return [str(item).upper() for item in ([added] if isinstance(added, str) else added)]


def rename(node: HyperVNode, state: dict[str, Any], new_name: str) -> None:
    _script(node, "replica_config.ps1", {"vm_id": state["vm_id"], "rename": new_name}, state["host"])


def unsynced(current: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    from .hyperv_restore import _kept_as_they_are

    source = {key: value for key, value in wanted.items() if key != "hard_drives"}
    return [_for_replica(item) for item in _kept_as_they_are(current, source, False)]


def _for_replica(text: str) -> str:
    for before, after in (
        ("(the backup: ", "(the source VM: "),
        (" was added after the backup", " is on the replica but not on the source VM"),
        (" of the backup is gone", " of the source VM is not on the replica"),
    ):
        text = text.replace(before, after)
    return text
