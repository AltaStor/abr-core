# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import re
import time
from contextlib import nullcontext
from typing import Any

from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, TASKS, catalog_records
from python.srv.core.utils import utc_now
from python.srv.plugins import build_hypervisor_plugin
from python.srv.plugins.hyperv import hyperv_replica
from python.srv.plugins.vcenter import vcenter_replica
from python.srv.plugins.vcenter.disk_transport import SOAP, VDDK, transport_problem
from python.srv.services.api_errors import ApiError
from python.srv.services.internal.replication.replica_config import settle_replica
from python.srv.services.internal.replication.replica_leftovers import (
    clean_first_run_leftovers,
    clean_update_leftovers,
    finish_interrupted_swap,
    finish_swap,
    forget_unfinished_replica,
    metadata_after_swap,
    remove_replica_vms,
    undo_swap,
)
from python.srv.services.internal.replication.replication_session import (
    WORKER_MODE,
    ProgressCallback,
    ReplicationError,
    ReplicationValidationError,
    disk_folder,
    is_own_snapshot,
    is_replication_task,
    load_job,
    replica_marker,
    replica_tag,
    report_progress,
    report_quietly,
    save_replica,
    wait_seconds,
)
from python.srv.services.internal.replication.replication_settings import (
    AVAILABLE_STATUSES,
    replica_of,
    restore_payload,
    settings_of,
    source_points,
    with_current_source_names,
)

logger = logging.getLogger(__name__)


def _size(value: float | None) -> str:
    size = float(value or 0)
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MiB"
    return f"{size / 1024**3:.1f} GiB"


def _platform(inventory: dict[str, Any]) -> Any:
    kind = str(inventory.get("kind") or "")
    if kind == "vcenter":
        return vcenter_replica
    if kind == "hyper-v":
        return hyperv_replica
    raise ReplicationValidationError(f"Replication to a {kind or 'unknown'} environment is not supported")


def _target(settings: dict[str, Any]) -> dict[str, Any]:
    inventory = HYPERVISORS.get(settings["target_inventory_id"])
    if not isinstance(inventory, dict):
        raise ReplicationValidationError(
            f"Target environment {settings['target_inventory_name'] or settings['target_inventory_id']} no longer exists"
        )
    return inventory


def _connect(inventory: dict[str, Any]) -> Any:
    node = build_hypervisor_plugin(inventory)
    try:
        node.connect()
    except Exception as exc:
        raise ReplicationError(
            f"The target environment {inventory.get('name')} cannot be reached: {exc}"
        ) from exc
    return node


def _source_check(
    job: dict[str, Any], settings: dict[str, Any], progress_callback: ProgressCallback = None
) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import normalize_job_record

    source = JOBS.get(settings["source_job_id"])
    if not isinstance(source, dict):
        raise ReplicationValidationError(
            f"Source backup job {settings['source_job_name']} no longer exists, so there is nothing to replicate from"
        )
    vm_ids = [str(vm_id) for vm_id in source.get("vm_ids") or []]
    if settings["source_vm_id"] not in vm_ids:
        reassigned = [
            entry
            for entry in source.get("vm_reassignments") or []
            if isinstance(entry, dict) and str(entry.get("previous_vm_id") or "") == settings["source_vm_id"]
        ]
        if reassigned:
            raise ReplicationValidationError(
                f"Source backup job {source.get('name')} now backs up another VM ({reassigned[-1].get('vm_id')}) under "
                f"the name {reassigned[-1].get('vm_name') or settings['source_vm_name']}; this replica belongs to the old one. "
                "Create a new Replication Job"
            )
        raise ReplicationValidationError(
            f"VM {settings['source_vm_name']} is no longer in source backup job {source.get('name')}"
        )
    points = source_points(settings["source_job_id"], settings["source_vm_id"])
    if not points:
        raise ReplicationValidationError(
            f"Source backup job {source.get('name')} has no restore point of {settings['source_vm_name']} that can be "
            "replicated"
        )
    if not normalize_job_record(source).get("enabled"):
        report_progress(
            progress_callback,
            2,
            f"Source backup job {source.get('name')} is disabled, so it takes no new backups; the replica stays at the "
            f"newest restore point it has ({points[-1].get('id')} of {points[-1].get('created_at')})",
            stage="prepare",
        )
    return points


