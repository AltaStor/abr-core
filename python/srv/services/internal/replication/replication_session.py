# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    CATALOG_LOCK,
    HYPERVISORS,
    JOBS,
    TASKS,
    catalog_records,
    persist_job,
    persist_task,
)
from python.srv.core.utils import utc_now
from python.srv.services.internal.replication.replication_settings import (
    REPLICATION_JOB_TYPE,
    settings_of,
    with_current_source_names,
)

logger = logging.getLogger(__name__)

TASK_TYPE = "replication"
WORKER_MODE = "replication"
TAG_PREFIX = "abr-replica"
ACTIVE_STATUSES = frozenset({"queued", "running", "stopping"})
FINISHED_STATUSES = frozenset({"completed", "failed", "stopped", "canceled", "cancelled"})

ProgressCallback = Callable[[dict[str, Any]], None] | None


class ReplicationError(RuntimeError):
    pass


class ReplicationValidationError(ReplicationError):
    pass


def is_replication_task(task: dict[str, Any]) -> bool:
    return str(task.get("task_type") or "") == TASK_TYPE


def replica_tag(job_id: str) -> str:
    return f"{TAG_PREFIX}-{job_id[:8]}"


def marker_prefix(job_id: str) -> str:
    return f"{TAG_PREFIX}:{job_id}:"


def replica_marker(job_id: str, session_id: str) -> str:
    return f"{marker_prefix(job_id)}{session_id}"


def marker_session(marker_text: str, job_id: str) -> str:
    prefix = marker_prefix(job_id)
    for line in str(marker_text or "").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _transport_parameters(inventory_id: str) -> dict[str, str]:
    from python.srv.services.abr_v41 import session_transport_parameters

    return session_transport_parameters(HYPERVISORS.get(inventory_id))


def create_replication_session(job: dict[str, Any], *, trigger: str, run_id: str) -> dict[str, Any]:
    from python.srv.services.abr_v41 import _append_task_log

    settings = with_current_source_names(settings_of(job))
    now = utc_now()
    task = {
        "id": str(uuid4()),
        "job_id": str(job["id"]),
        "task_type": TASK_TYPE,
        "hypervisor_id": settings["target_inventory_id"],
        "resource_type": "virtual_machine",
        "resource_external_id": settings["source_vm_id"],
        "status": "queued",
        "trigger": trigger,
        "run_id": run_id,
        "parameters": {
            "vm_name": settings["source_vm_name"],
            "source_job_id": settings["source_job_id"],
            "source_job_name": settings["source_job_name"],
            "target_inventory_name": settings["target_inventory_name"],
            "replica_name": settings["replica_name"],
            **_transport_parameters(settings["target_inventory_id"]),
        },
        "result": {"progress": 0, "worker_mode": WORKER_MODE},
        "created_at": now,
        "updated_at": now,
        "logs": [],
    }
    _append_task_log(
        task,
        level="info",
        message=(
            f"Replication job {job.get('name')}: replica {settings['replica_name']} of {settings['source_vm_name']} "
            f"(backup job {settings['source_job_name']}) on {settings['target_inventory_name']}, trigger {trigger}"
        ),
        component="session",
    )
    served = _served_request(str(job["id"]))
    if served:
        _append_task_log(
            task,
            level="info",
            message=f"This run also serves the {served.get('trigger')} request of {served.get('since')}: {served.get('detail')}",
            component="session",
        )
    persist_task(task)
    return task


def _served_request(job_id: str) -> dict[str, Any] | None:
    with CATALOG_LOCK:
        stored = JOBS.get(job_id)
        if not isinstance(stored, dict) or not stored.get("run_pending"):
            return None
        request = dict(stored.get("pending_trigger") or {})
        persist_job(dict(stored, run_pending=False, pending_trigger=None, updated_at=utc_now()))
    return request


def report_progress(progress_callback: ProgressCallback, progress: int, message: str, **extra: Any) -> None:
    if progress_callback is None:
        return
    payload: dict[str, Any] = {
        "stage": extra.pop("stage", "replicate"),
        "progress": progress,
        "message": message,
        "worker_mode": WORKER_MODE,
        "updated_at": utc_now(),
    }
    payload.update(extra)
    progress_callback(payload)


def report_quietly(progress_callback: ProgressCallback, progress: int, message: str, **extra: Any) -> None:
    from python.srv.core.task_executor import TaskStopRequested

    try:
        report_progress(progress_callback, progress, message, **extra)
    except TaskStopRequested:
        logger.info("%s", message)


def load_job(job_id: str) -> dict[str, Any]:
    from python.srv.services.abr_v41 import normalize_job_record

    stored = JOBS.get(job_id)
    if not isinstance(stored, dict) or str(stored.get("job_type") or "") != REPLICATION_JOB_TYPE:
        raise ReplicationValidationError(f"Replication job {job_id} no longer exists")
    return normalize_job_record(stored)


