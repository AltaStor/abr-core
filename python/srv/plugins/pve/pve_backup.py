# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Back up a Proxmox VE VM with vzdump and the AltaStor backup provider.

1. Find the VM's node and configuration through the API, and make sure the node has the abr-pve-backup package.
2. Decide for each disk whether PVE should keep its dirty bitmap ("use", an incremental) or start a new one ("new", a
   full), and write the run file /run/abr/backup-<vmid>.json over SSH: the repository as the node reaches it, the
   backup name and bitmap of each disk, and the secrets.
3. Start vzdump on the abr storage with the inventory's API token, and follow the task. The provider runs
   abr-nbd-tool for each disk, which writes the backups into this session's folder of the repository.
4. Read the provider's result file, write the manifest and register the session; a failed or stopped run is
   discarded, as it is for the other platforms.

A disk can be backed up in full within an incremental session: the TPM state always is, since PVE attaches it only
for the backup, and so is a disk whose bitmap a VM restart or migration lost. Each disk's chain starts at its newest
full backup.

vzdump holds a lock per node, so abr-core starts one at a time on each node. The task runs in pvedaemon and does not
depend on abr-core: its UPID is kept with the session, so a session the server lost by a restart follows the same task
again when it runs once more.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ...core.credential_encryption import decrypt_secret, is_encrypted
from ...core.mongo_db_wrapper import HYPERVISORS, REPOSITORIES, RESOURCES_BY_HV, TASKS, catalog_records
from ...core.server_defaults import env_int
from ...core.storage_tool import (
    StorageToolError,
    apply_repository_immutability,
    describe_bytes,
    disk_tool_immutability_args,
    disk_tool_storage_args,
    extract_s3_config,
    is_s3_repository,
    make_durable,
    run_disk_tool,
    write_file_durably,
)
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.utils import utc_now
from ...services.backup_mode import encryption_changed_since_last_backup, resolve_job_backup_mode
from ...services.repository_mounts import account_and_domain
from ..hyperv.hyperv_backup import (
    _discard_session,
    _make_task_root,
    _out_of_space_error,
    _previous_manifest,
    _record_left_data,
    _remove_left_session_data,
    _stopped,
)
from ..hyperv.vhdx_tool import VhdxToolError, for_hyperv_host
from ..vcenter.vcenter_backup import (
    VCenterBackupError,
    _compression_ratio,
    _emit_progress,
    _resolve_backup_storage_layout,
    _resolve_compression_level,
    _resolve_encryption_settings,
    _resolve_immutability_plan,
    _resolve_repository,
)
from .pve import PveError, PveNode, vm_disks
from .pve_api import PveApiError, task_error, task_succeeded
from .setup import PveSetupError, ensure_package, package_path
from .ssh import NodeShell, SshError

logger = logging.getLogger(__name__)

WORKER_MODE = "pve-abr-nbd-tool"
HYPERVISOR_KIND = "proxmox-ve"
RUN_FOLDER = "/run/abr"
LOCK_WAIT_SECONDS = 5.0
TPM_KEY = "tpmstate0"

ProgressCallback = Callable[[dict[str, object]], None] | None


class PveBackupError(RuntimeError):
    pass


# ---- progress ------------------------------------------------------------------------------------------------------


