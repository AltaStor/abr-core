# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

FULL = {"type": "weekly", "days": ["sunday"], "time": "02:00"}
STORES = (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)


class GuestConsistencyScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        registrable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        registrable.start()
        self.addCleanup(registrable.stop)
        for store in STORES:
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in STORES])
        REPOSITORIES["local"] = {"id": "local", "name": "Local", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["smb"] = {
            "id": "smb",
            "name": "Share",
            "type": "smb",
            "path": tempfile.mkdtemp(),
            "metadata": {"server": "nas.example", "share": "backups"},
        }
        self.hyperv = self._inventory("hyper-v", "Hyper-V", "10.0.0.1", "vm-hv", "hv-01")
        self.vcenter = self._inventory("vsphere", "vCenter", "10.0.0.2", "vm-vc", "vc-01")

    def _inventory(self, kind: str, name: str, address: str, vm_id: str, vm_name: str) -> dict:
        inventory = abr_v41.create_inventory_record(
            {"name": name, "type": kind, "address": address, "username": "administrator", "password": "secret"}
        )
        RESOURCES_BY_HV[str(inventory["id"])] = {
            ("virtual_machine", vm_id): {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": vm_name,
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        return inventory

    def _create(self, name: str, inventory: dict, vm: str, repo_id: str, parameters: dict | None = None) -> dict:
        payload = {"name": name, "vms": [vm], "inventory_id": inventory["id"], "repo_id": repo_id, "active_full_schedule": FULL}
        if parameters is not None:
            payload["task_parameters"] = parameters
        return abr_v41.create_backup_job(payload)

    def _refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception

    def test_a_vmware_job_with_guest_consistency_is_refused_and_not_made(self) -> None:
        error = self._refused(lambda: self._create("vc", self.vcenter, "vc-01", "local", {"hyperv_consistency": "application"}))

        self.assertEqual(error.status_code, 422)
        self.assertIn("guest consistency applies to jobs of a Hyper-V inventory, and vCenter is not one", error.message)
        self.assertEqual(list(JOBS.values()), [])

    def test_job_edit_refuses_it_on_a_vmware_job_and_keeps_the_job(self) -> None:
        job = self._create("vc", self.vcenter, "vc-01", "local")

        error = self._refused(lambda: abr_v41.edit_job_record(job["id"], {"task_parameters": {"hyperv_consistency": "crash"}}))

        self.assertEqual(error.status_code, 422)
        self.assertNotIn("hyperv_consistency", JOBS[job["id"]].get("task_parameters") or {})

    def test_a_vmware_job_that_already_holds_it_still_edits(self) -> None:
        job = self._create("vc", self.vcenter, "vc-01", "local")
        JOBS[job["id"]]["task_parameters"] = {"hyperv_consistency": "application"}

        abr_v41.edit_job_record(job["id"], {"task_parameters": {"hyperv_consistency": "application", "compression_level": 5}})
        abr_v41.edit_job_record(job["id"], {"task_parameters": {"compression_level": 4}})
        error = self._refused(lambda: abr_v41.edit_job_record(job["id"], {"task_parameters": {"hyperv_consistency": "crash"}}))

        self.assertEqual(error.status_code, 422)
        self.assertEqual(JOBS[job["id"]]["task_parameters"]["compression_level"], 4)

    def test_a_hyperv_job_takes_it_on_create_and_edit(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb", {"hyperv_consistency": "crash"})

        self.assertEqual(JOBS[job["id"]]["task_parameters"]["hyperv_consistency"], "crash")

        abr_v41.edit_job_record(job["id"], {"task_parameters": {"hyperv_consistency": "application"}})

        self.assertEqual(JOBS[job["id"]]["task_parameters"]["hyperv_consistency"], "application")

    def test_a_vmware_job_without_it_is_made_as_before(self) -> None:
        job = self._create("vc", self.vcenter, "vc-01", "local", {"compression_level": 3})

        self.assertEqual(JOBS[job["id"]]["task_parameters"], {"compression_level": 3})


if __name__ == "__main__":
    unittest.main()
