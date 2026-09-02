# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Tests for the resource-exhaustion fix: bounded backup concurrency, capped
subprocess capture, and jittered DB reconnect backoff.

See the incident where two concurrent backups took the (small, MongoDB-co-located)
host to 100% CPU/RAM and crashed the service.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import Future

try:
    from python.srv.core import task_pool as tp_mod
    from python.srv.core.subprocess_util import run_capped
    from python.srv.db import db_manager as dm
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.core import task_pool as tp_mod
    from python.srv.core.subprocess_util import run_capped
    from python.srv.db import db_manager as dm


class BackupConcurrencyCapTest(unittest.TestCase):
    """The pool must never run more than MAX_CONCURRENT_BACKUPS backups at once,
    while still letting lighter tasks use the spare worker slots."""

    def _make_pool(self, queued, max_concurrent_backups):
        pool = tp_mod.TaskProcessPool(
            worker_count=4, max_concurrent_backups=max_concurrent_backups
        )
        dispatched: list[str] = []

        def fake_submit(task):
            # Mimic the real dispatch bookkeeping without spawning a worker.
            task_id = str(task["id"])
            task_type = str(task.get("task_type", ""))
            vm_key = tp_mod.vm_key_for_task(task) if task_type == "backup" else None
            dispatched.append(task_id)
            pool._futures[Future()] = (task_id, task_type, vm_key)

        pool._submit_claimed_task = fake_submit  # type: ignore[method-assign]
        return pool, dispatched

    def _patch_queue(self, queued):
        by_id = {q["id"]: q for q in queued}
        orig_load = tp_mod.load_queued_tasks
        orig_claim = tp_mod.claim_task
        tp_mod.load_queued_tasks = lambda limit=None: list(queued)
        tp_mod.claim_task = lambda tid: {**by_id[tid], "status": "running"} if tid in by_id else None
        self.addCleanup(setattr, tp_mod, "load_queued_tasks", orig_load)
        self.addCleanup(setattr, tp_mod, "claim_task", orig_claim)

    def test_second_backup_stays_queued(self):
        queued = [
            {"id": "b1", "task_type": "backup"},
            {"id": "b2", "task_type": "backup"},
        ]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=1)

        pool._dispatch_available_work()

        # Only the first backup runs; the second is held even though 3 worker
        # slots are free — this is the core protection.
        self.assertEqual(dispatched, ["b1"])

    def test_restore_uses_spare_slot_while_backup_capped(self):
        queued = [
            {"id": "b1", "task_type": "backup"},
            {"id": "b2", "task_type": "backup"},
            {"id": "r1", "task_type": "restore"},
        ]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=1)

        pool._dispatch_available_work()

        # b1 takes the single backup slot, b2 is held, r1 (light) still runs.
        self.assertEqual(dispatched, ["b1", "r1"])

    def test_queue_drains_after_backup_finishes(self):
        queued = [{"id": "b1", "task_type": "backup"}, {"id": "b2", "task_type": "backup"}]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=1)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1"])

        # Simulate b1 completing: clear its future, drop it from the queue.
        pool._futures.clear()
        queued[:] = [{"id": "b2", "task_type": "backup"}]
        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1", "b2"])

    def test_higher_cap_allows_two_backups(self):
        queued = [{"id": "b1", "task_type": "backup"}, {"id": "b2", "task_type": "backup"}]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=2)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1", "b2"])

    def test_default_cap_equals_worker_count(self):
        pool = tp_mod.TaskProcessPool(worker_count=3)
        self.assertEqual(pool._max_concurrent_backups, 3)

    def test_invalid_cap_rejected(self):
        with self.assertRaises(ValueError):
            tp_mod.TaskProcessPool(worker_count=2, max_concurrent_backups=0)


