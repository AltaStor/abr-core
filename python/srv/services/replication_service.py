# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python.srv.services.internal.replication import (
    replica_update,
    replication_session,
    replication_settings,
)

REPLICATION_JOB_TYPE = replication_settings.REPLICATION_JOB_TYPE
SETTING_KEYS = replication_settings.SETTING_KEYS
ReplicationValidationError = replication_session.ReplicationValidationError


def is_replication_job(job: dict[str, Any] | None) -> bool:
    return replication_settings.is_replication_job(job)


def is_replication_task(task: dict[str, Any]) -> bool:
    return replication_session.is_replication_task(task)


def normalize_replication_fields(item: dict[str, Any]) -> None:
    replication_settings.normalize_replication_fields(item)


def apply_replication_settings(job: dict[str, Any], payload: dict[str, Any], *, creating: bool) -> bool:
    return replication_settings.apply_replication_settings(job, payload, creating=creating)


def keep_run_state(job: dict[str, Any]) -> None:
    replication_settings.keep_run_state(job)


def replication_view(job: dict[str, Any]) -> dict[str, Any]:
    return replication_settings.replication_view(job)


def ensure_source_job_free(source_job: dict[str, Any], *, removed_vm_ids: list[str] | None = None) -> None:
    replication_settings.ensure_source_job_free(source_job, removed_vm_ids=removed_vm_ids)


def create_replication_session(job: dict[str, Any], *, trigger: str, run_id: str) -> dict[str, Any]:
    return replication_session.create_replication_session(job, trigger=trigger, run_id=run_id)


def run_replication(
    task: dict[str, Any], progress_callback: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    return replica_update.run_replication(task, progress_callback)


def on_session_finished(task: dict[str, Any]) -> None:
    replication_session.on_backup_session_finished(task)


def pending_request(job: dict[str, Any]) -> dict[str, Any] | None:
    return replication_session.pending_request(job)


def drop_request(job_id: str, reason: str) -> None:
    replication_session.drop_request(job_id, reason)


def sessions_reading(source_job_id: str, vm_id: str) -> list[dict[str, Any]]:
    return replication_session.sessions_reading(source_job_id, vm_id)


def delete_replica(job: dict[str, Any]) -> dict[str, Any]:
    return replica_update.delete_replica(job)
