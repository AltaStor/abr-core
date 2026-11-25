# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.cli.commands.restore_commands import RestoreCommands
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, SYSTEM_STATE, TASKS
from python.srv.services import abr_v41, clock_guard
from python.srv.services.backup_mode import (
    restore_point_copied_at,
    restore_point_taken_at,
)


def _stamp(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _copy(point_id: str, *, copied_days: float, state_days: float | None, full: bool = True, parent: str | None = None) -> dict:
    point = {
        "id": point_id,
        "job_id": "copy-1",
        "vm_id": "vm-1",
        "vm_name": "demo-vm",
        "repository_id": "repo-2",
        "execution_type": "ActiveFull" if full else "Incremental",
        "execution_reason": "SourceNewChain" if full else "BackupCopy",
        "parent_point_id": parent,
        "segment_id": parent or point_id,
        "created_at": _stamp(copied_days),
        "artifact": {"backup_root": f"/does/not/matter/{point_id}"},
    }
    if state_days is not None:
        point["copy_source"] = {"job_id": "job-1", "point_id": f"src-{point_id}", "created_at": _stamp(state_days)}
    return abr_v41.normalize_backup_point(point)


def _copy_job(**overrides) -> dict:
    return {"id": "copy-1", "name": "Offsite", "job_type": "backup_copy", "repo_id": "repo-2", "retention_days": 3, "retention_min_fulls": 0, "gfs": {}, **overrides}


class StateTimeHelperTests(unittest.TestCase):
    def test_a_copy_is_taken_when_its_source_was_and_copied_when_it_was_written(self) -> None:
        point = _copy("c1", copied_days=2, state_days=4)

        self.assertEqual(restore_point_taken_at(point), point["copy_source"]["created_at"])
        self.assertEqual(restore_point_copied_at(point), point["created_at"])

    def test_a_point_that_is_not_a_copy_is_taken_when_it_was_written(self) -> None:
        point = _copy("p1", copied_days=2, state_days=None)

        self.assertEqual(restore_point_taken_at(point), point["created_at"])
        self.assertIsNone(restore_point_copied_at(point))

    def test_a_copy_of_a_copy_is_taken_when_the_vm_was_in_that_state(self) -> None:
        point = {
            "created_at": "2026-11-23T10:00:00+00:00",
            "copy_source": {"point_id": "first-copy", "created_at": "2026-11-22T10:00:00+00:00", "taken_at": "2026-11-20T10:00:00+00:00"},
        }

        self.assertEqual(restore_point_taken_at(point), "2026-11-20T10:00:00+00:00")
        self.assertEqual(restore_point_copied_at(point), "2026-11-23T10:00:00+00:00")

    def test_a_copy_record_without_a_source_time_falls_back_to_its_own(self) -> None:
        point = {"created_at": "2026-11-20T10:00:00+00:00", "copy_source": {"job_id": "job-1"}}

        self.assertEqual(restore_point_taken_at(point), "2026-11-20T10:00:00+00:00")
        self.assertIsNone(restore_point_copied_at(point))


class CopyRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE):
            store.clear()
        clock_guard.forget()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE)])
        deleter = patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True)
        deleter.start()
        self.addCleanup(deleter.stop)

    def retain(self, job: dict, *, dry_run: bool = False) -> dict:
        report = abr_v41.new_retention_report(job, "vm-1", "repo-2")
        abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-2", report=report, dry_run=dry_run)
        return report

    def test_a_copy_made_two_days_late_goes_once_the_state_it_holds_is_past_the_window(self) -> None:
        BACKUPS["c1"] = _copy("c1", copied_days=2, state_days=4)

        report = self.retain(_copy_job())

        self.assertEqual(report["removed"], ["c1"])

    def test_a_copy_whose_state_is_inside_the_window_stays(self) -> None:
        BACKUPS["c1"] = _copy("c1", copied_days=0.5, state_days=2)

        self.retain(_copy_job())

        self.assertIn("c1", BACKUPS)

    def test_a_point_that_is_not_a_copy_ages_from_its_own_time_as_before(self) -> None:
        BACKUPS["p1"] = _copy("p1", copied_days=2, state_days=None)
        BACKUPS["p2"] = _copy("p2", copied_days=4, state_days=None)

        self.retain(_copy_job(job_type="backup"))

        self.assertEqual(sorted(BACKUPS), ["p1"])

    def test_a_copied_chain_goes_together_once_its_newest_state_is_past_the_window(self) -> None:
        BACKUPS["c1"] = _copy("c1", copied_days=1, state_days=6)
        BACKUPS["c2"] = _copy("c2", copied_days=1, state_days=5, full=False, parent="c1")

        report = self.retain(_copy_job(), dry_run=True)

        self.assertEqual(sorted(report["removed"]), ["c1", "c2"])

    def test_gfs_weeks_of_copies_follow_the_state_they_hold(self) -> None:
        weeks = {"weekly": 2, "monthly": 0, "yearly": 0, "enabled": True}
        points = [_copy("c1", copied_days=0.2, state_days=14), _copy("c2", copied_days=0.1, state_days=7)]

        abr_v41._apply_gfs_marks(points, weeks)

        self.assertEqual([point["gfs_marks"]["weekly"] for point in points], [True, True])


class RestoreListColumnTests(unittest.TestCase):
    def test_a_list_with_copies_shows_the_state_time_as_created_and_a_copied_column(self) -> None:
        items = [{"id": "c1", "created_at": "2026-11-23T08:04:39+00:00", "taken_at": "2026-11-23T00:40:48+00:00", "copied_at": "2026-11-23T08:04:39+00:00"}]

        columns = RestoreCommands._point_columns(items)

        self.assertIn(("taken_at", "CREATED"), columns)
        self.assertEqual(columns[columns.index(("taken_at", "CREATED")) + 1], ("copied_at", "COPIED"))

    def test_a_list_without_copies_keeps_its_columns(self) -> None:
        columns = RestoreCommands._point_columns([{"id": "p1", "created_at": "t", "taken_at": "t", "copied_at": None}])

        self.assertNotIn(("copied_at", "COPIED"), columns)

    def test_an_older_server_without_taken_at_still_shows_its_time(self) -> None:
        decorated = RestoreCommands._decorate_restore_point({"id": "p1", "created_at": "2026-11-20T10:00:00+00:00"})

        self.assertEqual(decorated["taken_at"], "2026-11-20T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
