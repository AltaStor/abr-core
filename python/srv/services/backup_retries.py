# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from python.srv.core.mongo_db_wrapper import (
    JOBS,
    TASKS,
    catalog_records,
    persist_job,
    persist_task,
)
from python.srv.core.utils import utc_now

LOGGER = logging.getLogger(__name__)

DEFAULT_RETRY_COUNT = 3
DEFAULT_RETRY_INTERVAL_MINUTES = 10
NOT_RETRIED_FAILURES = frozenset({"validation"})
_LOCK = threading.Lock()


def retry_settings(job: dict[str, Any]) -> tuple[int, int]:
    count = job.get("retry_count")
    interval = job.get("retry_interval_minutes")
    return (
        0 if count is None else int(count),
        DEFAULT_RETRY_INTERVAL_MINUTES if interval is None else int(interval),
    )


def settings_from_request(payload: dict[str, Any], current: dict[str, Any], *, creating: bool) -> tuple[int, int]:
    from python.srv.services.backup_copy import _validate_retries

    count, interval = _validate_retries(payload, current)
    if creating:
        return (
            DEFAULT_RETRY_COUNT if count is None else count,
            DEFAULT_RETRY_INTERVAL_MINUTES if interval is None else interval,
        )
    stored_count, stored_interval = retry_settings(current)
    return (stored_count if count is None else count, stored_interval if interval is None else interval)


def retry_view(job: dict[str, Any]) -> dict[str, int]:
    count, interval = retry_settings(job)
    return {"count": count, "interval_minutes": interval}


def attempt_of(task: dict[str, Any]) -> int:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    try:
        return max(1, int(parameters.get("retry_attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _backup_job_of(task: dict[str, Any]) -> dict[str, Any] | None:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    if str(task.get("task_type") or "") != "backup" or parameters.get("copy_job"):
        return None
    job = JOBS.get(str(task.get("job_id") or ""))
    if not isinstance(job, dict) or str(job.get("job_type") or "backup") != "backup":
        return None
    return job


def _finished_at(task: dict[str, Any]) -> datetime | None:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return _parse(result.get("finished_at") or task.get("finished_at") or task.get("updated_at"))


def _superseded(task: dict[str, Any]) -> bool:
    created = str(task.get("created_at") or "")
    return any(
        str(other.get("job_id") or "") == str(task.get("job_id") or "")
        and str(other.get("resource_external_id") or "") == str(task.get("resource_external_id") or "")
        and str(other.get("task_type") or "") == "backup"
        and str(other.get("created_at") or "") > created
        for other in catalog_records(TASKS)
    )


def retry_due_at(task: dict[str, Any]) -> datetime | None:
    job = _backup_job_of(task)
    if job is None or str(task.get("status") or "") != "failed" or task.get("retried_by"):
        return None
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    if str(result.get("failure_kind") or "") in NOT_RETRIED_FAILURES:
        return None
    count, interval = retry_settings(job)
    if attempt_of(task) > count:
        return None
    finished = _finished_at(task)
    if finished is None or _superseded(task):
        return None
    return finished + timedelta(minutes=interval)


def retry_pending(task: dict[str, Any]) -> bool:
    return retry_due_at(task) is not None


def _retry_task(task: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    count, _interval = retry_settings(job)
    attempt = attempt_of(task) + 1
    parameters = dict(task.get("parameters") or {})
    parameters.pop("requires_task_id", None)
    parameters.update({"retry_attempt": attempt, "retry_total": count + 1, "retry_of": str(task.get("id") or "")})
    previous = task.get("result") if isinstance(task.get("result"), dict) else {}
    vm_name = str(parameters.get("vm_name") or task.get("resource_external_id") or "")
    reason = str(previous.get("message") or "it failed")
    now = utc_now()
    return {
        "id": str(uuid4()),
        "job_id": task.get("job_id"),
        "task_type": "backup",
        "hypervisor_id": task.get("hypervisor_id"),
        "resource_type": task.get("resource_type"),
        "resource_external_id": task.get("resource_external_id"),
        "status": "queued",
        "trigger": "retry",
        "run_id": task.get("run_id"),
        "parameters": parameters,
        "result": {
            "progress": 0,
            "resolved_backup_type": previous.get("resolved_backup_type") or parameters.get("resolved_backup_type"),
            "resolved_backup_reason": previous.get("resolved_backup_reason") or parameters.get("resolved_backup_reason"),
        },
        "logs": [
            {
                "timestamp": now,
                "level": "info",
                "component": "session",
                "message": f"Attempt {attempt}/{count + 1} of the backup of {vm_name}: attempt {attempt - 1} "
                f"(session {task.get('id')}) failed: {reason}",
            }
        ],
        "created_at": now,
        "updated_at": now,
        "note": f"Retry {attempt - 1} of {count} of the backup of {vm_name} after session {task.get('id')} failed.",
    }


def start_due_retries(now: datetime | None = None) -> list[dict[str, Any]]:
    moment = now or datetime.now(timezone.utc)
    started: list[dict[str, Any]] = []
    with _LOCK:
        for task in catalog_records(TASKS):
            due = retry_due_at(task)
            if due is None or due > moment:
                continue
            job = _backup_job_of(task)
            if job is None:
                continue
            retry = _retry_task(task, job)
            persist_task(retry)
            persist_task({**task, "retried_by": retry["id"]})
            run = job.get("last_run") if isinstance(job.get("last_run"), dict) else None
            if run and str(run.get("id") or "") == str(task.get("run_id") or ""):
                job["last_run"] = {**run, "session_ids": [*(run.get("session_ids") or []), retry["id"]]}
            job["last_session_id"] = retry["id"]
            job["last_task_id"] = retry["id"]
            job["updated_at"] = retry["created_at"]
            persist_job(dict(job))
            LOGGER.warning(
                "Backup of %s in job %s failed (session %s); %s",
                retry["parameters"].get("vm_name") or task.get("resource_external_id"),
                job.get("name") or job.get("id"),
                task.get("id"),
                retry["logs"][0]["message"],
            )
            started.append(retry)
    return started
