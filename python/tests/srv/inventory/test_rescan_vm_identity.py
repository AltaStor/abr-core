# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core import task_pool
from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS
from python.srv.services import abr_v41


def _vm(vm_id: str, name: str) -> dict:
    return {"resource_type": "virtual_machine", "external_id": vm_id, "name": name, "payload": {}}


class _Provider:
    def __init__(self, vms: list[dict]) -> None:
        self.vms = vms

    def rescan(self, record: dict) -> list[dict]:
        return list(self.vms)


class _RescanCase(unittest.TestCase):
    def setUp(self) -> None:
        reachable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        reachable.start()
        self.addCleanup(reachable.stop)
        for store in (HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        self.inventory_id = abr_v41.create_inventory_record(
            {"name": "vCenter", "type": "vsphere", "address": "vc.example.test", "username": "user", "password": "secret"}
        )["id"]

    def _seen(self, *vms: dict) -> None:
        RESOURCES_BY_HV[self.inventory_id] = {("virtual_machine", vm["external_id"]): vm for vm in vms}

    def _job(self, job_id: str, *vm_ids: str, inventory_id: str | None = None) -> None:
        JOBS[job_id] = abr_v41.normalize_job_record(
            {
                "id": job_id,
                "name": job_id,
                "job_type": "backup",
                "inventory_id": inventory_id or self.inventory_id,
                "vm_ids": list(vm_ids),
                "vm_names": list(vm_ids),
                "repo_id": "repo-1",
                "retention_days": 7,
            }
        )

    def _rescan(self, *vms: dict) -> None:
        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_Provider(list(vms))):
            abr_v41.rescan_inventory(self.inventory_id)


class RescanKeepsJobsOnTheirVmTests(_RescanCase):
    def test_a_job_stays_on_its_renamed_vm_when_another_vm_takes_the_old_name(self) -> None:
        self._seen(_vm("vm-own", "app"))
        self._job("job-app", "vm-own")

        self._rescan(_vm("vm-own", "app-renamed"), _vm("vm-other", "app"))

        self.assertEqual(JOBS["job-app"]["vm_ids"], ["vm-own"])
        self.assertEqual(JOBS["job-app"].get("vm_reassignments") or [], [])
        self.assertEqual(abr_v41.job_to_dto(JOBS["job-app"])["vm_reassignments"], [])

    def test_two_vms_that_swapped_names_keep_their_jobs(self) -> None:
        self._seen(_vm("vm-a", "alpha"), _vm("vm-b", "beta"))
        self._job("job-a", "vm-a")
        self._job("job-b", "vm-b")

        self._rescan(_vm("vm-a", "beta"), _vm("vm-b", "alpha"))

        self.assertEqual((JOBS["job-a"]["vm_ids"], JOBS["job-b"]["vm_ids"]), (["vm-a"], ["vm-b"]))

    def test_a_job_whose_vm_is_gone_moves_to_the_vm_that_took_its_name_and_says_so(self) -> None:
        self._seen(_vm("vm-old", "app"), _vm("vm-stable", "db"))
        self._job("job-multi", "vm-old", "vm-stable")

        self._rescan(_vm("vm-new", "app"), _vm("vm-stable", "db"))

        self.assertEqual(JOBS["job-multi"]["vm_ids"], ["vm-new", "vm-stable"])
        [move] = abr_v41.job_to_dto(JOBS["job-multi"])["vm_reassignments"]
        self.assertEqual((move["previous_vm_id"], move["vm_id"], move["vm_name"]), ("vm-old", "vm-new", "app"))
        self.assertIn("moved this job from VM vm-old", move["message"])
        self.assertIn("to VM vm-new", move["message"])

    def test_a_vm_gone_without_a_successor_leaves_the_job_as_it_was(self) -> None:
        self._seen(_vm("vm-old", "app"))
        self._job("job-app", "vm-old")

        self._rescan(_vm("vm-unrelated", "other"))

        self.assertEqual(JOBS["job-app"]["vm_ids"], ["vm-old"])
        self.assertEqual(JOBS["job-app"].get("vm_reassignments") or [], [])

    def test_a_second_rescan_records_the_move_once(self) -> None:
        self._seen(_vm("vm-old", "app"))
        self._job("job-app", "vm-old")

        self._rescan(_vm("vm-new", "app"))
        self._rescan(_vm("vm-new", "app"))

        self.assertEqual(len(JOBS["job-app"]["vm_reassignments"]), 1)

    def test_a_job_of_another_inventory_is_not_touched(self) -> None:
        self._seen(_vm("vm-old", "app"))
        self._job("job-elsewhere", "vm-old", inventory_id="inv-other")

        self._rescan(_vm("vm-new", "app"))

        self.assertEqual(JOBS["job-elsewhere"]["vm_ids"], ["vm-old"])

    def test_choosing_the_vms_again_clears_the_recorded_move(self) -> None:
        self._seen(_vm("vm-old", "app"))
        self._job("job-app", "vm-old")
        self._rescan(_vm("vm-new", "app"), _vm("vm-db", "db"))

        with patch.object(abr_v41, "_resolve_vm_selection", return_value=(["vm-db"], ["db"])):
            abr_v41.edit_job_record("job-app", {"vms": ["db"]})

        self.assertEqual(JOBS["job-app"]["vm_reassignments"], [])
        self.assertEqual(abr_v41.job_to_dto(JOBS["job-app"])["vm_reassignments"], [])


