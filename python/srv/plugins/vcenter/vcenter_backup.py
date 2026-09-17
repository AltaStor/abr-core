# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import re
import json
import logging
import shutil
import time
import subprocess
from pathlib import Path
from typing import Any, Callable

from ...core.subprocess_util import ProcessCancelled, run_capped
from ...core.mongo_db_wrapper import HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV
from ...core.server_defaults import env_int
from ...core.task_executor import TaskStopRequested, cancel_requested
from ...core.storage_tool import (
    StorageToolError,
    apply_repository_immutability,
    default_disk_tool_path,
    disk_tool_immutability_args,
    disk_tool_storage_args,
    ensure_private_directory,
    extract_s3_config,
    is_s3_repository,
    join_s3_prefix,
    read_storage_backup_info,
    redact_disk_tool_command,
    remove_disk_tool_backup,
    repository_cache_root,
    run_disk_tool,
    s3_block_generation_days,
    with_immutability_retention_days,
    safe_path_component,
)
from ...core.utils import utc_now
from ...services.backup_mode import object_lock_actual_retention_days, resolve_job_backup_mode

logger = logging.getLogger(__name__)


class VCenterBackupError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, object]], None] | None

_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _default_tool_path() -> Path:
    return default_disk_tool_path()


def _default_backup_root() -> Path:
    configured = os.getenv("BACKUPSERVER_STORAGE_ROOT")
    if configured:
        return Path(configured).expanduser()
    return _repo_root() / "backups"


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _resolve_repository_ids(task: dict[str, Any]) -> list[str]:
    parameters = task.get("parameters")
    if isinstance(parameters, dict):
        ids = _coerce_string_list(parameters.get("repository_ids"))
        if not ids:
            single = parameters.get("repository_id")
            if isinstance(single, str) and single:
                ids = [single]
        if ids:
            return [repo_id for repo_id in ids if repo_id in REPOSITORIES]

    job_id = task.get("job_id")
    job = JOBS.get(str(job_id)) if isinstance(job_id, str) else None
    if isinstance(job, dict):
        repo_id = str(job.get("repo_id") or job.get("repository_id") or "")
        if repo_id and repo_id in REPOSITORIES:
            return [repo_id]
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if isinstance(metadata, dict):
        ids = _coerce_string_list(metadata.get("repository_ids"))
        if ids:
            return [repo_id for repo_id in ids if repo_id in REPOSITORIES]

    return []


def _resolve_repository_backup_root(task: dict[str, Any]) -> Path | None:
    for repository_id in _resolve_repository_ids(task):
        repository = REPOSITORIES.get(repository_id)
        if not isinstance(repository, dict):
            continue
        if is_s3_repository(repository):
            continue
        path_value = repository.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            continue
        return Path(path_value).expanduser().resolve()
    return None


def _resolve_repository(task: dict[str, Any]) -> dict[str, Any] | None:
    for repository_id in _resolve_repository_ids(task):
        repository = REPOSITORIES.get(repository_id)
        if not isinstance(repository, dict):
            continue
        if not is_s3_repository(repository):
            return repository
        job_id = task.get("job_id")
        job = JOBS.get(str(job_id)) if isinstance(job_id, str) else None
        actual_days = object_lock_actual_retention_days(
            job if isinstance(job, dict) else None,
            extract_s3_config(repository).get("immutability") or {},
            block_generation_days=s3_block_generation_days(),
        )
        if actual_days < 1:
            return repository
        return with_immutability_retention_days(repository, actual_days)
    return None


def _default_tool_timeout_seconds() -> int:
    return env_int("BACKUPSERVER_VCENTER_TOOL_TIMEOUT_SECONDS")


# Snapshot open, transport negotiation and finalisation are size-independent, so
# a size-derived budget gets this added on top rather than starting from zero.
_TIMEOUT_FIXED_OVERHEAD_SECONDS = 300


