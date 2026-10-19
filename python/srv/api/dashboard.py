# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS, catalog_entries, catalog_records
from ._routing import FUNCTION_ID_KEY, AuditedRoute
from .auth_guard import authorize, require_session

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(require_session)],
    route_class=AuditedRoute,
)


SESSION_TYPE_LABELS = {"backup": "Backup", "restore": "Restore", "verify": "Verify", "delete": "Delete"}
RECENT_BACKUPS_WINDOW = timedelta(hours=24)
BYTES_PER_TB = 10**12


def _activity_name(task: dict, session: dict) -> str:
    job = JOBS.get(str(task.get("job_id") or ""))
    if isinstance(job, dict) and job.get("name"):
        return str(job["name"])
    return str(session.get("vm_name") or SESSION_TYPE_LABELS.get(str(task.get("task_type") or ""), "Unknown Job"))


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _relative_time(value: object, now: datetime) -> str | None:
    moment = _parse_time(value)
    if moment is None:
        return None
    seconds = int((now - moment).total_seconds())
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "Just now"


def _recent_backups_size_bytes(now: datetime) -> int:
    total = 0
    for backup in catalog_records(BACKUPS):
        if not isinstance(backup, dict) or not _backup_is_available(backup):
            continue
        created = _parse_time(backup.get("created_at"))
        if created is None or now - created > RECENT_BACKUPS_WINDOW:
            continue
        try:
            total += max(int(backup.get("size_bytes") or 0), 0)
        except (TypeError, ValueError):
            continue
    return total


class JobActivity(BaseModel):
    id: str
    name: str
    type: str
    status: str
    time: str | None = Field(default=None, description="How long ago the session finished, or started while it runs")
    started_at: str | None = None
    finished_at: str | None = None
    error_text: str | None = None


class AlertItem(BaseModel):
    id: str
    type: str
    message: str


class DashboardSummary(BaseModel):
    total_jobs: int
    protected_vms: int
    active_repositories: int
    recent_backups_size_tb: float = Field(description="Stored size of the backups taken in the last 24 hours, in TB")
    recent_backups_size_bytes: int = Field(description="The same size in bytes")
    recent_jobs: list[JobActivity]
    problem_areas: list[AlertItem]


def _compact_error_text(value) -> str | None:
    if isinstance(value, dict):
        text = str(
            value.get("message") or value.get("error") or value.get("detail") or ""
        )
    elif value:
        text = str(value)
    else:
        return None
    
    text = text.strip()
    if not text:
        return None
    
    text = text.split("Usage:", 1)[0].strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())

    if "Missing command" in text and "Disk backup failed" in text:
        prefix = text.split("Missing command", 1)[0].rstrip(": ")
        text = f"{prefix}: backup tool does not support the requested command"

    elif "Missing command" in text:
        text = "Backup tool does not support the requested command"

    if len(text) > 180:
        text = f"{text[:177].rstrip()}..."
    return text


def _backup_is_available(backup: dict) -> bool:
    return str(backup.get("status") or "").strip().lower() in {
        "available",
        "completed",
        "healthy",
        "success",
        "ok",
        "valid",
    }


def _backup_protected_vm_key(backup: dict) -> tuple[str, str] | None:
    source = backup.get("source") if isinstance(backup.get("source"), dict) else {}
    origin = backup.get("origin") if isinstance(backup.get("origin"), dict) else {}
    inventory_id = str(backup.get("inventory_id") or origin.get("inventory_id") or "")
    vm_text = str(
        backup.get("vm_id")
        or source.get("vm_external_id")
        or source.get("resource_external_id")
        or backup.get("vm_name")
        or source.get("vm_name")
        or ""
    ).strip()
    if not vm_text:
        return None
    return inventory_id, vm_text.lower()


@router.get(
    "/summary",
    response_model=DashboardSummary,
    dependencies=[Depends(authorize("dashboard.view"))],
    openapi_extra={FUNCTION_ID_KEY: "dashboard.view"},
    description="Function `dashboard.view`: View the dashboard summary.",
)
def get_dashboard_summary():
    from ..services.abr_v41 import session_to_dto

    total_jobs = len(JOBS)
    active_repositories = len(REPOSITORIES)
    protected_vm_keys = {
        key
        for backup in catalog_records(BACKUPS)
        if isinstance(backup, dict) and _backup_is_available(backup)
        for key in [_backup_protected_vm_key(backup)]
        if key is not None
    }
    protected_vms = len(protected_vm_keys)
    now = datetime.now(timezone.utc)
    recent_backups_size_bytes = _recent_backups_size_bytes(now)
    recent_jobs_list = []
    recent_tasks = catalog_records(TASKS)[-4:]

    for idx, t in enumerate(recent_tasks):
        session = session_to_dto(t, include_logs=False)
        error_text = _compact_error_text(t.get("error"))

        if not error_text and isinstance(t.get("result"), dict):
            error_text = _compact_error_text(t["result"].get("message"))

        recent_jobs_list.append(
            JobActivity(
                id=str(t.get("id", f"task_{idx}")),
                name=_activity_name(t, session),
                type=SESSION_TYPE_LABELS.get(str(t.get("task_type") or "backup"), str(t.get("task_type") or "").capitalize()),
                status=t.get("status", "Success").capitalize(),
                time=_relative_time(session.get("finished_at") or session.get("started_at"), now),
                started_at=session.get("started_at"),
                finished_at=session.get("finished_at"),
                error_text=error_text or None,
            )
        )

    problem_areas_list = []

    for repo_id, repo in catalog_entries(REPOSITORIES):
        health = repo.get("health", "Online")

        if health.lower() != "online":
            alert_type = "error" if health.lower() == "error" else "warning"
            problem_areas_list.append(
                AlertItem(
                    id=f"repo_{repo_id}",
                    type=alert_type,
                    message=f"Repository '{repo.get('name', repo_id)}' health is {health}.",
                )
            )

    for job_id, job in catalog_entries(JOBS):
        if job.get("last_status", "").lower() == "failed":
            problem_areas_list.append(
                AlertItem(
                    id=f"job_{job_id}",
                    type="error",
                    message=f"Job '{job.get('name', job_id)}' failed on last run.",
                )
            )

    return DashboardSummary(
        total_jobs=total_jobs,
        protected_vms=protected_vms,
        active_repositories=active_repositories,
        recent_backups_size_tb=round(recent_backups_size_bytes / BYTES_PER_TB, 6),
        recent_backups_size_bytes=recent_backups_size_bytes,
        recent_jobs=recent_jobs_list,
        problem_areas=problem_areas_list,
    )