def _emit(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    _emit_progress(progress_callback, stage, progress, message, worker_mode=WORKER_MODE, **extra)


def _warn(progress_callback: ProgressCallback, stage: str, progress: int, message: str, **extra: object) -> None:
    logger.warning("%s", message)
    _emit(progress_callback, stage, progress, message, log_level="warn", **extra)


def _gib(value: int | float | None) -> str:
    return "unknown" if value is None else describe_bytes(int(value))


# ---- names ---------------------------------------------------------------------------------------------------------


def drive_device(key: str) -> str:
    """The NBD export PVE offers a disk as: its QEMU drive ID, and a separate one for the TPM state."""
    return f"drive-{key}-backup" if key == TPM_KEY else f"drive-{key}"


def backup_name(key: str) -> str:
    """Stable from one backup of a disk to the next, since a synthetic full pairs a disk's backups by name; each
    session has a folder of its own."""
    return key


def run_file_path(vmid: str, suffix: str = "json") -> str:
    return f"{RUN_FOLDER}/backup-{vmid}.{suffix}"


# ---- the repository as a node reaches it ---------------------------------------------------------------------------


def node_repository(
    repository: dict[str, Any], abr_path: str, node_address: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """The run file's repository and the secrets it needs: an S3 bucket, or the share of an SMB repository.

    A local repository is a folder of the appliance alone; the nodes reach it only when the appliance shares it over
    SMB, as it does for Hyper-V hosts.
    """
    name = str(repository.get("name") or repository.get("id") or "repository")
    if is_s3_repository(repository):
        s3 = extract_s3_config(repository)
        if not s3.get("access_key") or not s3.get("secret_key"):
            raise PveBackupError(
                f"S3 repository {name} has no access key of its own, and the PVE nodes cannot use this appliance's "
                "AWS profile. Give the repository an access key and secret key"
            )
        settings = {
            "access_key": s3["access_key"],
            "path_style": bool(s3.get("path_style")),
            "verify_tls": bool(s3.get("verify_tls", True)),
        }
        if s3.get("endpoint_url"):
            settings["endpoint"] = str(s3["endpoint_url"]).rstrip("/")
        if s3.get("region"):
            settings["region"] = str(s3["region"])
        return {"mode": "s3", "folder": s3["bucket"], "path": abr_path, "s3": settings}, {
            "secret_key": s3["secret_key"]
        }
    try:
        reached = for_hyperv_host(repository, node_address) or repository
    except VhdxToolError as exc:
        raise PveBackupError(str(exc).replace("Hyper-V host", "PVE node")) from exc
    metadata = reached.get("metadata") if isinstance(reached.get("metadata"), dict) else {}
    server = str(reached.get("server") or metadata.get("server") or "").strip().strip("\\/")
    share = str(reached.get("share") or metadata.get("share") or "").strip().replace("\\", "/").strip("/")
    if str(reached.get("type") or "").lower() != "smb" or not server or not share:
        raise PveBackupError(
            f"PVE VMs need an S3 or SMB repository: the nodes write the backups themselves and cannot reach "
            f"{name}, a folder of this appliance. Use an S3 repository, an SMB repository, or share {name} over SMB "
            f'(abr repo share add --name "{name}")'
        )
    root = Path(str(reached.get("path") or "")).expanduser().resolve()
    try:
        relative = PurePosixPath(*Path(abr_path).expanduser().resolve().relative_to(root).parts)
    except ValueError:
        raise PveBackupError(
            f"{abr_path} is outside the folder of repository {name} ({root}), all the nodes reach"
        ) from None
    username, domain = account_and_domain(str(reached.get("username") or metadata.get("username") or ""))
    smb = {"share": f"//{server}/{share.split('/')[0]}", "username": username, "version": "3.1.1", "seal": True}
    if domain:
        smb["domain"] = domain
    secrets: dict[str, str] = {}
    password = decrypt_secret(reached.get("password") or metadata.get("password") or "")
    if is_encrypted(password):
        raise PveBackupError(
            f"The stored password of the share of repository {name} cannot be decrypted with this appliance's "
            "credential key"
        )
    if password:
        secrets["smb_password"] = str(password)
    inside = PurePosixPath(*share.split("/")[1:], *relative.parts[:-1])
    folder = "" if str(inside) == "." else str(inside)
    return {"mode": "file", "folder": folder, "path": relative.name, "smb": smb}, secrets


def immutability_option(repository: dict[str, Any]) -> dict[str, Any] | None:
    args = disk_tool_immutability_args(repository, takes_retention_till=lambda: True)
    if not args:
        return None
    option = {"mode": args[1]}
    if args[2] == "--retention":
        option["retention_days"] = int(args[3])
    else:
        option["retention_till"] = args[3]
    return option


# ---- full or incremental -------------------------------------------------------------------------------------------


def last_vzdump_task(hypervisor_id: str, vmid: str, exclude_task_id: str) -> str | None:
    """The newest session whose vzdump run of this VM succeeded: PVE moved the VM's bitmaps on with it."""
    newest: tuple[str, str] | None = None
    for record in catalog_records(TASKS):
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        run = result.get("pve_run") if isinstance(result.get("pve_run"), dict) else {}
        if (
            str(record.get("id") or "") == exclude_task_id
            or str(record.get("hypervisor_id") or "") != hypervisor_id
            or str(run.get("vmid") or "") != vmid
            or not run.get("vzdump_ok")
        ):
            continue
        finished = str(run.get("finished_at") or "")
        if newest is None or finished > newest[0]:
            newest = (finished, str(record.get("id") or ""))
    return newest[1] if newest else None


def disk_bitmaps(disks: list[dict[str, Any]], previous: dict[str, Any] | None, incremental: bool) -> dict[str, str]:
    """use for a disk the previous backup of this chain had at the same size, new for any other."""
    if not incremental or previous is None:
        return {disk["drive"]: "new" for disk in disks}
    earlier = {
        str(entry.get("drive") or ""): int(entry.get("capacity_bytes") or 0)
        for entry in previous.get("disks") or []
        if isinstance(entry, dict)
    }
    return {
        disk["drive"]: (
            "use" if disk["drive"] != TPM_KEY and earlier.get(disk["drive"]) == int(disk["capacity_bytes"]) else "new"
        )
        for disk in disks
    }


# ---- the run file --------------------------------------------------------------------------------------------------


def build_run_file(
    *,
    run_id: str,
    task_user: str,
    storage: str,
    vmid: str,
    repository: dict[str, Any],
    secrets: dict[str, str],
    disks: list[dict[str, Any]],
    bitmaps: dict[str, str],
    compression: int,
    immutability: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "compression": int(compression),
        "parallel_disks": env_int("ALTASTOR_PVE_PARALLEL_DISKS"),
    }
    if immutability:
        options["immutability"] = immutability
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "format": "abr-pve-run",
        "version": 1,
        "kind": "backup",
        "run_id": run_id,
        "created": now.strftime(stamp),
        "expires": (now + timedelta(minutes=env_int("ALTASTOR_PVE_RUN_FILE_MINUTES"))).strftime(stamp),
        "task_user": task_user,
        "storage": storage,
        "vmid": int(vmid),
        "repository": repository,
        "options": options,
        "disks": {
            drive_device(disk["drive"]): {
                "name": backup_name(disk["drive"]),
                "bitmap": bitmaps[disk["drive"]],
                "size": int(disk["capacity_bytes"]),
            }
            for disk in disks
        },
        "secrets": {key: value for key, value in secrets.items() if value},
    }


# ---- one vzdump per node -------------------------------------------------------------------------------------------

_NODE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_NODE_LOCKS_GUARD = threading.Lock()


def _node_lock(hypervisor_id: str, node: str) -> threading.Lock:
    with _NODE_LOCKS_GUARD:
        return _NODE_LOCKS.setdefault((hypervisor_id, node), threading.Lock())


def _acquire_node(lock: threading.Lock, stopped: Callable[[], bool], waiting: Callable[[], None]) -> bool:
    if lock.acquire(blocking=False):
        return True
    waiting()
    while not stopped():
        if lock.acquire(timeout=LOCK_WAIT_SECONDS):
            return True
    return False


def fleecing_storage(images_storages: list[dict[str, Any]], disks: list[dict[str, Any]], requested: str = "") -> str:
    """An image storage on the node for vzdump's fleecing images, which external providers need: the one asked for,
    else the storage of the VM's first disk, else the first active one."""
    active = [str(item.get("storage")) for item in images_storages if item.get("active", 1) and item.get("enabled", 1)]
    if requested:
        if requested not in active:
            raise PveBackupError(f"Fleecing storage {requested} is not an active storage for VM disks on this node")
        return requested
    for disk in disks:
        if disk.get("storage") in active:
            return str(disk["storage"])
    if not active:
        raise PveBackupError("The node has no active storage for VM disks, which vzdump needs for its fleecing images")
    return active[0]


# ---- following the task --------------------------------------------------------------------------------------------

_PERCENT = re.compile(r"^INFO: (drive-[a-z]+[0-9]+(?:-backup)?): ([0-9]{1,3})%$")
_STARTED = re.compile(r"^INFO: (drive-\S+): (full|incremental) backup to \S+, bitmap (\w+)$")
_FINISHED = re.compile(r"^INFO: (drive-\S+): (full|incremental) backup \S+, ([0-9]+) bytes read, ([0-9]+) stored$")
_BITMAP = re.compile(r"^INFO: (\S+): dirty-bitmap status: (.+)$")


class TaskLog:
    """Turns the provider's lines in the task log into progress and messages: the start and end of each disk, the
    bitmap PVE kept or made, and errors. Each disk's percent counts by its size."""

    def __init__(self, sizes: dict[str, int], emit: Callable[..., None], start: int = 15, span: int = 73) -> None:
        self.sizes = {device: max(size, 1) for device, size in sizes.items()}
        self.percent: dict[str, int] = {}
        self.emit = emit
        self.start = start
        self.span = span
        self.lines = 0

    def progress(self) -> int:
        total = sum(self.sizes.values()) or 1
        done = sum(self.sizes[device] * self.percent.get(device, 0) / 100 for device in self.sizes)
        return self.start + int(self.span * done / total)

    def line(self, text: str) -> None:
        self.lines += 1
        text = text.rstrip()
        percent = _PERCENT.match(text)
        if percent:
            self.percent[percent.group(1)] = min(int(percent.group(2)), 100)
            self.emit("transfer", self.progress(), f"{percent.group(1)}: {percent.group(2)}%", log_entry=False)
            return
        started = _STARTED.match(text)
        finished = _FINISHED.match(text)
        if finished:
            self.percent[finished.group(1)] = 100
            self.emit(
                "transfer",
                self.progress(),
                f"{finished.group(1)}: {finished.group(2)} backup done, {_gib(int(finished.group(3)))} read, "
                f"{_gib(int(finished.group(4)))} stored",
            )
        elif started:
            self.emit(
                "transfer",
                self.progress(),
                f"{started.group(1)}: {started.group(2)} backup, PVE bitmap {started.group(3)}",
            )
        elif _BITMAP.match(text):
            match = _BITMAP.match(text)
            assert match is not None
            self.emit("transfer", self.progress(), f"{match.group(1)}: dirty bitmap {match.group(2)}")
        elif text.startswith("ERROR:") or text.startswith("TASK ERROR:"):
            self.emit("transfer", self.progress(), text, log_level="warn")


# ---- the result ----------------------------------------------------------------------------------------------------


def disk_outputs(
    result: dict[str, Any], disks: list[dict[str, Any]], repository: dict[str, Any], abr_path: str
) -> list[dict[str, Any]]:
    """The manifest's disk entries, from the result file and the disks found in the VM's configuration."""
    outputs = []
    storage_mode = "s3" if is_s3_repository(repository) else "local"
    for index, disk in enumerate(disks, start=1):
        device = drive_device(disk["drive"])
        entry = (result.get("disks") or {}).get(device) or {}
        tool = entry.get("tool") if isinstance(entry.get("tool"), dict) else {}
        if entry.get("status") != "ok":
            raise PveBackupError(
                f"Disk {disk['drive']} has no backup in the provider's result: "
                f"{entry.get('error') or entry.get('status') or 'PVE did not offer it'}"
            )
        incremental = entry.get("kind") == "incremental"
        logical = int(tool.get("raw_data_size") or 0)
        stored = int(tool.get("stored_data_size") or 0)
        size = int(entry.get("size") or disk["capacity_bytes"])
        bitmap = str(entry.get("bitmap_name") or tool.get("change_id") or "")
        outputs.append(
            {
                "index": index,
                "disk_label": disk["drive"],
                "drive": disk["drive"],
                "device": device,
                "datastore_path": disk["datastore_path"],
                "storage": disk["storage"],
                "format": disk["format"],
                "uuid": None,
                "capacity_bytes": size,
                "capacity_gb": round(size / (1 << 30), 2),
                "logical_sector_size": int(tool.get("sector_size") or 512),
                "physical_sector_size": int(tool.get("physical_sector_size") or 512),
                "bitmap_requested": entry.get("bitmap_requested"),
                "bitmap_granted": entry.get("bitmap_granted"),
                "change_id": bitmap,
                "previous_change_id": bitmap if incremental else "",
                "store_mode": "incremental" if incremental else "full",
                "logical_bytes": logical,
                "stored_bytes": stored,
                "compression_ratio": _compression_ratio(logical, stored),
                "destination_path": "",
                "parent_path": "",
                "storage_mode": storage_mode,
                "repository_type": storage_mode,
                "repository_id": str(repository.get("id") or ""),
                "abr_path": abr_path,
                "abr_backup_name": str(entry.get("name") or backup_name(disk["drive"])),
            }
        )
    return outputs


def session_kind(outputs: list[dict[str, Any]], incremental_wanted: bool, reason: str) -> tuple[str, str, bool]:
    """The session's type, its reason, and whether it is incremental: a full when every disk was backed up in full,
    which an incremental becomes when PVE lost every bitmap."""
    if all(output["store_mode"] == "full" for output in outputs):
        return "ActiveFull", ("IncrementalFallbackToFull" if incremental_wanted else reason), False
    return "Incremental", reason, True


# ---- the session ---------------------------------------------------------------------------------------------------


def _hypervisor(task: dict[str, Any]) -> dict[str, Any]:
    hypervisor = HYPERVISORS.get(str(task.get("hypervisor_id") or ""))
    if not hypervisor:
        raise PveBackupError(f"Hypervisor not found: {task.get('hypervisor_id')}")
    if hypervisor.get("kind") != HYPERVISOR_KIND:
        raise PveBackupError(f"A Proxmox VE backup needs a Proxmox VE inventory, got {hypervisor.get('kind')!r}")
    return hypervisor


def _target(task: dict[str, Any]) -> dict[str, Any]:
    resource_type = str(task.get("resource_type") or "")
    external_id = str(task.get("resource_external_id") or "")
    if resource_type != "virtual_machine":
        raise PveBackupError(
            "Proxmox VE backs up whole VMs: vzdump takes every disk of the VM that is not marked backup=0"
        )
    resource = RESOURCES_BY_HV.get(str(task.get("hypervisor_id") or ""), {}).get((resource_type, external_id))
    if not resource:
        raise PveBackupError(
            f"VM {external_id} is no longer in the inventory of this Proxmox VE cluster. Run inventory rescan if it is "
            "still there; if it has been deleted, it cannot be backed up"
        )
    return resource


def _record_run(progress_callback: ProgressCallback, run: dict[str, Any]) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback({"updated_at": utc_now(), "log_entry": False, "pve_run": dict(run)})
    except TaskStopRequested:
        pass


def _remove_run_files(shell: NodeShell, vmid: str) -> None:
    for suffix in ("json", "result.json", "log"):
        try:
            shell.remove_file(run_file_path(vmid, suffix))
        except (SshError, OSError) as exc:
            logger.warning("Could not remove %s on %s: %s", run_file_path(vmid, suffix), shell.address, exc)


def _read_result(shell: NodeShell, vmid: str, run_id: str) -> dict[str, Any] | None:
    data = shell.read_file(run_file_path(vmid, "result.json"))
    if not data:
        return None
    try:
        result = json.loads(data)
    except ValueError:
        logger.warning("The result file of VM %s on %s is not JSON", vmid, shell.address)
        return None
    return result if isinstance(result, dict) and str(result.get("run_id") or "") == run_id else None


def _failure(vmid: str, node: str, status: dict[str, Any], result: dict[str, Any] | None, errors: list[str]) -> str:
    reason = str((result or {}).get("error") or "").strip() or task_error(status) or "unknown error"
    if result is not None and result.get("status") == "running":
        reason = f"the provider ended without a result ({task_error(status) or 'the task ended'})"
    elif result is None and task_succeeded(status):
        reason = "the provider left no result file for this run"
    # An error line of the task log that tells more than the reason, such as vzdump's own: not its summary lines.
    more = [line for line in errors if line not in reason and reason not in line and line != "job errors"]
    return f"vzdump of VM {vmid} on node {node} failed: {reason}" + (f"; the task log says: {more[0]}" if more else "")


def run_pve_backup(task: dict[str, Any], progress_callback: ProgressCallback = None) -> dict[str, Any]:
    start_time = time.time()
    hypervisor = _hypervisor(task)
    vm_resource = _target(task)
    vmid = str(vm_resource.get("external_id") or "")
    vm_name = str(vm_resource.get("name") or vmid)
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    existing = task.get("result") if isinstance(task.get("result"), dict) else {}
    earlier_run = existing.get("pve_run") if isinstance(existing.get("pve_run"), dict) else {}
    resumed = earlier_run if earlier_run.get("upid") else None

    try:
        compression_level = _resolve_compression_level(parameters)
        encryption_algorithm, encryption_key = _resolve_encryption_settings(parameters)
    except VCenterBackupError as exc:
        raise PveBackupError(str(exc)) from exc
    resolved_backup_type = str(
        parameters.get("resolved_backup_type") or existing.get("resolved_backup_type") or "ActiveFull"
    )
    resolved_backup_reason = str(
        parameters.get("resolved_backup_reason") or existing.get("resolved_backup_reason") or "FirstRun"
    )
    job_mode = resolve_job_backup_mode(task, parameters)
    incremental = job_mode == "incremental" if job_mode is not None else resolved_backup_type == "Incremental"
    if encryption_changed_since_last_backup(task, parameters):
        incremental = False
        resolved_backup_type, resolved_backup_reason = "ActiveFull", "EncryptionKeyChanged"

    repository = _resolve_repository(task)
    if repository is None:
        raise PveBackupError("The repository of this job is no longer registered")
    destination_root, task_root, abr_path = _resolve_backup_storage_layout(
        task=task, vm_name=vm_name, parameters=parameters, repository=repository, vm_external_id=vmid
    )
    abr_path = str(abr_path)
    _make_task_root(repository, task_root)
    if resumed is None:
        _remove_left_session_data(task, repository, progress_callback)
    previous_path, previous = _previous_manifest(
        parameters=parameters, destination_root=destination_root, vm_name=vm_name, task_root=task_root, vm_id=vmid
    )
    warnings: list[str] = []
    written: list[str] = []
    stop = {"requested": False}

    def emit(stage: str, progress: int, message: str, **extra: object) -> None:
        try:
            if extra.pop("log_level", None) == "warn":
                warnings.append(message)
                _warn(progress_callback, stage, progress, message, **extra)
            else:
                _emit(progress_callback, stage, progress, message, **extra)
        except TaskStopRequested:
            stop["requested"] = True

    def stopped() -> bool:
        return stop["requested"] or cancel_requested(task_id)

    try:
        with PveNode.from_record(hypervisor) as cluster:
            located = cluster.locate_vm(vmid)
            node = str((resumed or {}).get("node") or located.get("node") or "")
            config = cluster.vm_config(node, vmid)
            vm_name = str(config.get("name") or located.get("name") or vm_name)
            disks = [disk for disk in vm_disks(vmid, config) if disk["backup"]]
            if not disks:
                raise PveBackupError(f"VM {vm_name} ({vmid}) has no disk vzdump backs up: each is marked backup=0")
            written = [backup_name(disk["drive"]) for disk in disks]
            node_address = str(cluster.access.node(node).get("address") or node)
            run_repository, secrets = node_repository(repository, abr_path, node_address)
            if encryption_key:
                secrets["key"] = encryption_key
            shell = cluster.shell(node)
            lock = _node_lock(str(hypervisor.get("id")), node)
            waiting = f"Waiting for another backup on node {node} to end: vzdump runs one at a time on a node"
            if not _acquire_node(lock, stopped, lambda: emit("prepare", 8, waiting)):
                raise TaskStopRequested("Session stop requested")
            try:
                if resumed is None:
                    upid, run_id = _start(
                        task=task,
                        cluster=cluster,
                        shell=shell,
                        node=node,
                        vmid=vmid,
                        vm_name=vm_name,
                        disks=disks,
                        previous=previous,
                        incremental=incremental,
                        run_repository=run_repository,
                        secrets=secrets,
                        compression_level=compression_level,
                        repository=repository,
                        located=located,
                        emit=emit,
                        progress_callback=progress_callback,
                    )
                    log_start = 0
                else:
                    upid, run_id = str(resumed["upid"]), str(resumed.get("run_id") or task_id)
                    log_start = int(resumed.get("log_lines") or 0)
                    emit("prepare", 15, f"Following vzdump task {upid} on node {node} again after the server restarted")
                run_state: dict[str, Any] = {
                    "upid": upid,
                    "node": node,
                    "vmid": vmid,
                    "run_id": run_id,
                    "started_at": str((resumed or {}).get("started_at") or utc_now()),
                }
                log = TaskLog({drive_device(disk["drive"]): int(disk["capacity_bytes"]) for disk in disks}, emit)
                log.lines = log_start
                errors: list[str] = []

                def on_line(text: str) -> None:
                    if text.startswith(("ERROR:", "TASK ERROR:")):
                        errors.append(text.split(":", 1)[1].strip())
                    log.line(text)
                    if log.lines % 50 == 0:
                        _record_run(progress_callback, {**run_state, "log_lines": log.lines})

                status = cluster.api.wait_task(upid, on_log=on_line, should_stop=stopped, log_start=log_start)
                run_state.update(
                    {"finished_at": utc_now(), "vzdump_ok": task_succeeded(status), "log_lines": log.lines}
                )
                _record_run(progress_callback, run_state)
            finally:
                lock.release()
            # The result file stays on the node until the session is registered or discarded: a server that stops
            # in between reads it again when it follows the task once more.
            try:
                result = _read_result(shell, vmid, run_id)
                registered = _register(
                    task=task,
                    status=status,
                    result=result,
                    errors=errors,
                    disks=disks,
                    config=config,
                    located=located,
                    node=node,
                    vmid=vmid,
                    vm_name=vm_name,
                    upid=upid,
                    cluster=cluster,
                    repository=repository,
                    abr_path=abr_path,
                    task_root=task_root,
                    destination_root=destination_root,
                    previous=previous,
                    previous_path=previous_path,
                    incremental=incremental,
                    resolved_backup_reason=resolved_backup_reason,
                    compression_level=compression_level,
                    encryption_algorithm=encryption_algorithm,
                    parameters=parameters,
                    warnings=warnings,
                    written=written,
                    stopped=stopped,
                    emit=emit,
                )
            finally:
                _remove_run_files(shell, vmid)

        duration = time.time() - start_time
        summary = (
            f"Proxmox VE backup of {vm_name} completed: {registered['backup_mode']} backup of "
            f"{registered['disk_count']} disk(s), read {_gib(registered['processed_bytes'])}, stored "
            f"{_gib(registered['transferred_bytes'])}"
            + (f" (compression ratio {registered['compression_ratio']})" if registered["compression_ratio"] else "")
            + f" in {duration:.1f}s"
            + (f", {len(warnings)} warning(s)" if warnings else "")
        )
        logger.info("Session %s: %s", task_id, summary)
        emit("finalize", 98, summary)
        immutability_generation = (
            _resolve_immutability_plan(task, REPOSITORIES[str(repository.get("id") or "")])
            if str(repository.get("id") or "") in REPOSITORIES
            else None
        )
        return {
            **registered,
            "operation": "backup",
            "worker_mode": WORKER_MODE,
            "snapshot_removed": True,
            "stop_requested_after_completion": stopped(),
            "pve_run": run_state,
            "immutability_generation": immutability_generation,
            "duration_seconds": round(duration, 3),
            "warnings": warnings,
            "finished_at": utc_now(),
        }
    except TaskStopRequested:
        logger.info("Proxmox VE backup session %s of VM %s stopped; discarding what it had written", task_id, vm_name)
        message = _stopped(progress_callback, repository, abr_path, written, task_root, destination_root)
        raise TaskStopRequested(message) from None
    except Exception as exc:
        logger.exception(
            "Proxmox VE backup session %s of VM %s (%s) failed after %.1fs",
            task_id,
            vm_name,
            vmid,
            time.time() - start_time,
        )
        _stranded, reason = _discard_session(repository, abr_path, written, task_root, destination_root)
        if reason:
            _record_left_data(progress_callback, repository, task_root, reason, warn=True)
        if isinstance(exc, (PveError, PveApiError, SshError, PveSetupError)):
            raise PveBackupError(str(exc)) from exc
        raise


def _register(
    *,
    task: dict[str, Any],
    status: dict[str, Any],
    result: dict[str, Any] | None,
    errors: list[str],
    disks: list[dict[str, Any]],
    config: dict[str, Any],
    located: dict[str, Any],
    node: str,
    vmid: str,
    vm_name: str,
    upid: str,
    cluster: PveNode,
    repository: dict[str, Any],
    abr_path: str,
    task_root: Path,
    destination_root: Path,
    previous: dict[str, Any] | None,
    previous_path: Path | None,
    incremental: bool,
    resolved_backup_reason: str,
    compression_level: int,
    encryption_algorithm: Any,
    parameters: dict[str, Any],
    warnings: list[str],
    written: list[str],
    stopped: Callable[[], bool],
    emit: Callable[..., None],
) -> dict[str, Any]:
    """Writes the manifest of a run that succeeded and returns what the session result reports of it.

    A run that ended well is kept even when a stop came too late for it: PVE has moved the bitmaps on with it.
    """
    if not task_succeeded(status) or result is None or result.get("status") != "ok":
        if stopped():
            raise TaskStopRequested("Session stop requested")
        raise PveBackupError(_failure(vmid, node, status, result, errors))
    for warning in result.get("warnings") or []:
        emit("finalize", 88, f"The provider warned: {warning}", log_level="warn")
    outputs = disk_outputs(result, disks, repository, abr_path)
    lost = [
        output["drive"]
        for output in outputs
        if output.get("bitmap_requested") == "use" and output["store_mode"] == "full"
    ]
    if lost:
        emit(
            "finalize",
            88,
            f"PVE no longer had the dirty bitmap of {', '.join(lost)}, since the VM was restarted, stopped or migrated "
            "after the previous backup, so they were backed up in full",
            log_level="warn",
        )
    resolved_backup_type, resolved_backup_reason, incremental = session_kind(
        outputs, incremental, resolved_backup_reason
    )
    total_read = sum(output["logical_bytes"] for output in outputs)
    total_stored = sum(output["stored_bytes"] for output in outputs)
    emit(
        "finalize", 90, f"Writing the manifest of {vm_name}", processed_bytes=total_read, transferred_bytes=total_stored
    )
    vm_config = {
        "vmid": vmid,
        "name": vm_name,
        "node": node,
        "config": config,
        "guest_config": result.get("guest_config"),
        "firewall_config": result.get("firewall_config"),
    }
    pve_details = {
        "upid": upid,
        "node": node,
        "archive": result.get("archive"),
        "provider_version": result.get("provider_version"),
        "config_digest": str(config.get("digest") or ""),
        "power_state": str(located.get("status") or ""),
    }
    backup_mode = "incremental" if incremental else "full"
    base_point_id = (
        str(parameters.get("parent_point_id") or "") or None if incremental and previous is not None else None
    )
    compression_ratio = _compression_ratio(total_read, total_stored)
    storage_mode = "s3" if is_s3_repository(repository) else "local"
    manifest_path = task_root / "manifest.json"
    reported = {
        "hypervisor_kind": HYPERVISOR_KIND,
        "vm_name": vm_name,
        "vm_external_id": vmid,
        "backup_mode": backup_mode,
        "resolved_backup_type": resolved_backup_type,
        "resolved_backup_reason": resolved_backup_reason,
        "parent_point_id": base_point_id,
        "incremental": incremental,
        "differential": False,
        "pve": pve_details,
        "backup_root": str(task_root),
        "manifest_path": str(manifest_path),
        "repository_id": str(repository.get("id") or ""),
        "repository_type": storage_mode,
        "storage_mode": storage_mode,
        "repository_storage_path": abr_path,
        "vm_config": vm_config,
        "disk_count": len(outputs),
        "disks": outputs,
        "compression_level": compression_level,
        "encryption_enabled": bool(encryption_algorithm),
        "encryption_algorithm": encryption_algorithm,
        "service_type": f"Proxmox VE {cluster.get_service_type()}",
        "processed_bytes": total_read,
        "transferred_bytes": total_stored,
        "logical_bytes": total_read,
        "compression_ratio": compression_ratio,
    }
    manifest = {
        **reported,
        "format_version": 1,
        "operation": "vm-backup",
        "task_id": str(task.get("id") or ""),
        "hypervisor_id": str(task.get("hypervisor_id") or ""),
        "job_id": str(task.get("job_id") or ""),
        "cbt_enabled": True,
        "parent_manifest_path": str(previous_path) if incremental and previous_path else "",
        "snapshot_name": "",
        "snapshot_moref": "",
        "warnings": warnings,
        "created_at": utc_now(),
    }
    _write_manifest(
        manifest_path, manifest, repository, abr_path, task_root, destination_root, written, str(task.get("id"))
    )
    return reported


def _start(
    *,
    task: dict[str, Any],
    cluster: PveNode,
    shell: NodeShell,
    node: str,
    vmid: str,
    vm_name: str,
    disks: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    incremental: bool,
    run_repository: dict[str, Any],
    secrets: dict[str, str],
    compression_level: int,
    repository: dict[str, Any],
    located: dict[str, Any],
    emit: Callable[..., None],
    progress_callback: ProgressCallback,
) -> tuple[str, str]:
    """Writes the run file and starts vzdump; returns the task's UPID and the run ID."""
    task_id = str(task.get("id") or "")
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    version = ensure_package(shell, node, package_path(), lambda message: emit("prepare", 9, message))
    emit(
        "prepare",
        10,
        f"VM {vm_name} ({vmid}) is {located.get('status')} on node {node}, which has abr-pve-backup {version}",
    )

    if incremental and previous is not None:
        chained = str(previous.get("task_id") or "")
        last = last_vzdump_task(str(task.get("hypervisor_id") or ""), vmid, task_id)
        if chained and last and last != chained:
            incremental = False
            emit(
                "prepare",
                11,
                f"The dirty bitmaps of VM {vm_name} moved on with session {last}, which is not part of this chain, so "
                "every disk is backed up in full",
                log_level="warn",
            )
    elif incremental:
        emit("prepare", 11, f"No earlier backup of {vm_name} to chain onto; taking a full backup")
    bitmaps = disk_bitmaps(disks, previous if incremental else None, incremental)
    emit(
        "prepare",
        12,
        f"Backing up {len(disks)} disk(s) of {vm_name}: "
        + ", ".join(
            f"{disk['drive']} {disk['datastore_path']} ({_gib(disk['capacity_bytes'])}, "
            f"bitmap {bitmaps[disk['drive']]})"
            for disk in disks
        ),
    )

    images = [dict(item) for item in cluster.api.get(f"/nodes/{node}/storage", content="images", enabled=1) or []]
    fleecing = fleecing_storage(images, disks, str(parameters.get("pve_fleecing_storage") or ""))
    run = build_run_file(
        run_id=task_id,
        task_user=cluster.access.token_id,
        storage=cluster.access.storage,
        vmid=vmid,
        repository=run_repository,
        secrets=secrets,
        disks=disks,
        bitmaps=bitmaps,
        compression=compression_level,
        immutability=immutability_option(repository),
        now=datetime.now(timezone.utc),
    )
    shell.make_directory(RUN_FOLDER, mode=0o700)
    _remove_run_files(shell, vmid)
    shell.write_file(run_file_path(vmid), json.dumps(run, indent=2), mode=0o600)
    try:
        upid = cluster.api.start_task(
            f"/nodes/{node}/vzdump",
            vmid=vmid,
            storage=cluster.access.storage,
            mode="snapshot",
            remove=False,
            fleecing=f"enabled=1,storage={fleecing}",
            **{"notification-mode": "legacy-sendmail"},
        )
    except BaseException:
        shell.remove_file(run_file_path(vmid))
        raise
    _record_run(
        progress_callback, {"upid": upid, "node": node, "vmid": vmid, "run_id": task_id, "started_at": utc_now()}
    )
    emit("transfer", 15, f"Started vzdump task {upid} on node {node}, with fleecing images on {fleecing}")
    return upid, task_id


def _write_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    repository: dict[str, Any],
    abr_path: str,
    task_root: Path,
    destination_root: Path,
    written: list[str],
    task_id: str,
) -> None:
    try:
        write_file_durably(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    except OSError as exc:
        error = _out_of_space_error(repository, exc)
        if error is None:
            raise
        raise error from exc
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
        written.append("manifest")
        apply_repository_immutability(repository, abr_path, "manifest", timeout=120)
    except StorageToolError:
        logger.warning(
            "Could not mirror the manifest of session %s into the repository; the local copy at %s stays the reference",
            task_id,
            manifest_path,
            exc_info=True,
        )
    try:
        make_durable(task_root, up_to=destination_root)
    except OSError as exc:
        error = _out_of_space_error(repository, exc)
        if error is not None:
            raise error from exc
        raise PveBackupError(
            f"The backup was written but could not be flushed to stable storage in {task_root}: {exc}. It is not kept, "
            "so a crash cannot leave a restore point with empty files."
        ) from exc
