# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Restore a Proxmox VE VM from a restore point, as a new VM or in place.

1. Work out each disk's chain: its newest full backup up to the restore point, then the incrementals on top of it.
   The backups of one chain may lie in the folders of several sessions.
2. Prepare the guest configuration the backup recorded, from which PVE restores the VM: the new VM's name, the bridge
   of each network adapter and the storage of each disk.
3. Write the run file /run/abr/restore-<vmid>-<ctime>.json on the target node over SSH, and start the restore with the
   inventory's API token: as a new VM with new MAC addresses, or over the source VM, which is stopped first. The
   provider serves each disk's chain with nbdkit, and PVE copies it to a new volume with qemu-img.
4. Follow the task. A restore to a new VM that fails leaves an empty VM behind, which is destroyed.

PVE removes the disks of a VM it restores over before it writes the restored ones, so a failed in-place restore
leaves that VM without them. The task runs in pvedaemon and does not depend on abr-core: its UPID is kept with the
session, so a session the server lost by a restart follows the same task again when it runs once more.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection, Mapping
from urllib.parse import quote

from ...core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES
from ...core.server_defaults import env_int
from ...core.storage_tool import chain_source_names, describe_bytes
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.utils import utc_now
from ...services.synthetic_full import starts_disk_chain
from ..vcenter.vcenter_backup import VCenterBackupError, _emit_progress
from ..vcenter.vcenter_restore import _resolve_point_chain
from .pve import (
    PveError,
    PveNode,
    parse_property_string,
    vm_disks,
    vm_name_problem,
    vm_network_adapters,
    volume_storage,
)
from .pve_api import PveApiError, Upid, task_error, task_succeeded
from .pve_backup import RUN_FOLDER, STARTING, WORKER_MODE, drive_device, find_started_task, node_repository
from .setup import PveSetupError, ensure_package, package_path
from .node_helper import SecretsOffer
from .ssh import NodeShell, SshError

logger = logging.getLogger(__name__)

HYPERVISOR_KIND = "proxmox-ve"
NEW_VM_ATTEMPTS = 3
STATE_WAIT_SECONDS = 60.0

ProgressCallback = Callable[[dict[str, object]], None] | None

_MAP_LINE = re.compile(r"^#qmdump#map:([^:\s]+):([^:\s]+):([^:\s]*):([^:\s]*):$")
_NET_LINE = re.compile(r"^(net\d+):\s*(\S+)\s*$")
_NAME_LINE = re.compile(r"^name:")


class PveRestoreError(RuntimeError):
    pass


