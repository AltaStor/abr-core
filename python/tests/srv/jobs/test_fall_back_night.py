# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core import job_scheduler
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
from python.srv.services.abr_v41 import normalize_job_record, schedule_next_run

KYIV = "Europe/Kyiv"
NEW_YORK = "America/New_York"
SCHEDULER_LOG = "python.srv.core.job_scheduler"


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def _daily(time_of_day: str, zone: str = KYIV) -> dict:
    return {"type": "daily", "time": time_of_day, "timezone": zone}


def _sunday(time_of_day: str, zone: str = KYIV) -> dict:
    return {"type": "weekly", "days": ["sun"], "time": time_of_day, "timezone": zone}


def _cron(expression: str, zone: str = KYIV) -> dict:
    return {"type": "cron", "expression": expression, "timezone": zone}


class FallBackNightTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS, BACKUPS, REPOSITORIES):
            store.clear()
            self.addCleanup(store.clear)
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": "2026-10-01T00:00:00+00:00",
            }
        }
        self.clock = _utc(2026, 10, 24, 12, 0)
        for module in (job_scheduler, abr_v41):
            clock_patch = patch.object(module, "utc_now", side_effect=lambda: self.clock.isoformat())
            clock_patch.start()
            self.addCleanup(clock_patch.stop)
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)

    def _register_job(self, job_id: str, schedule: dict, active_full_schedule: dict | None = None, **overrides) -> None:
        REPOSITORIES[f"repo-{job_id}"] = {"id": f"repo-{job_id}", "name": f"Repo {job_id}", "type": "local", "path": f"/tmp/{job_id}"}
        JOBS[job_id] = {
            "id": job_id,
            "name": job_id,
            "job_type": "backup",
            "inventory_id": "hv-1",
            "hypervisor_id": "hv-1",
            "status": "active",
            "repo_id": f"repo-{job_id}",
            "repo_name": f"Repo {job_id}",
            "vm_ids": ["vm-1"],
            "vm_names": ["demo-vm"],
            "metadata": {"vm_external_id": "vm-1"},
            "schedule": schedule,
            "created_at": "2026-10-11T09:00:00+00:00",
            **overrides,
        }
        if active_full_schedule is not None:
            JOBS[job_id]["active_full_schedule"] = active_full_schedule
        normalized = normalize_job_record(JOBS[job_id])
        if "next_incremental_run_at" not in overrides:
            JOBS[job_id]["next_incremental_run_at"] = schedule_next_run(normalized["schedule"], after=self.clock).isoformat()
        if active_full_schedule is not None:
            JOBS[job_id]["active_full_next_run_at"] = schedule_next_run(
                normalized["active_full_schedule"], after=self.clock
            ).isoformat()

    def _run_night(self, start: datetime, end: datetime, *extra_moments: datetime) -> list[str]:
        moments = set(extra_moments)
        moment = start
        while moment <= end:
            moments.add(moment)
            moment += timedelta(minutes=1)
        with self.assertLogs(SCHEDULER_LOG, level="WARNING") as logs:
            for moment in sorted(moments):
                self.clock = moment
                self.scheduler._dispatch_due_jobs()
                for task in TASKS.values():
                    task["status"] = "completed"
            logging.getLogger(SCHEDULER_LOG).warning("end of the night")
        return [line for line in logs.output if "clock was ahead" in line]

    @staticmethod
    def _runs(job_id: str) -> list[tuple[str, str]]:
        tasks = sorted((task for task in TASKS.values() if task.get("job_id") == job_id), key=lambda task: task["created_at"])
        return [(task["created_at"], task["parameters"]["resolved_backup_type"]) for task in tasks]

    def test_every_job_in_the_repeated_hour_runs_once_and_nothing_is_logged_about_the_clock(self) -> None:
        self._register_job("daily", _daily("03:10"), _sunday("03:10"))
        self._register_job("weekly", _sunday("03:30"), _sunday("03:30"))
        self._register_job("cron", _cron("50 3 * * *"), _sunday("03:50"))

        clock_lines = self._run_night(
            _utc(2026, 10, 24, 22, 0), _utc(2026, 10, 25, 3, 0), _utc(2026, 10, 25, 1, 0, 1, 700121)
        )

        self.assertEqual(clock_lines, [])
        self.assertEqual(self._runs("daily"), [("2026-10-25T00:10:00+00:00", "ActiveFull")])
        self.assertEqual(self._runs("weekly"), [("2026-10-25T00:30:00+00:00", "ActiveFull")])
        self.assertEqual(self._runs("cron"), [("2026-10-25T00:50:00+00:00", "ActiveFull")])

    def test_after_the_night_the_next_runs_are_the_next_day_and_the_next_week(self) -> None:
        self._register_job("daily", _daily("03:10"), _sunday("03:10"))
        self._register_job("weekly", _sunday("03:30"), _sunday("03:30"))
        self._register_job("cron", _cron("50 3 * * *"), _sunday("03:50"))

        self._run_night(_utc(2026, 10, 24, 22, 0), _utc(2026, 10, 25, 3, 0))

        expected = {
            "daily": ("2026-10-26T01:10:00+00:00", "2026-11-01T01:10:00+00:00"),
            "weekly": ("2026-11-01T01:30:00+00:00", "2026-11-01T01:30:00+00:00"),
            "cron": ("2026-10-26T01:50:00+00:00", "2026-11-01T01:50:00+00:00"),
        }
        for job_id, (incremental, active_full) in expected.items():
            with self.subTest(job=job_id):
                self.assertEqual(JOBS[job_id]["next_incremental_run_at"], incremental)
                self.assertEqual(JOBS[job_id]["active_full_next_run_at"], active_full)

    def test_the_offset_change_leaves_the_stored_next_runs_as_they_are(self) -> None:
        self._register_job("daily", _daily("03:10"), _sunday("03:10"))
        self._register_job("weekly", _sunday("03:30"), _sunday("03:30"))
        self._register_job("cron", _cron("50 3 * * *"), _sunday("03:50"))
        self._run_night(_utc(2026, 10, 24, 22, 0), _utc(2026, 10, 25, 0, 59))
        stored = {
            job_id: (JOBS[job_id]["next_incremental_run_at"], JOBS[job_id]["active_full_next_run_at"])
            for job_id in ("daily", "weekly", "cron")
        }

        self._run_night(_utc(2026, 10, 25, 1, 0, 1, 700121), _utc(2026, 10, 25, 1, 0, 1, 700121))

        for job_id, values in stored.items():
            with self.subTest(job=job_id):
                self.assertEqual(
                    (JOBS[job_id]["next_incremental_run_at"], JOBS[job_id]["active_full_next_run_at"]), values
                )

    def test_a_us_time_zone_runs_its_repeated_hour_job_once(self) -> None:
        self.clock = _utc(2026, 10, 31, 12, 0)
        self._register_job("daily", _daily("01:30", NEW_YORK))

        clock_lines = self._run_night(_utc(2026, 11, 1, 3, 0), _utc(2026, 11, 1, 8, 0))

        self.assertEqual(clock_lines, [])
        self.assertEqual(self._runs("daily"), [("2026-11-01T05:30:00+00:00", "ActiveFull")])

    def test_the_spring_forward_night_still_runs_once(self) -> None:
        self.clock = _utc(2026, 3, 28, 12, 0)
        self._register_job("daily", _daily("03:30"))

        clock_lines = self._run_night(_utc(2026, 3, 28, 22, 0), _utc(2026, 3, 29, 3, 0))

        self.assertEqual(clock_lines, [])
        self.assertEqual(len(self._runs("daily")), 1)

    def test_a_job_created_in_the_repeated_hour_still_runs_at_its_second_pass(self) -> None:
        self.clock = _utc(2026, 10, 25, 1, 5)
        self._register_job("daily", _daily("03:30"), created_at=self.clock.isoformat())
        JOBS["daily"].pop("next_incremental_run_at")

        clock_lines = self._run_night(_utc(2026, 10, 25, 1, 5), _utc(2026, 10, 25, 3, 0))

        self.assertEqual(clock_lines, [])
        self.assertEqual(self._runs("daily"), [("2026-10-25T01:30:00+00:00", "ActiveFull")])

    def test_a_next_run_a_wrong_clock_pushed_away_is_still_recomputed_in_the_repeated_hour(self) -> None:
        self._register_job("daily", _daily("03:10"), next_incremental_run_at="2026-11-05T01:10:00+00:00")

        clock_lines = self._run_night(_utc(2026, 10, 25, 1, 5), _utc(2026, 10, 25, 1, 5))

        self.assertEqual(len(clock_lines), 1)
        self.assertIn("2026-11-05T01:10:00+00:00", clock_lines[0])
        self.assertEqual(JOBS["daily"]["next_incremental_run_at"], "2026-10-25T01:10:00+00:00")


if __name__ == "__main__":
    unittest.main()
