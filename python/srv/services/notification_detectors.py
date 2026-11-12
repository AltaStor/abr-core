# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Works out what happened and tells the notification service, so the
components only report: ``task_finished`` (backup runs, restores, and repository
space after a backup), ``clock_jumped``, and the daily certificate check."""

from __future__ import annotations

import logging
import shutil
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from python.srv.core.mongo_db_wrapper import JOBS, REPOSITORIES, TASKS, catalog_entries

from . import email_channel
from .notification_events import CERTIFICATE_WARNING_DAYS
from .notification_service import notify

LOGGER = logging.getLogger(__name__)

CERTIFICATE_CHECK_SECONDS = 24 * 3600


# --------------------------------------------------------------- wording --

def _appliance_zone() -> ZoneInfo | None:
    try:
        from . import abr_v41

        name = abr_v41.server_timezone()["timezone"]
        return None if name in {"UTC", "Etc/UTC"} else ZoneInfo(name)
    except Exception:
        return None


def when(iso: Any) -> str:
    """A moment for a message: UTC, then the appliance's own time when that
    differs, labelled, so a reader in any zone can place it."""
    text = str(iso or "").strip()
    if not text:
        return "-"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    zone = _appliance_zone()
    if zone is None:
        return utc
    local = moment.astimezone(zone)
    return f"{utc} ({local.strftime('%Y-%m-%d %H:%M:%S')} {zone.key}, appliance time)"


# --------------------------------------------------------------- detectors --

_notified_lock = threading.Lock()
_notified_runs: deque[str] = deque(maxlen=1000)
_low_space: set[str] = set()
_RESULT_EVENTS = {"failed": ("job.failed", "error"), "warning": ("job.warning", "warning"), "success": ("job.succeeded", "info")}


def task_finished(task: dict[str, Any]) -> None:
    """Report a backup or restore task that reached its final state."""
    try:
        kind = str(task.get("task_type") or "")
        if kind == "backup":
            _backup_finished(task)
        elif kind == "restore":
            _restore_finished(task)
    except Exception:
        LOGGER.exception("Could not work out the notifications for task %s", task.get("id"))


def _backup_finished(task: dict[str, Any]) -> None:
    from . import abr_v41

    job = JOBS.get(str(task.get("job_id") or ""))
    if not isinstance(job, dict):
        return
    run_id = str(task.get("run_id") or task.get("id"))
    session_ids = [task_id for task_id, item in catalog_entries(TASKS) if str(item.get("run_id") or "") == run_id] if task.get("run_id") else [str(task["id"])]
    first = min((TASKS.get(task_id) or {}).get("created_at") or "" for task_id in session_ids) if session_ids else task.get("created_at")
    summary = abr_v41.job_run_summary(
        {"last_run": {"id": run_id, "trigger": task.get("trigger") or "manual", "started_at": first, "session_ids": session_ids}}
    )
    if summary is None or summary["result"] == "running":
        return
    with _notified_lock:
        if run_id in _notified_runs:
            return
        _notified_runs.append(run_id)
    _check_repository_space(str(job.get("repo_id") or ""))
    event = _RESULT_EVENTS.get(summary["result"])
    if event is None:
        return
    name = str(job.get("name") or job.get("id"))
    total = summary["vm_count"]
    outcome = {
        "failed": f"failed: {summary['failed']} of {total} VM(s) not backed up",
        "warning": f"finished with warnings: {summary['warning']} of {total} VM(s)",
        "success": f"succeeded: {total} VM(s) backed up",
    }[summary["result"]]
    lines = [
        f'Backup job "{name}" {outcome}.',
        "",
        f"Started:   {when(summary.get('started_at'))}",
        f"Finished:  {when(summary.get('finished_at'))}",
        f"Trigger:   {summary.get('trigger') or 'manual'}",
        f"VMs:       {summary['succeeded']} backed up, {summary['failed']} failed, {summary['stopped']} stopped",
        "",
        "Per VM:",
    ]
    for vm in summary["vms"]:
        line = f"  {vm.get('vm_name') or vm.get('vm_id')}: {vm['result']}"
        if vm.get("message"):
            line += f" - {vm['message']}"
        lines.append(line)
    lines += ["", f"Job ID: {job.get('id')}, run ID: {run_id}. Session logs: abr session list --job \"{name}\""]
    notify(
        event[0],
        f'Backup job "{name}" {outcome}',
        "\n".join(lines) + "\n",
        severity=event[1],
        details={
            "job_id": job.get("id"),
            "job_name": name,
            "run_id": run_id,
            "result": summary["result"],
            "trigger": summary.get("trigger"),
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "vm_count": total,
            "succeeded": summary["succeeded"],
            "failed": summary["failed"],
            "warning": summary["warning"],
            "stopped": summary["stopped"],
            "vms": [
                {"id": vm.get("vm_id"), "name": vm.get("vm_name"), "result": vm["result"], "message": vm.get("message")}
                for vm in summary["vms"]
            ],
        },
    )


def _restore_finished(task: dict[str, Any]) -> None:
    from . import abr_v41

    state = abr_v41._session_run_result(task)
    if state not in {"failed", "success", "warning"}:
        return
    params = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    vm = params.get("target_vm_name") or params.get("source_vm_name") or task.get("resource_external_id")
    failed = state == "failed"
    lines = [
        f"The restore of {params.get('source_vm_name') or vm} {'failed' if failed else 'finished'}.",
        "",
        f"Restored as:   {vm} on {params.get('target_inventory_name') or '-'}" + (f", host {params['target_host_name']}" if params.get("target_host_name") else ""),
        f"Mode:          {params.get('restore_mode') or params.get('scenario') or '-'}",
        f"Restore point: {params.get('point_id') or '-'}",
        f"Started:       {when(task.get('created_at'))}",
        f"Finished:      {when(result.get('finished_at') or task.get('updated_at'))}",
    ]
    if failed and result.get("message"):
        lines += ["", f"Reason: {result['message']}"]
    if state == "warning":
        lines += ["", "Finished with warnings; see the session log."]
    lines += ["", f"Session ID: {task.get('id')}. Log: abr session log --id {task.get('id')}"]
    notify(
        "restore.failed" if failed else "restore.succeeded",
        f"Restore of {vm} {'failed' if failed else 'finished'}",
        "\n".join(lines) + "\n",
        severity="error" if failed else ("warning" if state == "warning" else "info"),
        details={
            "session_id": task.get("id"),
            "result": state,
            "vm_name": vm,
            "source_vm_name": params.get("source_vm_name"),
            "target_inventory": params.get("target_inventory_name"),
            "target_host": params.get("target_host_name"),
            "restore_point_id": params.get("point_id"),
            "started_at": task.get("created_at"),
            "finished_at": result.get("finished_at") or task.get("updated_at"),
            "message": result.get("message"),
        },
    )


def verify_finished(task: dict[str, Any], report: dict[str, Any]) -> None:
    try:
        if report.get("canceled") or report.get("result") != "invalid":
            return
        params = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
        marks = report.get("corrupt") if isinstance(report.get("corrupt"), dict) else {}
        damaged = sorted({str(item.get("point_id")) for item in report.get("errors") or [] if isinstance(item, dict) and item.get("damaged")})
        corrupt_now = [str(item) for item in marks.get("corrupt_now") or []]
        errors = [item for item in report.get("errors") or [] if isinstance(item, dict)]
        vm = params.get("vm_name") or task.get("resource_external_id") or "-"
        job = JOBS.get(str(task.get("job_id") or "")) or {}
        found = "found damaged data" if damaged else "could not check every restore point"
        lines = [
            f"The verification of the backup of {vm} {found}.",
            "",
            f"Backup chain:  {params.get('backup_id') or report.get('id') or '-'}",
            f"Repository:    {params.get('repository_name') or '-'}",
            f"Job:           {job.get('name') or params.get('job_name') or '-'}",
            f"Started by:    {params.get('trigger') or 'hand'}",
            f"Checked:       {report.get('checked_artifacts')} artifact(s) of {report.get('checked_restore_points')} restore point(s)",
            f"Finished:      {when(datetime.now(timezone.utc).isoformat())}",
        ]
        if damaged:
            lines += [
                "",
                f"Damaged data in: {', '.join(damaged)}.",
                f"{len(corrupt_now)} restore point(s) of the chain are marked corrupt, these and the ones that build on them.",
                "They are not offered for restore, and the job's next run takes an active full.",
            ]
        lines += ["", "Problems:"]
        for error in errors[:10]:
            lines.append(f"  {error.get('point_id')}: {str(error.get('message') or '').strip().splitlines()[-1] if error.get('message') else '-'}")
        lines += ["", f"Session ID: {task.get('id')}. Log: abr session log --id {task.get('id')}"]
        notify(
            "backup.verify_failed",
            f"Backup verification of {vm} {found}",
            "\n".join(lines) + "\n",
            severity="error",
            details={
                "session_id": task.get("id"),
                "backup_id": params.get("backup_id") or report.get("id"),
                "vm_name": vm,
                "job_name": job.get("name") or params.get("job_name"),
                "repository_name": params.get("repository_name"),
                "damaged_restore_point_ids": damaged,
                "corrupt_restore_point_ids": corrupt_now,
                "errors": len(errors),
            },
        )
    except Exception:
        LOGGER.exception("Could not work out the notification for verify session %s", task.get("id"))


def repository_verify_failed(repository: dict[str, Any], health: dict[str, Any]) -> None:
    try:
        if health.get("status") != "failed":
            return
        marks = health.get("corrupt") if isinstance(health.get("corrupt"), dict) else {}
        damaged = sorted({str(item.get("point_id")) for item in health.get("errors") or [] if isinstance(item, dict) and item.get("damaged")})
        found = "found damaged data" if damaged else "could not check every restore point"
        name = repository.get("name") or repository.get("id")
        lines = [
            f"Repo verify of repository {name} {found}.",
            "",
            str(health.get("message") or ""),
        ]
        if damaged:
            lines += [
                "",
                f"Damaged data in: {', '.join(damaged)}. {len(marks.get('corrupt_now') or [])} restore point(s) are marked corrupt, these and the ones that build on them.",
                "They are not offered for restore, and their jobs' next runs take an active full.",
            ]
        notify(
            "backup.verify_failed",
            f"Repo verify of {name} {found}",
            "\n".join(lines) + "\n",
            severity="error",
            details={"repository_id": repository.get("id"), "repository_name": name, "damaged_restore_point_ids": damaged},
        )
    except Exception:
        LOGGER.exception("Could not work out the notification for repo verify of %s", repository.get("id"))


def _check_repository_space(repository_id: str) -> None:
    repository = REPOSITORIES.get(repository_id)
    if not isinstance(repository, dict) or str(repository.get("type") or "") == "s3":
        return
    path = str(repository.get("path") or "").strip()
    if not path:
        return
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return
    percent = usage.used * 100 / usage.total if usage.total else 0
    threshold = int(email_channel.load_settings().get("repository_space_threshold_percent") or email_channel.DEFAULT_SPACE_THRESHOLD)
    with _notified_lock:
        if percent < threshold:
            _low_space.discard(repository_id)
            return
        if repository_id in _low_space:
            return
        _low_space.add(repository_id)
    name = str(repository.get("name") or repository_id)
    gib = 1024 ** 3
    notify(
        "repository.low_space",
        f'Repository "{name}" is {percent:.0f}% full',
        (
            f'Repository "{name}" is {percent:.0f}% full, at or above the {threshold}% threshold.\n\n'
            f"Path:   {path}\n"
            f"Used:   {usage.used / gib:.1f} GiB of {usage.total / gib:.1f} GiB\n"
            f"Free:   {usage.free / gib:.1f} GiB\n\n"
            "Backups fail once it is full. Free space (retention, deleting backups), or move jobs to another "
            "repository.\n"
        ),
        severity="warning",
        details={
            "repository_id": repository_id,
            "repository_name": name,
            "path": path,
            "used_percent": round(percent, 1),
            "threshold_percent": threshold,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        },
    )


def clock_jumped(message: str | None, *, detected_at: Any, last_trusted_at: Any) -> None:
    notify(
        "system.clock_jump",
        "Server clock jumped forward; retention is paused",
        (
            f"{message or 'The server clock jumped forward.'}\n\n"
            f"Detected:           {when(detected_at)}\n"
            f"Last trusted time:  {when(last_trusted_at)}\n\n"
            "Retention deletes nothing until an Administrator confirms the server time: in the web console's "
            "banner, or with `abr system clock confirm`.\n"
        ),
        severity="error",
        details={"detected_at": detected_at, "last_trusted_at": last_trusted_at},
    )


_certificate_timer: threading.Timer | None = None
_certificate_warned: str | None = None


def check_certificate() -> None:
    """Warn about an administrator-installed HTTPS certificate close to its
    end, once per certificate."""
    global _certificate_warned
    try:
        from . import tls_certificate_service

        cert = tls_certificate_service.get_certificate()
    except Exception:
        return
    if cert.get("auto_renew") or int(cert.get("days_remaining") or 0) > CERTIFICATE_WARNING_DAYS:
        return
    fingerprint = str(cert.get("fingerprint_sha256") or "")
    if fingerprint == _certificate_warned:
        return
    _certificate_warned = fingerprint
    expired = bool(cert.get("expired"))
    notify(
        "tls.certificate_expiring",
        "The HTTPS certificate has expired" if expired else f"The HTTPS certificate expires in {cert.get('days_remaining')} day(s)",
        (
            f"The HTTPS certificate of the appliance {'expired' if expired else 'expires'} on {when(cert.get('not_after'))}.\n\n"
            f"Subject: {cert.get('subject')}\n"
            f"Issuer:  {cert.get('issuer')}\n\n"
            "It was installed by an administrator, so the appliance does not renew it. Install a new one in "
            "the web console (Settings > Security), or with PUT /api/v1/system/tls-certificate.\n"
        ),
        severity="error" if expired else "warning",
        details={
            "subject": cert.get("subject"),
            "issuer": cert.get("issuer"),
            "not_after": cert.get("not_after"),
            "days_remaining": cert.get("days_remaining"),
            "expired": expired,
            "fingerprint_sha256": fingerprint,
        },
    )


def start_periodic_checks() -> None:
    """Start the daily certificate check (the server calls this at start-up)."""
    global _certificate_timer

    def run() -> None:
        global _certificate_timer
        check_certificate()
        _certificate_timer = threading.Timer(CERTIFICATE_CHECK_SECONDS, run)
        _certificate_timer.daemon = True
        _certificate_timer.start()

    if _certificate_timer is None:
        _certificate_timer = threading.Timer(60, run)
        _certificate_timer.daemon = True
        _certificate_timer.start()


def stop_periodic_checks() -> None:
    global _certificate_timer
    if _certificate_timer is not None:
        _certificate_timer.cancel()
        _certificate_timer = None
