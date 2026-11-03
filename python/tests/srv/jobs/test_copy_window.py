# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from python.srv.core import task_pool as tp_mod
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.core.task_executor import TaskStopRequested
from python.srv.services import abr_v41, backup_copy
from python.srv.services.api_errors import ApiError

FULL = {"type": "weekly", "days": ["sunday"], "time": "02:00"}
VM = "vm-1"
MONDAY = datetime(2026, 11, 2, tzinfo=timezone.utc)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return MONDAY.replace(day=MONDAY.day + day, hour=hour, minute=minute)


def _job(*rules: str) -> dict:
    window = backup_copy.parse_copy_window(list(rules))
    for rule in window:
        rule["timezone"] = "UTC"
    return {"copy_window": window}


class CopyWindowFormatTests(unittest.TestCase):
    def _refused(self, value: str) -> str:
        with self.assertRaises(ApiError) as caught:
            backup_copy.parse_copy_window(value)
        return caught.exception.message

    def test_days_are_a_comma_list_in_week_order_and_none_means_every_day(self) -> None:
        cases = {
            "mon,tue,wed,thu,fri 22:00-06:00": "Mon, Tue, Wed, Thu, Fri 22:00-06:00",
            "MONDAY,friday 21:00-22:00": "Mon, Fri 21:00-22:00",
            "21:00-22:00": "every day 21:00-22:00",
            "mon,tue,wed,thu,fri,sat,sun 00:00-24:00": "every day 00:00-24:00",
        }
        for rule, described in cases.items():
            with self.subTest(rule=rule):
                self.assertEqual(backup_copy.describe_copy_window(backup_copy.parse_copy_window(rule)), described)

    def test_several_rules_and_no_window(self) -> None:
        rules = backup_copy.parse_copy_window(["mon,tue,wed,thu,fri 22:00-06:00", "sat,sun 00:00-24:00"])

        self.assertEqual(backup_copy.describe_copy_window(rules), "Mon, Tue, Wed, Thu, Fri 22:00-06:00; Sat, Sun 00:00-24:00")
        self.assertEqual(backup_copy.parse_copy_window("none"), [])
        self.assertEqual(backup_copy.parse_copy_window(None), [])
        self.assertEqual(backup_copy.describe_copy_window([]), "any time")

    def test_out_of_order_repeated_ranged_and_unknown_days_are_refused(self) -> None:
        self.assertIn("week order, Monday to Sunday, as in mon,thu", self._refused("thu,mon 21:00-22:00"))
        self.assertIn("Mon is given more than once", self._refused("mon,mon 21:00-22:00"))
        self.assertIn("as in mon,fri,sat,sun", self._refused("fri-mon 22:00-06:00"))
        self.assertIn("Unknown weekday 'mox'", self._refused("mox 21:00-22:00"))

    def test_bad_times_are_refused(self) -> None:
        self.assertIn("not a time", self._refused("mon 25:00-26:00"))
        self.assertIn("end at another time", self._refused("mon 10:00-10:00"))
        self.assertIn("'[days] HH:MM-HH:MM'", self._refused("mon 10:00"))
        self.assertIn("must start before 24:00", self._refused("mon 24:00-01:00"))


