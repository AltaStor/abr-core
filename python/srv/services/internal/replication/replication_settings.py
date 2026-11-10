# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from python.srv.core.mongo_db_wrapper import JOBS, RESOURCES_BY_HV, catalog_records
from python.srv.services.api_errors import api_error

REPLICATION_JOB_TYPE = "replication"
SETTING_KEYS = (
    "source_job",
    "source_job_id",
    "source_vm",
    "target_inventory",
    "target_inventory_id",
    "target_host",
    "target_datastore",
    "network_map",
    "replica_name",
    "run_after_source",
    "restore_point_id",
)
LOCKED_AFTER_REPLICA = (
    "source_job",
    "source_job_id",
    "source_vm",
    "target_inventory",
    "target_inventory_id",
    "target_host",
    "target_datastore",
    "network_map",
    "restore_point_id",
)
LOCKED_MESSAGE = "Cannot be changed after the replica is created. Create a new Replication Job instead."
BACKUP_ONLY_KEYS = (
    "repo",
    "repo_id",
    "inventory",
    "inventory_id",
    "vms",
    "retention_days",
    "retention_min_fulls",
    "verify_schedule",
    "verify_enabled",
    "verify_whole_chain_every",
    "gfs_weekly",
    "gfs_monthly",
    "gfs_yearly",
    "gfs_enabled",
    "task_parameters",
    "active_full_schedule",
    "active_full_days",
    "active_full_enabled",
    "synthetic_full_schedule",
    "synthetic_full_days",
    "synthetic_full_enabled",
)
AVAILABLE_STATUSES = frozenset({"available", "completed", "healthy"})


def is_replication_job(job: dict[str, Any] | None) -> bool:
    return isinstance(job, dict) and str(job.get("job_type") or "") == REPLICATION_JOB_TYPE


def with_current_source_names(settings: dict[str, Any]) -> dict[str, Any]:
    source = JOBS.get(settings["source_job_id"])
    if not isinstance(source, dict):
        return settings
    inventory = str(source.get("inventory_id") or source.get("hypervisor_id") or "")
    resource = RESOURCES_BY_HV.get(inventory, {}).get(("virtual_machine", settings["source_vm_id"])) or {}
    return {
        **settings,
        "source_job_name": str(source.get("name") or settings["source_job_name"]),
        "source_vm_name": str(resource.get("name") or settings["source_vm_name"]),
    }


def settings_of(job: dict[str, Any]) -> dict[str, Any]:
    stored = job.get("replication") if isinstance(job.get("replication"), dict) else {}
    return {
        "source_job_id": str(stored.get("source_job_id") or ""),
        "source_job_name": str(stored.get("source_job_name") or ""),
        "source_vm_id": str(stored.get("source_vm_id") or ""),
        "source_vm_name": str(stored.get("source_vm_name") or ""),
        "target_inventory_id": str(stored.get("target_inventory_id") or ""),
        "target_inventory_name": str(stored.get("target_inventory_name") or ""),
        "target_host": str(stored.get("target_host") or ""),
        "target_host_name": str(stored.get("target_host_name") or ""),
        "target_datastore": str(stored.get("target_datastore") or ""),
        "target_datastore_name": str(stored.get("target_datastore_name") or ""),
        "network_map": [str(item) for item in stored.get("network_map") or [] if str(item or "").strip()],
        "replica_name": str(stored.get("replica_name") or ""),
        "run_after_source": bool(stored.get("run_after_source")),
        "restore_point_id": str(stored.get("restore_point_id") or ""),
    }


def replica_of(job: dict[str, Any]) -> dict[str, Any]:
    stored = job.get("replica") if isinstance(job.get("replica"), dict) else {}
    return dict(stored)


def replica_exists(job: dict[str, Any]) -> bool:
    return bool(replica_of(job).get("vm_id"))


