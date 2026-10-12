# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS, delete_backup
from python.srv.api.dashboard import get_dashboard_summary


class _CleanCatalog(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        REPOSITORIES.clear()
        TASKS.clear()
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        BACKUPS.clear()

    def tearDown(self) -> None:
        JOBS.clear()
        REPOSITORIES.clear()
        TASKS.clear()
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        BACKUPS.clear()


class DashboardSummaryTests(_CleanCatalog):
    def protected_vms(self) -> int:
        return int(get_dashboard_summary().protected_vms)

    def test_protected_vms_count_available_backups_not_jobs(self) -> None:
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Backup job",
            "inventory_id": "inv-1",
            "vm_ids": ["vm-1"],
        }
        self.assertEqual(self.protected_vms(), 0)

        BACKUPS["rp-failed"] = {
            "id": "rp-failed",
            "status": "failed",
            "inventory_id": "inv-1",
            "vm_id": "vm-1",
            "vm_name": "vm-one",
        }
        self.assertEqual(self.protected_vms(), 0)

        BACKUPS["rp-ok"] = {
            "id": "rp-ok",
            "status": "available",
            "inventory_id": "inv-1",
            "vm_id": "vm-1",
            "vm_name": "vm-one",
        }
        self.assertEqual(self.protected_vms(), 1)

        delete_backup("rp-ok")
        self.assertEqual(self.protected_vms(), 0)


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class DashboardRecentTimeTests(_CleanCatalog):
    def _session(self, task_id: str, finished: str | None, started: str, status: str = "completed") -> None:
        TASKS[task_id] = {
            "id": task_id,
            "task_type": "backup",
            "status": status,
            "created_at": started,
            "updated_at": finished or started,
            "result": {"finished_at": finished} if finished else {},
        }

    def test_each_recent_session_says_how_long_ago_it_finished(self) -> None:
        self._session("s-now", _ago(seconds=20), _ago(minutes=1))
        self._session("s-min", _ago(minutes=25), _ago(minutes=30))
        self._session("s-hour", _ago(hours=1, minutes=5), _ago(hours=2))
        self._session("s-days", _ago(days=3), _ago(days=3, minutes=10))

        items = {item.id: item for item in get_dashboard_summary().recent_jobs}

        self.assertEqual(items["s-now"].time, "Just now")
        self.assertEqual(items["s-min"].time, "25 minutes ago")
        self.assertEqual(items["s-hour"].time, "1 hour ago")
        self.assertEqual(items["s-days"].time, "3 days ago")
        self.assertEqual(items["s-min"].finished_at, TASKS["s-min"]["result"]["finished_at"])
        self.assertEqual(items["s-min"].started_at, TASKS["s-min"]["created_at"])

    def test_a_running_session_counts_from_its_start(self) -> None:
        self._session("s-run", None, _ago(minutes=7), status="running")

        item = get_dashboard_summary().recent_jobs[0]

        self.assertEqual((item.time, item.finished_at), ("7 minutes ago", None))


class DashboardRecentSizeTests(_CleanCatalog):
    def test_an_empty_catalog_reports_no_recent_backups(self) -> None:
        summary = get_dashboard_summary()
        self.assertEqual((summary.recent_backups_size_tb, summary.recent_backups_size_bytes), (0.0, 0))

    def test_only_available_backups_of_the_last_24_hours_are_added_up(self) -> None:
        BACKUPS["fresh-1"] = {"id": "fresh-1", "status": "available", "created_at": _ago(hours=2), "size_bytes": 1_500_000_000_000}
        BACKUPS["fresh-2"] = {"id": "fresh-2", "status": "available", "created_at": _ago(hours=23), "size_bytes": 250_000_000}
        BACKUPS["old"] = {"id": "old", "status": "available", "created_at": _ago(hours=25), "size_bytes": 9_000_000_000_000}
        BACKUPS["failed"] = {"id": "failed", "status": "failed", "created_at": _ago(hours=1), "size_bytes": 7_000_000_000_000}
        BACKUPS["unsized"] = {"id": "unsized", "status": "available", "created_at": _ago(hours=1), "size_bytes": None}

        summary = get_dashboard_summary()

        self.assertEqual(summary.recent_backups_size_bytes, 1_500_250_000_000)
        self.assertEqual(summary.recent_backups_size_tb, 1.50025)


if __name__ == "__main__":
    unittest.main()