def _sanitize_name(value: str) -> str:
    cleaned = _SAFE_NAME_PATTERN.sub("-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned or "resource"


def _parse_tool_result(stdout: str | None) -> dict[str, Any]:
    """The `--json` result document abr-vmdk-tool ends its output with.

    Progress ("42%") is plain text on the lines before it, and run_capped keeps
    only a bounded tail of stdout -- but the document is the last thing
    written, so the tail always carries it whole. Anything other than a
    status=="ok" document reads as absent, which is what makes it safe to hand
    this the stdout of a run that failed and was retried: only the run that
    actually stored data reports sizes.

    Never raises. A tool build without `--json`, or output truncated
    mid-document, just means the caller keeps its own byte estimates.
    """
    if not stdout:
        return {}
    start = stdout.find("{")
    if start == -1:
        return {}
    try:
        payload = json.loads(stdout[start:])
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or str(payload.get("status") or "") != "ok":
        return {}
    return payload


def _tool_reported_size(result: dict[str, Any], field: str) -> int | None:
    raw = result.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = int(raw)
    return value if value >= 0 else None


def _accumulate_tool_sizes(totals: dict[str, Any], stdout: str | None) -> tuple[int | None, int | None]:
    """Fold one disk's reported sizes into the run totals.

    abr-vmdk-tool counts what it actually read off the disk (`raw_data_size`,
    skipping empty blocks, so well under the provisioned capacity) and what it
    wrote to the repository after compression (`stored_data_size`). Both beat
    anything this module can measure itself -- and unlike the directory scan
    they exist for an S3 repository, which writes nothing locally to size up.

    A disk that reports neither marks the totals incomplete rather than
    silently undercounting them, so the caller can tell "nothing to add" from
    "this run's figures can't be trusted".
    """
    result = _parse_tool_result(stdout)
    logical = _tool_reported_size(result, "raw_data_size")
    stored = _tool_reported_size(result, "stored_data_size")
    if logical is None or stored is None:
        totals["complete"] = False
    else:
        totals["logical"] += logical
        totals["stored"] += stored
    return logical, stored


def _compression_ratio(logical_bytes: Any, stored_bytes: Any) -> float | None:
    """Stored / logical, the ratio abr-vmdk-tool reports: 0.36 means the backup
    occupies 36% of the data it came from. Derived from byte totals rather than
    averaging the per-disk ratios, which would weight a 2GB disk the same as a
    2TB one."""
    try:
        logical = int(logical_bytes)
        stored = int(stored_bytes)
    except (TypeError, ValueError):
        return None
    if logical <= 0 or stored < 0:
        return None
    return round(stored / logical, 6)


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive file-size sum, used to measure what a local .abr
    repository actually wrote for a task (abr-vmdk-tool has no byte-progress
    output of its own to parse). Never raises -- a read error just means the
    "transferred" figure is a slight undercount, not a failed backup."""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _emit_progress(
    progress_callback: ProgressCallback,
    stage: str,
    progress: int,
    message: str,
    **extra: object,
) -> None:
    if progress_callback is None:
        return
    payload: dict[str, object] = {
        "stage": stage,
        "progress": progress,
        "message": message,
        "updated_at": utc_now(),
    }
    payload.update(extra)
    progress_callback(payload)


def _disk_tool_progress_reporter(
    progress_callback: ProgressCallback,
    *,
    base_progress: int,
    progress_span: int,
    message_prefix: str,
    base_read_bytes: int = 0,
    disk_total_bytes: int = 0,
    measure_transferred: Callable[[], int] | None = None,
    base_sent_bytes: int = 0,
    disk_sent_bytes: int = 0,
) -> Callable[[int, int], None]:
    """Relay the mover's own per-disk progress, with live byte counters.

    The console derives its transfer rate by diffing consecutive byte counts, so
    it can only show a rate as often as those counts move. Reporting bytes only
    once a whole disk finished meant a single-disk backup published exactly two
    samples -- 0 and the total -- and the UI's Guest Read Rate / Throughput sat
    at "-" for the entire run, then spiked once at the end.

    So convert the tool's percent into a byte count on every progress line
    (~1/s): ``base_read_bytes`` is what earlier disks already read, and
    ``disk_total_bytes`` this disk's size, making processed_bytes a genuine
    interpolation of guest bytes read rather than a step function.
    ``measure_transferred`` re-measures what has actually landed in the
    repository, which is a real observation, not an estimate. A restore has no
    such local directory to stat -- it writes straight to a datastore over the
    wire -- so it interpolates the sent side too, via ``base_sent_bytes`` /
    ``disk_sent_bytes``. Measurement wins where it is available.
    """
    def _report(percent: int, block_index: int) -> None:
        mapped = min(base_progress + int(percent * progress_span / 100), base_progress + progress_span)
        counters: dict[str, object] = {}
        if disk_total_bytes > 0:
            bounded = min(max(percent, 0), 100)
            counters["processed_bytes"] = base_read_bytes + int(disk_total_bytes * bounded / 100)
        if measure_transferred is not None:
            try:
                counters["transferred_bytes"] = measure_transferred()
            except OSError:
                # A best-effort size probe; a read error just means this tick
                # carries no transferred figure, never a failed backup.
                pass
        elif disk_sent_bytes > 0:
            bounded = min(max(percent, 0), 100)
            counters["transferred_bytes"] = base_sent_bytes + int(disk_sent_bytes * bounded / 100)
        _emit_progress(
            progress_callback,
            "transfer",
            mapped,
            f"{message_prefix}: {percent}%",
            worker_mode="vcenter-abr-vmdk-tool",
            tool_block_index=block_index,
            log_entry=False,
            **counters,
        )

    return _report


def _get_hypervisor(task: dict[str, Any]) -> dict[str, Any]:
    hypervisor_id = str(task.get("hypervisor_id", ""))
    hypervisor = HYPERVISORS.get(hypervisor_id)
    if not hypervisor:
        raise VCenterBackupError(f"Hypervisor not found: {hypervisor_id}")
    if hypervisor.get("kind") != "vcenter":
        raise VCenterBackupError(
            f"Real VMware backup is only supported for vCenter hypervisors, got {hypervisor.get('kind')!r}"
        )
    return hypervisor


def _get_resource(task: dict[str, Any]) -> dict[str, Any]:
    hypervisor_id = str(task.get("hypervisor_id", ""))
    resource_type = str(task.get("resource_type", ""))
    resource_external_id = str(task.get("resource_external_id", ""))
    resource = RESOURCES_BY_HV.get(hypervisor_id, {}).get((resource_type, resource_external_id))
    if not resource:
        raise VCenterBackupError(
            f"Discovered resource not found for task: {resource_type}:{resource_external_id}"
        )
    return resource


def _get_vm_resource(hypervisor_id: str, vm_id: str) -> dict[str, Any]:
    resource = RESOURCES_BY_HV.get(hypervisor_id, {}).get(("virtual_machine", vm_id))
    if not resource:
        raise VCenterBackupError(f"Virtual machine resource not found: {vm_id}")
    return resource


def _get_discovered_vm_disks(hypervisor_id: str, vm_id: str) -> list[dict[str, Any]]:
    resources = RESOURCES_BY_HV.get(hypervisor_id, {}).values()
    disks: list[dict[str, Any]] = []
    for resource in resources:
        if resource.get("resource_type") != "disk":
            continue
        payload = resource.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("vm_id", "")) != vm_id:
            continue
        disks.append(resource)
    disks.sort(key=lambda item: str(item.get("external_id", "")))
    return disks


def _resolve_backup_target(
    task: dict[str, Any],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    hypervisor_id = str(task["hypervisor_id"])
    resource = _get_resource(task)
    resource_type = str(resource.get("resource_type"))
    payload = resource.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    if resource_type == "virtual_machine":
        return resource, str(resource["name"]), []

    if resource_type == "disk":
        vm_id = str(payload.get("vm_id", ""))
        if not vm_id:
            raise VCenterBackupError("Disk resource is missing payload.vm_id")
        vm_resource = _get_vm_resource(hypervisor_id, vm_id)
        datastore_path = payload.get("datastore_path")
        if not datastore_path:
            raise VCenterBackupError("Disk resource is missing payload.datastore_path")
        return vm_resource, str(vm_resource["name"]), [resource]

    raise VCenterBackupError(
        f"Real VMware backup is not supported for resource type {resource_type!r}"
    )


def _build_disk_destination(
    destination_root: Path,
    vm_name: str,
    disk: dict[str, Any],
    index: int,
) -> Path:
    payload = disk.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    datastore_path = str(payload.get("datastore_path") or "")
    disk_name = Path(datastore_path).name if datastore_path else str(disk.get("name") or f"disk-{index}")
    base_name = _sanitize_name(Path(disk_name).stem or f"disk-{index}")
    return destination_root / f"{index:02d}-{base_name}.vmdk"


def _normalize_change_id_for_disk_tool(change_id: str) -> str:
    """Convert pyVmomi's space-separated CBT UUID bytes to the utility format."""
    normalized_ids: list[str] = []
    for value in str(change_id).split(","):
        value = value.strip()
        if not value:
            continue
        prefix, separator, suffix = value.partition("/")
        if any(char.isspace() for char in prefix):
            prefix = re.sub(r"\s+", ":", prefix.strip())
        normalized_ids.append(f"{prefix}{separator}{suffix}" if separator else prefix)
    return ",".join(normalized_ids)


def _resolve_utility_change_id_format(parameters: dict[str, Any]) -> str:
    raw_value = parameters.get("utility_change_id_format", "auto")
    value = str(raw_value).strip().lower()
    if value in {"auto", "raw", "colon"}:
        return value
    raise VCenterBackupError("parameters.utility_change_id_format must be one of: auto, raw, colon")


def _format_utility_change_id(change_id: str, *, service_type: str, format_mode: str) -> str:
    if format_mode == "raw":
        return change_id
    if format_mode == "colon":
        return _normalize_change_id_for_disk_tool(change_id)
    return change_id


def _redact_command_for_logging(command: list[str]) -> list[str]:
    return redact_disk_tool_command(command)


# VDDK's VixDiskLib_OpenEx sometimes rejects the very first open of a disk
# covered by a snapshot that vCenter/hostd has only just finished creating --
# NBD access for the new delta isn't always registered yet by the time our
# command runs a couple of seconds later (most visible on a VM whose disk was
# just created, e.g. right after a restore). A short, bounded retry clears
# this transient race without masking a genuinely persistent permission
# problem, which keeps failing past the retry budget below.
_TRANSIENT_DISK_ACCESS_RETRIES = 2
_TRANSIENT_DISK_ACCESS_RETRY_DELAY_SECONDS = 4.0


def _is_transient_disk_access_error(output: str) -> bool:
    lowered = output.lower()
    return "access rights" in lowered or "file_access_error" in lowered


def _backup_still_stored(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
) -> bool:
    try:
        read_storage_backup_info(repository, abr_path, backup_name, timeout=60)
    except StorageToolError:
        return False
    return True


def _discard_task_backups(
    repository: dict[str, Any] | None,
    abr_path: str | None,
    backup_names: list[str],
    task_root: Path,
) -> list[str]:
    stranded: list[str] = []
    if isinstance(repository, dict):
        for backup_name in dict.fromkeys(name for name in backup_names if name):
            try:
                remove_disk_tool_backup(repository, abr_path, backup_name, timeout=120)
            except StorageToolError:
                if _backup_still_stored(repository, abr_path, backup_name):
                    stranded.append(backup_name)
    shutil.rmtree(task_root, ignore_errors=True)
    if stranded:
        logger.error(
            "Backup left %d disk image(s) behind in %s: %s. Object Lock keeps them until their "
            "retention expires; remove them once it does.",
            len(stranded),
            abr_path or task_root,
            ", ".join(stranded),
        )
    return stranded


def _stop_message(stranded: list[str], abr_path: str | None, task_root: Path) -> str:
    if not stranded:
        return "Session stop requested"
    return (
        f"Session stop requested. Object Lock kept {len(stranded)} unreferenced backup(s) in "
        f"{abr_path or task_root}: {', '.join(stranded)}. They belong to no restore point and "
        "can only be removed once their retention expires."
    )


def _resolve_backup_storage_layout(
    *,
    task: dict[str, Any],
    vm_name: str,
    parameters: dict[str, Any],
    repository: dict[str, Any] | None,
) -> tuple[Path, Path, str | None]:
    configured_destination = parameters.get("destination_dir") or parameters.get("backup_root")
    vm_path_component = safe_path_component(vm_name, fallback="vm")

    if is_s3_repository(repository):
        if configured_destination:
            destination_root = Path(str(configured_destination)).expanduser().resolve()
        else:
            destination_root = repository_cache_root(_default_backup_root(), repository).resolve()
        task_root = destination_root / vm_path_component / str(task["id"])
        abr_path = join_s3_prefix(
            extract_s3_config(repository).get("prefix"),
            vm_path_component,
            str(task["id"]),
        )
        return destination_root, task_root, abr_path

    if configured_destination:
        destination_root = Path(str(configured_destination)).expanduser().resolve()
    else:
        destination_root = _resolve_repository_backup_root(task) or _default_backup_root()
    task_root = destination_root / vm_path_component / str(task["id"])
    # Local repositories now use the .abr file backend; the abr path is the
    # per-task filesystem directory the FileBackupManager writes into, and
    # it is passed to the tool as --folder.
    return destination_root, task_root, str(task_root)


DEFAULT_COMPRESSION_LEVEL = 3
ENCRYPTION_ALGORITHM_NONE = 0
ENCRYPTION_ALGORITHM_AES_256 = 1


def _build_vddk_args(
    *,
    vmdk_path: str,
    host: str,
    port: int,
    username: str,
    password: str,
    thumbprint: str,
    vm_spec: str | None,
    snapshot_moref: str | None = None,
    transport_mode: str | None = None,
) -> list[str]:
    """VMDK (VDDK) options shared by every abr-vmdk-tool disk command.

    Nothing here names the VDDK bundle: the launcher script puts it on the
    library search path before the tool starts, which is the only way it can
    be selected.

    The tool rejects --host without --vm, so callers must resolve a VM spec
    before they get here.
    """
    if not vm_spec:
        raise VCenterBackupError(
            f"A VM spec is required to reach {vmdk_path} on {host}; "
            "abr-vmdk-tool cannot open a remote VMDK without --vm"
        )
    args = [
        "--vmdk",
        vmdk_path,
        "--host",
        host,
        "--user",
        username,
        "--password",
        password,
        "--port",
        str(port),
        "--vm",
        vm_spec,
    ]
    if snapshot_moref:
        args.extend(["--snapshot", snapshot_moref])
    if thumbprint:
        args.extend(["--thumbprint", thumbprint])
    if transport_mode:
        args.extend(["--transport", transport_mode])
    return args


def _build_backup_command(
    *,
    tool_path: Path,
    backup_name: str,
    repository: dict[str, Any],
    abr_path: str,
    datastore_path: str,
    host: str,
    port: int,
    username: str,
    password: str,
    thumbprint: str,
    vm_moref: str | None,
    snapshot_moref: str | None,
    transport_mode: str | None,
    change_id: str | None = None,
    compression_level: int = DEFAULT_COMPRESSION_LEVEL,
    encryption_key: str = "",
) -> list[str]:
    """A full backup, or an incremental one when change_id is given."""
    command = [
        str(tool_path),
        "backup",
        # Ends the run with a result document carrying the byte counts and
        # compression ratio the tool measured. Progress stays plain "NN%" lines
        # ahead of it, so the live reporter is unaffected.
        "--json",
        "--name",
        backup_name,
        *disk_tool_storage_args(repository, abr_path=abr_path, create_folder=True),
        *disk_tool_immutability_args(repository),
        "--compression",
        str(compression_level),
    ]
    if change_id:
        command.extend(["--type", "incremental", "--change-id", change_id])
    if encryption_key:
        command.extend(["--key", encryption_key])
    command.extend(
        _build_vddk_args(
            vmdk_path=datastore_path,
            host=host,
            port=port,
            username=username,
            password=password,
            thumbprint=thumbprint,
            vm_spec=vm_moref,
            snapshot_moref=snapshot_moref,
            transport_mode=transport_mode,
        )
    )
    return command


def _resolve_compression_level(parameters: dict[str, Any]) -> int:
    raw_value = parameters.get("compression_level")
    if raw_value in (None, ""):
        return DEFAULT_COMPRESSION_LEVEL

    try:
        compression_level = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise VCenterBackupError("parameters.compression_level must be an integer between 0 and 9") from exc
    if not 0 <= compression_level <= 9:
        raise VCenterBackupError("parameters.compression_level must be an integer between 0 and 9")
    return compression_level


def _coerce_bool_parameter(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError(f"parameters.{field} must be a boolean")


def _resolve_encryption_settings(parameters: dict[str, Any]) -> tuple[int, str]:
    enabled = _coerce_bool_parameter(parameters.get("encryption_enabled", False), field="encryption_enabled")
    if not enabled:
        return ENCRYPTION_ALGORITHM_NONE, ""

    key = str(parameters.get("encryption_key") or "").strip()
    if not key:
        raise VCenterBackupError("parameters.encryption_key is required when parameters.encryption_enabled is true")
    return ENCRYPTION_ALGORITHM_AES_256, key


def _resolve_tool_timeout_seconds(parameters: dict[str, Any]) -> int:
    raw_value = parameters.get("tool_timeout_seconds")
    if raw_value in (None, ""):
        return _default_tool_timeout_seconds()

    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise VCenterBackupError("parameters.tool_timeout_seconds must be a positive integer") from exc
    if timeout_seconds <= 0:
        raise VCenterBackupError("parameters.tool_timeout_seconds must be a positive integer")
    return timeout_seconds


def _disk_timeout_seconds(base_timeout_seconds: int, capacity_gb: Any) -> int:
    """Grow a per-disk timeout to fit the disk actually being moved.

    The configured timeout is a flat wall-clock cap, so it silently encodes an
    assumption about disk size: at the shipped 900s default, anything that takes
    longer than 15 minutes is killed mid-transfer no matter how healthy it is.
    A 94GB disk over nbdssl needs well past that, and failed with "Backup tool
    timed out after 900 seconds" after moving data the whole time.

    So treat the configured value as a FLOOR and scale it by the disk's own
    capacity, using a deliberately pessimistic throughput floor
    (BACKUPSERVER_VCENTER_MIN_TRANSFER_MBPS): well under what any real transport
    sustains, so a healthy transfer of any size finishes inside the window,
    while a genuinely hung tool is still reaped rather than running forever.

    Unknown capacity falls back to the configured timeout unchanged -- guessing
    a size would be worse than the operator's explicit setting.
    """
    try:
        gb = float(capacity_gb)
    except (TypeError, ValueError):
        return base_timeout_seconds
    if gb <= 0:
        return base_timeout_seconds
    floor_mbps = max(1, env_int("BACKUPSERVER_VCENTER_MIN_TRANSFER_MBPS"))
    needed_seconds = int((gb * 1024) / floor_mbps) + _TIMEOUT_FIXED_OVERHEAD_SECONDS
    return max(base_timeout_seconds, needed_seconds)


def _format_vm_spec(vm_moref: str | None, service_type: str) -> str | None:
    if not vm_moref:
        return None
    if "=" in vm_moref:
        return vm_moref
    if service_type in {"VirtualCenter", "HostAgent"}:
        return f"moref={vm_moref}"
    return None


def _hostagent_vm_retry_specs(vm_moref: str | None) -> list[str]:
    """VM spellings worth retrying on an ESXi host.

    abr-vmdk-tool refuses --host without --vm, so there is no "omit the VM
    entirely" variant to fall back on.
    """
    if not vm_moref:
        return []
    candidates = [
        vm_moref,
        f"moref={vm_moref}",
        f"moRef={vm_moref}",
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VCenterBackupError(f"Backup manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise VCenterBackupError(f"Backup manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise VCenterBackupError(f"Backup manifest must contain an object: {manifest_path}")
    return payload


def _manifest_sort_key(manifest_path: Path, manifest: dict[str, Any]) -> tuple[str, float]:
    created_at = str(manifest.get("created_at") or "")
    try:
        mtime = manifest_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return created_at, mtime


def _resolve_parent_manifest(
    *,
    parameters: dict[str, Any],
    destination_root: Path,
    vm_name: str,
    task_root: Path,
    vm_external_id: str,
    differential: bool = False,
) -> tuple[Path, dict[str, Any]]:
    explicit = parameters.get("parent_manifest_path")
    if explicit not in (None, ""):
        manifest_path = Path(str(explicit)).expanduser().resolve()
        return manifest_path, _load_manifest(manifest_path)

    vm_backup_root = destination_root / _sanitize_name(vm_name)
    if not vm_backup_root.exists():
        if differential:
            raise VCenterBackupError(
                "Differential backup requires parameters.parent_manifest_path or a previous full backup manifest in the VM backup directory"
            )
        raise VCenterBackupError(
            "Incremental backup requires parameters.parent_manifest_path or a previous backup manifest in the VM backup directory"
        )

    candidates: list[tuple[Path, dict[str, Any]]] = []
    current_manifest = (task_root / "manifest.json").resolve()
    for manifest_path in vm_backup_root.glob("*/manifest.json"):
        resolved_path = manifest_path.resolve()
        if resolved_path == current_manifest:
            continue
        try:
            manifest = _load_manifest(resolved_path)
        except VCenterBackupError:
            continue
        if str(manifest.get("operation") or "") != "vm-backup":
            continue
        if str(manifest.get("vm_external_id") or "") != vm_external_id:
            continue
        if differential:
            backup_mode = str(manifest.get("backup_mode") or "").strip().lower()
            if backup_mode not in {"", "full"}:
                continue
        candidates.append((resolved_path, manifest))

    if not candidates:
        if differential:
            raise VCenterBackupError(
                "Differential backup requires a previous full VM backup manifest. Provide parameters.parent_manifest_path or create a full backup first."
            )
        raise VCenterBackupError(
            "Incremental backup requires a previous VM backup manifest. Provide parameters.parent_manifest_path or create a full backup first."
        )

    candidates.sort(key=lambda item: _manifest_sort_key(item[0], item[1]), reverse=True)
    return candidates[0]


def _is_cbt_invalid_fault(exc: BaseException) -> bool:
    for message in getattr(exc, "faultMessage", None) or []:
        key = str(getattr(message, "key", "") or "").lower()

        if "cbt" in key:
            return True
        
    return False


def _resolve_parent_disk_entry(
    parent_manifest: dict[str, Any],
    *,
    datastore_path: str,
    uuid: str,
    index: int,
) -> dict[str, Any] | None:
    disks = parent_manifest.get("disks")
    if not isinstance(disks, list):
        raise VCenterBackupError("Parent backup manifest is missing the disks list")

    if uuid:
        for entry in disks:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("uuid") or "") == uuid:
                return entry

    if datastore_path:
        for entry in disks:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("datastore_path") or "") == datastore_path:
                return entry

    for entry in disks:
        if not isinstance(entry, dict):
            continue
        if int(entry.get("index") or 0) == index:
            return entry

    return None


def _vm_config_changed_since_parent(
    disks: list[dict[str, Any]], parent_manifest: dict[str, Any]
) -> tuple[bool, str]:
    parent_disks = parent_manifest.get("disks")
    if not isinstance(parent_disks, list):
        return False, ""

    for index, disk in enumerate(disks, start=1):
        payload = disk.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        datastore_path = str(payload.get("datastore_path") or disk.get("datastore_path") or "")
        uuid = str(payload.get("uuid") or disk.get("uuid") or "")
        try:
            parent_disk = _resolve_parent_disk_entry(
                parent_manifest, datastore_path=datastore_path, uuid=uuid, index=index
            )
        except VCenterBackupError:
            return False, ""
        if parent_disk is None:
            return True, f"disk {datastore_path or uuid or index} was added to the VM since the parent backup"

        parent_capacity_gb = parent_disk.get("capacity_gb")
        current_capacity_gb = payload.get("capacity_gb") or disk.get("capacity_gb")
        if parent_capacity_gb in (None, "") or current_capacity_gb in (None, ""):
            continue
        try:
            if float(parent_capacity_gb) != float(current_capacity_gb):
                return (
                    True,
                    f"disk {datastore_path or uuid or index} capacity changed from "
                    f"{parent_capacity_gb}GB to {current_capacity_gb}GB since the parent backup",
                )
        except (TypeError, ValueError):
            continue

    return False, ""


def _resolve_incremental_flag(parameters: dict[str, Any]) -> bool:
    value = parameters.get("incremental", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError("parameters.incremental must be a boolean")


def _resolve_differential_flag(parameters: dict[str, Any]) -> bool:
    value = parameters.get("differential", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError("parameters.differential must be a boolean")


def _resolve_query_changed_areas_only_flag(parameters: dict[str, Any]) -> bool:
    value = parameters.get("query_changed_areas_only", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError("parameters.query_changed_areas_only must be a boolean")


def _resolve_incremental_fallback_full_flag(parameters: dict[str, Any]) -> bool:
    value = parameters.get("incremental_fallback_full", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError("parameters.incremental_fallback_full must be a boolean")


def _resolve_incremental_fallback_metadata_flag(parameters: dict[str, Any]) -> bool:
    value = parameters.get("incremental_fallback_metadata", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise VCenterBackupError("parameters.incremental_fallback_metadata must be a boolean")


def _is_missing_parent_manifest_error(message: str) -> bool:
    normalized = message.strip().lower()
    return "previous" in normalized and "manifest" in normalized


def run_vcenter_backup(
    task: dict[str, Any],
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    start_time = time.time()

    try:
        from .vcenter import VCenterNode
    except ModuleNotFoundError as exc:
        logger.error("Missing dependency: %s. Please install required packages.", exc)
        if exc.name in {"pyVim", "pyVmomi"}:
            raise VCenterBackupError(
                "vCenter backup support requires pyVmomi/pyVim to be installed"
            ) from exc
        raise

    hypervisor = _get_hypervisor(task)
    vm_resource, vm_name, requested_disks = _resolve_backup_target(task)
    vm_id = str(vm_resource.get("external_id", ""))
    logger.info(
        "Starting vCenter backup task %s for VM %s (hypervisor=%s)",
        task.get("id"),
        vm_name,
        task.get("hypervisor_id"),
    )
    parameters = task.get("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}

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

    repository = _resolve_repository(task)
    repository_type = str(repository.get("type") or "").strip().lower() if isinstance(repository, dict) else ""
    destination_root, task_root, repository_abr_path = _resolve_backup_storage_layout(
        task=task,
        vm_name=vm_name,
        parameters=parameters,
        repository=repository,
    )
    ensure_private_directory(task_root)
    # Every repository now uses the .abr format. When no repository record is
    # configured, treat it as a local-file repo rooted at the task directory so
    # the abr command builders have a concrete context to work with.
    if repository is None:
        repository = {"type": "local", "id": ""}
        repository_type = "local"

    transport_mode_value = parameters.get("transport_mode")
    if transport_mode_value is None or transport_mode_value == "":
        transport_mode = "nbdssl"
    else:
        requested_transport_mode = str(transport_mode_value).strip()
        transport_mode = None if requested_transport_mode.lower() in {"auto", "none", "omit"} else requested_transport_mode
    tool_timeout_seconds = _resolve_tool_timeout_seconds(parameters)
    # Live "transferred" probe for the per-disk progress reporter. Only a local
    # repository has a directory whose size IS what has landed so far; an S3
    # repository streams straight to the bucket, leaving nothing local to
    # measure, so it reports no live transferred figure rather than a wrong one
    # (the same condition guards the post-disk measurement below).
    measure_transferred: Callable[[], int] | None = (
        None if repository_type == "s3" else lambda: _dir_size_bytes(task_root)
    )
    compression_level = _resolve_compression_level(parameters)
    encryption_algorithm, encryption_key = _resolve_encryption_settings(parameters)
    incremental = _resolve_incremental_flag(parameters)
    differential = _resolve_differential_flag(parameters)
    resolved_job_backup_mode = resolve_job_backup_mode(task, parameters)
    if resolved_job_backup_mode is not None and not differential:
        incremental = resolved_job_backup_mode == "incremental"
    changed_block_backup = incremental or differential
    query_changed_areas_only = _resolve_query_changed_areas_only_flag(parameters)
    incremental_fallback_full = _resolve_incremental_fallback_full_flag(parameters)
    incremental_fallback_metadata = _resolve_incremental_fallback_metadata_flag(parameters)
    utility_change_id_format = _resolve_utility_change_id_format(parameters)
    if incremental and differential:
        raise VCenterBackupError("parameters.incremental and parameters.differential cannot both be true")
    if query_changed_areas_only and not changed_block_backup:
        raise VCenterBackupError(
            "parameters.query_changed_areas_only requires parameters.incremental=true or parameters.differential=true"
        )
    if incremental_fallback_full and not changed_block_backup:
        raise VCenterBackupError(
            "parameters.incremental_fallback_full requires parameters.incremental=true or parameters.differential=true"
        )
    if incremental_fallback_full and query_changed_areas_only:
        raise VCenterBackupError("parameters.incremental_fallback_full cannot be combined with parameters.query_changed_areas_only")
    if incremental_fallback_metadata and not changed_block_backup:
        raise VCenterBackupError(
            "parameters.incremental_fallback_metadata requires parameters.incremental=true or parameters.differential=true"
        )
    if incremental_fallback_metadata and query_changed_areas_only:
        raise VCenterBackupError("parameters.incremental_fallback_metadata cannot be combined with parameters.query_changed_areas_only")

    tool_path = Path(str(parameters.get("tool_path") or _default_tool_path())).expanduser()
    logger.debug(
        "Backup tool path=%s host=%s port=%s verify_ssl=%s",
        tool_path,
        host,
        port,
        verify_ssl,
    )
    if not query_changed_areas_only and not tool_path.exists():
        raise VCenterBackupError(f"Backup tool not found: {tool_path}")

    logger.info(
        "Preparing VMware backup for VM %s (task=%s) using destination %s",
        vm_name,
        task.get("id"),
        task_root,
    )
    _emit_progress(
        progress_callback,
        "prepare",
        5,
        f"Preparing VMware backup for {vm_name}",
        worker_mode="vcenter-abr-vmdk-tool",
        vm_name=vm_name,
        backup_root=str(task_root),
    )

    snapshot_name: str | None = None
    snapshot_moref: str | None = None
    vm_moref: str | None = None
    thumbprint = ""
    service_type = ""
    cbt_enabled = False
    incremental_full_fallback_used = False
    incremental_metadata_fallback_used = False
    vm_config_changed_full_required = False
    vm_config_change_reason = ""
    task_id = str(task.get("id") or "")
    written_backup_names: list[str] = []
    stop_requested_after_completion = False

    try:
        # The data mover carries the disk over its own VDDK connection, so this
        # SOAP session is idle for the whole transfer -- long past vCenter's
        # session timeout on a large VM, which is what left snapshot removal
        # below failing with "The session is not authenticated". Keep it warm
        # for as long as we hold the node.
        with VCenterNode(host, username, password, port=port, verify_ssl=verify_ssl) as node, node.session_keepalive():
            service_type = node.get_service_type()
            is_virtual_center = service_type == "VirtualCenter"
            logger.info(
                "Connected to vCenter host %s service_type=%s for VM %s",
                host,
                service_type,
                vm_name,
            )
            # A standalone ESXi host that has since been joined to a cluster can
            # no longer be backed up directly (its VMs may be moved off-host by
            # DRS/vMotion mid-job). Refuse before taking a snapshot so we never
            # leave an orphaned snapshot behind. Only direct host connections are
            # affected; a vCenter is the supported path for clustered hosts.
            if service_type == "HostAgent":
                cluster_name = node.clustered_host_name()
                if cluster_name:
                    raise VCenterBackupError(
                        f"Cannot run backup: ESXi host {host} now belongs to "
                        f"cluster '{cluster_name}'. A clustered host must be "
                        "backed up through its managing vCenter; re-register the "
                        "vCenter and retarget this job."
                    )
            # A VM whose ESXi host has dropped its connection to vCenter can't
            # take a snapshot -- vCenter rejects it with a raw
            # vim.fault.HostNotConnected before a task even starts. Check this
            # up front so the job fails with an actionable message instead of
            # that fault's unreadable str() dump.
            try:
                node.ensure_vm_host_connected(vm_name)
            except RuntimeError as exc:
                raise VCenterBackupError(str(exc)) from exc
            # CBT must be enabled BEFORE the snapshot that anchors a backup chain,
            # for EVERY backup including fulls. Enabling it only for incrementals
            # (the old behavior) meant an ActiveFull was captured with CBT off, so
            # its snapshot carried no valid changeId. The first incremental then
            # enabled CBT for the first time -- too late -- and every subsequent
            # incremental queried QueryChangedDiskAreas against a changeId the
            # freshly-initialized CBT generation did not recognize, failing with
            # "Nonmatching change UUID" (vim.fault.FileFault). Ensuring CBT on the
            # full anchors the chain on a valid CBT baseline so incrementals chain
            # correctly.
            _emit_progress(
                progress_callback,
                "cbt",
                10,
                f"Ensuring CBT is enabled for {vm_name}",
                worker_mode="vcenter-abr-vmdk-tool",
            )
            cbt_enabled = node.ensure_cbt_enabled(vm_name)
            _emit_progress(
                progress_callback,
                "snapshot",
                15,
                f"Creating backup snapshot for {vm_name}",
                worker_mode="vcenter-abr-vmdk-tool",
            )
            snapshot_name = node.create_backup_snapshot(vm_name)
            backup_params = node.get_backup_parameters(vm_name, snapshot_name)
            vm_moref = str(backup_params.get("vm_moref") or "")
            snapshot_moref = str(backup_params.get("snapshot_moref") or "")
            if not vm_moref or not snapshot_moref:
                raise VCenterBackupError("vCenter did not return VM or snapshot MoRef values")

            thumbprint = node.get_thumbprint()
            live_disks = node.get_vm_disks(vm_name)
            logger.debug(
                "VM %s has %d live disk(s) from vCenter",
                vm_name,
                len(live_disks),
            )
            live_disk_paths = {
                str(disk.get("datastore_path") or ""): str(
                    disk.get("current_datastore_path") or disk.get("datastore_path") or ""
                )
                for disk in live_disks
            }

            disks = requested_disks or _get_discovered_vm_disks(str(task["hypervisor_id"]), vm_id)
            if not disks:
                disks = [
                    {
                        "name": str(disk.get("disk_label") or "disk"),
                        "payload": {
                            "datastore_path": disk.get("datastore_path"),
                            "uuid": disk.get("uuid"),
                            "capacity_gb": disk.get("capacity_gb"),
                            "source_disk_type_code": disk.get("source_disk_type_code"),
                            "source_disk_type_name": disk.get("source_disk_type_name"),
                        },
                    }
                    for disk in live_disks
                    if disk.get("datastore_path")
                ]
            elif not requested_disks:
                known_uuids = {str((d.get("payload") or {}).get("uuid") or "") for d in disks} - {""}
                known_paths = {str((d.get("payload") or {}).get("datastore_path") or "") for d in disks} - {""}
                newly_seen_disks = [
                    {
                        "name": str(disk.get("disk_label") or "disk"),
                        "payload": {
                            "datastore_path": disk.get("datastore_path"),
                            "uuid": disk.get("uuid"),
                            "capacity_gb": disk.get("capacity_gb"),
                            "source_disk_type_code": disk.get("source_disk_type_code"),
                            "source_disk_type_name": disk.get("source_disk_type_name"),
                        },
                    }
                    for disk in live_disks
                    if disk.get("datastore_path")
                    and str(disk.get("datastore_path")) not in known_paths
                    and str(disk.get("uuid") or "") not in known_uuids
                ]
                if newly_seen_disks:
                    logger.warning(
                        "VM %s has %d live disk(s) not present in the last discovered inventory "
                        "scan (task %s); including them in this backup. Run `abr inventory rescan` "
                        "to refresh the catalog.",
                        vm_name,
                        len(newly_seen_disks),
                        task.get("id"),
                    )
                    _emit_progress(
                        progress_callback,
                        "prepare",
                        20,
                        f"VM configuration change detected for {vm_name}: {len(newly_seen_disks)} "
                        "disk(s) added since the last inventory scan; including them in this backup",
                        worker_mode="vcenter-abr-vmdk-tool",
                        vm_name=vm_name,
                    )
                    disks = [*disks, *newly_seen_disks]

            live_by_uuid = {
                str(live_disk.get("uuid")): live_disk
                for live_disk in live_disks
                if live_disk.get("uuid")
            }
            live_by_path = {
                str(live_disk.get("datastore_path")): live_disk
                for live_disk in live_disks
                if live_disk.get("datastore_path")
            }
            refreshed_disks: list[dict[str, Any]] = []
            removed_disks: list[dict[str, Any]] = []
            for disk in disks:
                payload = disk.get("payload")
                if not isinstance(payload, dict):
                    refreshed_disks.append(disk)
                    continue
                uuid = str(payload.get("uuid") or "")
                datastore_path = str(payload.get("datastore_path") or "")
                live_disk = (live_by_uuid.get(uuid) if uuid else None) or (
                    live_by_path.get(datastore_path) if datastore_path else None
                )
                if live_disk is None:
                    if uuid or datastore_path:
                        removed_disks.append(disk)
                    else:
                        refreshed_disks.append(disk)
                    continue

                updates: dict[str, Any] = {}

                live_capacity_gb = live_disk.get("capacity_gb")
                cached_capacity_gb = payload.get("capacity_gb")
                if live_capacity_gb is not None:
                    try:
                        stale = cached_capacity_gb in (None, "") or float(cached_capacity_gb) != float(live_capacity_gb)
                    except (TypeError, ValueError):
                        stale = True
                    if stale:
                        if cached_capacity_gb not in (None, ""):
                            logger.warning(
                                "VM %s disk %s capacity changed from %sGB (last inventory scan) to "
                                "%sGB (live, task %s); using the live value for this backup. Run "
                                "`abr inventory rescan` to refresh the catalog.",
                                vm_name,
                                datastore_path or uuid,
                                cached_capacity_gb,
                                live_capacity_gb,
                                task.get("id"),
                            )
                        updates["capacity_gb"] = live_capacity_gb

                live_uuid = str(live_disk.get("uuid") or "").strip()
                if live_uuid and live_uuid != uuid:
                    logger.debug(
                        "VM %s disk %s: using live disk UUID %s for this backup (catalog had %r)",
                        vm_name,
                        datastore_path or "unknown",
                        live_uuid,
                        uuid or None,
                    )
                    updates["uuid"] = live_uuid

                if not updates:
                    refreshed_disks.append(disk)
                    continue
                refreshed_disk = dict(disk)
                refreshed_disk["payload"] = {**payload, **updates}
                refreshed_disks.append(refreshed_disk)
            disks = refreshed_disks

            if removed_disks:
                removed_labels = [
                    str((d.get("payload") or {}).get("datastore_path") or d.get("name") or "disk")
                    for d in removed_disks
                ]
                logger.warning(
                    "VM %s no longer has %d disk(s) from the last discovered inventory scan "
                    "(task %s); excluding them from this backup: %s. Run `abr inventory rescan` "
                    "to refresh the catalog.",
                    vm_name,
                    len(removed_disks),
                    task.get("id"),
                    ", ".join(removed_labels),
                )
                _emit_progress(
                    progress_callback,
                    "prepare",
                    20,
                    f"VM configuration change detected for {vm_name}: {len(removed_disks)} "
                    "disk(s) removed since the last inventory scan; excluding them from this backup",
                    worker_mode="vcenter-abr-vmdk-tool",
                    vm_name=vm_name,
                )

            if not disks:
                raise VCenterBackupError(f"No disks found to back up for VM {vm_name!r}")

            vm_config = node.get_vm_restore_metadata(vm_name)

            disk_outputs: list[dict[str, Any]] = []
            # Best-effort byte counters surfaced to the Activity page's Read/Sent
            # columns. There is no byte-progress channel from abr-vmdk-tool itself:
            # "read" is approximated from CBT changed-block size (incremental) or
            # disk capacity (full), and "transferred" from the actual bytes written
            # into the local .abr repository directory (S3 has no cheap equivalent,
            # so it falls back to the read estimate).
            total_read_bytes = 0
            total_transferred_bytes = 0
            # What abr-vmdk-tool itself counted, summed over the disks it
            # stored. "complete" drops the moment one disk doesn't report, so
            # a partial total never gets published as if it were the whole run.
            tool_sizes: dict[str, Any] = {"logical": 0, "stored": 0, "complete": True}
            snapshot_change_ids = node.get_snapshot_change_ids(vm_name, snapshot_name)
            change_id_by_datastore_path = {
                str(entry.get("datastore_path") or ""): entry
                for entry in snapshot_change_ids
                if entry.get("datastore_path")
            }
            change_id_by_uuid = {
                str(entry.get("uuid") or ""): entry
                for entry in snapshot_change_ids
                if entry.get("uuid")
            }
            change_id_by_index = {
                index: entry
                for index, entry in enumerate(snapshot_change_ids, start=1)
            }
            use_changed_block_backup = changed_block_backup
            parent_manifest_path: Path | None = None
            parent_manifest: dict[str, Any] | None = None
            if use_changed_block_backup:
                try:
                    parent_manifest_path, parent_manifest = _resolve_parent_manifest(
                        parameters=parameters,
                        destination_root=destination_root,
                        vm_name=vm_name,
                        task_root=task_root,
                        vm_external_id=str(vm_resource["external_id"]),
                        differential=differential,
                    )
                except VCenterBackupError as exc:
                    if not incremental_fallback_full or not _is_missing_parent_manifest_error(str(exc)):
                        raise
                    use_changed_block_backup = False
                    incremental_full_fallback_used = True
                    logger.warning(
                        "No previous manifest found for task %s VM %s; falling back to full backup.",
                        task.get("id"),
                        vm_name,
                    )
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        20,
                        f"No previous manifest found for {vm_name}; falling back to full backup",
                        worker_mode="vcenter-abr-vmdk-tool",
                        vm_name=vm_name,
                    )

            if use_changed_block_backup and not query_changed_areas_only and parent_manifest is not None:
                vm_config_changed_full_required, vm_config_change_reason = _vm_config_changed_since_parent(
                    disks, parent_manifest
                )
                if vm_config_changed_full_required:
                    use_changed_block_backup = False
                    incremental = False
                    differential = False
                    logger.warning(
                        "VM configuration change detected for %s (task %s): %s; performing a full "
                        "backup of the entire VM instead of an incremental to keep the backup chain "
                        "consistent.",
                        vm_name,
                        task.get("id"),
                        vm_config_change_reason,
                    )
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        20,
                        f"VM configuration changed for {vm_name}: {vm_config_change_reason}; "
                        "performing a full backup of the entire VM",
                        worker_mode="vcenter-abr-vmdk-tool",
                        vm_name=vm_name,
                        vm_config_changed=True,
                    )

            if use_changed_block_backup:
                for index, disk in enumerate(disks, start=1):
                    payload = disk.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}
                    datastore_path = str(payload.get("datastore_path") or disk.get("datastore_path") or "")
                    uuid = str(payload.get("uuid") or disk.get("uuid") or "")
                    change_entry = (
                        change_id_by_uuid.get(uuid)
                        or change_id_by_datastore_path.get(datastore_path)
                        or change_id_by_index.get(index)
                    )
                    change_id = str(change_entry.get("change_id") or "") if change_entry is not None else ""
                    parent_disk = _resolve_parent_disk_entry(
                        parent_manifest or {},
                        datastore_path=datastore_path,
                        uuid=uuid,
                        index=index,
                    )
                    new_disk_detected = parent_disk is None
                    parent_disk = parent_disk or {}
                    parent_change_id = str(
                        parent_disk.get("change_id")
                        or parent_disk.get("next_change_id")
                        or ""
                    )
                    query_change_id = parent_change_id or change_id
                    missing_cbt_change_id = not query_change_id or new_disk_detected
                    parent_capacity_gb = parent_disk.get("capacity_gb")
                    current_capacity_gb = payload.get("capacity_gb") or disk.get("capacity_gb")
                    capacity_changed = False

                    if parent_capacity_gb not in (None, "") and current_capacity_gb not in (None, ""):
                        try:
                            capacity_changed = float(parent_capacity_gb) != float(current_capacity_gb)
                        except (TypeError, ValueError):
                            capacity_changed = False

                    if capacity_changed:
                        missing_cbt_change_id = True

                    if new_disk_detected and query_changed_areas_only:
                        raise VCenterBackupError(
                            f"Disk {datastore_path or uuid or index} was added to the VM after the "
                            "parent backup; a full copy is required to establish a baseline before "
                            "changed areas can be queried"
                        )
                    if missing_cbt_change_id and query_changed_areas_only:
                        if capacity_changed:
                            raise VCenterBackupError(
                                f"Disk {datastore_path or uuid or index} capacity changed from "
                                f"{parent_capacity_gb}GB to {current_capacity_gb}GB since the parent "
                                "backup; a full copy is required to re-baseline before changed areas "
                                "can be queried"
                            )
                        raise VCenterBackupError(
                            f"Snapshot did not return a CBT change ID for disk {datastore_path or uuid or index}"
                        )
                    parent_destination_path = str(parent_disk.get("destination_path") or "").strip()
                    parent_path = (
                        Path(parent_destination_path).expanduser().resolve()
                        if parent_destination_path
                        else Path()
                    )
                    # .abr incrementals chain by backup name + changeId, so there
                    # is no parent .vmdk file to validate on disk.
                    disk_key = int(change_entry.get("key") or 0) if change_entry is not None else 0
                    snapshot_datastore_path = (
                        str(change_entry.get("datastore_path") or "")
                        if change_entry is not None
                        else ""
                    )
                    utility_datastore_path = snapshot_datastore_path or datastore_path
                    if query_changed_areas_only and not disk_key:
                        raise VCenterBackupError(
                            f"Snapshot did not return a disk key for disk {datastore_path or uuid or index}"
                        )
                    changed_areas: list[dict[str, int]] = []
                    if disk_key and not missing_cbt_change_id:
                        try:
                            changed_areas = node.query_changed_disk_areas(
                                vm_name,
                                snapshot_name,
                                disk_key,
                                query_change_id,
                            )
                        except Exception as exc:
                            if query_changed_areas_only or not _is_cbt_invalid_fault(exc):
                                raise
                            logger.warning(
                                "CBT could not resolve changes for disk %s (task %s, VM %s): %s; "
                                "storing a full copy of this disk to re-baseline CBT instead of "
                                "failing the backup.",
                                datastore_path,
                                task.get("id"),
                                vm_name,
                                exc,
                            )
                            missing_cbt_change_id = True
                    changed_bytes = sum(int(area.get("length", 0) or 0) for area in changed_areas)
                    # Bytes read by earlier disks: the baseline this disk's live
                    # progress interpolates from, so the counter climbs during
                    # the transfer instead of jumping before it starts.
                    base_read_bytes = total_read_bytes
                    total_read_bytes += changed_bytes
                    # An incremental moves only the changed extents, so size its
                    # budget on those -- except on the no-CBT-baseline and
                    # utility-failure paths below, which fall back to storing the
                    # WHOLE disk and therefore need the full-capacity budget.
                    disk_capacity_gb = payload.get("capacity_gb") or disk.get("capacity_gb")
                    full_disk_bytes = (
                        int(disk_capacity_gb * 1024**3)
                        if isinstance(disk_capacity_gb, (int, float))
                        else 0
                    )
                    full_disk_timeout_seconds = _disk_timeout_seconds(tool_timeout_seconds, disk_capacity_gb)
                    disk_timeout_seconds = _disk_timeout_seconds(
                        tool_timeout_seconds, changed_bytes / 1024**3
                    )
                    disk_resource = {
                        "name": disk.get("name") or disk.get("disk_label") or f"disk-{index}",
                        "payload": {"datastore_path": datastore_path},
                    }
                    final_destination_path = _build_disk_destination(task_root, vm_name, disk_resource, index)
                    final_destination_path.parent.mkdir(parents=True, exist_ok=True)
                    disk_backup_name = final_destination_path.name
                    written_backup_names.append(disk_backup_name)
                    progress = min(25 + int((index - 1) * 60 / max(len(disks), 1)), 85)
                    progress_span = max(1, min(25 + int(index * 60 / max(len(disks), 1)), 85) - progress)
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        progress,
                        (
                            f"Collected changed areas for disk {index}/{len(disks)} from {datastore_path}"
                            if query_changed_areas_only
                            else f"Storing changed blocks for disk {index}/{len(disks)} from {datastore_path}"
                        ),
                        worker_mode="vcenter-abr-vmdk-tool",
                        current_disk=datastore_path,
                        changed_area_count=len(changed_areas),
                        changed_bytes=changed_bytes,
                        processed_bytes=base_read_bytes,
                    )
                    command: list[str] = []
                    utility_change_id = _format_utility_change_id(
                        query_change_id,
                        service_type=service_type,
                        format_mode=utility_change_id_format,
                    )
                    disk_store_mode = "query-only" if query_changed_areas_only else "incremental"
                    diagnostic_path: Path | None = None
                    fallback_error = ""
                    # stdout of the run that stored this disk. Every attempt
                    # overwrites it; _parse_tool_result ignores all but the
                    # status=="ok" one, so retries and fallbacks sort themselves
                    # out without tracking which branch won.
                    disk_tool_stdout = ""
                    if not query_changed_areas_only:
                        if repository_abr_path is None:
                            raise VCenterBackupError("Repository context is missing for incremental backup")

                        if missing_cbt_change_id:
                            if new_disk_detected:
                                logger.warning(
                                    "VM configuration change detected for %s: disk %s is not present "
                                    "in the parent backup manifest (task %s), so it was added to the "
                                    "VM since the last backup. Storing a full copy of this disk to "
                                    "include it and re-baseline the backup chain.",
                                    vm_name,
                                    datastore_path,
                                    task.get("id"),
                                )
                            elif capacity_changed:
                                logger.warning(
                                    "Disk %s capacity changed from %sGB to %sGB since the parent "
                                    "backup (task %s, VM %s); the existing CBT baseline no longer "
                                    "describes this disk. Storing a full copy instead of a delta "
                                    "that could never be restored at the current size.",
                                    datastore_path,
                                    parent_capacity_gb,
                                    current_capacity_gb,
                                    task.get("id"),
                                    vm_name,
                                )
                            else:
                                logger.warning(
                                    "No CBT change ID available for disk %s (task %s, VM %s); "
                                    "no incremental baseline exists (e.g. the VM has stayed "
                                    "powered off since CBT was enabled). Storing a full copy "
                                    "of the disk instead.",
                                    datastore_path,
                                    task.get("id"),
                                    vm_name,
                                )
                            _emit_progress(
                                progress_callback,
                                "transfer",
                                progress,
                                (
                                    f"New disk detected since the parent backup; storing a full "
                                    f"copy of disk {index}/{len(disks)} ({datastore_path}) to include it"
                                    if new_disk_detected
                                    else f"Disk {index}/{len(disks)} capacity changed since the parent "
                                    f"backup; storing a full copy for {datastore_path}"
                                    if capacity_changed
                                    else f"No CBT baseline for disk {index}/{len(disks)}; "
                                    f"storing a full copy for {datastore_path}"
                                ),
                                worker_mode="vcenter-abr-vmdk-tool",
                                current_disk=datastore_path,
                                new_disk_detected=new_disk_detected,
                            )
                            no_cbt_command = _build_backup_command(
                                tool_path=tool_path,
                                backup_name=disk_backup_name,
                                repository=repository,
                                abr_path=repository_abr_path,
                                datastore_path=datastore_path,
                                host=host,
                                port=port,
                                username=username,
                                password=password,
                                thumbprint=thumbprint,
                                vm_moref=_format_vm_spec(vm_moref, service_type),
                                snapshot_moref=snapshot_moref,
                                transport_mode=transport_mode if is_virtual_center else None,
                                compression_level=compression_level,
                                encryption_key=encryption_key,
                            )
                            command = no_cbt_command
                            logger.info(
                                "Full disk command preview for task %s (no-CBT-baseline fallback): %s",
                                task.get("id"),
                                _redact_command_for_logging(no_cbt_command),
                            )
                            completed = run_capped(
                                no_cbt_command,
                                check=False,
                                capture_output=True,
                                text=True,
                                should_cancel=lambda: cancel_requested(task_id),
                                timeout=full_disk_timeout_seconds,
                                on_progress=_disk_tool_progress_reporter(
                                    progress_callback,
                                    base_progress=progress,
                                    progress_span=progress_span,
                                    message_prefix=f"Storing changed blocks for disk {index}/{len(disks)}",
                                    base_read_bytes=base_read_bytes,
                                    # Re-baselining CBT stores the whole disk, so
                                    # this tick's reads span its full capacity.
                                    disk_total_bytes=full_disk_bytes,
                                    measure_transferred=measure_transferred,
                                ),
                            )
                            disk_tool_stdout = completed.stdout or ""
                            if completed.returncode != 0:
                                stderr = (completed.stderr or completed.stdout or "").strip()
                                raise VCenterBackupError(
                                    f"Full disk backup failed for {datastore_path} while "
                                    "recovering from a missing CBT change ID: "
                                    f"{stderr or f'exit code {completed.returncode}'}"
                                )
                            disk_store_mode = (
                                "full-new-disk"
                                if new_disk_detected
                                else "full-capacity-changed" if capacity_changed else "full-no-cbt-baseline"
                            )
                            incremental_full_fallback_used = True
                        else:
                            command = _build_backup_command(
                                tool_path=tool_path,
                                backup_name=disk_backup_name,
                                repository=repository,
                                abr_path=repository_abr_path,
                                datastore_path=utility_datastore_path,
                                change_id=utility_change_id,
                                host=host,
                                port=port,
                                username=username,
                                password=password,
                                thumbprint=thumbprint,
                                vm_moref=_format_vm_spec(vm_moref, service_type),
                                snapshot_moref=snapshot_moref,
                                transport_mode=transport_mode if is_virtual_center else None,
                                compression_level=compression_level,
                                encryption_key=encryption_key,
                            )
                            logger.info(
                                "Incremental disk command parameters for task %s: datastore_path=%s utility_datastore_path=%s parent_path=%s change_id=%s utility_change_id=%s utility_change_id_format=%s service_type=%s snapshot_moref=%s vm_moref=%s",
                                task.get("id"),
                                datastore_path,
                                utility_datastore_path,
                                parent_path,
                                query_change_id,
                                utility_change_id,
                                utility_change_id_format,
                                service_type,
                                snapshot_moref,
                                _format_vm_spec(vm_moref, service_type),
                            )
                            logger.info(
                                "Incremental disk command preview for task %s: %s",
                                task.get("id"),
                                _redact_command_for_logging(command),
                            )
                            completed = run_capped(
                                command,
                                check=False,
                                capture_output=True,
                                text=True,
                                should_cancel=lambda: cancel_requested(task_id),
                                timeout=disk_timeout_seconds,
                                on_progress=_disk_tool_progress_reporter(
                                    progress_callback,
                                    base_progress=progress,
                                    progress_span=progress_span,
                                    message_prefix=f"Storing changed blocks for disk {index}/{len(disks)}",
                                    base_read_bytes=base_read_bytes,
                                    disk_total_bytes=changed_bytes,
                                    measure_transferred=measure_transferred,
                                ),
                            )
                            disk_tool_stdout = completed.stdout or ""
                            if completed.returncode != 0:
                                stderr = (completed.stderr or completed.stdout or "").strip()

                                diagnostic_path = final_destination_path.with_suffix(
                                    final_destination_path.suffix + ".diagnostics.json"
                                )
                                diagnostic_path.write_text(
                                    json.dumps(
                                        {
                                            "task_id": task.get("id"),
                                            "datastore_path": datastore_path,
                                            "parent_path": str(parent_path),
                                            "destination_path": str(final_destination_path),
                                            "change_id": query_change_id,
                                            "utility_change_id": utility_change_id,
                                            "utility_change_id_format": utility_change_id_format,
                                            "utility_datastore_path": utility_datastore_path,
                                            "service_type": service_type,
                                            "snapshot_moref": snapshot_moref,
                                            "vm_moref": _format_vm_spec(vm_moref, service_type),
                                            "disk_key": disk_key,
                                            "changed_area_count": len(changed_areas),
                                            "changed_bytes": changed_bytes,
                                            "changed_areas": changed_areas,
                                            "command": _redact_command_for_logging(command),
                                            "returncode": completed.returncode,
                                            "stdout": completed.stdout,
                                            "stderr": completed.stderr,
                                        },
                                        indent=2,
                                        sort_keys=True,
                                    ),
                                    encoding="utf-8",
                                )
                                logger.error(
                                    "Incremental utility failed for task %s disk %s; wrote diagnostics to %s",
                                    task.get("id"),
                                    datastore_path,
                                    diagnostic_path,
                                )
                                if not incremental_fallback_full and not incremental_fallback_metadata:
                                    raise VCenterBackupError(
                                        f"Incremental disk backup failed for {datastore_path}: {stderr or f'exit code {completed.returncode}'}"
                                    )

                                if incremental_fallback_full:
                                    disk_store_mode = "full-fallback"
                                    _emit_progress(
                                        progress_callback,
                                        "transfer",
                                        progress,
                                        (
                                            f"Incremental utility failed for disk {index}/{len(disks)}; "
                                            f"falling back to full VMDK store for {datastore_path}"
                                        ),
                                        worker_mode="vcenter-abr-vmdk-tool",
                                        current_disk=datastore_path,
                                        changed_area_count=len(changed_areas),
                                        changed_bytes=changed_bytes,
                                    )
                                    if repository_abr_path is None:
                                        raise VCenterBackupError("Repository context is missing for full fallback")
                                    fallback_command = _build_backup_command(
                                        tool_path=tool_path,
                                        backup_name=disk_backup_name,
                                        repository=repository,
                                        abr_path=repository_abr_path,
                                        datastore_path=datastore_path,
                                        host=host,
                                        port=port,
                                        username=username,
                                        password=password,
                                        thumbprint=thumbprint,
                                        vm_moref=_format_vm_spec(vm_moref, service_type),
                                        snapshot_moref=snapshot_moref,
                                        transport_mode=transport_mode if is_virtual_center else None,
                                        compression_level=compression_level,
                                        encryption_key=encryption_key,
                                    )
                                    logger.warning(
                                        "Incremental utility failed for task %s disk %s; falling back to full store command: %s",
                                        task.get("id"),
                                        datastore_path,
                                        _redact_command_for_logging(fallback_command),
                                    )
                                    fallback_completed = run_capped(
                                        fallback_command,
                                        check=False,
                                        capture_output=True,
                                        text=True,
                                        should_cancel=lambda: cancel_requested(task_id),
                                        timeout=full_disk_timeout_seconds,
                                        on_progress=_disk_tool_progress_reporter(
                                            progress_callback,
                                            base_progress=progress,
                                            progress_span=progress_span,
                                            message_prefix=f"Storing changed blocks for disk {index}/{len(disks)}",
                                            base_read_bytes=base_read_bytes,
                                            # Fallback stores the whole disk.
                                            disk_total_bytes=full_disk_bytes,
                                            measure_transferred=measure_transferred,
                                        ),
                                    )
                                    disk_tool_stdout = fallback_completed.stdout or ""
                                    if fallback_completed.returncode != 0:
                                        fallback_stderr = (
                                            fallback_completed.stderr
                                            or fallback_completed.stdout
                                            or ""
                                        ).strip()
                                        fallback_error = fallback_stderr or f"exit code {fallback_completed.returncode}"
                                    else:
                                        incremental_full_fallback_used = True

                                if not incremental_full_fallback_used:
                                    if not incremental_fallback_metadata:
                                        raise VCenterBackupError(
                                            "Incremental disk backup failed for "
                                            f"{datastore_path}: {stderr or f'exit code {completed.returncode}'}. "
                                            "Full fallback also failed: "
                                            f"{fallback_error or 'not attempted'}"
                                        )
                                    incremental_metadata_fallback_used = True
                                    disk_store_mode = "metadata-fallback"
                                    logger.warning(
                                        "Incremental utility failed for task %s disk %s; completing with metadata fallback only. full_fallback_error=%s",
                                        task.get("id"),
                                        datastore_path,
                                        fallback_error,
                                    )
                    logger.info(
                        "Incremental changed areas for task %s disk %s: previous_change_id=%s current_change_id=%s disk_key=%s area_count=%d changed_bytes=%d",
                        task.get("id"),
                        datastore_path,
                        query_change_id,
                        change_id,
                        disk_key,
                        len(changed_areas),
                        changed_bytes,
                    )
                    disk_logical_bytes, disk_stored_bytes = _accumulate_tool_sizes(
                        tool_sizes, disk_tool_stdout
                    )
                    disk_outputs.append(
                        {
                            "index": index,
                            "disk_label": disk.get("name") or disk.get("disk_label"),
                            "datastore_path": datastore_path,
                            "uuid": uuid or None,
                            "logical_bytes": disk_logical_bytes,
                            "stored_bytes": disk_stored_bytes,
                            "compression_ratio": _compression_ratio(disk_logical_bytes, disk_stored_bytes),
                            "capacity_gb": payload.get("capacity_gb") or disk.get("capacity_gb"),
                            "source_disk_type_code": (
                                payload.get("source_disk_type_code")
                                if payload.get("source_disk_type_code") is not None
                                else disk.get("source_disk_type_code")
                            ),
                            "source_disk_type_name": (
                                payload.get("source_disk_type_name")
                                or disk.get("source_disk_type_name")
                            ),
                            "change_id": change_id,
                            "previous_change_id": query_change_id,
                            "utility_change_id": utility_change_id,
                            "utility_change_id_format": utility_change_id_format,
                            "utility_datastore_path": utility_datastore_path,
                            "store_mode": disk_store_mode,
                            "incremental_fallback_full": incremental_fallback_full,
                            "incremental_fallback_metadata": incremental_fallback_metadata,
                            "fallback_error": fallback_error,
                            "diagnostic_path": str(diagnostic_path) if diagnostic_path else "",
                            "disk_key": disk_key,
                            "snapshot_datastore_path": (
                                str(change_entry.get("datastore_path") or "")
                                if change_entry is not None
                                else ""
                            ),
                            # .abr backups chain by backup name; there is no parent
                            # .vmdk nor a single destination file to record.
                            "parent_path": "",
                            "destination_path": "",
                            "parent_manifest_path": str(parent_manifest_path) if parent_manifest_path else "",
                            "storage_mode": "s3" if repository_type == "s3" else "local",
                            "repository_type": repository_type or "local",
                            "repository_id": str(repository.get("id") or "") if isinstance(repository, dict) else "",
                            "abr_path": (
                                repository_abr_path if disk_store_mode != "metadata-fallback" else ""
                            ),
                            "abr_backup_name": (
                                disk_backup_name if disk_store_mode != "metadata-fallback" else ""
                            ),
                            "query_changed_areas_only": query_changed_areas_only,
                            "changed_area_count": len(changed_areas),
                            "changed_bytes": changed_bytes,
                            "changed_areas": changed_areas,
                        }
                    )
                    if repository_type != "s3":
                        total_transferred_bytes = _dir_size_bytes(task_root)
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        progress,
                        f"Stored disk {index}/{len(disks)} to repository",
                        worker_mode="vcenter-abr-vmdk-tool",
                        processed_bytes=total_read_bytes,
                        transferred_bytes=total_transferred_bytes,
                    )
            else:
                for index, disk in enumerate(disks, start=1):
                    disk_start_time = time.time()
                    payload = disk.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}
                    datastore_path = str(payload.get("datastore_path") or disk.get("datastore_path") or "")
                    if not datastore_path:
                        raise VCenterBackupError(f"Disk entry is missing datastore_path: {disk}")
                    formatted_vm_moref = _format_vm_spec(vm_moref, service_type)
                    transfer_datastore_path = datastore_path
                    live_snapshot_datastore_path = live_disk_paths.get(datastore_path, datastore_path)
                    if not is_virtual_center and not formatted_vm_moref:
                        transfer_datastore_path = live_snapshot_datastore_path

                    disk_resource = {
                        "name": disk.get("name") or disk.get("disk_label") or f"disk-{index}",
                        "payload": {"datastore_path": datastore_path},
                    }
                    final_destination_path = _build_disk_destination(task_root, vm_name, disk_resource, index)
                    final_destination_path.parent.mkdir(parents=True, exist_ok=True)
                    disk_backup_name = final_destination_path.name
                    written_backup_names.append(disk_backup_name)
                    logger.info(
                        "Backing up disk %d/%d from %s as .abr backup %s",
                        index,
                        len(disks),
                        transfer_datastore_path,
                        disk_backup_name,
                    )
                    disk_capacity_gb = payload.get("capacity_gb") or disk.get("capacity_gb")
                    # Bytes read by the disks BEFORE this one -- the baseline the
                    # progress reporter interpolates this disk's reads on top of.
                    # total_read_bytes only takes this disk's own capacity once
                    # the transfer is done, so the interpolated figure never
                    # double-counts and never jumps to the total up front.
                    base_read_bytes = total_read_bytes
                    disk_total_bytes = (
                        int(disk_capacity_gb * 1024**3)
                        if isinstance(disk_capacity_gb, (int, float))
                        else 0
                    )
                    # A full backup moves the whole disk, so its budget scales
                    # with capacity -- a flat 900s killed a healthy 94GB transfer
                    # mid-flight.
                    disk_timeout_seconds = _disk_timeout_seconds(tool_timeout_seconds, disk_capacity_gb)
                    progress = min(25 + int((index - 1) * 60 / max(len(disks), 1)), 85)
                    progress_span = max(1, min(25 + int(index * 60 / max(len(disks), 1)), 85) - progress)
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        progress,
                        f"Backing up disk {index}/{len(disks)} from {transfer_datastore_path}",
                        worker_mode="vcenter-abr-vmdk-tool",
                        current_disk=transfer_datastore_path,
                        processed_bytes=total_read_bytes,
                    )

                    if repository_abr_path is None:
                        raise VCenterBackupError("Repository context is missing for full backup")
                    command = _build_backup_command(
                        tool_path=tool_path,
                        backup_name=disk_backup_name,
                        repository=repository,
                        abr_path=repository_abr_path,
                        datastore_path=transfer_datastore_path,
                        host=host,
                        port=port,
                        username=username,
                        password=password,
                        thumbprint=thumbprint,
                        vm_moref=formatted_vm_moref,
                        snapshot_moref=snapshot_moref,
                        transport_mode=transport_mode if is_virtual_center else None,
                        compression_level=compression_level,
                        encryption_key=encryption_key,
                    )
                    logger.info(
                        "Full disk command parameters for task %s: datastore_path=%s live_snapshot_datastore_path=%s transfer_datastore_path=%s service_type=%s is_virtual_center=%s snapshot_moref=%s raw_vm_moref=%s formatted_vm_moref=%s host=%s port=%s transport_mode=%s tool_path=%s destination_path=%s timeout_seconds=%s",
                        task.get("id"),
                        datastore_path,
                        live_snapshot_datastore_path,
                        transfer_datastore_path,
                        service_type,
                        is_virtual_center,
                        snapshot_moref,
                        vm_moref,
                        formatted_vm_moref,
                        host,
                        port,
                        transport_mode if is_virtual_center else None,
                        tool_path,
                        disk_backup_name,
                        tool_timeout_seconds,
                    )
                    logger.info(
                        "Full disk command preview for task %s: %s",
                        task.get("id"),
                        _redact_command_for_logging(command),
                    )
                    try:
                        completed = run_capped(
                            command,
                            check=False,
                            capture_output=True,
                            text=True,
                            should_cancel=lambda: cancel_requested(task_id),
                            timeout=disk_timeout_seconds,
                            on_progress=_disk_tool_progress_reporter(
                                progress_callback,
                                base_progress=progress,
                                progress_span=progress_span,
                                message_prefix=f"Backing up disk {index}/{len(disks)}",
                                base_read_bytes=base_read_bytes,
                                disk_total_bytes=disk_total_bytes,
                                measure_transferred=measure_transferred,
                            ),
                        )
                    except subprocess.TimeoutExpired as exc:
                        stdout = exc.stdout if isinstance(exc.stdout, str) else (
                            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else ""
                        )
                        stderr = exc.stderr if isinstance(exc.stderr, str) else (
                            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else ""
                        )
                        tool_output = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
                        timeout_context = (
                            f"Backup tool timed out after {disk_timeout_seconds} seconds for {transfer_datastore_path}"
                        )
                        logger.error("%s. stdout=%s stderr=%s", timeout_context, stdout, stderr)
                        if tool_output:
                            timeout_context = f"{timeout_context}. Tool output: {tool_output}"
                        raise VCenterBackupError(
                            f"{timeout_context}. That budget already scales with this disk's capacity; "
                            "raise parameters.tool_timeout_seconds (or lower "
                            "BACKUPSERVER_VCENTER_MIN_TRANSFER_MBPS) if the transport is slower than expected."
                        ) from exc

                    logger.debug(
                        "Backup tool exit=%s for disk %d, stdout=%s stderr=%s",
                        completed.returncode,
                        index,
                        completed.stdout,
                        completed.stderr,
                    )
                    tool_output = "\n".join(
                        part.strip()
                        for part in (completed.stdout or "", completed.stderr or "")
                        if part and part.strip()
                    )
                    retry_budget = _TRANSIENT_DISK_ACCESS_RETRIES
                    while (
                        completed.returncode != 0
                        and retry_budget > 0
                        and _is_transient_disk_access_error(tool_output)
                    ):
                        retry_budget -= 1
                        logger.warning(
                            "Disk open for %s hit a transient storage-access error right after "
                            "snapshot creation; retrying in %.0fs (%d attempt(s) left). %s",
                            transfer_datastore_path,
                            _TRANSIENT_DISK_ACCESS_RETRY_DELAY_SECONDS,
                            retry_budget,
                            tool_output,
                        )
                        time.sleep(_TRANSIENT_DISK_ACCESS_RETRY_DELAY_SECONDS)
                        completed = run_capped(
                            command,
                            check=False,
                            capture_output=True,
                            text=True,
                            should_cancel=lambda: cancel_requested(task_id),
                            timeout=disk_timeout_seconds,
                            on_progress=_disk_tool_progress_reporter(
                                progress_callback,
                                base_progress=progress,
                                progress_span=progress_span,
                                message_prefix=f"Backing up disk {index}/{len(disks)}",
                                base_read_bytes=base_read_bytes,
                                disk_total_bytes=disk_total_bytes,
                                measure_transferred=measure_transferred,
                            ),
                        )
                        tool_output = "\n".join(
                            part.strip()
                            for part in (completed.stdout or "", completed.stderr or "")
                            if part and part.strip()
                        )
                    if completed.returncode != 0 and not is_virtual_center:
                        hostagent_retry_variants = []
                        for variant_vm_moref in _hostagent_vm_retry_specs(vm_moref):
                            hostagent_retry_variants.append(
                                (f"with_vm_moref_only[{variant_vm_moref}]", variant_vm_moref, None)
                            )
                            hostagent_retry_variants.append(
                                (
                                    f"with_vm_and_snapshot_moref[{variant_vm_moref}]",
                                    variant_vm_moref,
                                    snapshot_moref,
                                )
                            )
                        attempted_signatures = {
                            (
                                formatted_vm_moref,
                                snapshot_moref,
                                transfer_datastore_path,
                            )
                        }
                        for variant_name, variant_vm_moref, variant_snapshot_moref in hostagent_retry_variants:
                            signature = (
                                variant_vm_moref,
                                variant_snapshot_moref,
                                transfer_datastore_path,
                            )
                            if signature in attempted_signatures:
                                continue
                            attempted_signatures.add(signature)
                            if repository_abr_path is None:
                                raise VCenterBackupError("Repository context is missing for HostAgent retry")
                            retry_command = _build_backup_command(
                                tool_path=tool_path,
                                backup_name=disk_backup_name,
                                repository=repository,
                                abr_path=repository_abr_path,
                                datastore_path=transfer_datastore_path,
                                host=host,
                                port=port,
                                username=username,
                                password=password,
                                thumbprint=thumbprint,
                                vm_moref=variant_vm_moref,
                                snapshot_moref=variant_snapshot_moref,
                                transport_mode=None,
                                compression_level=compression_level,
                                encryption_key=encryption_key,
                            )
                            logger.info(
                                "HostAgent retry command parameters for task %s variant=%s: datastore_path=%s transfer_datastore_path=%s service_type=%s snapshot_moref=%s raw_vm_moref=%s host=%s port=%s tool_path=%s abr_backup_name=%s",
                                task.get("id"),
                                variant_name,
                                datastore_path,
                                transfer_datastore_path,
                                service_type,
                                variant_snapshot_moref,
                                variant_vm_moref,
                                host,
                                port,
                                tool_path,
                                disk_backup_name,
                            )
                            logger.info(
                                "HostAgent retry command preview for task %s variant=%s: %s",
                                task.get("id"),
                                variant_name,
                                _redact_command_for_logging(retry_command),
                            )
                            retry_completed = run_capped(
                                retry_command,
                                check=False,
                                capture_output=True,
                                text=True,
                                should_cancel=lambda: cancel_requested(task_id),
                                timeout=disk_timeout_seconds,
                                on_progress=_disk_tool_progress_reporter(
                                    progress_callback,
                                    base_progress=progress,
                                    progress_span=progress_span,
                                    message_prefix=f"Backing up disk {index}/{len(disks)}",
                                    base_read_bytes=base_read_bytes,
                                    disk_total_bytes=disk_total_bytes,
                                    measure_transferred=measure_transferred,
                                ),
                            )
                            retry_output = "\n".join(
                                part.strip()
                                for part in (retry_completed.stdout or "", retry_completed.stderr or "")
                                if part and part.strip()
                            )
                            logger.debug(
                                "HostAgent retry exit=%s for disk %d variant=%s, stdout=%s stderr=%s",
                                retry_completed.returncode,
                                index,
                                variant_name,
                                retry_completed.stdout,
                                retry_completed.stderr,
                            )
                            if retry_completed.returncode == 0:
                                completed = retry_completed
                                tool_output = retry_output
                                break
                            tool_output = "\n".join(
                                value
                                for value in (tool_output, f"[{variant_name}] {retry_output}")
                                if value.strip()
                            )
                    if completed.returncode != 0:
                        stderr = (completed.stderr or completed.stdout or "").strip()
                        logger.error(
                            "Disk backup failed for %s: %s",
                            datastore_path,
                            stderr or f"exit code {completed.returncode}",
                        )
                        raise VCenterBackupError(
                            f"Disk backup failed for {datastore_path}: {stderr or f'exit code {completed.returncode}'}"
                        )

                    disk_duration = time.time() - disk_start_time
                    logger.info("Successfully backed up disk %d in %.2f seconds.", index, disk_duration)

                    # Bank this disk's capacity only now that it is actually
                    # read, so the interpolated per-tick figure above and this
                    # running total agree instead of the total jumping to the
                    # disk's full size before a single block moved.
                    total_read_bytes = base_read_bytes + disk_total_bytes

                    disk_logical_bytes, disk_stored_bytes = _accumulate_tool_sizes(
                        tool_sizes, completed.stdout
                    )
                    disk_outputs.append(
                        {
                            "index": index,
                            "disk_label": disk.get("name") or disk.get("disk_label"),
                            "datastore_path": datastore_path,
                            "transfer_datastore_path": transfer_datastore_path,
                            "logical_bytes": disk_logical_bytes,
                            "stored_bytes": disk_stored_bytes,
                            "compression_ratio": _compression_ratio(disk_logical_bytes, disk_stored_bytes),
                            "uuid": payload.get("uuid") or disk.get("uuid"),
                            "capacity_gb": payload.get("capacity_gb") or disk.get("capacity_gb"),
                            "source_disk_type_code": (
                                payload.get("source_disk_type_code")
                                if payload.get("source_disk_type_code") is not None
                                else disk.get("source_disk_type_code")
                            ),
                            "source_disk_type_name": (
                                payload.get("source_disk_type_name")
                                or disk.get("source_disk_type_name")
                            ),
                            "change_id": str(
                                (
                                    change_id_by_uuid.get(str(payload.get("uuid") or disk.get("uuid") or ""))
                                    or change_id_by_datastore_path.get(datastore_path)
                                    or {}
                                ).get("change_id")
                                or ""
                            ),
                            "destination_path": "",
                            "storage_mode": "s3" if repository_type == "s3" else "local",
                            "repository_type": repository_type or "local",
                            "repository_id": str(repository.get("id") or "") if isinstance(repository, dict) else "",
                            "abr_path": repository_abr_path,
                            "abr_backup_name": disk_backup_name,
                        }
                    )
                    if repository_type != "s3":
                        total_transferred_bytes = _dir_size_bytes(task_root)
                    _emit_progress(
                        progress_callback,
                        "transfer",
                        progress,
                        f"Stored disk {index}/{len(disks)} to repository",
                        worker_mode="vcenter-abr-vmdk-tool",
                        processed_bytes=total_read_bytes,
                        transferred_bytes=total_transferred_bytes,
                    )

            manifest_path = task_root / "manifest.json"
            # S3 repositories stream straight to the bucket, so there is no local
            # directory to measure -- fall back to the read-side estimate.
            if repository_type == "s3":
                total_transferred_bytes = total_read_bytes
            # The tool's own counts supersede both of those whenever every disk
            # reported them: they are exact, they exclude the empty blocks the
            # capacity-based estimate counts, and they are the only figures an
            # S3 repository has at all. total_logical_bytes is the real data
            # size behind this backup -- what a restore would write back --
            # against which the stored size gives the compression ratio.
            total_logical_bytes: int | None = None
            if tool_sizes["complete"] and tool_sizes["logical"] > 0:
                total_logical_bytes = int(tool_sizes["logical"])
                total_transferred_bytes = int(tool_sizes["stored"])
            backup_compression_ratio = _compression_ratio(
                total_logical_bytes, total_transferred_bytes
            )
            backup_mode = (
                "incremental-full-fallback"
                if incremental_full_fallback_used
                else "incremental-metadata-fallback" if incremental_metadata_fallback_used
                else "differential" if differential
                else "incremental" if incremental else "full"
            )
            existing_result = task.get("result") if isinstance(task.get("result"), dict) else {}
            resolved_backup_type = str(
                parameters.get("resolved_backup_type")
                or existing_result.get("resolved_backup_type")
                or ("Incremental" if incremental else "ActiveFull")
            )
            resolved_backup_reason = str(
                parameters.get("resolved_backup_reason")
                or existing_result.get("resolved_backup_reason")
                or ("StandardIncremental" if incremental else "FirstRun")
            )
            if incremental_full_fallback_used:
                # The backup tool had no working incremental (.abr) path and fell
                # back to a full legacy VMDK copy -- every byte of the disk was
                # actually written, regardless of what the scheduler originally
                # intended. The catalog must reflect reality here, or the
                # resulting restore point keeps the scheduler's "Incremental"
                # label while being full-sized on disk.
                resolved_backup_type = "ActiveFull"
                resolved_backup_reason = "IncrementalFallbackToFull"
            if vm_config_changed_full_required:
                resolved_backup_type = "ActiveFull"
                resolved_backup_reason = "VmConfigurationChanged"
            manifest_payload = {
                "format_version": 1,
                "operation": "vm-backup",
                "backup_mode": backup_mode,
                "resolved_backup_type": resolved_backup_type,
                "resolved_backup_reason": resolved_backup_reason,
                "hypervisor_kind": "vcenter",
                "hypervisor_id": str(task["hypervisor_id"]),
                "vm_name": vm_name,
                "vm_external_id": vm_resource["external_id"],
                "vm_config": vm_config,
                "transport_mode": transport_mode,
                "compression_level": compression_level,
                "encryption_enabled": bool(encryption_algorithm),
                "encryption_algorithm": encryption_algorithm,
                "service_type": service_type,
                "incremental": incremental,
                "differential": differential,
                "query_changed_areas_only": query_changed_areas_only,
                "incremental_fallback_full": incremental_fallback_full,
                "incremental_fallback_metadata": incremental_fallback_metadata,
                "incremental_full_fallback_used": incremental_full_fallback_used,
                "incremental_metadata_fallback_used": incremental_metadata_fallback_used,
                "vm_config_changed_full_required": vm_config_changed_full_required,
                "vm_config_change_reason": vm_config_change_reason,
                "cbt_enabled": cbt_enabled,
                "snapshot_name": snapshot_name,
                "snapshot_moref": snapshot_moref,
                "disks": disk_outputs,
                "repository_id": str(repository.get("id") or "") if isinstance(repository, dict) else "",
                "repository_type": repository_type or "local",
                "storage_mode": "s3" if repository_type == "s3" else "local",
                "repository_storage_path": repository_abr_path if repository_type == "s3" else str(task_root),
                "backup_root": str(task_root),
                "processed_bytes": total_read_bytes,
                "transferred_bytes": total_transferred_bytes,
                "logical_bytes": total_logical_bytes,
                "compression_ratio": backup_compression_ratio,
                "created_at": utc_now(),
            }
            manifest_path.write_text(
                json.dumps(manifest_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            try:
                run_disk_tool(
                    [
                        "meta_save",
                        "--name",
                        "manifest",
                        *disk_tool_storage_args(repository, abr_path=repository_abr_path),
                        "--source",
                        str(manifest_path),
                    ],
                    tool_path=tool_path,
                    timeout=60,
                )
                written_backup_names.append("manifest")
                apply_repository_immutability(repository, repository_abr_path, "manifest", timeout=120)
            except StorageToolError:
                logger.debug("Ignoring manifest metadata mirror failure for %s", manifest_path, exc_info=True)

            try:
                _emit_progress(
                    progress_callback,
                    "finalize",
                    92,
                    f"Removing snapshot {snapshot_name}",
                    worker_mode="vcenter-abr-vmdk-tool",
                    processed_bytes=total_read_bytes,
                    transferred_bytes=total_transferred_bytes,
                )
            except TaskStopRequested:
                stop_requested_after_completion = True
                logger.info(
                    "Stop requested for backup task %s after its data was already complete; "
                    "keeping the backup instead of discarding it.",
                    task.get("id"),
                )
            node.remove_snapshot(vm_name, snapshot_name)
            logger.info(
                "Backup snapshot %s removed for VM %s",
                snapshot_name,
                vm_name,
            )
            snapshot_name = None

        if not stop_requested_after_completion and cancel_requested(task_id):
            stop_requested_after_completion = True
            logger.info(
                "Stop requested for backup task %s after its data was already complete; "
                "keeping the backup instead of discarding it.",
                task.get("id"),
            )

        total_duration = time.time() - start_time
        logger.info(
            "Backup completed for VM %s with %d disk(s) in %.2f seconds.",
            vm_name,
            len(disk_outputs),
            total_duration
        )
        return {
            "operation": "backup",
            "worker_mode": "vcenter-abr-vmdk-tool",
            "hypervisor_kind": "vcenter",
            "vm_name": vm_name,
            "vm_external_id": vm_resource["external_id"],
            "backup_mode": backup_mode,
            "resolved_backup_type": resolved_backup_type,
            "resolved_backup_reason": resolved_backup_reason,
            "incremental": incremental,
            "differential": differential,
            "query_changed_areas_only": query_changed_areas_only,
            "incremental_fallback_full": incremental_fallback_full,
            "incremental_fallback_metadata": incremental_fallback_metadata,
            "incremental_full_fallback_used": incremental_full_fallback_used,
            "incremental_metadata_fallback_used": incremental_metadata_fallback_used,
            "vm_config_changed_full_required": vm_config_changed_full_required,
            "vm_config_change_reason": vm_config_change_reason,
            "cbt_enabled": cbt_enabled,
            "snapshot_removed": True,
            "stop_requested_after_completion": stop_requested_after_completion,
            "backup_root": str(task_root),
            "manifest_path": str(manifest_path),
            "repository_id": str(repository.get("id") or "") if isinstance(repository, dict) else "",
            "repository_type": repository_type or "local",
            "storage_mode": "s3" if repository_type == "s3" else "local",
            "repository_storage_path": repository_abr_path if repository_type == "s3" else str(task_root),
            "vm_config": vm_config,
            "disk_count": len(disk_outputs),
            "disks": disk_outputs,
            "transport_mode": transport_mode,
            "compression_level": compression_level,
            "encryption_enabled": bool(encryption_algorithm),
            "encryption_algorithm": encryption_algorithm,
            "service_type": service_type,
            "processed_bytes": total_read_bytes,
            "transferred_bytes": total_transferred_bytes,
            "logical_bytes": total_logical_bytes,
            "compression_ratio": backup_compression_ratio,
            "finished_at": utc_now(),
        }
    except ProcessCancelled as exc:
        logger.info(
            "Backup task %s stopped; discarding the backup data it had written.", task.get("id")
        )
        stranded = _discard_task_backups(
            repository, repository_abr_path, written_backup_names, task_root
        )
        raise TaskStopRequested(
            _stop_message(stranded, repository_abr_path, task_root)
        ) from exc
    except TaskStopRequested:
        logger.info(
            "Backup task %s stopped; discarding the backup data it had written.", task.get("id")
        )
        stranded = _discard_task_backups(
            repository, repository_abr_path, written_backup_names, task_root
        )
        raise TaskStopRequested(_stop_message(stranded, repository_abr_path, task_root)) from None
    except Exception as exc:
        logger.exception("Task %s failed critically during vCenter interaction: %s", task.get("id"), exc)
        _discard_task_backups(repository, repository_abr_path, written_backup_names, task_root)
        raise
    finally:
        if snapshot_name:
            try:
                try:
                    _emit_progress(
                        progress_callback,
                        "cleanup",
                        96,
                        f"Removing snapshot {snapshot_name}",
                        worker_mode="vcenter-abr-vmdk-tool",
                    )
                except TaskStopRequested:
                    pass
                with VCenterNode(host, username, password, port=port, verify_ssl=verify_ssl) as node:
                    node.remove_snapshot(vm_name, snapshot_name)
                logger.info(
                    "Backup snapshot %s removed for VM %s",
                    snapshot_name,
                    vm_name,
                )
            except Exception as exc:
                logger.warning("Snapshot cleanup warning for VM %s: %s", vm_name, exc)
                try:
                    _emit_progress(
                        progress_callback,
                        "cleanup",
                        96,
                        f"Snapshot cleanup warning: {exc}",
                        worker_mode="vcenter-abr-vmdk-tool",
                    )
                except TaskStopRequested:
                    pass
