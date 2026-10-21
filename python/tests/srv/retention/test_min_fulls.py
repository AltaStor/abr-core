# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, SYSTEM_STATE, TASKS
from python.srv.services import abr_v41, clock_guard


def _stamp(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).isoformat()


def _point(point_id: str, age_days: float, *, full: bool, parent: str | None = None) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "demo-vm",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent,
            "segment_id": parent or point_id,
            "created_at": _stamp(timedelta(days=age_days)),
            "artifact": {"backup_root": f"/does/not/matter/{point_id}"},
        }
    )


def _job(**overrides) -> dict:
    return {"id": "job-1", "name": "Nightly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 7, "gfs": {}, **overrides}


class MinFullsCase(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE):
            store.clear()
        clock_guard.forget()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE)])
        deleter = patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True)
        deleter.start()
        self.addCleanup(deleter.stop)

    @staticmethod
    def store(*points: dict) -> None:
        for point in points:
            BACKUPS[point["id"]] = point

    def store_three_expired_segments(self) -> None:
        self.store(
            _point("full-a", 40, full=True),
            _point("incr-a", 39, full=False, parent="full-a"),
            _point("full-b", 30, full=True),
            _point("incr-b", 29, full=False, parent="full-b"),
            _point("full-c", 20, full=True),
            _point("incr-c", 19, full=False, parent="full-c"),
        )

    def retain(self, job: dict, *, dry_run: bool = False) -> dict:
        report = abr_v41.new_retention_report(job, "vm-1", "repo-1")
        abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1", report=report, dry_run=dry_run)
        return report


class RetentionMinFullsTests(MinFullsCase):
    def test_without_the_setting_an_expired_chain_still_goes_entirely(self) -> None:
        self.store_three_expired_segments()

        report = self.retain(_job())

        self.assertEqual(BACKUPS, {})
        self.assertEqual(report["kept_min_fulls"], [])

    def test_zero_behaves_exactly_like_a_job_from_before_the_setting(self) -> None:
        self.store_three_expired_segments()

        self.retain(_job(retention_min_fulls=0))

        self.assertEqual(BACKUPS, {})

    def test_the_newest_full_and_its_incrementals_stay_whatever_their_age(self) -> None:
        self.store_three_expired_segments()

        with self.assertLogs("python.srv.services.abr_v41", level="INFO") as captured:
            report = self.retain(_job(retention_min_fulls=1))

        self.assertEqual(sorted(BACKUPS), ["full-c", "incr-c"])
        self.assertEqual(sorted(report["kept_min_fulls"]), ["full-c", "incr-c"])
        self.assertEqual(sorted(report["removed"]), ["full-a", "full-b", "incr-a", "incr-b"])
        self.assertTrue(any("retention-min-fulls" in line and "newest 1 full" in line for line in captured.output))

    def test_two_keeps_the_two_newest_segments(self) -> None:
        self.store_three_expired_segments()

        self.retain(_job(retention_min_fulls=2))

        self.assertEqual(sorted(BACKUPS), ["full-b", "full-c", "incr-b", "incr-c"])

    def test_a_floor_above_the_number_of_fulls_keeps_the_whole_chain(self) -> None:
        self.store_three_expired_segments()

        report = self.retain(_job(retention_min_fulls=5))

        self.assertEqual(len(BACKUPS), 6)
        self.assertEqual(report["removed"], [])

    def test_a_chain_with_a_fresh_segment_is_pruned_as_before(self) -> None:
        for min_fulls in (0, 1):
            with self.subTest(min_fulls=min_fulls):
                BACKUPS.clear()
                self.store(
                    _point("full-old", 20, full=True),
                    _point("incr-old", 19, full=False, parent="full-old"),
                    _point("full-new", 1, full=True),
                )

                report = self.retain(_job(retention_min_fulls=min_fulls))

                self.assertEqual(sorted(BACKUPS), ["full-new"])
                self.assertEqual(report["kept_min_fulls"], [])

    def test_a_gfs_kept_newest_full_keeps_its_expired_incrementals_under_the_floor(self) -> None:
        full = _point("full-1", 20, full=True)
        full["gfs_marks"] = {"weekly": True, "monthly": False, "yearly": False}
        self.store(full, _point("incr-1", 19, full=False, parent="full-1"))
        gfs = {"enabled": True, "weekly": 1, "monthly": 0, "yearly": 0}

        self.retain(_job(gfs=gfs))
        self.assertEqual(sorted(BACKUPS), ["full-1"])

        BACKUPS.clear()
        self.store(full, _point("incr-1", 19, full=False, parent="full-1"))
        self.retain(_job(gfs=gfs, retention_min_fulls=1))
        self.assertEqual(sorted(BACKUPS), ["full-1", "incr-1"])

    def test_a_dry_run_names_what_the_floor_keeps_and_deletes_nothing(self) -> None:
        self.store_three_expired_segments()

        report = self.retain(_job(retention_min_fulls=1), dry_run=True)

        self.assertEqual(len(BACKUPS), 6)
        self.assertEqual(sorted(report["kept_min_fulls"]), ["full-c", "incr-c"])
        self.assertEqual(len(report["removed"]), 4)

    def test_the_job_retention_report_carries_the_setting_and_the_count(self) -> None:
        self.store_three_expired_segments()
        JOBS["job-1"] = _job(retention_min_fulls=1)

        result = abr_v41.run_job_retention("job-1")

        self.assertEqual(result["retention_min_fulls"], 1)
        self.assertEqual((result["summary"]["removed"], result["summary"]["kept_min_fulls"]), (4, 2))

    def test_a_job_record_from_before_the_setting_reads_as_off(self) -> None:
        normalized = abr_v41.normalize_job_record(_job())

        self.assertEqual(normalized["retention_min_fulls"], 0)
        self.assertEqual(abr_v41.normalize_job_record(_job(retention_min_fulls=3))["retention_min_fulls"], 3)


if __name__ == "__main__":
    unittest.main()