def _chosen_point(points: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    wanted = settings["restore_point_id"]
    if not wanted:
        return points[-1]
    for point in points:
        if str(point.get("id") or "") == wanted:
            return point
    raise ReplicationValidationError(
        f"Restore point {wanted}, which the replica is to be created from, is no longer available"
    )


def _create_replica(
    task: dict[str, Any],
    job: dict[str, Any],
    settings: dict[str, Any],
    point: dict[str, Any],
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
) -> dict[str, Any]:
    from python.srv.services.abr_v41 import plan_restore_session

    job_id = str(job["id"])
    session_id = str(task["id"])
    try:
        planned, _point, summary = plan_restore_session(restore_payload(settings, str(point["id"])))
    except ApiError as exc:
        raise ReplicationValidationError(exc.message) from exc
    parameters = dict(planned["parameters"])
    parameters["replica_marker"] = replica_marker(job_id, session_id)
    parameters["disk_name_suffix"] = f"-{replica_tag(job_id)}-a"
    restore_task = {**planned, "id": session_id, "job_id": job_id, "parameters": parameters}
    report_progress(
        progress_callback,
        5,
        f"Creating replica {settings['replica_name']} from restore point {point['id']} ({point.get('created_at')}, "
        f"{point.get('execution_type')}): {summary}",
        stage="create",
    )

    def forward(update: dict[str, Any]) -> None:
        created = str(update.get("created_vm_id") or "")
        if created:
            save_replica(job_id, {"vm_id": created, "vm_name": settings["replica_name"], "created_by_session": session_id})
        if progress_callback is not None:
            progress_callback(update)

    _touch(progress_callback)
    result = build_hypervisor_plugin(_target(settings)).restore(restore_task, progress_callback=forward)
    vm_id = str(result.get("target_vm_id") or replica_of(load_job(job_id)).get("vm_id") or "")
    state = platform.find_replica(node, vm_id) if vm_id else None
    if state is None:
        raise ReplicationError(f"The replica {settings['replica_name']} was created but cannot be found on the target")
    save_replica(
        job_id,
        {
            "vm_id": state["vm_id"],
            "vm_name": state["name"],
            "host": state["host"],
            "point_id": str(point["id"]),
            "point_created_at": str(point.get("created_at") or ""),
            "last_success_at": utc_now(),
            "created_by_session": session_id,
            "disks": replica_disks(state, manifest_disks(point), result.get("disks") or []),
            "fingerprint": state["fingerprint"],
            "start_marker": state.get("start_marker"),
        },
    )
    return {"restore": result, "state": state}


def _touch(progress_callback: ProgressCallback) -> None:
    if progress_callback is not None:
        progress_callback({"replica_touched": True, "log_entry": False})


def disk_key(disk: dict[str, Any], position: int) -> str:
    from python.srv.plugins.vcenter.vcenter_restore import _PLACEHOLDER_DISK_UUIDS

    uuid = str(disk.get("uuid") or "").strip()
    if uuid and uuid.lower() not in _PLACEHOLDER_DISK_UUIDS:
        return f"uuid:{uuid.lower()}"
    for field in ("datastore_path", "slot"):
        value = str(disk.get(field) or "").strip()
        if value:
            return f"{field}:{value.lower()}"
    return f"index:{position}"


def disk_capacity(disk: dict[str, Any]) -> int:
    from python.srv.plugins.vcenter.vcenter_restore import _disk_capacity_gb

    recorded = int(disk.get("capacity_bytes") or 0)
    return recorded or round(_disk_capacity_gb(disk) * 1024**3)


def manifest_disks(point: dict[str, Any]) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import _load_restore_manifest_record

    manifest = _load_restore_manifest_record(point)
    disks = manifest.get("disks") if isinstance(manifest, dict) else None
    if not isinstance(disks, list) or not disks:
        raise ReplicationError(f"The manifest of restore point {point.get('id')} lists no disks")
    return [disk for disk in disks if isinstance(disk, dict)]


def replica_disks(
    state: dict[str, Any], source_disks: list[dict[str, Any]], restored: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_path = {disk["path"]: disk for disk in state["disks"]}
    by_slot = {disk["slot"]: disk for disk in state["disks"]}
    entries = []
    for position, source in enumerate(source_disks, start=1):
        written = next(
            (
                item
                for item in restored
                if isinstance(item, dict)
                and (int(item.get("index") or 0) == position or (source.get("slot") and item.get("slot") == source.get("slot")))
            ),
            {},
        )
        path = str(written.get("restored_datastore_path") or written.get("path") or "")
        current = by_path.get(path) or by_slot.get(str(source.get("slot") or "")) or (
            state["disks"][position - 1] if len(state["disks"]) >= position else None
        )
        if current is None:
            raise ReplicationError(f"Disk {position} of the replica {state['name']} cannot be found after it was written")
        entries.append(
            {
                "key": disk_key(source, position),
                "slot": current["slot"],
                "path": current["path"],
                "size_bytes": current["size_bytes"],
                "source_capacity_bytes": disk_capacity(source),
            }
        )
    return entries


def _snapshot_name(job_id: str, session_id: str) -> str:
    return f"{replica_tag(job_id)}-{session_id[:8]}"


def _previous_session(job_id: str, session_id: str) -> dict[str, Any] | None:
    earlier = [
        task
        for task in catalog_records(TASKS)
        if isinstance(task, dict)
        and str(task.get("job_id") or "") == job_id
        and str(task.get("id") or "") != session_id
        and is_replication_task(task)
    ]
    earlier.sort(key=lambda task: str(task.get("created_at") or ""))
    return earlier[-1] if earlier else None


def _changed(job_id: str, state: dict[str, Any], replica: dict[str, Any], node: Any, platform: Any, *, trust_fingerprint: bool) -> str | None:
    if state["powered_on"]:
        return f"{state['name']} is powered on; a replica in use is never updated"
    foreign = [name for name in state["snapshots"] if not is_own_snapshot(name, job_id)]
    if foreign:
        return f"{state['name']} has snapshot(s) or checkpoint(s) {', '.join(foreign)} that this job did not take"
    expected = {disk["slot"]: int(disk.get("size_bytes") or 0) for disk in replica.get("disks") or []}
    current = {disk["slot"]: int(disk.get("size_bytes") or 0) for disk in state["disks"]}
    if set(expected) != set(current):
        return f"the disks of {state['name']} ({', '.join(sorted(current)) or 'none'}) are not the ones the last run left ({', '.join(sorted(expected)) or 'none'})"
    resized = [slot for slot, size in current.items() if expected[slot] and size != expected[slot]]
    if resized:
        return f"disk(s) {', '.join(resized)} of {state['name']} changed size since the last run"
    current_start = state.get("start_marker")
    if "start_marker" in replica and current_start is not None:
        recorded_start = replica["start_marker"]
        if recorded_start is None or int(current_start) > int(recorded_start):
            return f"{state['name']} was powered on since the last run"
    probe = getattr(platform, "powered_on_since", None)
    if probe is not None and probe(node, state, str(replica.get("last_success_at") or "")):
        return f"{state['name']} was powered on since the last run"
    if not trust_fingerprint:
        return None
    if replica.get("fingerprint") and state["fingerprint"] != replica["fingerprint"]:
        return f"the configuration or the disks of {state['name']} changed since the last run"
    return None


def _plan(
    target: dict[str, Any], replica: dict[str, Any], repository: dict[str, Any], encryption_key: str
) -> list[dict[str, Any]] | None:
    from python.srv.plugins.vcenter.vcenter_backup import VCenterBackupError

    known = {disk["key"]: disk for disk in replica.get("disks") or [] if disk.get("key")}
    source = manifest_disks(target)
    keys = [disk_key(disk, position) for position, disk in enumerate(source, start=1)]
    if sorted(keys) != sorted(known):
        return None
    plan = []
    for position, (disk, key) in enumerate(zip(source, keys, strict=True), start=1):
        mine = known[key]
        if mine.get("source_capacity_bytes") and disk_capacity(disk) != int(mine["source_capacity_bytes"]):
            return None
        try:
            tail = _layers_since(
                target, disk, int(disk.get("index") or position), str(replica.get("point_id") or ""), repository, encryption_key
            )
        except VCenterBackupError:
            return None
        if tail is None or any(layer.get("store_mode") != "incremental" for layer in tail):
            return None
        plan.append({"key": key, "slot": mine["slot"], "layers": tail, "source": disk, "index": int(disk.get("index") or position)})
    return plan


def layer_bytes(layers: list[dict[str, Any]], disk_index: int) -> int:
    from python.srv.plugins.vcenter.vcenter_restore import _backup_point_by_id

    total = 0
    for layer in layers:
        point = _backup_point_by_id(str(layer.get("point_id") or "")) or {}
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        disks = artifact.get("disks")
        for entry in disks if isinstance(disks, list) else []:
            if isinstance(entry, dict) and int(entry.get("index") or 0) == disk_index:
                total += int(entry.get("logical_bytes") or entry.get("changed_bytes") or 0)
                break
    return total


def _layers_since(
    point: dict[str, Any],
    disk: dict[str, Any],
    disk_index: int,
    replica_point_id: str,
    repository: dict[str, Any],
    encryption_key: str,
    seen: frozenset[str] = frozenset(),
) -> list[dict[str, Any]] | None:
    from python.srv.plugins.vcenter.vcenter_restore import (
        _backup_point_by_id,
        _resolve_abr_restore_layers,
    )

    layers = _resolve_abr_restore_layers(selected_point=point, selected_disk=disk, disk_index=disk_index, repository=repository)
    ids = [str(layer.get("point_id") or "") for layer in layers]
    if replica_point_id in ids:
        return layers[ids.index(replica_point_id) + 1 :]
    if not layers or ids[0] in seen:
        return None
    merged_into = _synthetic_source(ids[0], disk, disk_index, repository, encryption_key)
    if merged_into is None:
        return None
    earlier = _layers_since(merged_into, disk, disk_index, replica_point_id, repository, encryption_key, seen | {ids[0]})
    if earlier is None or not all(_available(_backup_point_by_id(str(layer.get("point_id") or ""))) for layer in earlier):
        return None
    return earlier + layers[1:]


def _synthetic_source(
    synthetic_id: str, disk: dict[str, Any], disk_index: int, repository: dict[str, Any], encryption_key: str
) -> dict[str, Any] | None:
    from python.srv.plugins.vcenter.vcenter_restore import (
        _backup_point_by_id,
        _load_manifest_for_point,
        _resolve_chain_disk_entry,
    )
    from python.srv.services.abr_v41 import _resolve_restore_encryption_key

    synthetic = _backup_point_by_id(synthetic_id)
    if synthetic is None:
        return None
    manifest = _load_manifest_for_point(synthetic)
    merged = [str(item) for item in manifest.get("merged_restore_point_ids") or [] if item] if manifest.get("synthetic_full") else []
    source = _backup_point_by_id(merged[-1]) if merged else None
    if not _available(source):
        return None
    if str(source.get("repository_id") or "") != str(repository.get("id") or ""):
        return None
    if str(_resolve_restore_encryption_key(source) or "") != encryption_key:
        return None
    merged_disk = _resolve_chain_disk_entry(manifest, selected_disk=disk, disk_index=disk_index) or {}
    source_disk = _resolve_chain_disk_entry(_load_manifest_for_point(source), selected_disk=disk, disk_index=disk_index) or {}
    change_id = _change_id(merged_disk)
    if not change_id or change_id != _change_id(source_disk):
        return None
    return source


def _change_id(disk: dict[str, Any]) -> str:
    return str(disk.get("change_id") or disk.get("next_change_id") or "").strip()


def _available(point: dict[str, Any] | None) -> bool:
    return isinstance(point, dict) and str(point.get("status") or "available").lower() in AVAILABLE_STATUSES


def _repository_and_key(point: dict[str, Any]) -> tuple[dict[str, Any], str]:
    from python.srv.core.credential_encryption import is_encrypted
    from python.srv.core.mongo_db_wrapper import REPOSITORIES
    from python.srv.services.abr_v41 import (
        _ensure_share_mounted,
        _point_is_encrypted,
        _resolve_restore_encryption_key,
    )

    repository = REPOSITORIES.get(str(point.get("repository_id") or ""))
    if not isinstance(repository, dict):
        raise ReplicationValidationError(f"The repository of restore point {point.get('id')} is no longer registered")
    try:
        _ensure_share_mounted(repository)
    except ApiError as exc:
        raise ReplicationError(exc.message) from exc
    key = str(_resolve_restore_encryption_key(point) or "")
    if _point_is_encrypted(point) and (not key or is_encrypted(key)):
        raise ReplicationValidationError(
            f"Restore point {point.get('id')} is encrypted and its key cannot be read, so it cannot be replicated"
        )
    return repository, key


def _update_mode_one(
    task: dict[str, Any],
    job_id: str,
    state: dict[str, Any],
    plan: list[dict[str, Any]],
    repository: dict[str, Any],
    encryption_key: str,
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
) -> dict[str, Any]:
    from python.srv.core.subprocess_util import ProcessCancelled
    from python.srv.core.task_executor import TaskStopRequested, cancel_requested
    from python.srv.plugins.vcenter.vcenter_backup import _disk_tool_progress_reporter

    session_id = str(task["id"])
    name = _snapshot_name(job_id, session_id)
    _touch(progress_callback)
    report_progress(progress_callback, 10, f"Taking snapshot {name} of {state['name']} before the update", stage="snapshot")
    snapped = platform.create_snapshot(node, state, name, wait_seconds())
    current = {disk["slot"]: disk for disk in snapped["disks"]}
    written = 0
    try:
        for index, item in enumerate(plan, start=1):
            if not item["layers"]:
                continue
            disk = current[item["slot"]]
            base = 15 + int((index - 1) * 70 / len(plan))
            span = max(1, int(70 / len(plan)))
            report_progress(
                progress_callback,
                base,
                f"Updating disk {index}/{len(plan)} ({item['slot']}) of {state['name']} with "
                f"{len(item['layers'])} incremental(s): {', '.join(str(layer.get('point_id')) for layer in item['layers'])}",
                stage="transfer",
            )
            report = _disk_tool_progress_reporter(
                progress_callback,
                base_progress=base,
                progress_span=span,
                message_prefix=f"Updating disk {index}/{len(plan)}",
                worker_mode=WORKER_MODE,
            )
            outcome = platform.write_increments(
                node,
                snapped,
                disk,
                item["layers"],
                repository,
                encryption_key,
                report=report,
                should_cancel=lambda: cancel_requested(session_id),
                timeout_seconds=None,
            )
            written += int(outcome.get("written_bytes") or 0) or layer_bytes(item["layers"], int(item.get("index") or index))
            if cancel_requested(session_id):
                raise TaskStopRequested("Session stop requested")
    except BaseException as exc:
        stopped = isinstance(exc, (TaskStopRequested, ProcessCancelled))
        report_quietly(
            progress_callback,
            90,
            f"{'Stopped' if stopped else 'The update failed'}: reverting {state['name']} to snapshot {name}, so the "
            "replica stays at the restore point it was at",
            stage="revert",
        )
        try:
            platform.revert_snapshot(node, snapped, name, wait_seconds())
        except Exception as revert_error:
            logger.exception("Reverting %s to %s failed", state["name"], name)
            raise ReplicationError(
                f"{exc}; reverting {state['name']} to snapshot {name} failed too ({revert_error}). The next run reverts it "
                "before anything else"
            ) from exc
        if isinstance(exc, ProcessCancelled):
            raise TaskStopRequested("Session stop requested") from exc
        raise
    report_progress(progress_callback, 92, f"Removing snapshot {name}: {state['name']} keeps the update", stage="consolidate")
    platform.remove_snapshot(node, snapped, name, wait_seconds())
    applied = [str(layer.get("point_id")) for item in plan for layer in item["layers"]]
    return {"written_bytes": written, "applied": list(dict.fromkeys(applied))}


STAGING_NAME_LIMIT = 80


def staging_name(settings: dict[str, Any], session_id: str) -> str:
    suffix = f"-staging-{session_id[:8]}"
    return f"{settings['replica_name'][: STAGING_NAME_LIMIT - len(suffix)]}{suffix}"


def _next_suffix(job_id: str, state: dict[str, Any]) -> str:
    current_a = any(f"-{replica_tag(job_id)}-a" in disk["path"] for disk in state["disks"])
    return "b" if current_a else "a"


def _swap_changes(
    replica: dict[str, Any], state: dict[str, Any], staging: dict[str, Any], new_disks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    known = {disk["key"]: disk for disk in replica.get("disks") or [] if disk.get("key")}
    current = {disk["slot"]: disk["path"] for disk in state["disks"]}
    changes = []
    for disk in new_disks:
        old = known.get(disk["key"])
        slot = old["slot"] if old else disk["slot"]
        changes.append(
            {
                "key": disk["key"],
                "slot": slot,
                "staging_slot": disk["slot"],
                "old_path": current.get(slot, old["path"] if old else ""),
                "new_path": disk["path"],
                "source_capacity_bytes": disk["source_capacity_bytes"],
            }
        )
    new_keys = {disk["key"] for disk in new_disks}
    for key, old in known.items():
        if key not in new_keys:
            changes.append(
                {
                    "key": key,
                    "slot": old["slot"],
                    "staging_slot": "",
                    "old_path": current.get(old["slot"], old["path"]),
                    "new_path": "",
                    "source_capacity_bytes": 0,
                }
            )
    return changes


def _record_swap_plan(progress_callback: ProgressCallback, plan: dict[str, Any] | None) -> None:
    from python.srv.core.task_executor import TaskStopRequested

    if progress_callback is None:
        return
    try:
        progress_callback({"swap_plan": plan or {}, "log_entry": False})
    except TaskStopRequested:
        if plan:
            raise


def _update_mode_two(
    task: dict[str, Any],
    job: dict[str, Any],
    settings: dict[str, Any],
    state: dict[str, Any],
    target: dict[str, Any],
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
) -> dict[str, Any]:
    from python.srv.core.task_executor import TaskStopRequested
    from python.srv.services.abr_v41 import plan_restore_session

    job_id = str(job["id"])
    session_id = str(task["id"])
    replica = replica_of(job)
    name = staging_name(settings, session_id)
    if any(disk_folder(disk["path"]).lower().endswith(name.lower()) for disk in state["disks"]):
        raise ReplicationError(
            f"The temporary VM name {name} is the folder the disks of {state['name']} live in; the disks are not rebuilt"
        )
    host, datastore = platform.rebuild_location(state, settings)
    staging_settings = dict(settings, replica_name=name, target_host=host, target_datastore=datastore)
    _touch(progress_callback)
    report_progress(
        progress_callback,
        6,
        f"Restore point {target['id']} cannot be applied to {state['name']} as incrementals (a new full backup, "
        f"changed disks, or restore points between it and the replica that are no longer kept): rebuilding its {len(state['disks'])} disk(s) next to the current ones through a temporary "
        f"VM {name} on {host}/{datastore}, where the replica is now; the current disks stay as they are until the new "
        "ones are complete",
        stage="rebuild",
    )
    try:
        planned, _point, _summary = plan_restore_session(restore_payload(staging_settings, str(target["id"])))
    except ApiError as exc:
        raise ReplicationError(f"The replica's disks cannot be rebuilt next to the current ones: {exc.message}") from exc
    parameters = dict(planned["parameters"])
    parameters["replica_marker"] = replica_marker(job_id, session_id)
    parameters["disk_name_suffix"] = f"-{replica_tag(job_id)}-{_next_suffix(job_id, state)}"
    restore_task = {**planned, "id": session_id, "job_id": job_id, "parameters": parameters}
    created: dict[str, str] = {}

    def forward(update: dict[str, Any]) -> None:
        vm_id = str(update.get("created_vm_id") or "")
        if vm_id:
            created["vm_id"] = vm_id
            if progress_callback is not None:
                progress_callback({"staging_vm_id": vm_id, "log_entry": False})
        if progress_callback is not None:
            progress_callback(update)

    try:
        result = build_hypervisor_plugin(_target(settings)).restore(restore_task, progress_callback=forward)
        staging_id = str(result.get("target_vm_id") or created.get("vm_id") or "")
        staging = platform.find_replica(node, staging_id) if staging_id else None
        if staging is None:
            raise ReplicationError(f"The temporary VM {name} was restored but cannot be found on the target")
    except BaseException:
        leftover = platform.find_replica(node, created["vm_id"]) if created.get("vm_id") else None
        if leftover is not None:
            platform.remove_staging(node, leftover)
        raise

    new_disks = replica_disks(staging, manifest_disks(target), result.get("disks") or [])
    changes = _swap_changes(replica, state, staging, new_disks)
    plan = {
        "replica_vm_id": state["vm_id"],
        "staging_vm_id": staging["vm_id"],
        "staging_name": staging["name"],
        "staging_configuration_location": staging.get("configuration_location", ""),
        "host": staging["host"],
        "point_id": str(target["id"]),
        "point_created_at": str(target.get("created_at") or ""),
        "changes": changes,
    }
    _record_swap_plan(progress_callback, plan)
    try:
        report_quietly(progress_callback, 88, f"Switching {state['name']} to its rebuilt disks", stage="swap")
        platform.release_staging_disks(node, staging)
        platform.swap_disks(node, state, staging, changes)
    except BaseException as exc:
        report_quietly(
            progress_callback,
            90,
            f"{'Stopped' if isinstance(exc, TaskStopRequested) else 'Switching the disks failed'}: {state['name']} keeps "
            "its current disks; removing the rebuilt ones",
            stage="revert",
        )
        undo_swap(node, platform, state, staging, plan)
        _record_swap_plan(progress_callback, None)
        raise
    finish_swap(node, platform, plan, progress_callback)
    finished = platform.find_replica(node, state["vm_id"]) or state
    save_replica(job_id, metadata_after_swap(replica, plan, finished))
    return {
        "mode": "rebuild",
        "state": finished,
        "point": target,
        "processed_bytes": int(result.get("processed_bytes") or 0),
        "written_bytes": int(result.get("transferred_bytes") or 0),
        "warnings": list(result.get("warnings") or []),
        "applied": [str(target["id"])],
    }


def _update_replica(
    task: dict[str, Any],
    job: dict[str, Any],
    settings: dict[str, Any],
    points: list[dict[str, Any]],
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
    *,
    retrying: bool = False,
) -> dict[str, Any]:
    job_id = str(job["id"])
    session_id = str(task["id"])
    replica = replica_of(job)
    state = platform.find_replica(node, replica["vm_id"])
    if state is None:
        raise ReplicationError(
            f"Replica {replica.get('vm_name') or replica['vm_id']} is not on {settings['target_inventory_name']} any "
            "more. Replica not found. Create a new Replication Job"
        )
    if replica.get("host") and state["host"] and state["host"].lower() != str(replica["host"]).lower():
        report_progress(
            progress_callback,
            2,
            f"Replica {state['name']} moved from host {replica['host']} to {state['host']}; it is updated where it is now",
            log_level="warn",
        )
    state, swapped = finish_interrupted_swap(job, session_id, state, node, platform, progress_callback, retrying=retrying)
    if swapped:
        replica = replica_of(load_job(job_id))
    state, found = clean_update_leftovers(job_id, state, node, platform, progress_callback)
    found = found or swapped
    previous = _previous_session(job_id, session_id)
    interrupted = retrying or bool(
        previous
        and (previous.get("result") or {}).get("replica_touched")
        and str(previous.get("status") or "") != "completed"
    )
    reason = _changed(job_id, state, replica, node, platform, trust_fingerprint=not (found or interrupted))
    if reason:
        raise ReplicationValidationError(f"Replica was changed: {reason}. Create a new Replication Job")

    target = points[-1]
    if str(target["id"]) == str(replica.get("point_id") or ""):
        return {"mode": "up_to_date", "state": state, "point": target, "written_bytes": 0, "applied": []}
    repository, encryption_key = _repository_and_key(target)
    if str((task.get("parameters") or {}).get("vsphere_transport") or "") == SOAP:
        report_progress(
            progress_callback,
            5,
            f"{settings.get('target_inventory_name') or 'The target inventory'} reads and writes disks over the vSphere SOAP API, "
            "which cannot write into a disk that has a snapshot, so the replica is rebuilt from the restore point instead "
            "of updated with its incrementals",
            stage="rebuild",
        )
        return _update_mode_two(task, job, settings, state, target, node, platform, progress_callback)
    plan = _plan(target, replica, repository, encryption_key)
    if plan is None:
        return _update_mode_two(task, job, settings, state, target, node, platform, progress_callback)
    if not any(item["layers"] for item in plan):
        save_replica(
            job_id,
            {
                **replica,
                "point_id": str(target["id"]),
                "point_created_at": str(target.get("created_at") or ""),
                "last_success_at": utc_now(),
            },
        )
        return {"mode": "up_to_date", "state": state, "point": target, "written_bytes": 0, "applied": []}
    outcome = _update_mode_one(task, job_id, state, plan, repository, encryption_key, node, platform, progress_callback)
    finished = platform.find_replica(node, state["vm_id"]) or state
    disks = {disk["slot"]: disk for disk in finished["disks"]}
    save_replica(
        job_id,
        {
            **replica,
            "vm_name": finished["name"],
            "host": finished["host"],
            "point_id": str(target["id"]),
            "point_created_at": str(target.get("created_at") or ""),
            "last_success_at": utc_now(),
            "fingerprint": finished["fingerprint"],
            "start_marker": finished.get("start_marker"),
            "disks": [
                {**disk, "path": disks.get(disk["slot"], {}).get("path", disk["path"]), "size_bytes": disks.get(disk["slot"], {}).get("size_bytes", disk["size_bytes"])}
                for disk in replica.get("disks") or []
            ],
        },
    )
    return {"mode": "incremental", "state": finished, "point": target, **outcome}


def run_replication(task: dict[str, Any], progress_callback: ProgressCallback = None) -> dict[str, Any]:
    from python.srv.core.task_executor import cancel_requested

    started = time.time()
    retries, interval = retry_settings()
    attempt = 0
    while True:
        attempt += 1
        try:
            settings, outcome = _attempt(task, progress_callback, retrying=attempt > 1)
            break
        except Exception as exc:
            if attempt > retries or not is_temporary(exc) or cancel_requested(str(task.get("id") or "")):
                raise
            report_progress(
                progress_callback,
                1,
                f"Attempt {attempt}/{retries + 1} failed with a temporary error: {exc}. "
                + (f"Retrying in {interval} minute(s)" if interval else "Retrying now")
                + "; the next attempt first cleans up whatever this one left",
                stage="retry",
                log_level="warn",
            )
            _wait_before_retry(str(task.get("id") or ""), interval, progress_callback)
    result = _result(task, settings, outcome, time.time() - started)
    if attempt > 1:
        result["message"] = f"{result['message']} (attempt {attempt}/{retries + 1})"
    result["attempts"] = attempt
    return result


def retry_settings() -> tuple[int, int]:
    from python.srv.core.server_defaults import env_int

    return env_int("ALTASTOR_REPLICA_RETRY_COUNT", minimum=0), env_int("ALTASTOR_REPLICA_RETRY_INTERVAL_MINUTES", minimum=0)


def _wait_before_retry(session_id: str, minutes: int, progress_callback: ProgressCallback) -> None:
    from python.srv.core.task_executor import TaskStopRequested, cancel_requested

    deadline = time.monotonic() + minutes * 60
    next_beat = time.monotonic() + 60
    while time.monotonic() < deadline:
        if cancel_requested(session_id):
            raise TaskStopRequested("Session stop requested")
        if progress_callback is not None and time.monotonic() >= next_beat:
            progress_callback({"stage": "retry", "log_entry": False, "updated_at": utc_now()})
            next_beat = time.monotonic() + 60
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    if cancel_requested(session_id):
        raise TaskStopRequested("Session stop requested")


TEMPORARY_MESSAGE = re.compile(
    r"ended unexpectedly|cannot be reached|refused connection|connection (?:was )?(?:reset|lost|refused|aborted|closed|timed out)"
    r"|NBD_ERR_NETWORK|timed out|could not connect|failed to connect|network is unreachable|no route to host|broken pipe"
    r"|temporarily unavailable|service unavailable|slow ?down|request ?timeout",
    re.IGNORECASE,
)


def is_temporary(exc: BaseException) -> bool:
    from pypsrp.exceptions import WinRMTransportError
    from pyVmomi import vim, vmodl
    from requests import ConnectionError as RequestsConnectionError
    from requests import Timeout as RequestsTimeout

    from python.srv.core.subprocess_util import ProcessCancelled
    from python.srv.core.task_executor import TaskStopRequested
    from python.srv.plugins.hyperv.hyperv import HyperVUnreachableError

    network = (
        ConnectionError,
        TimeoutError,
        RequestsConnectionError,
        RequestsTimeout,
        WinRMTransportError,
        HyperVUnreachableError,
        vim.fault.HostConnectFault,
        vmodl.fault.HostCommunication,
    )
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ReplicationValidationError, TaskStopRequested, ProcessCancelled)):
            return False
        if isinstance(current, network) or TEMPORARY_MESSAGE.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _attempt(
    task: dict[str, Any], progress_callback: ProgressCallback, *, retrying: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    job_id = str(task.get("job_id") or "")
    job = load_job(job_id)
    settings = with_current_source_names(settings_of(job))
    inventory = _target(settings)
    platform = _platform(inventory)
    if str(inventory.get("kind") or "") == "vcenter":
        problem = transport_problem(str((task.get("parameters") or {}).get("vsphere_transport") or VDDK))
        if problem:
            raise ReplicationError(problem)
    report_progress(progress_callback, 1, f"Connecting to the target environment {settings['target_inventory_name']}", stage="prepare")
    node = _connect(inventory)
    try:
        with node.session_keepalive() if hasattr(node, "session_keepalive") else nullcontext():
            clean_first_run_leftovers(job, settings, node, platform, progress_callback)
            job = load_job(job_id)
            points = _source_check(job, settings, progress_callback)
            if replica_of(job).get("vm_id"):
                outcome = _update_replica(task, job, settings, points, node, platform, progress_callback, retrying=retrying)
            else:
                point = _chosen_point(points, settings)
                try:
                    created = _create_replica(task, job, settings, point, node, platform, progress_callback)
                except BaseException:
                    forget_unfinished_replica(job_id, node, platform)
                    raise
                restore = created["restore"]
                outcome = {
                    "mode": "create",
                    "state": created["state"],
                    "point": point,
                    "processed_bytes": int(restore.get("processed_bytes") or 0),
                    "written_bytes": int(restore.get("transferred_bytes") or 0),
                    "warnings": list(restore.get("warnings") or []),
                }
            outcome = _settle(job_id, settings, outcome, node, platform, progress_callback)
    finally:
        try:
            node.disconnect()
        except Exception:
            logger.debug("Disconnecting from %s failed", inventory.get("name"), exc_info=True)
    return settings, outcome


def _settle(
    job_id: str, settings: dict[str, Any], outcome: dict[str, Any], node: Any, platform: Any, progress_callback: ProgressCallback
) -> dict[str, Any]:
    settled = settle_replica(
        job_id, settings, outcome["state"], outcome["point"], node, platform, progress_callback, creating=outcome["mode"] == "create"
    )
    state = settled["state"]
    save_replica(
        job_id,
        {
            **replica_of(load_job(job_id)),
            "vm_name": state["name"],
            "fingerprint": state["fingerprint"],
            "start_marker": state.get("start_marker"),
            "adapters": settled["adapters"],
            "config_warnings": settled["config_warnings"],
        },
        renamed_to=settled["renamed_to"],
    )
    return {**outcome, "state": state, "touched": settled["touched"]}


def _result(task: dict[str, Any], settings: dict[str, Any], outcome: dict[str, Any], duration: float) -> dict[str, Any]:
    state = outcome["state"]
    point = outcome["point"]
    mode = outcome["mode"]
    written = int(outcome.get("written_bytes") or 0)
    where = f"{settings['target_inventory_name']}/{state['host']}"
    source = f"{settings['source_vm_name']} (backup job {settings['source_job_name']})"
    if mode == "create":
        message = (
            f"Created replica {state['name']} on {where} from restore point {point['id']} ({point.get('created_at')}) "
            f"of {source}: {len(state['disks'])} disk(s), {_size(outcome.get('processed_bytes'))} read, "
            f"{_size(written)} written, in {duration:.0f}s"
        )
    elif mode == "rebuild":
        message = (
            f"Rebuilt the disks of replica {state['name']} on {where} from restore point {point['id']} "
            f"({point.get('created_at')}) of {source}: {len(state['disks'])} disk(s), {_size(written)} written, in "
            f"{duration:.0f}s"
        )
    elif mode == "up_to_date":
        message = (
            f"Replica is already up to date: {state['name']} on {where} is at restore point {point['id']} "
            f"({point.get('created_at')}), the newest of {source}"
        )
    else:
        applied = outcome.get("applied") or []
        message = (
            f"Updated replica {state['name']} on {where} to restore point {point['id']} ({point.get('created_at')}) of "
            f"{source} with {len(applied)} incremental(s): {_size(written)} written, in {duration:.0f}s"
        )
    logger.info("Session %s: %s", task.get("id"), message)
    return {
        "operation": "replication",
        "worker_mode": WORKER_MODE,
        "mode": mode,
        "message": message,
        "source_job_id": settings["source_job_id"],
        "source_job_name": settings["source_job_name"],
        "source_vm_id": settings["source_vm_id"],
        "source_vm_name": settings["source_vm_name"],
        "restore_point_id": str(point["id"]),
        "restore_point_created_at": str(point.get("created_at") or ""),
        "applied_point_ids": list(outcome.get("applied") or []),
        "target_inventory_id": settings["target_inventory_id"],
        "target_inventory_name": settings["target_inventory_name"],
        "target_host_name": state["host"],
        "target_datastore_name": settings["target_datastore_name"] or settings["target_datastore"],
        "replica_vm_id": state["vm_id"],
        "replica_vm_name": state["name"],
        "disk_count": len(state["disks"]),
        "processed_bytes": int(outcome.get("processed_bytes") or 0),
        "transferred_bytes": written,
        "duration_seconds": round(duration, 3),
        "replica_touched": mode != "up_to_date" or bool(outcome.get("touched")),
        "warnings": list(outcome.get("warnings") or []),
        "finished_at": utc_now(),
    }


def delete_replica(job: dict[str, Any]) -> dict[str, Any]:
    from python.srv.services.api_errors import api_error

    settings = settings_of(job)
    replica = replica_of(job)
    if not replica.get("vm_id"):
        return {"replica_deleted": False, "replica_vm_name": "", "removed_vms": [], "files_left": []}
    inventory = HYPERVISORS.get(settings["target_inventory_id"])
    if not isinstance(inventory, dict):
        raise api_error(
            409,
            "replica_not_deleted",
            f"Target environment {settings['target_inventory_name']} is no longer registered, so replica "
            f"{replica.get('vm_name')} cannot be deleted. Delete the job without --delete-replica and remove the VM by hand",
        )
    platform = _platform(inventory)
    try:
        node = _connect(inventory)
    except ReplicationError as exc:
        raise api_error(
            503,
            "replica_not_deleted",
            f"{exc}, so replica {replica.get('vm_name')} cannot be deleted. Try again, or delete the job without "
            "--delete-replica to keep the VM",
        ) from exc
    try:
        return remove_replica_vms(job, node, platform)
    except ReplicationError as exc:
        raise api_error(409, "replica_not_deleted", str(exc), vm_name=replica.get("vm_name")) from exc
    except Exception as exc:
        logger.exception("Deleting replica %s failed", replica.get("vm_name"))
        raise api_error(
            502,
            "replica_not_deleted",
            f"Replica {replica.get('vm_name')} could not be deleted ({exc}); the job is kept. Try again, or delete the "
            "job without --delete-replica",
        ) from exc
    finally:
        try:
            node.disconnect()
        except Exception:
            logger.debug("Disconnecting from %s failed", inventory.get("name"), exc_info=True)
