# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from .mongo_db_wrapper import TASKS, TASK_LOCK, claim_task, load_queued_tasks, persist_task
from ..services.backup_catalog import register_backup_from_completed_task
from .task_executor import TaskStopRequested, run_task
from .utils import utc_now

logger = logging.getLogger(__name__)


def _refresh_backup_decision(task: dict[str, object]) -> dict[str, object]:
    try:
        from python.srv.services.abr_v41 import refresh_queued_backup_decision

        return refresh_queued_backup_decision(task)
    except Exception:
        logger.exception("Could not re-check the backup type of task '%s' at start; it runs as queued", task.get("id"))
        return task


def vm_key_for_task(task: dict[str, object]) -> tuple[str, str] | None:
    """Identity of the live VM a backup reads from, or ``None`` if unknown.

    Two backups of the *same* VM must never run at once. Each one creates a
    VMware snapshot, holds an NFC/NBD lease on the VM's disks while reading, and
    removes the snapshot at the end. Run concurrently they fight over the VM's
    snapshot tree and per-VM lease limits, and both jobs hang — the field
    incident where "two sessions on one VM get stuck". We serialize them on
    ``(hypervisor, vm external id)``, the stable target identity carried on every
    backup session (see ``_create_backup_session`` in ``abr_v41``).
    """
    external_id = str(task.get("resource_external_id") or "")
    if not external_id:
        return None
    return (str(task.get("hypervisor_id") or ""), external_id)