class SameVmSerializationTest(unittest.TestCase):
    """Two backups of the SAME VM must never run at once even when backup slots
    are free — concurrent snapshots/leases on one VM hang both sessions."""

    def _make_pool(self, queued, max_concurrent_backups):
        pool = tp_mod.TaskProcessPool(
            worker_count=4, max_concurrent_backups=max_concurrent_backups
        )
        dispatched: list[str] = []

        def fake_submit(task):
            task_id = str(task["id"])
            task_type = str(task.get("task_type", ""))
            vm_key = tp_mod.vm_key_for_task(task) if task_type == "backup" else None
            dispatched.append(task_id)
            pool._futures[Future()] = (task_id, task_type, vm_key)

        pool._submit_claimed_task = fake_submit  # type: ignore[method-assign]
        return pool, dispatched

    def _patch_queue(self, queued):
        by_id = {q["id"]: q for q in queued}
        orig_load = tp_mod.load_queued_tasks
        orig_claim = tp_mod.claim_task
        tp_mod.load_queued_tasks = lambda limit=None: list(queued)
        tp_mod.claim_task = lambda tid: {**by_id[tid], "status": "running"} if tid in by_id else None
        self.addCleanup(setattr, tp_mod, "load_queued_tasks", orig_load)
        self.addCleanup(setattr, tp_mod, "claim_task", orig_claim)

    @staticmethod
    def _backup(task_id, vm_id, hv="hv1"):
        return {
            "id": task_id,
            "task_type": "backup",
            "hypervisor_id": hv,
            "resource_external_id": vm_id,
        }

    def test_second_backup_of_same_vm_is_held(self):
        # Two jobs target the same VM. Even with 4 backup slots free, only one
        # backup of that VM may run; the duplicate stays queued.
        queued = [self._backup("b1", "vm-100"), self._backup("b2", "vm-100")]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=4)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1"])

    def test_different_vms_run_in_parallel(self):
        # Distinct VMs are independent: per-VM serialization must not block them.
        queued = [self._backup("b1", "vm-100"), self._backup("b2", "vm-200")]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=4)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1", "b2"])

    def test_same_vm_different_hypervisor_not_confused(self):
        # Same VM id on two different inventories are different machines.
        queued = [
            self._backup("b1", "vm-100", hv="hvA"),
            self._backup("b2", "vm-100", hv="hvB"),
        ]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=4)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1", "b2"])

    def test_held_duplicate_dispatches_after_first_completes(self):
        queued = [self._backup("b1", "vm-100"), self._backup("b2", "vm-100")]
        self._patch_queue(queued)
        pool, dispatched = self._make_pool(queued, max_concurrent_backups=4)

        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1"])

        # b1 finishes and leaves the queue; the duplicate is now free to run.
        pool._futures.clear()
        queued[:] = [self._backup("b2", "vm-100")]
        pool._dispatch_available_work()
        self.assertEqual(dispatched, ["b1", "b2"])

    def test_vm_key_for_task(self):
        self.assertEqual(
            tp_mod.vm_key_for_task(self._backup("b1", "vm-1", hv="hv9")),
            ("hv9", "vm-1"),
        )
        # No external id → no key (cannot serialize what we can't identify).
        self.assertIsNone(tp_mod.vm_key_for_task({"id": "x", "task_type": "backup"}))


class TaskCompletionProgressTest(unittest.TestCase):
    def setUp(self):
        self.pool = tp_mod.TaskProcessPool(worker_count=1)
        self.task_id = "t1"
        tp_mod.TASKS[self.task_id] = {
            "id": self.task_id,
            "task_type": "backup",
            "status": "running",
            "result": {"progress": 61},
        }
        self.addCleanup(tp_mod.TASKS.pop, self.task_id, None)

    def _resolve(self, outcome):
        future: Future = Future()
        future.set_result(outcome)
        self.pool._futures[future] = (self.task_id, "backup", None)
        self.pool._complete_task(future)

    def test_completed_task_gets_progress_defaulted_to_100(self):
        self._resolve({"task_id": self.task_id, "status": "completed", "result": {"message": "done"}, "updated_at": "now"})
        self.assertEqual(tp_mod.TASKS[self.task_id]["result"]["progress"], 100)

    def test_stopped_task_keeps_last_known_progress_instead_of_null(self):
        self._resolve({"task_id": self.task_id, "status": "stopped", "result": {"message": "Session stop requested"}, "updated_at": "now"})
        self.assertEqual(tp_mod.TASKS[self.task_id]["result"]["progress"], 61)

    def test_failed_task_keeps_last_known_progress_instead_of_null(self):
        self._resolve({"task_id": self.task_id, "status": "failed", "result": {"message": "boom"}, "updated_at": "now"})
        self.assertEqual(tp_mod.TASKS[self.task_id]["result"]["progress"], 61)

    def test_explicit_progress_from_outcome_is_not_overridden(self):
        self._resolve({"task_id": self.task_id, "status": "completed", "result": {"message": "done", "progress": 42}, "updated_at": "now"})
        self.assertEqual(tp_mod.TASKS[self.task_id]["result"]["progress"], 42)

    def test_unhandled_crash_also_keeps_last_known_progress(self):
        future: Future = Future()
        future.set_exception(RuntimeError("worker crashed"))
        self.pool._futures[future] = (self.task_id, "backup", None)
        self.pool._complete_task(future)
        self.assertEqual(tp_mod.TASKS[self.task_id]["result"]["progress"], 61)


class ProgressTickLoggingTest(unittest.TestCase):
    def setUp(self):
        self.pool = tp_mod.TaskProcessPool(worker_count=1)
        self.task_id = "t-progress"
        tp_mod.TASKS[self.task_id] = {"id": self.task_id, "task_type": "backup", "status": "running", "logs": []}
        self.addCleanup(tp_mod.TASKS.pop, self.task_id, None)

    def test_progress_tick_updates_live_state_without_growing_logs(self):
        self.pool._report_progress(self.task_id, {"message": "Backing up disk 1/1: 10%", "progress": 40, "log_entry": False})
        self.pool._report_progress(self.task_id, {"message": "Backing up disk 1/1: 55%", "progress": 55, "log_entry": False})

        task = tp_mod.TASKS[self.task_id]
        self.assertEqual(task["result"]["progress"], 55)
        self.assertEqual(task["result"]["message"], "Backing up disk 1/1: 55%")
        self.assertEqual(task["logs"], [])

    def test_milestone_without_the_flag_is_still_logged_as_before(self):
        self.pool._report_progress(self.task_id, {"message": "Creating backup snapshot", "progress": 5})

        task = tp_mod.TASKS[self.task_id]
        self.assertEqual(len(task["logs"]), 1)
        self.assertEqual(task["logs"][0]["message"], "Creating backup snapshot")

    def test_log_entry_flag_itself_does_not_leak_into_result(self):
        self.pool._report_progress(self.task_id, {"message": "tick", "progress": 12, "log_entry": False})

        self.assertNotIn("log_entry", tp_mod.TASKS[self.task_id]["result"])


