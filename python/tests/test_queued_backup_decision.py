# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from concurrent.futures import Future
from unittest.mock import patch

from python.srv.core import task_pool as tp_mod
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.services import abr_v41


JOB_ID = "job-1"
VM_ID = "vm-1"
REPO_ID = "repo-1"


def _task(execution_type: str, execution_reason: str, **extra_parameters: object) -> dict:
    parameters = {
        "repository_id": REPO_ID,
        "vm_name": "vm",
        "resolved_backup_type": execution_type,
        "resolved_backup_reason": execution_reason,
        "backup_mode_request": "full" if execution_type in {"ActiveFull", "SyntheticFull"} else "auto",
        "incremental": execution_type == "Incremental",
        "synthetic_full": execution_type == "SyntheticFull",
        "active_full": execution_type == "ActiveFull",
        **extra_parameters,
    }
    return {
        "id": "task-2",
        "job_id": JOB_ID,
        "task_type": "backup",
        "resource_type": "virtual_machine",
        "resource_external_id": VM_ID,
        "status": "running",
        "parameters": parameters,
        "result": {"progress": 0, "resolved_backup_type": execution_type, "resolved_backup_reason": execution_reason},
        "logs": [],
    }


class RefreshQueuedBackupDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        JOBS[JOB_ID] = {
            "id": JOB_ID,
            "name": "j1",
            "job_type": "backup",
            "repo_id": REPO_ID,
            "retention_days": 7,
            "active_full_days": [],
            "synthetic_full_days": [],
        }
        persist = patch.object(abr_v41, "persist_task")
        persist.start()
        self.addCleanup(persist.stop)

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def _seed_full(self) -> None:
        BACKUPS["full-1"] = abr_v41.normalize_backup_point(
            {
                "id": "full-1",
                "job_id": JOB_ID,
                "vm_id": VM_ID,
                "vm_name": "vm",
                "repository_id": REPO_ID,
                "status": "available",
                "full_backup": True,
                "execution_type": "ActiveFull",
                "execution_reason": "FirstRun",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )

    def test_a_first_run_queued_behind_the_first_full_runs_as_an_incremental(self) -> None:
        self._seed_full()

        task = abr_v41.refresh_queued_backup_decision(_task("ActiveFull", "FirstRun"))

        parameters = task["parameters"]
        self.assertEqual((parameters["resolved_backup_type"], parameters["resolved_backup_reason"]), ("Incremental", "StandardIncremental"))
        self.assertEqual(
            (parameters["incremental"], parameters["active_full"], parameters["synthetic_full"], parameters["backup_mode_request"]),
            (True, False, False, "auto"),
        )
        self.assertEqual(task["result"]["resolved_backup_type"], "Incremental")
        self.assertIn("queued as ActiveFull (FirstRun)", task["logs"][-1]["message"])

    def test_it_follows_the_full_backup_schedule_of_the_day_it_starts(self) -> None:
        self._seed_full()
        JOBS[JOB_ID]["active_full_days"] = ["sun"]

        with patch.object(abr_v41, "schedule_today_matches_days", side_effect=lambda job, days, now=None: bool(days)):
            task = abr_v41.refresh_queued_backup_decision(_task("ActiveFull", "FirstRun"))

        self.assertEqual(task["parameters"]["resolved_backup_reason"], "ActiveFullWindow")

    def test_a_synthetic_day_still_runs_an_incremental_behind_a_lone_full(self) -> None:
        self._seed_full()
        JOBS[JOB_ID]["synthetic_full_days"] = ["mon"]

        with patch.object(abr_v41, "schedule_today_matches_days", side_effect=lambda job, days, now=None: bool(days)):
            task = abr_v41.refresh_queued_backup_decision(_task("ActiveFull", "FirstRun"))

        self.assertEqual(task["parameters"]["resolved_backup_type"], "Incremental")

    def test_an_incremental_with_no_base_left_runs_as_the_first_full(self) -> None:
        task = abr_v41.refresh_queued_backup_decision(_task("Incremental", "StandardIncremental"))

        self.assertEqual((task["parameters"]["resolved_backup_type"], task["parameters"]["resolved_backup_reason"]), ("ActiveFull", "FirstRun"))
        self.assertTrue(task["parameters"]["active_full"])

    def test_decisions_that_do_not_depend_on_the_chain_are_kept(self) -> None:
        self._seed_full()
        cases = [
            _task("ActiveFull", "ForceFull"),
            _task("SyntheticFull", "ForceSynthetic"),
            _task("SyntheticFull", "SyntheticFullSchedule", requires_task_id="task-1"),
            _task("ActiveFull", "FirstRun", copy_job=True),
            _task("Incremental", "StandardIncremental"),
        ]
        for task in cases:
            before = (task["parameters"]["resolved_backup_type"], task["parameters"]["resolved_backup_reason"])
            refreshed = abr_v41.refresh_queued_backup_decision(task)
            self.assertEqual(
                (refreshed["parameters"]["resolved_backup_type"], refreshed["parameters"]["resolved_backup_reason"]),
                before,
            )
            self.assertEqual(refreshed["logs"], [])

    def test_a_first_run_with_the_chain_still_empty_stays_a_first_run(self) -> None:
        task = abr_v41.refresh_queued_backup_decision(_task("ActiveFull", "FirstRun"))

        self.assertEqual(task["parameters"]["resolved_backup_reason"], "FirstRun")
        self.assertEqual(task["logs"], [])

    def test_restores_and_tasks_without_a_job_are_left_alone(self) -> None:
        restore = {"id": "r1", "task_type": "restore", "parameters": {"point_id": "p"}}
        orphan = {**_task("ActiveFull", "FirstRun"), "job_id": None}

        self.assertIs(abr_v41.refresh_queued_backup_decision(restore), restore)
        self.assertEqual(abr_v41.refresh_queued_backup_decision(orphan)["parameters"]["resolved_backup_reason"], "FirstRun")


class PoolDispatchTests(unittest.TestCase):
    def _pool(self) -> tuple[tp_mod.TaskProcessPool, list[dict]]:
        pool = tp_mod.TaskProcessPool(worker_count=2, max_concurrent_backups=2)
        submitted: list[dict] = []
        pool._submit_claimed_task = submitted.append  # type: ignore[method-assign]
        return pool, submitted

    def _queue(self, tasks: list[dict]) -> None:
        by_id = {task["id"]: task for task in tasks}
        for name, value in (
            ("load_queued_tasks", lambda limit=None: list(tasks)),
            ("claim_task", lambda task_id: {**by_id[task_id], "status": "running"}),
        ):
            original = getattr(tp_mod, name)
            setattr(tp_mod, name, value)
            self.addCleanup(setattr, tp_mod, name, original)

    def test_a_backup_is_re_decided_when_it_is_claimed_and_a_restore_is_not(self) -> None:
        self._queue([{"id": "b1", "task_type": "backup"}, {"id": "r1", "task_type": "restore"}])
        pool, submitted = self._pool()

        with patch(
            "python.srv.services.abr_v41.refresh_queued_backup_decision",
            side_effect=lambda task: {**task, "refreshed": True},
        ) as refresh:
            pool._dispatch_available_work()

        self.assertEqual(refresh.call_count, 1)
        self.assertEqual([(task["id"], task.get("refreshed")) for task in submitted], [("b1", True), ("r1", None)])

    def test_a_failing_re_check_lets_the_backup_run_as_queued(self) -> None:
        self._queue([{"id": "b1", "task_type": "backup", "job_id": JOB_ID}])
        pool, submitted = self._pool()

        with patch("python.srv.services.abr_v41.refresh_queued_backup_decision", side_effect=RuntimeError("catalog down")):
            with self.assertLogs(tp_mod.logger, level="ERROR"):
                pool._dispatch_available_work()

        self.assertEqual([task["id"] for task in submitted], ["b1"])

    def test_the_vm_stays_busy_until_the_finished_backup_has_its_point(self) -> None:
        pool = tp_mod.TaskProcessPool(worker_count=2, max_concurrent_backups=2)
        future: Future = Future()
        future.set_result({"task_id": "b1", "status": "completed", "result": {}, "updated_at": "2026-01-01T00:00:00+00:00"})
        pool._futures[future] = ("b1", "backup", ("inv-1", VM_ID))
        TASKS["b1"] = {"id": "b1", "task_type": "backup", "status": "running", "result": {}, "logs": []}
        self.addCleanup(TASKS.clear)
        busy_while_registering: list[bool] = []

        def register(task):
            busy_while_registering.append(("inv-1", VM_ID) in pool._capacity_snapshot()[2])
            return None

        with patch.object(tp_mod, "persist_task"), patch.object(tp_mod, "register_backup_from_completed_task", side_effect=register):
            pool._complete_task(future)

        self.assertEqual(busy_while_registering, [True])
        self.assertNotIn(future, pool._futures)


if __name__ == "__main__":
    unittest.main()