class TaskProcessPool:
    def __init__(
        self,
        worker_count: int,
        simulated_seconds: float = 1.5,
        poll_interval_seconds: float = 0.5,
        max_concurrent_backups: int | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        # Backups are the heavy tasks (each spawns an abr-vmdk-tool subprocess that
        # reads a VMDK). On a small, MongoDB-co-located host, letting every worker
        # run a backup at once exhausts RAM/CPU and takes the service down. Cap the
        # number of *backup* tasks that may run concurrently, independently of the
        # general worker count (restores/light tasks still use the spare slots).
        if max_concurrent_backups is None:
            max_concurrent_backups = worker_count
        if max_concurrent_backups < 1:
            raise ValueError("max_concurrent_backups must be >= 1")
        self._worker_count = worker_count
        self._max_concurrent_backups = max_concurrent_backups
        self._simulated_seconds = simulated_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._executor: ThreadPoolExecutor | None = None
        # future -> (task_id, task_type, vm_key); task_type lets us count running
        # backups, vm_key lets us refuse a second backup of a VM already in flight.
        self._futures: dict[Future[dict[str, object]], tuple[str, str, tuple[str, str] | None]] = {}
        self._futures_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poller: threading.Thread | None = None

    def start(self) -> None:
        if self._executor is not None:
            logger.debug("TaskProcessPool is already running.")
            return
        logger.info("Starting TaskProcessPool with %d workers.", self._worker_count)
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._worker_count,
            thread_name_prefix="task-worker",
        )
        self._poller = threading.Thread(
            target=self._poll_loop,
            name="task-poller",
            daemon=True,
        )
        self._poller.start()

    def submit(self, task_id: str) -> None:
        logger.debug("Submission requested for task '%s'.", task_id)
        claimed = claim_task(task_id)
        if claimed is None:
            logger.warning("Failed to claim task '%s' for manual submission.", task_id)
            return
        self._submit_claimed_task(claimed)

    def stop(self, wait: bool = True) -> None:
        logger.info("Stopping TaskProcessPool (wait=%s)...", wait)
        self._stop_event.set()
        if self._poller is not None:
            self._poller.join(timeout=max(self._poll_interval_seconds * 2, 1.0))
            self._poller = None
        if self._executor is None:
            return
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        self._executor = None
        logger.info("TaskProcessPool stopped successfully.")

    def _poll_loop(self) -> None:
        logger.debug("Task poller thread started.")
        while not self._stop_event.is_set():
            try:
                self._dispatch_available_work()
            except Exception as exc:
                logger.exception("Unexpected error in poll loop: %s", exc)
            finally:
                self._stop_event.wait(self._poll_interval_seconds)
        logger.debug("Task poller thread exiting.")

    def _dispatch_available_work(self) -> None:
        available_slots, running_backups, busy_vm_keys = self._capacity_snapshot()
        if available_slots <= 0:
            return
        # Pull a window wide enough to skip over backups that are at the cap (or
        # whose VM is busy) and still reach any queued restores/light tasks behind
        # them.
        queued_tasks = load_queued_tasks(limit=self._worker_count)
        if queued_tasks:
            logger.debug("Found %d queued task(s). Attempting to dispatch...", len(queued_tasks))

        for queued_task in queued_tasks:
            if available_slots <= 0:
                break
            task_id = queued_task.get("id")
            if not isinstance(task_id, str):
                continue
            task_type = str(queued_task.get("task_type", ""))
            vm_key = vm_key_for_task(queued_task) if task_type == "backup" else None
            if task_type == "backup":
                if running_backups >= self._max_concurrent_backups:
                    # Leave it queued; a later poll picks it up once a slot frees.
                    logger.debug(
                        "Backup task '%s' held: %d/%d backups already running.",
                        task_id, running_backups, self._max_concurrent_backups,
                    )
                    continue
                if vm_key is not None and vm_key in busy_vm_keys:
                    # Another backup of this VM is already in flight. Running both
                    # at once corrupts the snapshot tree / exhausts the VM's lease
                    # limit and hangs both. Hold this one until the first finishes.
                    logger.info(
                        "Backup task '%s' held: VM %s already has a backup running.",
                        task_id, vm_key,
                    )
                    continue
            claimed = claim_task(task_id)
            if claimed is None:
                continue
            if task_type == "backup":
                claimed = _refresh_backup_decision(claimed)
            self._submit_claimed_task(claimed)
            available_slots -= 1
            if task_type == "backup":
                running_backups += 1
                if vm_key is not None:
                    busy_vm_keys.add(vm_key)

    def _capacity_snapshot(self) -> tuple[int, int, set[tuple[str, str]]]:
        with self._futures_lock:
            available = self._worker_count - len(self._futures)
            running_backups = 0
            busy_vm_keys: set[tuple[str, str]] = set()
            for (_tid, ttype, vm_key) in self._futures.values():
                if ttype == "backup":
                    running_backups += 1
                    if vm_key is not None:
                        busy_vm_keys.add(vm_key)
        return available, running_backups, busy_vm_keys

    def _available_slots(self) -> int:
        with self._futures_lock:
            return self._worker_count - len(self._futures)

    def _submit_claimed_task(self, task: dict[str, object]) -> None:
        if self._executor is None:
            raise RuntimeError("Task pool must be started before submitting work")

        task_id = str(task["id"])
        task_type = str(task.get("task_type", ""))
        vm_key = vm_key_for_task(task) if task_type == "backup" else None
        snapshot = dict(task)
        snapshot["status"] = "queued"
        logger.info("Dispatching task '%s' to worker pool.", task_id)

        future = self._executor.submit(
            run_task,
            snapshot,
            self._simulated_seconds,
            lambda update: self._report_progress(task_id, update),
        )
        with self._futures_lock:
            self._futures[future] = (task_id, task_type, vm_key)
        future.add_done_callback(self._complete_task)

    def _report_progress(self, task_id: str, update: dict[str, object]) -> None:
        with TASK_LOCK:
            task = TASKS.get(task_id)
            if not task:
                return
            cancel_requested = bool(task.get("cancel_requested"))
            if not cancel_requested:
                task["status"] = "running"
            update = dict(update)
            log_entry = update.pop("log_entry", True)
            current_result = task.get("result")
            if not isinstance(current_result, dict):
                current_result = {}
            current_result.update(update)
            task["result"] = current_result
            logs = task.get("logs") if isinstance(task.get("logs"), list) else []
            if update.get("message") and log_entry:
                logs.append(
                    {
                        "timestamp": utc_now(),
                        "level": "info",
                        "component": "engine",
                        "message": str(update.get("message")),
                    }
                )
            task["logs"] = logs
            task["updated_at"] = utc_now()
            try:
                persist_task(dict(task))
            except Exception as exc:
                logger.warning("Failed to persist progress for task '%s': %s", task_id, exc)
        if cancel_requested:
            raise TaskStopRequested("Session stop requested")

    def _complete_task(self, future: Future[dict[str, object]]) -> None:
        try:
            self._finish_task(future)
        finally:
            with self._futures_lock:
                self._futures.pop(future, None)

    def _finish_task(self, future: Future[dict[str, object]]) -> None:
        with self._futures_lock:
            entry = self._futures.get(future)
        if not entry:
            logger.warning("Future completed but no task_id found in tracker.")
            return
        task_id, _task_type, _vm_key = entry

        try:
            outcome = future.result()
            logger.info("Task '%s' finished successfully with status: %s", task_id, outcome.get("status"))
        except Exception as exc:
            logger.exception("Task '%s' crashed with an unhandled exception: %s", task_id, exc)
            with TASK_LOCK:
                task = TASKS.get(task_id)
                if not task:
                    return
                previous_result = task.get("result") if isinstance(task.get("result"), dict) else {}
                task["status"] = "failed"
                task["result"] = {"message": str(exc), "finished_at": utc_now(), "progress": previous_result.get("progress")}
                task["updated_at"] = utc_now()
                try:
                    persist_task(dict(task))
                except Exception as persist_exc:
                    logger.error(
                        "Failed to persist crash outcome for task '%s': %s",
                        task_id,
                        persist_exc,
                    )
            return

        with TASK_LOCK:
            task = TASKS.get(task_id)
            if not task:
                return
            final_status = str(outcome["status"])
            result = dict(outcome["result"]) if isinstance(outcome.get("result"), dict) else {}
            if result.get("progress") is None:
                previous_result = task.get("result") if isinstance(task.get("result"), dict) else {}
                result["progress"] = 100 if final_status == "completed" else previous_result.get("progress")
            task["status"] = final_status
            task["result"] = result
            task["updated_at"] = outcome["updated_at"]
            logs = task.get("logs") if isinstance(task.get("logs"), list) else []
            logs.append(
                {
                    "timestamp": utc_now(),
                    "level": "info" if final_status == "completed" else "warn" if final_status == "stopped" else "error",
                    "component": "engine",
                    "message": str(result.get("message") or f"Task {final_status}"),
                }
            )
            task["logs"] = logs
            try:
                persist_task(dict(task))
                created_point = register_backup_from_completed_task(task)
                if isinstance(created_point, dict) and isinstance(task.get("result"), dict):
                    task["result"]["restore_point_id"] = created_point.get("id")
                    task["result"]["backup_id"] = created_point.get("chain_id")
                if created_point or task.get("warnings"):
                    persist_task(dict(task))
            except Exception as exc:
                logger.error("Failed to persist final outcome for task '%s': %s", task_id, exc)