def keep_run_state(job: dict[str, Any]) -> None:
    stored = JOBS.get(str(job.get("id") or ""))
    if not isinstance(stored, dict):
        return
    fresh = replica_of(stored)
    pending = str(replica_of(job).get("pending_name") or "")
    if fresh.get("vm_id") and pending:
        fresh["pending_name"] = pending
    job["replica"] = fresh
    job["run_pending"] = bool(stored.get("run_pending"))
    job["pending_trigger"] = stored.get("pending_trigger") if job["run_pending"] else None


def normalize_replication_fields(item: dict[str, Any]) -> None:
    item["replication"] = settings_of(item)
    item["replica"] = replica_of(item)
    item["run_pending"] = bool(item.get("run_pending"))
    request = item.get("pending_trigger")
    item["pending_trigger"] = dict(request) if item["run_pending"] and isinstance(request, dict) else None


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        return [f"{source}={target}" for source, target in value.items()]
    return [str(part).strip() for part in value if str(part or "").strip()]


def _given(payload: dict[str, Any], key: str) -> bool:
    return key in payload and payload[key] is not None


def _refuse_foreign_keys(job: dict[str, Any], payload: dict[str, Any]) -> None:
    from python.srv.services.abr_v41 import COPY_JOB_FIELDS

    foreign = [key for key in (*BACKUP_ONLY_KEYS, *COPY_JOB_FIELDS) if _given(payload, key)]
    if foreign:
        raise api_error(
            422,
            "invalid_job",
            f"{', '.join(foreign)} do not apply to replication job {job.get('name') or ''}: it keeps no backups of "
            "its own, it only updates a replica from the restore points of its source backup job".replace("  ", " "),
            fields=foreign,
        )


def _refuse_locked_keys(job: dict[str, Any], payload: dict[str, Any]) -> None:
    if not replica_exists(job):
        return
    current = settings_of(job)
    locked = []
    for key in LOCKED_AFTER_REPLICA:
        if not _given(payload, key):
            continue
        if key == "network_map" and sorted(_tokens(payload[key])) == sorted(current["network_map"]):
            continue
        same = {str(current.get(key) or ""), str(current.get(key.replace("_id", "") + "_name") or "")} - {""}
        if key in ("source_job_id", "target_inventory_id", "target_host", "target_datastore", "restore_point_id") and (
            str(payload[key]) in same or (key == "restore_point_id" and not same and not str(payload[key]))
        ):
            continue
        locked.append(key)
    if locked:
        raise api_error(422, "replication_setting_locked", LOCKED_MESSAGE, fields=locked)


def apply_replication_settings(job: dict[str, Any], payload: dict[str, Any], *, creating: bool) -> bool:
    _refuse_foreign_keys(job, payload)
    _refuse_locked_keys(job, payload)
    settings = settings_of(job)
    changed = False

    if creating or _given(payload, "source_job") or _given(payload, "source_job_id"):
        settings["source_job_id"] = str(payload.get("source_job_id") or "")
        settings["source_job_name"] = str(payload.get("source_job") or "")
        settings["source_vm_id"] = ""
        settings["source_vm_name"] = str(payload.get("source_vm") or "")
        changed = True
    elif _given(payload, "source_vm"):
        settings["source_vm_id"] = ""
        settings["source_vm_name"] = str(payload["source_vm"])
        changed = True

    if creating or _given(payload, "target_inventory") or _given(payload, "target_inventory_id"):
        settings["target_inventory_id"] = str(payload.get("target_inventory_id") or "")
        settings["target_inventory_name"] = str(payload.get("target_inventory") or "")
        changed = True

    for key in ("target_host", "target_datastore"):
        if creating or _given(payload, key):
            settings[key] = str(payload.get(key) or "").strip()
            changed = True

    if creating or _given(payload, "network_map"):
        settings["network_map"] = _tokens(payload.get("network_map"))
        changed = True

    renamed = False
    if creating or _given(payload, "replica_name"):
        new_name = str(payload.get("replica_name") or "").strip()
        renamed = new_name != settings["replica_name"]
        settings["replica_name"] = new_name
        changed = True

    if creating or _given(payload, "run_after_source"):
        settings["run_after_source"] = bool(payload.get("run_after_source"))
        changed = True

    if creating or _given(payload, "restore_point_id"):
        settings["restore_point_id"] = str(payload.get("restore_point_id") or "").strip()
        changed = True

    if not changed:
        return False

    resolved = validate_replication_settings(job, settings, renamed=renamed)
    job["replication"] = resolved
    job["inventory_id"] = resolved["target_inventory_id"]
    job["inventory_name"] = resolved["target_inventory_name"]
    job["vm_ids"] = [resolved["source_vm_id"]]
    job["vm_names"] = [resolved["source_vm_name"]]
    job["repo_id"] = resolved.pop("repository_id", "")
    job["repo_name"] = resolved.pop("repository_name", "")
    if renamed and replica_exists(job):
        replica = replica_of(job)
        replica["pending_name"] = resolved["replica_name"]
        job["replica"] = replica
    return True