def _emit(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    _emit_progress(progress_callback, stage, progress, message, worker_mode=WORKER_MODE, **extra)


def _gib(value: int | float | None) -> str:
    return "unknown" if value is None else describe_bytes(int(value))


# ---- names ---------------------------------------------------------------------------------------------------------


def archive_name(vmid: str, ctime: int) -> str:
    """The volume a restore reads on the abr storage, without the storage ID; it names the run file too."""
    return f"backup/{vmid}/qemu-{ctime}"


def run_base(vmid: str, ctime: int) -> str:
    """The name of a restore's run on its node, after the restore point: its files are <base>.json and .result.json in
    /run/abr."""
    return f"restore-{vmid}-{ctime}"


def run_file_path(vmid: str, ctime: int, suffix: str = "json") -> str:
    return f"{RUN_FOLDER}/{run_base(vmid, ctime)}.{suffix}"


def point_ctime(point: Mapping[str, Any]) -> int:
    """The restore point's time in seconds since 1970, as PVE names backups."""
    text = str(point.get("created_at") or "").strip().replace("Z", "+00:00")
    try:
        created = datetime.fromisoformat(text)
    except ValueError:
        return int(time.time())
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return int(created.timestamp())


# ---- chains --------------------------------------------------------------------------------------------------------


def disk_chain(points: list[dict[str, Any]], manifests: list[dict[str, Any]], drive: str) -> list[dict[str, Any]]:
    """The backups of one disk to restore, oldest first: from its newest full backup up to the restore point.

    points and manifests run from the oldest point of the restore point's chain to the restore point itself. A point
    without the disk ends its chain, since a disk added again later starts with a full backup.
    """
    layers: list[dict[str, Any]] = []
    for point, manifest in zip(points, manifests):
        entry = next(
            (disk for disk in manifest.get("disks") or [] if isinstance(disk, dict) and disk.get("drive") == drive),
            None,
        )
        if entry is None:
            layers = []
            continue
        if not entry.get("abr_path") or not entry.get("abr_backup_name"):
            raise PveRestoreError(f"Restore point {point.get('id')} records no stored backup of disk {drive}")
        layer = {
            "point_id": str(point.get("id") or ""),
            "abr_path": str(entry["abr_path"]),
            "abr_backup_name": str(entry["abr_backup_name"]),
            "store_mode": str(entry.get("store_mode") or ""),
        }
        layers = [layer] if starts_disk_chain(entry) else [*layers, layer]
    if not layers or not starts_disk_chain({"store_mode": layers[0]["store_mode"]}):
        raise PveRestoreError(f"The restore point's chain holds no full backup of disk {drive} to start from")
    return layers


def qualified_chains(chains: dict[str, list[dict[str, Any]]]) -> tuple[str, dict[str, list[str]]]:
    """One repository path for the run file, and each disk's chain as names below it."""
    paths = [layer["abr_path"] for layers in chains.values() for layer in layers]
    names = [layer["abr_backup_name"] for layers in chains.values() for layer in layers]
    common, qualified = chain_source_names(paths, names)
    result: dict[str, list[str]] = {}
    position = 0
    for drive, layers in chains.items():
        result[drive] = qualified[position : position + len(layers)]
        position += len(layers)
    return common, result


# ---- the guest configuration ---------------------------------------------------------------------------------------


def map_lines(guest_config: str) -> dict[str, dict[str, str]]:
    """The disks the guest configuration restores, by config key: the device, storage and format PVE recorded."""
    disks = {}
    for line in guest_config.splitlines():
        match = _MAP_LINE.match(line)
        if match:
            key, device, storage, image_format = match.groups()
            disks[key] = {"device": device, "storage": storage, "format": image_format}
    return disks


def _net_value(value: str, bridge: str | None, link_down: bool) -> str:
    parts = [part for part in value.split(",") if part]
    keys = [part.partition("=")[0] for part in parts]
    for key, wanted in (("bridge", bridge), ("link_down", "1" if link_down else None)):
        if wanted is None:
            continue
        if key in keys:
            parts[keys.index(key)] = f"{key}={wanted}"
        else:
            parts.append(f"{key}={wanted}")
            keys.append(key)
    return ",".join(parts)


def restore_guest_config(
    guest_config: str,
    *,
    name: str | None = None,
    storages: Mapping[str, str] | None = None,
    bridges: Mapping[str, str] | None = None,
    link_down: bool = False,
    left_out: Collection[str] = (),
) -> str:
    """The guest configuration PVE restores the VM from: the backup's, with the VM's name, the storage of each disk
    and the bridge of each network adapter changed, and the adapters' links down when asked. A disk left out loses its
    map line, so PVE keeps its config line as it is and restores nothing to it."""
    storages = storages or {}
    bridges = bridges or {}
    lines = []
    named = False
    in_main = True
    for line in guest_config.splitlines():
        in_main = in_main and not line.startswith("[")
        disk = _MAP_LINE.match(line)
        net = _NET_LINE.match(line) if in_main else None
        if disk and disk.group(1) in left_out:
            continue
        if disk and disk.group(1) in storages:
            key, device, _storage, image_format = disk.groups()
            line = f"#qmdump#map:{key}:{device}:{storages[key]}:{image_format}:"
        elif in_main and name is not None and _NAME_LINE.match(line):
            line = f"name: {name}"
            named = True
        elif net and (net.group(1) in bridges or link_down):
            line = f"{net.group(1)}: {_net_value(net.group(2), bridges.get(net.group(1)), link_down)}"
        lines.append(line)
    if name is not None and not named:
        lines.insert(0, f"name: {name}")
    return "\n".join(lines) + "\n"


def is_passthrough(disk: Mapping[str, Any]) -> bool:
    """A physical disk the VM uses by its path, which no storage holds."""
    return str(disk.get("datastore_path") or "").startswith("/")


def backup_adapters(vm_config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The network adapters the backup recorded, in the order the restore wizard numbers them."""
    adapters = vm_config.get("network_adapters")
    if isinstance(adapters, list) and adapters:
        return [dict(adapter) for adapter in adapters if isinstance(adapter, dict)]
    config = vm_config.get("config") if isinstance(vm_config.get("config"), dict) else {}
    return [
        {"label": adapter["name"], "network": adapter["network"], "connected": adapter["connected"]}
        for adapter in vm_network_adapters(str(vm_config.get("vmid") or ""), config)
    ]


def adapter_targets(vm_config: Mapping[str, Any], parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each network adapter of the backup with the bridge it lands on: as mapped by its position or its bridge, and
    its own bridge otherwise."""
    by_index = {
        int(entry["index"]): str(entry.get("target_network") or "")
        for entry in parameters.get("network_adapter_map") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }
    by_name = parameters.get("network_map") if isinstance(parameters.get("network_map"), dict) else {}
    planned = []
    for index, adapter in enumerate(backup_adapters(vm_config)):
        source = str(adapter.get("network") or "")
        planned.append(
            {
                "label": str(adapter.get("label") or f"net{index}"),
                "source_network": source,
                "target_network": by_index.get(index) or str(by_name.get(source) or "") or source,
            }
        )
    return planned


def disk_targets(
    disks: list[dict[str, Any]], parameters: Mapping[str, Any], current: Mapping[str, Any] | None
) -> dict[str, str]:
    """The storage each disk is restored to, by config key.

    In place, a disk goes where the VM keeps it now, or where it was backed up when the VM no longer has it. A new
    VM's disk goes where the restore maps it: by its position, by the storage it was backed up from, or to the
    restore's storage.
    """
    if current is not None:
        return {
            disk["drive"]: volume_storage(
                parse_property_string(str(current.get(disk["drive"]) or ""), "file").get("file", "")
            )
            or str(disk.get("storage") or "")
            for disk in disks
        }
    by_index = {
        int(entry["index"]): str(entry.get("target_datastore") or "")
        for entry in parameters.get("disk_datastore_map") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }
    by_storage = parameters.get("datastore_map") if isinstance(parameters.get("datastore_map"), dict) else {}
    default = str(parameters.get("target_datastore_name") or "")
    return {
        disk["drive"]: by_index.get(position)
        or str(by_storage.get(str(disk.get("storage") or "")) or "")
        or default
        or str(disk.get("storage") or "")
        for position, disk in enumerate(disks, start=1)
    }


# ---- the run file --------------------------------------------------------------------------------------------------


def build_run_file(
    *,
    run_id: str,
    task_user: str,
    storage: str,
    archive: str,
    target_vmid: str,
    guest_config: str,
    firewall_config: str | None,
    repository: dict[str, Any],
    secrets: dict[str, str],
    disks: list[dict[str, Any]],
    chains: dict[str, list[str]],
    now: datetime,
) -> dict[str, Any]:
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "format": "abr-pve-run",
        "version": 1,
        "kind": "restore",
        "run_id": run_id,
        "created": now.strftime(stamp),
        "expires": (now + timedelta(minutes=env_int("ALTASTOR_PVE_RUN_FILE_MINUTES"))).strftime(stamp),
        "task_user": task_user,
        "storage": storage,
        "archive": archive,
        "target_vmid": int(target_vmid),
        "guest_config": guest_config,
        "firewall_config": firewall_config or None,
        "repository": repository,
        "disks": {
            drive_device(disk["drive"]): {"size": int(disk["capacity_bytes"]), "chain": chains[disk["drive"]]}
            for disk in disks
        },
        "secrets": {key: value for key, value in secrets.items() if value},
    }


# ---- one restore of an archive at a time ---------------------------------------------------------------------------

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _archive_lock(hypervisor_id: str, node: str, archive: str) -> threading.Lock:
    """Two restores of one restore point on one node would share a run file."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(f"{hypervisor_id}/{node}/{archive}", threading.Lock())


def _acquire(lock: threading.Lock, stopped: Callable[[], bool], waiting: Callable[[], None]) -> bool:
    told = False
    while not lock.acquire(timeout=5.0):
        if stopped():
            return False
        if not told:
            waiting()
            told = True
    return True


# ---- following the task --------------------------------------------------------------------------------------------

_IMPORTING = re.compile(r"^importing drive '(drive-[a-z]+[0-9]+(?:-backup)?)' from '(\S+)'$")
_SERVING = re.compile(r"^INFO: serving (drive-\S+) from a chain of ([0-9]+) backups? at (\S+)$")
_TRANSFERRED = re.compile(r"^transferred .* \(([0-9]+(?:\.[0-9]+)?)%\)$")


class RestoreLog:
    """Turns the restore task's log into progress and messages: the provider serving each disk, and qemu-img's
    percentages while PVE copies it. Each disk counts by its size."""

    def __init__(self, sizes: dict[str, int], emit: Callable[..., None], start: int = 20, span: int = 70) -> None:
        self.sizes = {device: max(size, 1) for device, size in sizes.items()}
        self.percent: dict[str, float] = {}
        self.current = ""
        self.reported = -10
        self.emit = emit
        self.start = start
        self.span = span
        self.lines = 0
        self.errors: list[str] = []

    def progress(self) -> int:
        total = sum(self.sizes.values()) or 1
        done = sum(self.sizes[device] * self.percent.get(device, 0) / 100 for device in self.sizes)
        return self.start + int(self.span * done / total)

    def line(self, text: str) -> None:
        self.lines += 1
        text = text.rstrip()
        transferred = _TRANSFERRED.match(text)
        if transferred and self.current:
            percent = min(float(transferred.group(1)), 100.0)
            self.percent[self.current] = percent
            if percent >= self.reported + 10 or percent >= 100:
                self.reported = int(percent // 10 * 10)
                self.emit("transfer", self.progress(), f"{self.current}: {int(percent)}%", log_entry=False)
            return
        serving = _SERVING.match(text)
        importing = _IMPORTING.match(text)
        if serving:
            count = int(serving.group(2))
            backups = f"{count} backup{'' if count == 1 else 's'}"
            self.emit(
                "transfer", self.progress(), f"{serving.group(1)}: serving a chain of {backups} at {serving.group(3)}"
            )
        elif importing:
            if self.current:
                self.percent[self.current] = 100
            self.current = importing.group(1)
            self.reported = -10
            size = self.sizes.get(self.current)
            self.emit(
                "transfer",
                self.progress(),
                f"{self.current}: PVE copies it to its new volume" + (f" ({_gib(size)})" if size else ""),
            )
        elif text.startswith(("ERROR:", "TASK ERROR:", "error ", "unable to restore")):
            message = text.split(":", 1)[1].strip() if text.startswith(("ERROR:", "TASK ERROR:")) else text
            self.errors.append(message)
            self.emit("transfer", self.progress(), text, log_level="warn")
        elif text.startswith(("WARN:", "warning:")):
            self.emit("transfer", self.progress(), text, log_level="warn")


# ---- the session ---------------------------------------------------------------------------------------------------


def _hypervisor(task: dict[str, Any]) -> dict[str, Any]:
    hypervisor = HYPERVISORS.get(str(task.get("hypervisor_id") or ""))
    if not hypervisor:
        raise PveRestoreError(f"Hypervisor not found: {task.get('hypervisor_id')}")
    if hypervisor.get("kind") != HYPERVISOR_KIND:
        raise PveRestoreError(f"A Proxmox VE restore needs a Proxmox VE inventory, got {hypervisor.get('kind')!r}")
    return hypervisor


def _load_manifest(point: dict[str, Any], manifest_path: str = "") -> dict[str, Any]:
    """A restore point's manifest, from this appliance or else from the repository."""
    from ...services.backup_catalog import _load_backup_manifest_payload

    artifact = dict(point.get("artifact") or {}) if isinstance(point.get("artifact"), dict) else {}
    if manifest_path:
        artifact["manifest_path"] = manifest_path
    manifest = _load_backup_manifest_payload(point, artifact)
    if not isinstance(manifest, dict) or not manifest:
        raise PveRestoreError(f"The manifest of restore point {point.get('id')} cannot be read")
    if str(manifest.get("hypervisor_kind") or "") != HYPERVISOR_KIND:
        raise PveRestoreError(f"Restore point {point.get('id')} was not written by a Proxmox VE backup")
    return manifest


def _record_run(progress_callback: ProgressCallback, run: dict[str, Any]) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback({"updated_at": utc_now(), "log_entry": False, "pve_run": dict(run)})
    except TaskStopRequested:
        pass


def _remove_run_files(shell: NodeShell, vmid: str, ctime: int) -> None:
    try:
        shell.helper.remove_run(run_base(vmid, ctime))
    except (SshError, OSError) as exc:
        logger.warning("Could not remove the run files of %s on %s: %s", run_base(vmid, ctime), shell.address, exc)


def _read_result(shell: NodeShell, vmid: str, ctime: int, run_id: str) -> dict[str, Any] | None:
    try:
        data = shell.helper.get_result(f"{run_base(vmid, ctime)}.result.json")
    except (SshError, OSError) as exc:
        logger.warning("Could not read the result file of the restore on %s: %s", shell.address, exc)
        return None
    if not data:
        return None
    try:
        result = json.loads(data)
    except ValueError:
        return None
    return result if isinstance(result, dict) and str(result.get("run_id") or "") == run_id else None


def _failure(status: dict[str, Any], result: dict[str, Any] | None, errors: list[str]) -> str:
    """Why a restore failed: the task's own error, which carries the provider's reason, and an error line of the task
    log that tells more. PVE's note that it left the VM behind is not one: abr-core removes that VM."""
    reason = task_error(status) or str((result or {}).get("error") or "").strip() or "unknown error"
    more = [line for line in errors if line not in reason and reason not in line and "NOT cleaned up" not in line]
    return reason.rstrip(".") + (f"; the task log says: {more[0].rstrip('.')}" if more else "")


def _vm_path(node: str, vmid: str) -> str:
    return f"/nodes/{quote(node, safe='')}/qemu/{quote(str(vmid), safe='')}"


def _run_task(cluster: PveNode, path: str, method: str = "POST", **params: Any) -> dict[str, Any]:
    upid = cluster.api.start_task(path, method=method, **params)
    return cluster.api.wait_task(upid)


def _stop_vm(cluster: PveNode, node: str, vmid: str, vm_name: str, emit: Callable[..., None]) -> None:
    """Turns the VM off, as PVE restores only over a VM that does not run."""
    if cluster.vm_state(node, vmid) != "running":
        return
    emit("prepare", 14, f"Turning off {vm_name} ({vmid}) on node {node} for the in-place restore")
    status = _run_task(cluster, f"{_vm_path(node, vmid)}/status/stop")
    if not task_succeeded(status):
        raise PveRestoreError(f"{vm_name} ({vmid}) could not be turned off: {task_error(status)}")
    # An HA-managed VM stops after its task has ended.
    deadline = time.monotonic() + STATE_WAIT_SECONDS
    while cluster.vm_state(node, vmid) == "running":
        if time.monotonic() > deadline:
            raise PveRestoreError(f"{vm_name} ({vmid}) still runs {int(STATE_WAIT_SECONDS)}s after it was turned off")
        time.sleep(2)


def _destroy_vm(cluster: PveNode, node: str, vmid: str) -> str:
    """Removes the VM a failed restore created; returns what is left to do by hand, or an empty text."""
    try:
        status = _run_task(cluster, _vm_path(node, vmid), "DELETE", purge=1, **{"destroy-unreferenced-disks": 1})
    except PveApiError as exc:
        return f"VM {vmid} the restore created on node {node} could not be removed ({exc}); remove it by hand"
    if not task_succeeded(status):
        return (
            f"VM {vmid} the restore created on node {node} could not be removed ({task_error(status)}); remove it "
            "by hand"
        )
    return ""


def _in_place_note(cluster: PveNode, node: str, vmid: str, vm_name: str) -> str:
    """What a failed restore left of the VM it replaced: PVE removes its disks before it writes the restored ones."""
    try:
        left = vm_disks(vmid, cluster.vm_config(node, vmid))
    except PveApiError:
        return f". Check {vm_name} ({vmid}): PVE may have removed its disks before the restore failed"
    if left:
        return f". {vm_name} ({vmid}) keeps its {len(left)} disk(s)"
    return f". PVE had removed the disks of {vm_name} ({vmid}) before the restore failed; restore it again"


def _check_storages(cluster: PveNode, node: str, storages: dict[str, str]) -> None:
    available = cluster.image_storages(node)
    for drive, storage in sorted(storages.items()):
        info = available.get(storage)
        if info is None:
            raise PveRestoreError(
                f"Storage {storage} for disk {drive} does not hold VM disks on node {node}, or is not enabled there"
            )
        if not info.get("active"):
            raise PveRestoreError(f"Storage {storage} for disk {drive} is not active on node {node}")


def nbd_devices(shell: NodeShell) -> tuple[int, int]:
    """The kernel NBD devices of a node and how many are in use; 0 devices when the nbd module is not loaded."""
    return shell.helper.nbd_devices()


def _check_nbd_devices(shell: NodeShell, node: str) -> None:
    """A restore holds one kernel NBD device while PVE copies a disk, so it needs one free on its node."""
    total, used = nbd_devices(shell)
    if total == 0:
        raise PveRestoreError(
            f"The nbd kernel module is not loaded on node {node}; restores need it. The abr-pve-backup package loads "
            "it at boot and when it is installed"
        )
    if used >= total:
        raise PveRestoreError(
            f"All {total} kernel NBD devices of node {node} are in use, by other restores or other NBD users; each "
            "restore needs one while PVE copies a disk. Run the restore when one has ended"
        )


def _check_bridges(cluster: PveNode, node: str, adapters: list[dict[str, Any]]) -> None:
    bridges = set(cluster.bridges(node))
    for adapter in adapters:
        if adapter["target_network"] and adapter["target_network"] not in bridges:
            raise PveRestoreError(
                f"Network adapter {adapter['label']} would land on bridge {adapter['target_network']}, which node "
                f"{node} does not have; map it to one of {', '.join(sorted(bridges)) or 'its bridges'}"
            )


def _find_unrecorded(
    cluster: PveNode, earlier_run: dict[str, Any], task_id: str, emit: Callable[..., None]
) -> dict[str, Any] | None:
    """The restore task a session started before the server stopped, to follow again, or None when it never started."""
    node = str(earlier_run.get("node") or "")
    target_vmid = str(earlier_run.get("target_vmid") or "")
    upid = find_started_task(
        cluster.api,
        node,
        vmid=target_vmid,
        task_type="qmrestore",
        user=cluster.access.token_id,
        run_id=str(earlier_run.get("run_id") or task_id),
        since=float(earlier_run.get("prepared_at") or 0),
        # A VMID the session chose for a new VM is its own, even when the provider refused the run before it logged it.
        any_of_vm=bool(earlier_run.get("created_vm")),
    )
    if upid is None:
        emit("prepare", 8, f"The restore this session prepared on node {node} before the server stopped never started")
        return None
    emit(
        "prepare", 8, f"Found restore task {upid} on node {node}, which this session started before the server stopped"
    )
    return {**earlier_run, "upid": upid, "started_at": utc_now()}


def run_pve_restore(task: dict[str, Any], progress_callback: ProgressCallback = None) -> dict[str, Any]:
    start_time = time.time()
    task_id = str(task.get("id") or "")
    hypervisor = _hypervisor(task)
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    existing = task.get("result") if isinstance(task.get("result"), dict) else {}
    earlier_run = existing.get("pve_run") if isinstance(existing.get("pve_run"), dict) else {}
    resumed = earlier_run if earlier_run.get("upid") else None
    # The server stopped after the session prepared its restore and before it recorded the task's UPID.
    unrecorded = resumed is None and earlier_run.get("state") == STARTING

    point_id = str(parameters.get("point_id") or "")
    point = BACKUPS.get(point_id) if point_id else None
    if not isinstance(point, dict):
        raise PveRestoreError(f"Restore point {point_id} is no longer in the catalog")
    manifest = _load_manifest(point, str(parameters.get("manifest_path") or ""))
    vm_config = manifest.get("vm_config") if isinstance(manifest.get("vm_config"), dict) else {}
    guest_config = str(vm_config.get("guest_config") or "")
    if not guest_config:
        raise PveRestoreError(f"Restore point {point_id} holds no guest configuration for PVE to restore the VM from")
    disks = [disk for disk in manifest.get("disks") or [] if isinstance(disk, dict) and disk.get("drive")]
    if not disks:
        raise PveRestoreError(f"Restore point {point_id} records no disks to restore")
    recorded = map_lines(guest_config)
    unknown = sorted(set(recorded) - {disk["drive"] for disk in disks})
    if unknown:
        raise PveRestoreError(
            f"The guest configuration of restore point {point_id} restores {', '.join(unknown)}, which the point "
            "holds no backup of"
        )
    repository = REPOSITORIES.get(str(parameters.get("repository_id") or ""))
    if not repository:
        raise PveRestoreError("The repository of this restore point is no longer registered")
    encryption_key = str(parameters.get("encryption_key") or "").strip()

    mode = "in_place" if str(parameters.get("restore_mode") or "") == "in_place" else "new_vm"
    mode_label = "In-place" if mode == "in_place" else "Out-of-place"
    point_created = str(point.get("created_at") or manifest.get("created_at") or "")
    source_vmid = str(manifest.get("vm_external_id") or vm_config.get("vmid") or "")
    source_name = str(manifest.get("vm_name") or parameters.get("source_vm_name") or source_vmid)
    target_name = source_name if mode == "in_place" else str(parameters.get("target_vm_name") or source_name)
    power_on = bool(parameters.get("power_on"))
    if vm_name_problem(target_name):
        raise PveRestoreError(vm_name_problem(target_name))

    points = _resolve_point_chain(point)
    manifests = [manifest if item is point else _load_manifest(item) for item in points]
    chains = {disk["drive"]: disk_chain(points, manifests, disk["drive"]) for disk in disks}
    common_path, qualified = qualified_chains(chains)
    sizes = {drive_device(disk["drive"]): int(disk.get("capacity_bytes") or 0) for disk in disks}
    total_bytes = sum(sizes.values())
    adapters = adapter_targets(vm_config, parameters) if mode == "new_vm" else []
    connect = parameters.get("connect_nics", True) is not False

    warnings: list[str] = []
    stop = {"requested": False}

    def emit(stage: str, progress: int, message: str, **extra: object) -> None:
        try:
            if extra.pop("log_level", None) == "warn":
                warnings.append(message)
                logger.warning("%s", message)
                _emit(progress_callback, stage, progress, message, log_level="warn", **extra)
            else:
                _emit(progress_callback, stage, progress, message, **extra)
        except TaskStopRequested:
            stop["requested"] = True

    def stopped() -> bool:
        return stop["requested"] or cancel_requested(task_id)

    emit(
        "prepare",
        3,
        f"{mode_label} recovery of {source_name} ({source_vmid}) from restore point {point_id} taken {point_created}"
        + (f" as new VM {target_name}" if mode == "new_vm" else f", replacing VM {source_vmid}"),
    )
    emit(
        "prepare",
        4,
        f"{len(disks)} disk(s), {_gib(total_bytes)}: "
        + ", ".join(f"{disk['drive']} from {len(chains[disk['drive']])} backup(s)" for disk in disks),
    )
    if parameters.get("disk_type"):
        emit(
            "prepare",
            4,
            f"Proxmox VE storages decide how a disk is provisioned, so disk type {parameters['disk_type']} is not "
            "applied",
            log_level="warn",
        )

    node = ""
    target_vmid = ""
    created = False
    run_state: dict[str, Any] = {}
    left_out: set[str] = set()
    try:
        with PveNode.from_record(hypervisor) as cluster:
            if unrecorded:
                resumed = _find_unrecorded(cluster, earlier_run, task_id, emit)
            if resumed is not None:
                node = str(resumed.get("node") or "")
                target_vmid = str(resumed.get("target_vmid") or "")
                created = bool(resumed.get("created_vm"))
                run_state = dict(resumed)
                storages = dict(resumed.get("storages") or {})
                emit(
                    "prepare",
                    15,
                    f"Following restore task {resumed['upid']} on node {node} again after the server restarted",
                )
            else:
                if mode == "in_place":
                    try:
                        located = cluster.locate_vm(source_vmid)
                    except PveError as exc:
                        raise PveRestoreError(
                            f"In-place recovery replaces VM {source_vmid}, which is no longer in the cluster; restore "
                            "it as a new VM"
                        ) from exc
                    node = str(located.get("node") or "")
                    current = cluster.vm_config(node, source_vmid)
                    current_name = str(current.get("name") or located.get("name") or "")
                    if current_name != source_name:
                        raise PveRestoreError(
                            f"In-place recovery replaces VM {source_vmid}, which is now named {current_name}, not "
                            f"{source_name} as when it was backed up. Restore it as a new VM, or rename the VM back "
                            "if it is the same one"
                        )
                    if str(current.get("protection") or "") == "1":
                        raise PveRestoreError(
                            f"{source_name} ({source_vmid}) is protected; PVE does not restore over it"
                        )
                    if current.get("lock"):
                        raise PveRestoreError(f"{source_name} ({source_vmid}) is locked ({current['lock']}) by PVE")
                    target_vmid = source_vmid
                    storages = disk_targets(disks, parameters, current)
                else:
                    node = str(parameters.get("target_host_name") or parameters.get("target_host") or "")
                    online = {
                        str(item.get("name")): bool(item.get("online"))
                        for item in cluster.cluster_status()
                        if item.get("type") == "node"
                    }
                    if node not in online:
                        raise PveRestoreError(f"Node {node} is not part of this Proxmox VE cluster")
                    if not online[node]:
                        raise PveRestoreError(f"Node {node} is offline")
                    storages = disk_targets(disks, parameters, None)
                    _check_bridges(cluster, node, adapters)
                for disk in [disk for disk in disks if is_passthrough(disk)]:
                    if mode == "in_place":
                        left_out.add(disk["drive"])
                        storages.pop(disk["drive"], None)
                        emit(
                            "validate",
                            6,
                            f"{disk['drive']} is a physical disk ({disk['datastore_path']}); an in-place restore "
                            "leaves it as it is, without the backup's data of it",
                            log_level="warn",
                        )
                    else:
                        emit(
                            "validate",
                            6,
                            f"{disk['drive']} was a physical disk ({disk['datastore_path']}); the new VM gets it as a "
                            f"volume on {storages[disk['drive']]}",
                            log_level="warn",
                        )
                disks = [disk for disk in disks if disk["drive"] not in left_out]
                if not disks:
                    raise PveRestoreError(
                        f"{source_name} has only physical disks, which an in-place restore leaves as they are; restore "
                        "it as a new VM"
                    )
                sizes = {drive_device(disk["drive"]): int(disk.get("capacity_bytes") or 0) for disk in disks}
                total_bytes = sum(sizes.values())
                _check_storages(cluster, node, storages)
                emit(
                    "validate",
                    6,
                    "Disks go to "
                    + ", ".join(f"{drive} -> {storage}" for drive, storage in storages.items())
                    + f" on node {node}",
                )
                if adapters:
                    emit(
                        "validate",
                        7,
                        "Network mapping: "
                        + ", ".join(
                            f"{adapter['label']} ({adapter['source_network'] or 'no bridge'}) -> "
                            f"{adapter['target_network'] or 'no bridge'}"
                            for adapter in adapters
                        )
                        + ("" if connect else ", links down"),
                    )

            node_address = str(cluster.access.node(node).get("address") or node)
            shell = cluster.shell(node)
            ctime = int((resumed or {}).get("ctime") or point_ctime(point))
            archive = archive_name(source_vmid, ctime)
            lock = _archive_lock(str(hypervisor.get("id")), node, archive)
            waiting = f"Waiting for another restore of this restore point on node {node} to end"
            if not _acquire(lock, stopped, lambda: emit("prepare", 9, waiting)):
                raise TaskStopRequested("Session stop requested")
            offer: SecretsOffer | None = None
            try:
                if resumed is None:
                    version = ensure_package(shell, node, package_path(), lambda message: emit("prepare", 10, message))
                    emit("prepare", 11, f"Node {node} has abr-pve-backup {version}")
                    _check_nbd_devices(shell, node)
                    run_repository, secrets = node_repository(repository, common_path, node_address)
                    if encryption_key:
                        secrets["key"] = encryption_key
                    restored_config = restore_guest_config(
                        guest_config,
                        name=target_name if mode == "new_vm" else None,
                        storages=storages,
                        bridges={adapter["label"]: adapter["target_network"] for adapter in adapters},
                        link_down=mode == "new_vm" and not connect,
                        left_out=left_out,
                    )
                    if mode == "in_place":
                        _stop_vm(cluster, node, source_vmid, source_name, emit)
                        emit(
                            "prepare",
                            15,
                            f"PVE removes the disks of {source_name} before it writes the restored ones; if the "
                            "restore fails, the VM is left without them",
                        )
                    if stopped():
                        raise TaskStopRequested("Session stop requested")
                    for attempt in range(1, NEW_VM_ATTEMPTS + 1):
                        if mode == "new_vm":
                            target_vmid = cluster.next_vm_id()
                        run = build_run_file(
                            run_id=task_id,
                            task_user=cluster.access.token_id,
                            storage=cluster.access.storage,
                            archive=archive,
                            target_vmid=target_vmid,
                            guest_config=restored_config,
                            firewall_config=vm_config.get("firewall_config"),
                            repository=run_repository,
                            secrets=secrets,
                            disks=disks,
                            chains=qualified,
                            now=datetime.now(timezone.utc),
                        )
                        # The secrets stay out of the run file: the node helper keeps them for the task of this target
                        # VM alone, so each attempt offers them anew.
                        offered = run.pop("secrets", None) or {}
                        if not offered:
                            run["secrets"] = {}
                        _remove_run_files(shell, source_vmid, ctime)
                        shell.helper.put_run(f"{run_base(source_vmid, ctime)}.json", json.dumps(run, indent=2))
                        offer = shell.helper.offer_secrets(run_base(source_vmid, ctime), offered) if offered else None
                        options = {"force": 1} if mode == "in_place" else {"unique": 1}
                        _record_run(
                            progress_callback,
                            {
                                "kind": "restore",
                                "state": STARTING,
                                "node": node,
                                "target_vmid": target_vmid,
                                "created_vm": mode == "new_vm",
                                "ctime": ctime,
                                "run_id": task_id,
                                "storages": storages,
                                "prepared_at": time.time(),
                            },
                        )
                        try:
                            upid = cluster.api.start_task(
                                f"/nodes/{quote(node, safe='')}/qemu",
                                vmid=target_vmid,
                                archive=f"{cluster.access.storage}:{archive}",
                                **options,
                            )
                        except BaseException as exc:
                            if offer is not None:
                                offer.close()
                                offer = None
                            _remove_run_files(shell, source_vmid, ctime)
                            if not isinstance(exc, PveApiError):
                                raise
                            # Another VM took the ID between the question and the start.
                            if mode == "new_vm" and "already exists" in str(exc) and attempt < NEW_VM_ATTEMPTS:
                                continue
                            raise
                        break
                    created = mode == "new_vm"
                    run_state = {
                        "kind": "restore",
                        "upid": upid,
                        "node": node,
                        "target_vmid": target_vmid,
                        "created_vm": created,
                        "ctime": ctime,
                        "run_id": task_id,
                        "storages": storages,
                        "started_at": utc_now(),
                    }
                    _record_run(progress_callback, run_state)
                    emit(
                        "transfer",
                        18,
                        f"Started restore task {upid} on node {node}, to "
                        + (f"new VM {target_vmid}" if created else f"VM {target_vmid}"),
                    )
                log = RestoreLog(sizes, emit)
                log.lines = int(run_state.get("log_lines") or 0)

                def on_line(text: str) -> None:
                    log.line(text)
                    if log.lines % 100 == 0:
                        _record_run(progress_callback, {**run_state, "log_lines": log.lines})

                status = cluster.api.wait_task(
                    str(run_state["upid"]), on_log=on_line, should_stop=stopped, log_start=log.lines
                )
                run_state.update(
                    {"finished_at": utc_now(), "restore_ok": task_succeeded(status), "log_lines": log.lines}
                )
                _record_run(progress_callback, run_state)
                result = _read_result(shell, source_vmid, ctime, str(run_state.get("run_id") or task_id))
                _remove_run_files(shell, source_vmid, ctime)
            finally:
                if offer is not None:
                    offer.close()
                lock.release()

            if not task_succeeded(status):
                note = _in_place_note(cluster, node, target_vmid, source_name) if mode == "in_place" else ""
                if stopped():
                    raise TaskStopRequested("Session stop requested" + note)
                raise PveRestoreError(
                    f"The restore to VM {target_vmid} on node {node} failed: {_failure(status, result, log.errors)}"
                    + note
                )
            for warning in (result or {}).get("warnings") or []:
                emit("finalize", 91, f"The provider warned: {warning}", log_level="warn")
            # The VM is complete; nothing below undoes it.
            created = False

            power_state = "stopped"
            if power_on:
                emit("finalize", 93, f"Starting {target_name} ({target_vmid})")
                try:
                    started = _run_task(cluster, f"{_vm_path(node, target_vmid)}/status/start")
                except PveApiError as exc:
                    started = {"status": "stopped", "exitstatus": str(exc)}
                if task_succeeded(started):
                    power_state = "running"
                else:
                    emit(
                        "finalize",
                        94,
                        f"{target_name} ({target_vmid}) is restored but did not start: {task_error(started)}",
                        log_level="warn",
                    )

        try:
            from ...services.abr_v41 import schedule_inventory_discovery

            schedule_inventory_discovery(str(task["hypervisor_id"]), force=True)
        except Exception:
            logger.warning(
                "Failed to schedule the inventory rescan after restore session %s; run `abr inventory rescan` so the "
                "inventory shows %s",
                task_id,
                target_name,
                exc_info=True,
            )
        duration = time.time() - start_time
        summary = (
            f"{mode_label} recovery of {source_name} completed: {target_name} ({target_vmid}) on node {node}, "
            f"{len(disks)} disk(s), {_gib(total_bytes)}, in {duration:.1f}s"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )
        logger.info("Session %s: %s", task_id, summary)
        emit("finalize", 98, summary, processed_bytes=total_bytes)
        return {
            "operation": "restore",
            "worker_mode": WORKER_MODE,
            "hypervisor_kind": HYPERVISOR_KIND,
            "restore_mode": mode,
            "restore_point_id": point_id,
            "restore_point_created_at": point_created,
            "identity_preserved": mode == "in_place",
            "source_vm_name": source_name,
            "source_vm_id": source_vmid,
            "target_vm_name": target_name,
            "target_vm_id": target_vmid,
            "target_host_name": node,
            "target_datastore_name": str(parameters.get("target_datastore_name") or next(iter(storages.values()), "")),
            "datastore_map": storages,
            "network_adapters": adapters or None,
            "disk_count": len(disks),
            "disks": [
                {
                    "drive": disk["drive"],
                    "storage": storages.get(disk["drive"]),
                    "capacity_bytes": int(disk.get("capacity_bytes") or 0),
                    "backups": len(chains[disk["drive"]]),
                }
                for disk in disks
            ],
            "power_on": power_on,
            "power_state": power_state,
            "processed_bytes": total_bytes,
            "pve_run": run_state,
            "duration_seconds": round(duration, 3),
            "warnings": warnings,
            "finished_at": utc_now(),
        }
    except TaskStopRequested as exc:
        left = _undo(hypervisor, node, target_vmid, created)
        logger.info("Proxmox VE restore session %s stopped", task_id)
        raise TaskStopRequested(
            "; ".join(part for part in (str(exc) or "Session stop requested", left) if part)
        ) from None
    except Exception as exc:
        logger.exception(
            "Proxmox VE restore session %s (%s recovery of %s from restore point %s) failed after %.1fs",
            task_id,
            mode_label.lower(),
            source_name,
            point_id,
            time.time() - start_time,
        )
        left = _undo(hypervisor, node, target_vmid, created)
        message = str(exc)
        if left:
            message += f". {left}"
        if isinstance(exc, (PveRestoreError, PveError, PveApiError, SshError, PveSetupError, VCenterBackupError)):
            raise PveRestoreError(message) from exc
        raise


def _undo(hypervisor: dict[str, Any], node: str, vmid: str, created: bool) -> str:
    """Removes the VM a restore created and did not finish, over a connection of its own."""
    if not created or not node or not vmid:
        return ""
    try:
        with PveNode.from_record(hypervisor) as again:
            return _destroy_vm(again, node, vmid) or f"VM {vmid} the restore had created was removed"
    except Exception as exc:
        return f"VM {vmid} the restore created on node {node} could not be removed ({exc}); remove it by hand"
