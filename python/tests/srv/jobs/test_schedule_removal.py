# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41

LONG_FULL = {"type": "periodic", "every": "3650d", "interval_minutes": 5256000, "enabled": True, "timezone": "UTC"}


def _due_daily() -> dict:
    moment = datetime.now(timezone.utc) - timedelta(minutes=1)
    return {"type": "daily", "time": moment.strftime("%H:%M"), "timezone": "UTC", "enabled": True}


class ScheduleRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS, BACKUPS, REPOSITORIES):
            store.clear()
            self.addCleanup(store.clear)
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo", "type": "local", "path": "/tmp/repo-1"}

    def register(self, **overrides) -> dict:
        schedule = {"type": "daily", "time": "23:00", "timezone": "UTC", "enabled": True}
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "nightly",
            "job_type": "backup",
            "inventory_id": "hv-1",
            "hypervisor_id": "hv-1",
            "repo_id": "repo-1",
            "repo_name": "Repo",
            "vm_ids": ["vm-1"],
            "vm_names": ["demo-vm"],
            "metadata": {"vm_external_id": "vm-1"},
            "task_parameters": {"compression_level": 3},
            "schedule": schedule,
            "schedule_summary": abr_v41._render_schedule_summary(schedule),
            "next_incremental_run_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "enabled": True,
            "status": "active",
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
            **overrides,
        }
        return JOBS["job-1"]

    def dispatch(self) -> list[str]:
        self.scheduler._dispatch_due_jobs()
        return [task["parameters"]["resolved_backup_type"] for task in sorted(TASKS.values(), key=lambda task: str(task.get("created_at")))]

    def test_null_removes_the_schedule_and_keeps_the_full_schedule(self) -> None:
        full_next = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        self.register(active_full_schedule=dict(LONG_FULL), active_full_next_run_at=full_next)

        job = abr_v41.edit_job_record("job-1", {"schedule": None})

        self.assertIsNone(job["schedule"])
        self.assertIsNone(job.get("schedule_summary"))
        self.assertIsNone(job["next_incremental_run_at"])
        self.assertTrue(job["enabled"])
        self.assertEqual(job["status"], "active")
        self.assertEqual(job["active_full_schedule"]["every"], "3650d")
        self.assertEqual(JOBS["job-1"]["active_full_next_run_at"], full_next)

    def test_a_job_left_without_any_schedule_is_planned_like_a_new_manual_job(self) -> None:
        self.register()

        job = abr_v41.edit_job_record("job-1", {"schedule": None})

        self.assertEqual((job["status"], job["enabled"]), ("planned", True))

    def test_removing_it_again_is_accepted(self) -> None:
        self.register(schedule=None, schedule_summary=None, next_incremental_run_at=None)

        job = abr_v41.edit_job_record("job-1", {"schedule": None})

        self.assertIsNone(job["schedule"])

    def test_an_omitted_schedule_is_left_as_it_is(self) -> None:
        self.register()

        job = abr_v41.edit_job_record("job-1", {"description": "kept"})

        self.assertEqual(job["schedule"]["time"], "23:00")

    def test_removed_together_with_switching_the_job_off(self) -> None:
        self.register()

        job = abr_v41.edit_job_record("job-1", {"schedule": None, "enabled": False})

        self.assertIsNone(job["schedule"])
        self.assertEqual((job["enabled"], job["status"]), (False, "disabled"))

    def test_a_disabled_job_reads_disabled_whatever_schedules_it_has(self) -> None:
        full_next = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
        cases = (
            ({}, True, "enabled"),
            ({}, False, "disabled"),
            ({"schedule": None, "schedule_summary": None, "next_incremental_run_at": None, "active_full_schedule": dict(LONG_FULL), "active_full_next_run_at": full_next}, True, "manual"),
            ({"schedule": None, "schedule_summary": None, "next_incremental_run_at": None, "active_full_schedule": dict(LONG_FULL), "active_full_next_run_at": full_next}, False, "disabled"),
            ({"schedule": None, "schedule_summary": None, "next_incremental_run_at": None}, False, "disabled"),
        )
        for overrides, enabled, state in cases:
            with self.subTest(overrides=sorted(overrides), enabled=enabled):
                self.register(**overrides, enabled=enabled)

                listed = next(job for job in abr_v41.list_jobs() if job["id"] == "job-1")

                self.assertEqual(listed["schedule_state"], state)

    def test_switching_a_full_only_job_off_and_on_moves_its_state(self) -> None:
        self.register(schedule=None, schedule_summary=None, next_incremental_run_at=None, active_full_schedule=dict(LONG_FULL))

        off = abr_v41.edit_job_record("job-1", {"enabled": False})
        on = abr_v41.edit_job_record("job-1", {"enabled": True})

        self.assertEqual((off["schedule_state"], on["schedule_state"]), ("disabled", "manual"))

    def test_a_schedule_set_again_gets_its_next_run(self) -> None:
        self.register()
        abr_v41.edit_job_record("job-1", {"schedule": None})

        job = abr_v41.edit_job_record("job-1", {"schedule": {"type": "daily", "time": "22:00", "timezone": "UTC"}})

        self.assertEqual(job["schedule"]["time"], "22:00")
        self.assertIsNotNone(JOBS["job-1"]["next_incremental_run_at"])
        self.assertEqual((job["enabled"], job["status"]), (True, "active"))

    def test_the_scheduler_no_longer_runs_a_removed_due_schedule(self) -> None:
        overdue = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.register(schedule=_due_daily(), next_incremental_run_at=overdue)
        abr_v41.edit_job_record("job-1", {"schedule": None})

        self.assertEqual(self.dispatch(), [])

    def test_a_due_full_schedule_still_runs_after_the_schedule_is_removed(self) -> None:
        overdue = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.register(active_full_schedule=_due_daily(), active_full_next_run_at=overdue)
        abr_v41.edit_job_record("job-1", {"schedule": None})

        self.assertEqual(self.dispatch(), ["ActiveFull"])


if __name__ == "__main__":
    unittest.main()