class CappedSubprocessTest(unittest.TestCase):
    """run_capped must bound captured output regardless of how much the child
    emits, so a chatty mover can't OOM the host."""

    def test_captures_small_output(self):
        result = run_capped(["printf", "hello"], nice=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello")

    def test_caps_large_output(self):
        result = run_capped(
            ["python3", "-c", "print('x' * 1000000)"],
            nice=False,
            max_capture_bytes=2000,
        )
        # Tail kept is bounded even though the child wrote ~1 MB.
        self.assertLessEqual(len(result.stdout), 2000 + 200)
        self.assertIn("output truncated", result.stdout)

    def test_nonzero_exit_preserved(self):
        result = run_capped(["python3", "-c", "import sys; sys.exit(7)"], nice=False)
        self.assertEqual(result.returncode, 7)

    def test_timeout_raises_with_tail(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_capped(
                ["python3", "-c", "import time; time.sleep(5)"],
                timeout=0.3,
                nice=False,
            )

    def test_check_raises_called_process_error(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run_capped(["python3", "-c", "import sys; sys.exit(3)"], check=True, nice=False)


class ReconnectBackoffTest(unittest.TestCase):
    """The DB retry path must back off with jitter (and a cap), not busy-spin.

    No manual reconnect: MongoClient heals its own connection pool once the
    server is reachable again (verified live against a real mongod outage),
    so _retry() just retries the same call on the same client.
    """

    def test_backoff_grows_and_is_jittered(self):
        sleeps: list[float] = []
        orig_sleep = dm.time.sleep
        orig_uniform = dm.random.uniform
        dm.time.sleep = lambda s: sleeps.append(s)
        # Deterministic jitter: always return the upper bound so we can assert growth.
        dm.random.uniform = lambda a, b: b
        self.addCleanup(setattr, dm.time, "sleep", orig_sleep)
        self.addCleanup(setattr, dm.random, "uniform", orig_uniform)

        calls = {"n": 0}

        class FakeDb:
            @dm._retry(attempts=3, base_delay=0.5)
            def op(self):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise dm.ConnectionFailure("connection refused")
                return "ok"

        self.assertEqual(FakeDb().op(), "ok")
        self.assertEqual(calls["n"], 3)
        # Each retry sleeps (no busy-spin) and the delay grows: 0.5 -> 1.0.
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_backoff_is_capped(self):
        sleeps: list[float] = []
        orig_sleep = dm.time.sleep
        orig_uniform = dm.random.uniform
        dm.time.sleep = lambda s: sleeps.append(s)
        dm.random.uniform = lambda a, b: b  # upper bound == capped delay
        self.addCleanup(setattr, dm.time, "sleep", orig_sleep)
        self.addCleanup(setattr, dm.random, "uniform", orig_uniform)

        class FakeDb:
            @dm._retry(attempts=6, base_delay=10.0)
            def op(self):
                raise dm.ConnectionFailure("down")

        with self.assertRaises(dm.ConnectionFailure):
            FakeDb().op()
        # Delay must never exceed the configured cap.
        self.assertTrue(all(s <= dm._RETRY_MAX_DELAY for s in sleeps), sleeps)

    def test_retrying_never_touches_self_client_or_self_db(self):
        calls = {"n": 0}

        class FakeDb:
            def __init__(self):
                self._client = object()
                self._db = object()

            @dm._retry(attempts=3, base_delay=0.0)
            def op(self):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise dm.ConnectionFailure("blip")
                return "ok"

        db = FakeDb()
        client_before, db_before = db._client, db._db
        self.assertEqual(db.op(), "ok")
        self.assertIs(db._client, client_before)
        self.assertIs(db._db, db_before)


class DatabaseConcurrencyTest(unittest.TestCase):
    def test_retry_wrapped_calls_run_concurrently_on_one_instance(self):
        overlap_detected = threading.Event()
        occupied_count = {"n": 0}
        occupied_lock = threading.Lock()

        class FakeDb:
            @dm._retry(attempts=1, base_delay=0.0)
            def op(self):
                with occupied_lock:
                    occupied_count["n"] += 1
                    if occupied_count["n"] > 1:
                        overlap_detected.set()
                time.sleep(0.05)
                with occupied_lock:
                    occupied_count["n"] -= 1
                return "ok"

        db = FakeDb()
        threads = [threading.Thread(target=db.op) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(
            overlap_detected.is_set(),
            "8 threads never overlapped -- calls are still serialized somewhere",
        )


if __name__ == "__main__":
    unittest.main()