class CopyWindowStateTests(unittest.TestCase):
    def test_no_window_is_always_open(self) -> None:
        self.assertEqual(backup_copy.copy_window_state({}, _at(0, 12)), {"open": True, "closes_at": None, "opens_at": None})

    def test_a_night_window_runs_past_midnight_from_the_days_it_starts_on(self) -> None:
        job = _job("mon,tue,wed,thu,fri 22:00-06:00")

        self.assertFalse(backup_copy.copy_window_state(job, _at(0, 12))["open"])
        self.assertEqual(backup_copy.copy_window_state(job, _at(0, 12))["opens_at"], _at(0, 22))
        self.assertTrue(backup_copy.copy_window_state(job, _at(0, 23))["open"])
        self.assertEqual(backup_copy.copy_window_state(job, _at(0, 23))["closes_at"], _at(1, 6))
        self.assertTrue(backup_copy.copy_window_state(job, _at(5, 3))["open"])
        self.assertFalse(backup_copy.copy_window_state(job, _at(6, 3))["open"])

    def test_touching_rules_make_one_longer_window(self) -> None:
        job = _job("sat 00:00-24:00", "sun 00:00-24:00")

        self.assertEqual(backup_copy.copy_window_state(job, _at(5, 10))["closes_at"], _at(7, 0))


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)])
        HYPERVISORS["vc"] = {"id": "vc", "name": "vCenter", "kind": "vcenter", "type": "vsphere"}
        REPOSITORIES["src"] = {"id": "src", "name": "Source", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["dst"] = {"id": "dst", "name": "Offsite", "type": "local", "path": tempfile.mkdtemp()}
        JOBS["nightly"] = abr_v41.normalize_job_record(
            {
                "id": "nightly",
                "name": "Nightly",
                "job_type": "backup",
                "inventory_id": "vc",
                "vm_ids": [VM],
                "vm_names": ["sql-01"],
                "repo_id": "src",
                "repo_name": "Source",
                "task_parameters": {},
            }
        )
        BACKUPS["f1"] = abr_v41.normalize_backup_point(
            {
                "id": "f1",
                "status": "available",
                "job_id": "nightly",
                "job_name": "Nightly",
                "vm_id": VM,
                "vm_name": "sql-01",
                "inventory_id": "vc",
                "repository_id": "src",
                "repository_name": "Source",
                "execution_type": "ActiveFull",
                "full_backup": True,
                "created_at": "2026-11-01T01:00:00+00:00",
                "chain_id": f"backup::nightly::{VM}::src",
                "artifact": {"disks": [{"index": 0, "abr_backup_name": "disk-0.vmdk", "abr_path": "/r/f1"}]},
            }
        )
        self.copy = abr_v41.create_backup_copy_job(
            {
                "name": "Offsite copy",
                "source_jobs": ["Nightly"],
                "repo": "Offsite",
                "active_full_schedule": FULL,
                "copy_window": ["mon 22:00-23:00"],
            }
        )


class CopyWindowJobTests(_Catalog):
    def test_the_window_is_kept_on_the_job_and_shown(self) -> None:
        view = self.copy["backup_copy"]

        self.assertEqual(view["copy_window_summary"], "Mon 22:00-23:00")
        self.assertEqual(
            abr_v41.edit_job_record(self.copy["id"], {"copy_window": None})["backup_copy"]["copy_window_summary"], "any time"
        )

    def test_a_bad_window_is_refused_and_the_job_is_kept(self) -> None:
        with self.assertRaises(ApiError) as caught:
            abr_v41.edit_job_record(self.copy["id"], {"copy_window": ["thu,mon 22:00-23:00"]})

        self.assertEqual(caught.exception.code, "invalid_copy_window")
        self.assertEqual(JOBS[self.copy["id"]]["copy_window"][0]["days"], ["Mon"])

    def test_a_session_queued_outside_the_window_says_when_it_opens(self) -> None:
        with patch.object(backup_copy, "copy_window_state", return_value={"open": False, "closes_at": None, "opens_at": _at(0, 22)}):
            task = backup_copy.create_copy_sessions(JOBS[self.copy["id"]], trigger="manual", include_idle=True)[0]

        self.assertTrue(any("Waits for the copy window (Mon 22:00-23:00): it opens" in log["message"] for log in task["logs"]))

    def test_the_pool_holds_copies_outside_the_window_and_still_runs_the_rest(self) -> None:
        held = {"id": "c1", "task_type": "backup", "job_id": self.copy["id"], "parameters": {"copy_job": True}}
        backups = [{"id": f"b{n}", "task_type": "backup", "job_id": "nightly"} for n in range(3)]
        queue = [held, *backups]
        by_id = {task["id"]: task for task in queue}
        pool = tp_mod.TaskProcessPool(worker_count=2, max_concurrent_backups=2)
        submitted: list[dict] = []
        pool._submit_claimed_task = submitted.append  # type: ignore[method-assign]

        with (
            patch.object(tp_mod, "load_queued_tasks", lambda limit=None: list(queue)),
            patch.object(tp_mod, "claim_task", lambda task_id: {**by_id[task_id], "status": "running"}),
            patch.object(tp_mod, "_refresh_backup_decision", lambda task: task),
            patch.object(backup_copy, "copy_window_state", return_value={"open": False, "closes_at": None, "opens_at": None}),
        ):
            pool._dispatch_available_work()

        self.assertEqual([task["id"] for task in submitted], ["b0", "b1"])

    def test_a_copy_cut_by_the_window_is_queued_again_for_its_next_opening(self) -> None:
        stopped = {
            "id": "c1",
            "job_id": self.copy["id"],
            "task_type": "backup",
            "status": "stopped",
            "resource_external_id": VM,
            "parameters": {"copy_job": True},
            "result": {"stop_reason": backup_copy.WINDOW_STOP_REASON},
        }

        backup_copy.on_session_finished(stopped)

        sessions = [task for task in TASKS.values() if task.get("job_id") == self.copy["id"]]
        self.assertEqual([task["trigger"] for task in sessions], ["copy-window"])

    def test_a_cut_copy_of_a_switched_off_job_is_still_finished(self) -> None:
        abr_v41.edit_job_record(self.copy["id"], {"enabled": False})

        backup_copy.on_session_finished(
            {
                "id": "c1",
                "job_id": self.copy["id"],
                "task_type": "backup",
                "status": "stopped",
                "resource_external_id": VM,
                "parameters": {"copy_job": True},
                "result": {"stop_reason": backup_copy.WINDOW_STOP_REASON},
            }
        )

        self.assertEqual([task["trigger"] for task in TASKS.values() if task.get("job_id") == self.copy["id"]], ["copy-window"])

    def test_a_copy_stopped_by_the_user_is_not_queued_again(self) -> None:
        backup_copy.on_session_finished(
            {
                "id": "c1",
                "job_id": self.copy["id"],
                "task_type": "backup",
                "status": "stopped",
                "resource_external_id": VM,
                "parameters": {"copy_job": True},
                "result": {"stop_reason": None},
            }
        )

        self.assertEqual([task for task in TASKS.values() if task.get("job_id") == self.copy["id"]], [])

    def test_a_run_that_finds_the_window_closed_stops_with_the_reason(self) -> None:
        task = backup_copy.create_copy_sessions(JOBS[self.copy["id"]], trigger="manual", include_idle=True)[0]

        closed = {"open": False, "closes_at": None, "opens_at": _at(0, 22)}
        with patch.object(backup_copy, "copy_window_state", return_value=closed), self.assertRaises(TaskStopRequested) as caught:
            backup_copy.run_backup_copy(task)

        self.assertEqual(caught.exception.reason, backup_copy.WINDOW_STOP_REASON)
        self.assertIn("The copy window (Mon 22:00-23:00) closed before the copy could start", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
