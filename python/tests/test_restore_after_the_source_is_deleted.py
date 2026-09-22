# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.core.mongo_db_wrapper import RESOURCES_BY_HV
from python.srv.plugins.vcenter.vcenter_backup import VCenterBackupError, _get_resource
from python.srv.plugins.vcenter.vcenter_restore import _restore_source_resource

HYPERVISOR = "hv-1"
VM_ID = "4212a0f1-0000-0000-0000-000000000001"


def _task(resource_type: str = "virtual_machine", **overrides: object) -> dict:
    task = {
        "id": "task-1",
        "task_type": "restore",
        "hypervisor_id": HYPERVISOR,
        "resource_type": resource_type,
        "resource_external_id": VM_ID,
    }
    task.update(overrides)
    return task


class RestoreSourceResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        RESOURCES_BY_HV.clear()

    def _discover(self, name: str = "vm-1") -> None:
        RESOURCES_BY_HV[HYPERVISOR] = {
            ("virtual_machine", VM_ID): {
                "resource_type": "virtual_machine",
                "external_id": VM_ID,
                "name": name,
                "payload": {"vm_id": VM_ID},
            }
        }

    def test_a_discovered_machine_is_returned_as_it_was_found(self) -> None:
        self._discover("still-here")

        resource = _restore_source_resource(_task())

        self.assertEqual(resource["name"], "still-here")
        self.assertEqual(resource["external_id"], VM_ID)

    def test_a_machine_discovery_has_dropped_no_longer_blocks_the_restore(self) -> None:
        resource = _restore_source_resource(_task())

        self.assertEqual(resource["resource_type"], "virtual_machine")
        self.assertEqual(resource["external_id"], VM_ID)
        self.assertIsNone(resource["name"])

    def test_the_rescan_that_drops_the_record_does_not_change_the_answer(self) -> None:
        self._discover("about-to-go")
        before = _restore_source_resource(_task())

        RESOURCES_BY_HV[HYPERVISOR] = {}
        after = _restore_source_resource(_task())

        self.assertEqual(before["external_id"], after["external_id"])
        self.assertEqual(after["resource_type"], "virtual_machine")

    def test_a_task_that_already_knew_the_source_was_gone_still_works(self) -> None:
        resource = _restore_source_resource(_task(source_resource_available=False))

        self.assertEqual(resource["external_id"], VM_ID)

    def test_a_disk_restore_still_needs_the_machine_it_belongs_to(self) -> None:
        with self.assertRaises(VCenterBackupError) as caught:
            _restore_source_resource(_task(resource_type="disk"))

        self.assertIn("no longer in the inventory", str(caught.exception))

    def test_the_backup_side_still_refuses_an_undiscovered_machine(self) -> None:
        with self.assertRaises(VCenterBackupError) as caught:
            _get_resource(_task())

        self.assertIn("no longer in the inventory", str(caught.exception))
        self.assertIn("inventory rescan", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
