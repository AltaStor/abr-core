# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS, delete_backup
from python.srv.api.dashboard import get_dashboard_summary


class DashboardSummaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