def save_replica(job_id: str, replica: dict[str, Any], *, renamed_to: str | None = None) -> None:
    with CATALOG_LOCK:
        stored = JOBS.get(job_id)
        if not isinstance(stored, dict):
            return
        saved = dict(replica)
        saved.pop("pending_name", None)
        previous = stored.get("replica") if isinstance(stored.get("replica"), dict) else {}
        pending = str(previous.get("pending_name") or "")
        if saved.get("vm_id") and pending and pending != (renamed_to or ""):
            saved["pending_name"] = pending
        persist_job(dict(stored, replica=saved, updated_at=utc_now()))


def is_own_snapshot(name: str, job_id: str) -> bool:
    return str(name or "").startswith(f"{replica_tag(job_id)}-")


def wait_seconds() -> int:
    from python.srv.core.server_defaults import env_int

    return env_int("ALTASTOR_REPLICA_TASK_WAIT_SECONDS")


def disk_folder(path: str) -> str:
    text = str(path or "").replace("\\", "/")
    return text.rsplit("/", 1)[0] if "/" in text else ""


def active_sessions(job_id: str) -> list[dict[str, Any]]:
    return [
        task
        for task in catalog_records(TASKS)
        if isinstance(task, dict)
        and str(task.get("job_id") or "") == job_id
        and is_replication_task(task)
        and str(task.get("status") or "") in ACTIVE_STATUSES
    ]


def request_run(job_id: str, *, trigger: str, detail: str) -> bool:
    with CATALOG_LOCK:
        stored = JOBS.get(job_id)
        if not isinstance(stored, dict) or str(stored.get("job_type") or "") != REPLICATION_JOB_TYPE:
            return False
        if stored.get("run_pending"):
            logger.info(
                "Replication job %s already has a run requested (%s); the %s request is served by it: %s",
                stored.get("name"),
                (stored.get("pending_trigger") or {}).get("trigger"),
                trigger,
                detail,
            )
            return False
        now = utc_now()
        persist_job(
            dict(
                stored,
                run_pending=True,
                pending_trigger={"trigger": trigger, "detail": detail, "since": now},
                updated_at=now,
            )
        )
    running = active_sessions(job_id)
    logger.info(
        "Replication job %s: %s requested a run (%s)%s",
        stored.get("name"),
        trigger,
        detail,
        f"; it starts when session {running[0]['id']} has finished" if running else "",
    )
    return True


def pending_request(job: dict[str, Any]) -> dict[str, Any] | None:
    if not job.get("run_pending"):
        return None
    request = job.get("pending_trigger")
    return dict(request) if isinstance(request, dict) else {"trigger": "pending", "detail": "", "since": ""}


def drop_request(job_id: str, reason: str) -> None:
    with CATALOG_LOCK:
        stored = JOBS.get(job_id)
        if not isinstance(stored, dict) or not stored.get("run_pending"):
            return
        request = stored.get("pending_trigger") or {}
        persist_job(dict(stored, run_pending=False, pending_trigger=None, updated_at=utc_now()))
    logger.info(
        "Replication job %s: the %s request of %s is dropped: %s",
        stored.get("name"),
        request.get("trigger"),
        request.get("since"),
        reason,
    )


def sessions_reading(source_job_id: str, vm_id: str) -> list[dict[str, Any]]:
    reading = []
    for job in catalog_records(JOBS):
        if not isinstance(job, dict) or str(job.get("job_type") or "") != REPLICATION_JOB_TYPE:
            continue
        settings = settings_of(job)
        if settings["source_job_id"] != source_job_id or (vm_id and settings["source_vm_id"] != vm_id):
            continue
        reading.extend(active_sessions(str(job.get("id") or "")))
    return reading


def _run_of(task: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = str(task.get("run_id") or "")
    job_id = str(task.get("job_id") or "")
    if not run_id:
        return [task]
    return [
        item
        for item in catalog_records(TASKS)
        if isinstance(item, dict) and str(item.get("run_id") or "") == run_id and str(item.get("job_id") or "") == job_id
    ] or [task]


def on_backup_session_finished(task: dict[str, Any]) -> None:
    from python.srv.services.abr_v41 import normalize_job_record

    if str(task.get("task_type") or "") != "backup":
        return
    source_job_id = str(task.get("job_id") or "")
    run = _run_of(task)
    if any(str(item.get("status") or "") not in FINISHED_STATUSES for item in run):
        return
    if any(str(item.get("status") or "") != "completed" for item in run):
        return
    if not any((item.get("result") or {}).get("restore_point_id") for item in run):
        return
    for stored in catalog_records(JOBS):
        if not isinstance(stored, dict) or str(stored.get("job_type") or "") != REPLICATION_JOB_TYPE:
            continue
        job = normalize_job_record(stored)
        settings = with_current_source_names(settings_of(job))
        if settings["source_job_id"] != source_job_id or not settings["run_after_source"] or not job.get("enabled"):
            continue
        request_run(
            str(job["id"]),
            trigger="after-source",
            detail=(
                f"backup job {settings['source_job_name']} completed a run (session {task.get('id')}) with a new "
                f"restore point of {settings['source_vm_name']}"
            ),
        )
