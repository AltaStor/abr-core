# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
import logging
import threading
import time
from uuid import uuid4
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .mongo_db_wrapper import HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS, catalog_records, persist_job, persist_task
from .server_defaults import env_int
from .utils import utc_now
from python.srv.services import clock_guard
from python.srv.services.abr_v41 import (
    execution_phases,
    job_clock_anomalies,
    job_run_record,
    normalize_job_record,
    resolve_job_execution,
    schedule_latest_next_run,
    schedule_next_run,
    stored_slot_next_run,
)

logger = logging.getLogger(__name__)
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_running_task_timeout_seconds() -> int:
    return env_int("BACKUPSERVER_VCENTER_TOOL_TIMEOUT_SECONDS")


class JobScheduler:
    def __init__(self, poll_interval_seconds: float = 5.0) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._access_lock_sweep: threading.Thread | None = None
        self._access_lock_sweep_at = time.monotonic()
        self._reported_clock_anomalies: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None:
            logger.debug("JobScheduler is already running.")
            return
        logger.info("Starting JobScheduler with %.1fs poll interval.", self._poll_interval_seconds)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="job-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        logger.info("Stopping JobScheduler...")
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._poll_interval_seconds * 2, 1.0))
            self._thread = None
        logger.info("JobScheduler stopped successfully.")

    def _poll_loop(self) -> None:
        logger.debug("Job scheduler thread started.")
        while not self._stop_event.is_set():
            try:
                clock_guard.tick()
            except Exception as exc:
                logger.exception("Could not check the server clock: %s", exc)
            try:
                self._dispatch_due_jobs()
            except Exception as exc:
                logger.exception("Unexpected error in job scheduler loop: %s", exc)
            try:
                self._retry_access_locks()
            except Exception as exc:
                logger.exception("Could not start the relocation block retry: %s", exc)
            finally:
                self._stop_event.wait(self._poll_interval_seconds)
        logger.debug("Job scheduler thread exiting.")

    def _retry_access_locks(self) -> None:
        now = time.monotonic()
        if now - self._access_lock_sweep_at < env_int("BACKUPSERVER_ACCESS_LOCK_RETRY_SECONDS"):
            return
        if self._access_lock_sweep is not None and self._access_lock_sweep.is_alive():
            return
        self._access_lock_sweep_at = now
        from python.srv.services.backup_catalog import release_pending_access_locks

        self._access_lock_sweep = threading.Thread(
            target=release_pending_access_locks,
            name="access-lock-retry",
            daemon=True,
        )
        self._access_lock_sweep.start()

    def _dispatch_due_jobs(self) -> None:
        now = datetime.fromisoformat(utc_now())
        jobs = sorted(catalog_records(JOBS), key=lambda item: str(item.get("created_at", "")))
        self._report_clock_anomalies(jobs)
        for job in jobs:
            if not self._is_supported_backup_job(job):
                continue

            if self._has_pending_task(job, now):
                continue

            normalized_job = normalize_job_record(job)
            due_slots = self._due_slots(normalized_job, job, now)
            if not due_slots:
                continue

            run_id = str(uuid4())
            tasks = self._create_scheduled_tasks(normalized_job, scheduled_type=due_slots[0][1], run_id=run_id)
            if not tasks:
                continue
            self._advance_due_slots(job, due_slots)

            job["status"] = "active"
            job["last_task_id"] = tasks[-1]["id"]
            job["last_session_id"] = tasks[-1]["id"]
            job["last_scheduled_at"] = tasks[-1]["created_at"]
            job["last_run_at"] = tasks[-1]["created_at"]
            job["last_run"] = job_run_record(
                run_id, trigger="schedule", started_at=tasks[0]["created_at"], session_ids=[task["id"] for task in tasks]
            )
            job["updated_at"] = tasks[-1]["created_at"]
            job["last_scheduler_error"] = None
            persist_job(dict(job))
            logger.info(
                "Scheduled job '%s' enqueued %d task(s); last task is '%s'.",
                job["id"],
                len(tasks),
                tasks[-1]["id"],
            )

    EXECUTION_SLOTS = (
        ("active_full_schedule", "ActiveFull", "active_full_next_run_at"),
        ("synthetic_full_schedule", "SyntheticFull", "synthetic_full_next_run_at"),
        ("schedule", "Incremental", "next_incremental_run_at"),
    )

    def _due_slots(
        self, normalized_job: dict[str, Any], job: dict[str, Any], now: datetime
    ) -> list[tuple[str, str, str, dict[str, Any]]]:
        due: list[tuple[str, str, str, dict[str, Any]]] = []
        for schedule_key, execution_type, next_run_key in self.EXECUTION_SLOTS:
            schedule = normalized_job.get(schedule_key)
            if not isinstance(schedule, dict) or not bool(schedule.get("enabled", True)):
                continue
            next_run_at = self._slot_next_run_at(job, schedule, next_run_key, now)
            if next_run_at is None or now < next_run_at:
                continue
            due.append((schedule_key, execution_type, next_run_key, schedule))
        return due

    def _slot_next_run_at(
        self, job: dict[str, Any], schedule: dict[str, Any], next_run_key: str, now: datetime
    ) -> datetime | None:
        stored = _parse_timestamp(stored_slot_next_run(job, next_run_key))
        if stored is not None:
            return self._trusted_slot_next_run(job, schedule, next_run_key, stored, now)
        base = _parse_timestamp(job.get("last_scheduled_at")) or _parse_timestamp(job.get("created_at"))
        if base is None:
            return None
        if job.get("status") != "active":
            job["status"] = "active"
        try:
            computed = self._compute_next_run_after(schedule, base)
        except ValueError as exc:
            logger.warning("Skipping schedule of job '%s': %s", job.get("id"), exc)
            return None
        job[next_run_key] = computed.isoformat()
        job["updated_at"] = utc_now()
        persist_job(dict(job))
        return computed

    def _trusted_slot_next_run(
        self,
        job: dict[str, Any],
        schedule: dict[str, Any],
        next_run_key: str,
        stored: datetime,
        now: datetime,
    ) -> datetime:
        try:
            soonest = self._compute_next_run_after(schedule, now)
            latest = schedule_latest_next_run(schedule, until=now)
        except ValueError:
            return stored
        if stored <= latest:
            return stored
        logger.warning(
            "Job '%s' had %s set to %s, later than the next run its schedule allows from %s. That value was "
            "written while this server's clock was ahead, so it is recomputed to %s; the runs due in between "
            "did not happen.",
            job.get("id"),
            next_run_key,
            stored.isoformat(),
            now.isoformat(),
            soonest.isoformat(),
        )
        job[next_run_key] = soonest.isoformat()
        job["updated_at"] = utc_now()
        persist_job(dict(job))
        return soonest

    def _advance_due_slots(
        self, job: dict[str, Any], due_slots: list[tuple[str, str, str, dict[str, Any]]]
    ) -> None:
        fired_at = datetime.fromisoformat(utc_now())
        for _schedule_key, _execution_type, next_run_key, schedule in due_slots:
            try:
                job[next_run_key] = self._compute_next_run_after(schedule, fired_at).isoformat()
            except ValueError:
                job[next_run_key] = None

    SCHEDULE_KINDS = frozenset({"cron", "daily", "weekly", "monthly", "periodic"})

    @classmethod
    def _schedule_can_fire(cls, schedule: Any) -> bool:
        if not isinstance(schedule, dict):
            return False
        if not bool(schedule.get("enabled", True)):
            return False
        return schedule.get("kind") in cls.SCHEDULE_KINDS

    def _report_clock_anomalies(self, jobs: list[dict[str, Any]]) -> None:
        reported: dict[str, str] = {}
        for job in jobs:
            job_id = str(job.get("id") or "")
            anomalies = job_clock_anomalies(job)
            if not job_id or not anomalies:
                continue
            reported[job_id] = str(job.get("last_run_at") or "")
            if self._reported_clock_anomalies.get(job_id) == reported[job_id]:
                continue
            for anomaly in anomalies:
                logger.warning("Job '%s' (%s): %s", job.get("name"), job_id, anomaly.get("message"))
        self._reported_clock_anomalies = reported

    def _is_supported_backup_job(self, job: dict[str, Any]) -> bool:
        normalized_job = normalize_job_record(job)
        if normalized_job.get("job_type") != "backup":
            return False
        if not normalized_job.get("inventory_id"):
            return False
        if not normalized_job.get("enabled"):
            return False
        return any(
            self._schedule_can_fire(normalized_job.get(schedule_key))
            for schedule_key, _execution_type, _next_run_key in self.EXECUTION_SLOTS
        )

    def _get_interval_minutes(self, job: dict[str, Any]) -> int:
        schedule = job.get("schedule", {})
        interval = schedule.get("interval_minutes")
        if not isinstance(interval, int) or interval <= 0:
            raise ValueError(f"Scheduled job {job.get('id')} is missing a valid interval_minutes value")
        return interval

    def _schedule_timezone(self, schedule: dict[str, Any]) -> ZoneInfo:
        name = str(schedule.get("timezone") or "UTC")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown schedule timezone %r; falling back to UTC.", name)
            return ZoneInfo("UTC")

    def _parse_start_time(self, schedule: dict[str, Any]) -> tuple[int, int]:
        value = schedule.get("start_time")
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("Scheduled job is missing a valid start_time")
        hour, minute = value.split(":", 1)
        return int(hour), int(minute)

    def _next_daily_after(self, schedule: dict[str, Any], base: datetime) -> datetime:
        tz = self._schedule_timezone(schedule)
        hour, minute = self._parse_start_time(schedule)
        base_local = base.astimezone(tz)
        candidate = base_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base_local:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def _next_weekly_after(self, schedule: dict[str, Any], base: datetime) -> datetime:
        tz = self._schedule_timezone(schedule)
        hour, minute = self._parse_start_time(schedule)
        weekdays = schedule.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            raise ValueError("Weekly schedule is missing weekdays")
        wanted = sorted(WEEKDAY_INDEX[str(day)] for day in weekdays if str(day) in WEEKDAY_INDEX)
        if not wanted:
            raise ValueError("Weekly schedule has no valid weekdays")
        base_local = base.astimezone(tz)
        for offset in range(0, 8):
            candidate_date = base_local.date() + timedelta(days=offset)
            if candidate_date.weekday() not in wanted:
                continue
            candidate = datetime.combine(candidate_date, datetime.min.time(), tzinfo=tz).replace(
                hour=hour,
                minute=minute,
            )
            if candidate > base_local:
                return candidate.astimezone(timezone.utc)
        raise ValueError("Unable to compute next weekly run")

    def _next_monthly_after(self, schedule: dict[str, Any], base: datetime) -> datetime:
        tz = self._schedule_timezone(schedule)
        hour, minute = self._parse_start_time(schedule)
        day_of_month = schedule.get("day_of_month")
        if not isinstance(day_of_month, int) or day_of_month < 1:
            raise ValueError("Monthly schedule is missing day_of_month")
        base_local = base.astimezone(tz)
        year = base_local.year
        month = base_local.month
        for _ in range(0, 14):
            last_day = calendar.monthrange(year, month)[1]
            day = min(day_of_month, last_day)
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
            if candidate > base_local:
                return candidate.astimezone(timezone.utc)
            month += 1
            if month > 12:
                month = 1
                year += 1
        raise ValueError("Unable to compute next monthly run")

    def _compute_next_run_after(self, schedule: dict[str, Any], base: datetime) -> datetime:
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        else:
            base = base.astimezone(timezone.utc)
        return schedule_next_run(schedule, after=base)

    def _resolve_running_task_timeout_seconds(self, job: dict[str, Any]) -> int:
        parameters = self._build_task_parameters(job)
        raw_value = parameters.get("tool_timeout_seconds")
        if raw_value in (None, ""):
            return _default_running_task_timeout_seconds()
        try:
            timeout_seconds = int(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "Scheduled job '%s' has invalid task_parameters.tool_timeout_seconds=%r; using default.",
                job.get("id"),
                raw_value,
            )
            return _default_running_task_timeout_seconds()
        if timeout_seconds <= 0:
            logger.warning(
                "Scheduled job '%s' has non-positive task_parameters.tool_timeout_seconds=%r; using default.",
                job.get("id"),
                raw_value,
            )
            return _default_running_task_timeout_seconds()
        return timeout_seconds

    def _mark_stale_task_failed(
        self,
        job: dict[str, Any],
        task: dict[str, Any],
        *,
        now: datetime,
        reason: str,
    ) -> None:
        task["status"] = "failed"
        current_result = task.get("result")
        result: dict[str, Any]
        if isinstance(current_result, dict):
            result = dict(current_result)
        else:
            result = {}
        result["message"] = reason
        result["finished_at"] = now.isoformat()
        task["result"] = result
        task["updated_at"] = now.isoformat()
        persist_task(dict(task))
        logger.warning("Marked stale task '%s' from job '%s' as failed: %s", task.get("id"), job.get("id"), reason)

    def _has_pending_task(self, job: dict[str, Any], now: datetime) -> bool:
        job_id = str(job["id"])
        running_timeout_seconds = self._resolve_running_task_timeout_seconds(job)
        for task in catalog_records(TASKS):
            if task.get("job_id") != job_id:
                continue
            status = task.get("status")
            if status == "queued":
                return True
            if status != "running":
                continue

            updated_at = _parse_timestamp(task.get("updated_at")) or _parse_timestamp(task.get("created_at"))
            if updated_at is None:
                self._mark_stale_task_failed(
                    job,
                    task,
                    now=now,
                    reason="Task had invalid timestamps and was recovered by the scheduler.",
                )
                continue

            age_seconds = max((now - updated_at).total_seconds(), 0.0)
            if age_seconds > running_timeout_seconds:
                self._mark_stale_task_failed(
                    job,
                    task,
                    now=now,
                    reason=(
                        f"Task exceeded the scheduler running timeout of {running_timeout_seconds} seconds "
                        f"without progress and was marked failed."
                    ),
                )
                continue
            return True
        return False

    def _resolve_task_targets(self, job: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        normalized_job = normalize_job_record(job)
        hypervisor_id = str(normalized_job.get("inventory_id") or "")
        metadata = job.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        resource_type = str(metadata.get("resource_type") or "virtual_machine")
        if resource_type != "virtual_machine":
            logger.warning(
                "Skipping scheduled job '%s': only virtual_machine backup schedules are supported right now.",
                job["id"],
            )
            return []

        raw_targets = normalized_job.get("vm_ids") or metadata.get("vm_external_ids")
        if isinstance(raw_targets, list):
            resource_external_ids = [str(item) for item in raw_targets if str(item)]
        else:
            resource_external_id = metadata.get("resource_external_id") or metadata.get("vm_external_id")
            resource_external_ids = [str(resource_external_id)] if isinstance(resource_external_id, str) and resource_external_id else []
        if not resource_external_ids:
            logger.warning(
                "Skipping scheduled job '%s': metadata.vm_external_ids, metadata.vm_external_id, or metadata.resource_external_id is required.",
                job["id"],
            )
            return []

        if hypervisor_id not in HYPERVISORS:
            logger.warning("Skipping scheduled job '%s': hypervisor '%s' not found.", job["id"], hypervisor_id)
            return []

        discovered = RESOURCES_BY_HV.get(hypervisor_id, {})
        targets: list[tuple[str, str, dict[str, Any]]] = []
        for resource_external_id in resource_external_ids:
            resource = discovered.get((resource_type, resource_external_id))
            if not resource:
                logger.warning(
                    "Skipping resource for scheduled job '%s': resource '%s:%s' was not discovered.",
                    job["id"],
                    resource_type,
                    resource_external_id,
                )
                continue
            targets.append((resource_type, resource_external_id, resource))

        return targets

    def _build_task_parameters(
        self,
        job: dict[str, Any],
        *,
        vm_name: str = "",
        execution_type: str = "",
        execution_reason: str = "",
        requires_task_id: str | None = None,
    ) -> dict[str, Any]:
        # task_parameters lives at the top level of the job record (see
        # create_backup_job/normalize_job_record in abr_v41.py) -- metadata is
        # only a fallback for legacy records that predate that field moving
        # out of metadata. Checking metadata alone (the old behavior here)
        # meant every schedule-triggered task silently got parameters={},
        # dropping compression/encryption settings for auto-run backups.
        parameters = job.get("task_parameters")
        if isinstance(parameters, dict):
            parameters = dict(parameters)
        else:
            metadata = job.get("metadata")
            parameters = (
                dict(metadata["task_parameters"])
                if isinstance(metadata, dict) and isinstance(metadata.get("task_parameters"), dict)
                else {}
            )

        if not execution_type:
            return parameters

        parameters.update(
            {
                "repository_id": job.get("repo_id"),
                "repository_name": job.get("repo_name"),
                "vm_name": vm_name,
                "resolved_backup_type": execution_type,
                "resolved_backup_reason": execution_reason,
                "backup_mode_request": (
                    "full" if execution_type in {"ActiveFull", "SyntheticFull"} else "auto"
                ),
                "incremental": execution_type == "Incremental",
                "synthetic_full": execution_type == "SyntheticFull",
                "active_full": execution_type == "ActiveFull",
            }
        )
        if requires_task_id:
            parameters["requires_task_id"] = requires_task_id
        return parameters

    def _create_scheduled_tasks(
        self, job: dict[str, Any], *, scheduled_type: str = "", run_id: str | None = None
    ) -> list[dict[str, Any]]:
        resolved_targets = self._resolve_task_targets(job)
        if not resolved_targets:
            return []

        tasks: list[dict[str, Any]] = []
        for resource_type, resource_external_id, resource in resolved_targets:
            execution_type, execution_reason = resolve_job_execution(
                job,
                vm_id=resource_external_id,
                force_full=False,
                scheduled_type=scheduled_type or None,
            )
            vm_name = str(resource.get("name") or resource_external_id)
            requires_task_id: str | None = None
            for phase_type, phase_reason in execution_phases(execution_type, execution_reason):
                now = utc_now()
                task = {
                    "id": str(uuid4()),
                    "job_id": str(job["id"]),
                    "task_type": "backup",
                    "hypervisor_id": str(job.get("inventory_id") or job.get("hypervisor_id")),
                    "resource_type": resource_type,
                    "resource_external_id": resource_external_id,
                    "status": "queued",
                    "trigger": "schedule",
                    "run_id": run_id,
                    "parameters": self._build_task_parameters(
                        job,
                        vm_name=vm_name,
                        execution_type=phase_type,
                        execution_reason=phase_reason,
                        requires_task_id=requires_task_id,
                    ),
                    "result": {
                        "progress": 0,
                        "resolved_backup_type": phase_type,
                        "resolved_backup_reason": phase_reason,
                    },
                    "created_at": now,
                    "updated_at": now,
                    "note": (
                        "Task was enqueued automatically from the job schedule template for "
                        f"{vm_name}."
                    ),
                }
                persist_task(task)
                tasks.append(task)
                requires_task_id = task["id"]
        return tasks