def source_points(source_job_id: str, vm_id: str) -> list[dict[str, Any]]:
    from python.srv.services.abr_v41 import normalized_backup_points

    return [
        point
        for point in normalized_backup_points()
        if str(point.get("job_id") or "") == source_job_id
        and str(point.get("vm_id") or "") == vm_id
        and str(point.get("status") or "available").lower() in AVAILABLE_STATUSES
    ]


def _require(value: str, message: str, field: str) -> None:
    if not value:
        raise api_error(422, "invalid_job", message, field=field)


def _source_job(settings: dict[str, Any]) -> dict[str, Any]:
    from python.srv.services.abr_v41 import resolve_job

    _require(
        settings["source_job_id"] or settings["source_job_name"],
        "A replication job needs a source backup job (--source-job)",
        "source_job",
    )
    source = resolve_job(name=settings["source_job_name"] or None, record_id=settings["source_job_id"] or None)
    kind = str(source.get("job_type") or "backup")
    if kind != "backup":
        label = "a backup copy job" if kind == "backup-copy" else "a replication job"
        raise api_error(
            422,
            "invalid_job",
            f"Job {source.get('name')} is {label}; the source of a replication job must be a backup job",
            source_job=source.get("name"),
        )
    return source


def _source_vm(source: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    from python.srv.services.abr_v41 import _job_vm_current_names

    vm_ids = [str(vm_id) for vm_id in source.get("vm_ids") or []]
    names = _job_vm_current_names(source) if len(vm_ids) == len(source.get("vm_names") or []) else list(source.get("vm_names") or [])
    if settings["source_vm_id"] and settings["source_vm_id"] in vm_ids:
        index = vm_ids.index(settings["source_vm_id"])
        return vm_ids[index], str(names[index] if index < len(names) else vm_ids[index])
    if len(vm_ids) != 1:
        raise api_error(
            422,
            "invalid_job",
            "Source backup job contains more than one VM. Only backup jobs with a single VM are supported."
            if len(vm_ids) > 1
            else f"Source backup job {source.get('name')} protects no VM",
            source_job=source.get("name"),
            vm_count=len(vm_ids),
        )
    vm_id, vm_name = vm_ids[0], str(names[0] if names else vm_ids[0])
    wanted = settings["source_vm_name"]
    if wanted and wanted.lower() not in (vm_id.lower(), vm_name.lower()):
        raise api_error(
            422,
            "invalid_job",
            f"VM {wanted} is not in source backup job {source.get('name')}, which backs up {vm_name}",
            source_job=source.get("name"),
            vm=wanted,
        )
    return vm_id, vm_name


def _start_point(source: dict[str, Any], vm_id: str, vm_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    points = source_points(str(source["id"]), vm_id)
    if not points:
        raise api_error(
            422,
            "invalid_job",
            f"Source backup job {source.get('name')} has no restore point of {vm_name} that can be replicated; "
            "run the backup first",
            source_job=source.get("name"),
            vm=vm_name,
        )
    wanted = settings["restore_point_id"]
    if not wanted:
        return points[-1]
    for point in points:
        if str(point.get("id") or "") == wanted:
            return point
    raise api_error(
        422,
        "invalid_job",
        f"Restore point {wanted} is not an available restore point of {vm_name} in source backup job {source.get('name')}",
        restore_point_id=wanted,
    )


def platform_mismatch(point: dict[str, Any], source: dict[str, Any], target_inventory: dict[str, Any]) -> str | None:
    from python.srv.core.mongo_db_wrapper import HYPERVISORS
    from python.srv.services.abr_v41 import _PLATFORM_LABELS, _point_inventory_type
    from python.srv.services.inventory_management import inventory_type_from_record

    source_type = _point_inventory_type(point)
    if not source_type:
        source_inventory = HYPERVISORS.get(str(source.get("inventory_id") or ""))
        source_type = inventory_type_from_record(source_inventory) if isinstance(source_inventory, dict) else ""
    target_type = inventory_type_from_record(target_inventory)
    if source_type and source_type == target_type:
        return None
    source_label = _PLATFORM_LABELS.get(source_type or "", source_type or "unknown")
    target_label = _PLATFORM_LABELS.get(target_type, str(target_inventory.get("type") or target_type or "unknown"))
    return f"Source VM platform ({source_label}) does not match target environment platform ({target_label})."


def _target_inventory(settings: dict[str, Any]) -> dict[str, Any]:
    from python.srv.services.abr_v41 import resolve_inventory

    _require(
        settings["target_inventory_id"] or settings["target_inventory_name"],
        "A replication job needs a target environment (--target-inventory)",
        "target_inventory",
    )
    return resolve_inventory(
        name=settings["target_inventory_name"] or None,
        record_id=settings["target_inventory_id"] or None,
        name_option="--target-inventory",
        id_option="--target-inventory-id",
    )


def restore_payload(settings: dict[str, Any], point_id: str) -> dict[str, Any]:
    return {
        "point_id": point_id,
        "restore_mode": "new_vm",
        "target_inventory_id": settings["target_inventory_id"],
        "target_host": settings["target_host"],
        "target_datastore": settings["target_datastore"],
        "target_vm_name": settings["replica_name"],
        "network_map": list(settings["network_map"]),
        "power_on": False,
        "connect_nics": True,
        "preserve_firmware": True,
    }


def validate_replication_settings(job: dict[str, Any], settings: dict[str, Any], *, renamed: bool = False) -> dict[str, Any]:
    from python.srv.services.abr_v41 import (
        MAX_NAME_LENGTH,
        _ensure_ascii_text,
        _ensure_hyperv_reaches_repository,
        _inventory_vm_name_exists,
        plan_restore_session,
        resolve_repository,
    )

    source = _source_job(settings)
    vm_id, vm_name = _source_vm(source, settings)
    target_inventory = _target_inventory(settings)
    _require(settings["target_host"], "A replication job needs a target host (--target-host)", "target_host")
    _require(settings["target_datastore"], "A replication job needs a target datastore (--datastore)", "target_datastore")
    _require(settings["replica_name"], "--replica-name is required", "replica_name")
    _ensure_ascii_text(settings["replica_name"], field="Replica name", code="invalid_job", max_length=MAX_NAME_LENGTH)

    point = _start_point(source, vm_id, vm_name, settings)
    mismatch = platform_mismatch(point, source, target_inventory)
    if mismatch:
        raise api_error(
            422,
            "replication_platform_mismatch",
            mismatch,
            source_job=source.get("name"),
            target_inventory=target_inventory.get("name"),
        )
    repository_id = str(point.get("repository_id") or "")
    if repository_id:
        _ensure_hyperv_reaches_repository(target_inventory["id"], resolve_repository(record_id=repository_id))

    resolved = dict(
        settings,
        source_job_id=str(source["id"]),
        source_job_name=str(source["name"]),
        source_vm_id=vm_id,
        source_vm_name=vm_name,
        target_inventory_id=str(target_inventory["id"]),
        target_inventory_name=str(target_inventory["name"]),
        repository_id=repository_id,
        repository_name=str(point.get("repository_name") or ""),
    )

    if replica_exists(job):
        if renamed and _inventory_vm_name_exists(resolved["target_inventory_id"], resolved["replica_name"]):
            raise api_error(
                409,
                "target_vm_exists",
                f"A VM named {resolved['replica_name']} already exists on {resolved['target_inventory_name']}",
                vm_name=resolved["replica_name"],
            )
        return resolved

    task, _point, _summary = plan_restore_session(restore_payload(resolved, str(point["id"])))
    parameters = task["parameters"]
    resolved["target_host_name"] = str(parameters.get("target_host_name") or resolved["target_host"])
    resolved["target_datastore_name"] = str(parameters.get("target_datastore_name") or resolved["target_datastore"])
    return resolved


def replication_view(job: dict[str, Any]) -> dict[str, Any]:
    settings = with_current_source_names(settings_of(job))
    replica = replica_of(job)
    return {
        "source_job": {"id": settings["source_job_id"] or None, "name": settings["source_job_name"] or None},
        "source_vm": {"id": settings["source_vm_id"] or None, "name": settings["source_vm_name"] or None},
        "target_inventory": {
            "id": settings["target_inventory_id"] or None,
            "name": settings["target_inventory_name"] or None,
        },
        "target_host": settings["target_host_name"] or settings["target_host"] or None,
        "target_datastore": settings["target_datastore_name"] or settings["target_datastore"] or None,
        "network_map": list(settings["network_map"]),
        "replica_name": settings["replica_name"] or None,
        "run_after_source": settings["run_after_source"],
        "start_point_id": settings["restore_point_id"] or None,
        "replica": {
            "vm_id": replica.get("vm_id") or None,
            "vm_name": replica.get("vm_name") or None,
            "point_id": replica.get("point_id") or None,
            "point_created_at": replica.get("point_created_at") or None,
            "last_success_at": replica.get("last_success_at") or None,
            "pending_name": replica.get("pending_name") or None,
        },
        "run_pending": bool(job.get("run_pending")),
        "pending_trigger": (
            {key: (job.get("pending_trigger") or {}).get(key) for key in ("trigger", "since")}
            if job.get("run_pending") and isinstance(job.get("pending_trigger"), dict)
            else None
        ),
    }


def replication_jobs_of_source(source_job_id: str) -> list[dict[str, Any]]:
    found = []
    for record in catalog_records(JOBS):
        if not is_replication_job(record):
            continue
        if settings_of(record)["source_job_id"] == source_job_id:
            found.append(record)
    return found


def ensure_source_job_free(source_job: dict[str, Any], *, removed_vm_ids: list[str] | None = None) -> None:
    for record in replication_jobs_of_source(str(source_job.get("id") or "")):
        settings = settings_of(record)
        if removed_vm_ids is None:
            raise api_error(
                409,
                "replication_source_in_use",
                f"Backup job {source_job.get('name')} is the source of replication job {record.get('name')}. "
                f"Delete replication job {record.get('name')} first.",
                replication_job=record.get("name"),
            )
        if settings["source_vm_id"] in removed_vm_ids:
            raise api_error(
                409,
                "replication_source_in_use",
                f"VM {settings['source_vm_name'] or settings['source_vm_id']} is replicated by replication job "
                f"{record.get('name')}. Delete replication job {record.get('name')} first.",
                replication_job=record.get("name"),
            )
