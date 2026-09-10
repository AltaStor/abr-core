# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from ..core.mongo_db_wrapper import HYPERVISORS
from ..plugins import build_hypervisor_plugin
from .utils import utc_now

logger = logging.getLogger(__name__)


class TaskExecutionError(RuntimeError):
    pass


class TaskStopRequested(RuntimeError):
    pass


def _task_resolved_execution_field(task: dict[str, Any], field_name: str) -> Any:
    parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
    if field_name in parameters:
        return parameters.get(field_name)
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return result.get(field_name)


def run_task(
    task: dict[str, Any],
    simulated_seconds: float = 1.5,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    task_id = task.get("id", "UNKNOWN")
    logger.debug("Executing run_task for task_id: %s", task_id)

    if task.get("status") != "queued":
        logger.error("Task '%s' rejected: status is '%s', not 'queued'.", task_id, task.get("status"))
        raise TaskExecutionError("Only queued tasks can be executed")

    from python.srv.services.synthetic_full import (
        SyntheticFullValidationError,
        is_synthetic_full_task,
        run_synthetic_full,
    )

    task_type = str(task.get("task_type"))
    hypervisor_id = str(task.get("hypervisor_id", ""))
    hypervisor = HYPERVISORS.get(hypervisor_id, {})
    hypervisor_kind = str(hypervisor.get("kind", ""))
    synthetic_full = task_type == "backup" and is_synthetic_full_task(task)
    plugin = None if synthetic_full else build_hypervisor_plugin(hypervisor)

    logger.info("Task '%s': Type='%s', HypervisorKind='%s'", task_id, task_type, hypervisor_kind)

    try:
        if synthetic_full:
            result = run_synthetic_full(task, progress_callback=progress_callback)
        elif task_type == "backup":
            result = plugin.backup(task, progress_callback=progress_callback)
        elif task_type == "restore":
            result = plugin.restore(task, progress_callback=progress_callback)
        else:
            logger.error("Task '%s' failed: Unsupported task type '%s'", task_id, task_type)
            raise TaskExecutionError(f"Unsupported task type: {task_type}")
    except TaskStopRequested as exc:
        logger.info("Execution of task '%s' stopped: %s", task_id, exc)
        return {
            "task_id": task["id"],
            "status": "stopped",
            "result": {
                "message": str(exc),
                "finished_at": utc_now(),
                "resolved_backup_type": _task_resolved_execution_field(task, "resolved_backup_type"),
                "resolved_backup_reason": _task_resolved_execution_field(task, "resolved_backup_reason"),
                "worker_mode": (
                    "synthetic-full"
                    if synthetic_full
                    else "mock-backup-emulator"
                    if hypervisor_kind != "vcenter"
                    else "vcenter-abr-vmdk-tool"
                ),
            },
            "updated_at": utc_now(),
        }
    except Exception as exc:
        logger.error("Execution of task '%s' failed: %s", task_id, exc)
        return {
            "task_id": task["id"],
            "status": "failed",
            "result": {
                "message": str(exc),
                "error_type": type(exc).__name__,
                "failure_kind": "validation" if isinstance(exc, SyntheticFullValidationError) else "runtime",
                "traceback": traceback.format_exc(),
                "finished_at": utc_now(),
                "resolved_backup_type": _task_resolved_execution_field(task, "resolved_backup_type"),
                "resolved_backup_reason": _task_resolved_execution_field(task, "resolved_backup_reason"),
                "worker_mode": (
                    "synthetic-full"
                    if synthetic_full
                    else "mock-backup-emulator"
                    if hypervisor_kind != "vcenter"
                    else "vcenter-abr-vmdk-tool"
                ),
            },
            "updated_at": utc_now(),
        }

    result.setdefault("finished_at", utc_now())
    result.setdefault(
        "worker_mode",
        "mock-backup-emulator" if hypervisor_kind != "vcenter" else "vcenter-abr-vmdk-tool",
    )

    logger.debug("Task '%s' execution completed inside run_task.", task_id)
    
    return {
        "task_id": task["id"],
        "status": "completed",
        "result": result,
        "updated_at": utc_now(),
    }
