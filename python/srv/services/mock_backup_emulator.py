# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any, Callable

from .backup_mode import resolve_job_backup_mode


_GIB = 1024**3
_DEFAULT_COMPRESSION_LEVEL = 3


class MockBackupEmulator:
    def __init__(self, min_seconds: float = 10.0, max_seconds: float = 20.0) -> None:
        if min_seconds <= 0:
            raise ValueError("min_seconds must be > 0")
        if max_seconds < min_seconds:
            raise ValueError("max_seconds must be >= min_seconds")
        self._min_seconds = min_seconds
        self._max_seconds = max_seconds

    def backup(
        self,
        task: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._run("backup", task, progress_callback)

    def restore(
        self,
        task: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return self._run("restore", task, progress_callback)

    def _run(
        self,
        operation: str,
        task: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        duration_seconds = self._pick_duration(task)
        process_id = os.getpid()
        worker = threading.current_thread()
        stages = self._build_stages(operation)
        stage_duration = duration_seconds / len(stages)
        # Restores have no real byte-progress channel of their own here (see
        # _pick_size_bytes) -- surface a deterministic size scaled by stage
        # progress so the Activity page's Read/Sent counters visibly climb
        # while a mock restore runs, instead of only appearing at completion.
        restore_size_bytes = self._pick_size_bytes(task, "full") if operation == "restore" else 0

        for index, stage in enumerate(stages, start=1):
            if progress_callback is not None:
                event: dict[str, Any] = {
                    "message": stage["message"],
                    "operation": operation,
                    "stage": stage["name"],
                    "progress": stage["progress"],
                    "duration_seconds": round(duration_seconds, 2),
                    "resource_type": task["resource_type"],
                    "resource_external_id": task["resource_external_id"],
                    "process_id": process_id,
                    "thread_id": worker.ident,
                    "thread_name": worker.name,
                }
                if operation == "restore":
                    live_bytes = int(restore_size_bytes * stage["progress"] / 100)
                    event["processed_bytes"] = live_bytes
                    event["transferred_bytes"] = live_bytes
                progress_callback(event)
            if index < len(stages):
                time.sleep(stage_duration)

        result = {
            "message": f"Mock {operation} completed",
            "operation": operation,
            "duration_seconds": round(duration_seconds, 2),
            "resource_type": task["resource_type"],
            "resource_external_id": task["resource_external_id"],
            "progress": 100,
            "stage": "completed",
            "process_id": process_id,
            "thread_id": worker.ident,
            "thread_name": worker.name,
        }
        if operation == "backup":
            parameters = task.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}
            existing_result = task.get("result") if isinstance(task.get("result"), dict) else {}
            backup_mode = resolve_job_backup_mode(task, parameters) or "full"
            resolved_backup_type = parameters.get("resolved_backup_type") or existing_result.get("resolved_backup_type")
            resolved_backup_reason = parameters.get("resolved_backup_reason") or existing_result.get("resolved_backup_reason")
            if not resolved_backup_type:
                resolved_backup_type = "Incremental" if backup_mode == "incremental" else "ActiveFull"
            if not resolved_backup_reason:
                resolved_backup_reason = "StandardIncremental" if backup_mode == "incremental" else "FirstRun"
            result["backup_mode"] = backup_mode
            result["incremental"] = backup_mode == "incremental"
            result["resolved_backup_type"] = resolved_backup_type
            result["resolved_backup_reason"] = resolved_backup_reason
            size_bytes = self._pick_size_bytes(task, backup_mode)
            result["processed_bytes"] = size_bytes
            result["transferred_bytes"] = size_bytes
            result["compression_level"] = self._resolve_compression_level(parameters)
            result["encryption_enabled"] = bool(parameters.get("encryption_enabled"))
        elif operation == "restore":
            result["processed_bytes"] = restore_size_bytes
            result["transferred_bytes"] = restore_size_bytes
        return result

    def _resolve_compression_level(self, parameters: dict[str, Any]) -> int:
        raw_value = parameters.get("compression_level")
        if raw_value in (None, ""):
            return _DEFAULT_COMPRESSION_LEVEL
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return _DEFAULT_COMPRESSION_LEVEL

    def _pick_size_bytes(self, task: dict[str, Any], backup_mode: str) -> int:
        """A deterministic, per-resource size so repeated runs of the same VM
        stay consistent. An incremental only carries the changed blocks, so it
        gets a small fraction of the full's size -- mirroring how a real CBT
        incremental compares to the first full."""
        resource_key = f"{task.get('resource_type', '')}:{task.get('resource_external_id', '')}"
        rng = random.Random(sum(ord(char) for char in resource_key) or 1)
        full_bytes = int(rng.uniform(8.0, 180.0) * _GIB)
        if backup_mode != "incremental":
            return full_bytes
        return max(1, int(full_bytes * rng.uniform(0.02, 0.12)))

    def _build_stages(self, operation: str) -> list[dict[str, Any]]:
        if operation == "backup":
            return [
                {"name": "prepare", "progress": 5, "message": "Preparing backup task"},
                {"name": "snapshot", "progress": 25, "message": "Creating backup snapshot"},
                {"name": "transfer", "progress": 55, "message": "Transferring backup data"},
                {"name": "verify", "progress": 80, "message": "Verifying backup payload"},
                {"name": "finalize", "progress": 95, "message": "Finalizing backup metadata"},
            ]
        return [
            {"name": "prepare", "progress": 5, "message": "Preparing restore task"},
            {"name": "fetch", "progress": 25, "message": "Fetching restore payload"},
            {"name": "apply", "progress": 55, "message": "Applying restore data"},
            {"name": "verify", "progress": 80, "message": "Verifying restored resource"},
            {"name": "finalize", "progress": 95, "message": "Finalizing restore metadata"},
        ]

    def _pick_duration(self, task: dict[str, Any]) -> float:
        parameters = task.get("parameters", {})
        if isinstance(parameters, dict):
            requested = parameters.get("duration_seconds")
            if isinstance(requested, (int, float)) and requested > 0:
                return float(requested)

        task_id = str(task.get("id", "mock-task"))
        seed = sum(ord(char) for char in task_id)
        rng = random.Random(seed)
        return rng.uniform(self._min_seconds, self._max_seconds)