class FirstSessionAfterAMoveWarnsTests(_RescanCase):
    def setUp(self) -> None:
        super().setUp()
        self._seen(_vm("vm-old", "app"), _vm("vm-db", "db"))
        self._job("job-app", "vm-old", "vm-db")
        self._rescan(_vm("vm-new", "app"), _vm("vm-db", "db"))

    def _task(self, task_id: str, vm_id: str) -> dict:
        task = {
            "id": task_id,
            "job_id": "job-app",
            "task_type": "backup",
            "resource_external_id": vm_id,
            "status": "queued",
            "created_at": abr_v41.utc_now(),
            "parameters": {"resolved_backup_type": "ActiveFull", "resolved_backup_reason": "FirstRun"},
            "logs": [],
        }
        TASKS[task_id] = task
        return task

    @staticmethod
    def _codes(task: dict) -> list:
        return [item.get("code") for item in task.get("warnings") or []]

    def test_the_first_backup_session_of_the_moved_vm_carries_the_warning(self) -> None:
        task = abr_v41.note_vm_reassignment(self._task("s-1", "vm-new"))

        [warning] = task["warnings"]
        self.assertEqual((warning["code"], warning["previous_vm_id"], warning["vm_id"]), ("vm_reassigned", "vm-old", "vm-new"))
        self.assertIn("moved this job from VM vm-old", warning["message"])
        self.assertEqual(TASKS["s-1"]["warnings"], task["warnings"])

    def test_the_warning_is_added_once(self) -> None:
        task = abr_v41.note_vm_reassignment(abr_v41.note_vm_reassignment(self._task("s-1", "vm-new")))

        self.assertEqual(self._codes(task), ["vm_reassigned"])

    def test_a_later_session_of_that_vm_does_not_repeat_it(self) -> None:
        abr_v41.note_vm_reassignment(self._task("s-1", "vm-new"))

        later = abr_v41.note_vm_reassignment(self._task("s-2", "vm-new"))

        self.assertEqual(self._codes(later), [])

    def test_a_session_of_another_vm_of_the_job_has_no_warning(self) -> None:
        self.assertEqual(self._codes(abr_v41.note_vm_reassignment(self._task("s-1", "vm-db"))), [])

    def test_a_session_started_by_the_task_pool_gets_the_warning(self) -> None:
        task = task_pool._refresh_backup_decision(self._task("s-1", "vm-new"))

        self.assertIn("vm_reassigned", self._codes(task))


if __name__ == "__main__":
    unittest.main()
