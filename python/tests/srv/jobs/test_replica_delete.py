# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, TASKS
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError
from python.srv.services.internal.replication import replica_update
from python.srv.services.internal.replication.replica_leftovers import (
    clean_first_run_leftovers,
    remove_replica_vms,
    unfinished_marked_vm,
)
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
)
from python.tests.srv.jobs.test_replica_first_run import FakePlatform

JOB_ID = "12345678-aaaa-bbbb-cccc-000000000009"
UPDATE = "python.srv.services.internal.replication.replica_update"


class DeletingPlatform(FakePlatform):
    def __init__(self) -> None:
        super().__init__()
        self.folders: list[str] = []
        self.left: list[str] = []

    def destroy_replica(self, node: Any, state: dict[str, Any]) -> list[str]:
        super().destroy_replica(node, state)
        return list(self.left)

    def remove_folder(self, _node: Any, _host: str, folder: str) -> None:
        self.folders.append(folder)


class Node:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def _job(**replica: Any) -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "name": "sql-dr",
        "job_type": "replication",
        "replication": {"target_inventory_id": "dr", "target_inventory_name": "vc-dr", "replica_name": "sql-01-replica"},
        "replica": {"vm_id": "vm-r", "vm_name": "sql-01-replica", "point_id": "rp-1", **replica},
    }


class RemoveReplicaVmsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = DeletingPlatform()
        self.replica = self.platform.vm("vm-r", "sql-01-replica", marker=f"abr-replica:{JOB_ID}:first")

    def test_the_replica_and_the_temporary_vms_of_this_job_are_removed(self) -> None:
        self.replica["disks"] = [{"slot": "1000:0", "path": "[ds] sql-01-replica-staging-abcdef12/01-disk-abr-replica-12345678-b.vmdk", "size_bytes": 1, "parent_path": ""}]
        self.platform.vm("vm-s", "sql-01-replica-staging-11111111", marker=f"abr-replica:{JOB_ID}:s2")
        self.platform.vm("vm-on", "sql-01-replica-staging-22222222", marker=f"abr-replica:{JOB_ID}:s3", powered_on=True)
        self.platform.vm("vm-other", "other-replica", marker="abr-replica:another-job:s1")

        result = remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual(self.platform.destroyed, ["vm-r", "vm-s"])
        self.assertEqual(result["removed_vms"], ["sql-01-replica", "sql-01-replica-staging-11111111"])
        self.assertTrue(result["replica_deleted"])
        self.assertEqual(self.platform.folders, ["[ds] sql-01-replica-staging-abcdef12", "[ds] sql-01-replica-staging-11111111"])

    def test_a_clone_of_the_replica_is_kept_and_named(self) -> None:
        self.platform.vm("vm-clone", "sql-01-replica-clone", marker=f"abr-replica:{JOB_ID}:first")

        result = remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual(self.platform.destroyed, ["vm-r"])
        self.assertEqual(result["kept_vms"], ["sql-01-replica-clone"])

    def test_a_vm_marked_by_a_finished_run_is_kept_and_an_unfinished_one_removed(self) -> None:
        TASKS["s4"] = {"id": "s4", "status": "completed"}
        self.addCleanup(TASKS.clear)
        self.platform.vm("vm-done", "copy-made-later", marker=f"abr-replica:{JOB_ID}:s4")
        self.platform.vm("vm-left", "sql-01-replica-staging-55555555", marker=f"abr-replica:{JOB_ID}:s5")
        TASKS["s5"] = {"id": "s5", "status": "failed"}

        result = remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual(self.platform.destroyed, ["vm-r", "vm-left"])
        self.assertEqual(result["kept_vms"], ["copy-made-later"])

    def test_a_clone_is_recognised_by_the_session_that_made_the_replica_once_the_replica_is_gone(self) -> None:
        self.platform.vms.pop("vm-r")
        self.platform.vm("vm-clone", "sql-01-replica-clone", marker=f"abr-replica:{JOB_ID}:first")

        result = remove_replica_vms(_job(created_by_session="first"), Node(), self.platform)

        self.assertEqual((self.platform.destroyed, result["kept_vms"]), ([], ["sql-01-replica-clone"]))

    def test_a_powered_on_replica_is_never_deleted(self) -> None:
        self.replica["powered_on"] = True

        with self.assertRaisesRegex(ReplicationError, "is powered on, so it may be in use after a failover"):
            remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual(self.platform.destroyed, [])

    def test_a_replica_that_is_gone_is_reported_as_not_deleted(self) -> None:
        self.platform.vms.clear()

        result = remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual((result["replica_deleted"], result["replica_vm_name"], result["removed_vms"]), (False, "sql-01-replica", []))

    def test_the_folder_of_the_replica_itself_is_left_to_the_platform(self) -> None:
        remove_replica_vms(_job(), Node(), self.platform)

        self.assertEqual(self.platform.folders, [])


class UnfinishedMarkedVmTests(unittest.TestCase):
    def tearDown(self) -> None:
        TASKS.clear()

    def _vm(self, session: str) -> dict[str, Any]:
        return {"marker_text": f"note\nabr-replica:{JOB_ID}:{session}"}

    def test_the_session_that_made_the_replica_marks_a_copy_even_without_its_record(self) -> None:
        self.assertFalse(unfinished_marked_vm(self._vm("first"), JOB_ID, "first"))

    def test_a_finished_session_marks_a_copy(self) -> None:
        TASKS["s2"] = {"id": "s2", "status": "completed"}

        self.assertFalse(unfinished_marked_vm(self._vm("s2"), JOB_ID, "first"))

    def test_a_session_that_did_not_finish_or_is_unknown_marks_a_leftover(self) -> None:
        TASKS["s3"] = {"id": "s3", "status": "failed"}

        self.assertTrue(unfinished_marked_vm(self._vm("s3"), JOB_ID, "first"))
        self.assertTrue(unfinished_marked_vm(self._vm("s9"), JOB_ID, ""))


class RunCleanupKeepsCopiesTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        TASKS.clear()
        self.addCleanup(lambda: [store.clear() for store in (JOBS, TASKS)])
        self.platform = DeletingPlatform()
        self.platform.vm("vm-r", "sql-01-replica", marker=f"abr-replica:{JOB_ID}:first")
        self.job = _job(created_by_session="first")
        JOBS[JOB_ID] = self.job

    def test_a_clone_of_the_replica_survives_a_run_even_when_the_session_record_is_gone(self) -> None:
        self.platform.vm("vm-clone", "sql-01-replica-clone", marker=f"abr-replica:{JOB_ID}:first")
        self.platform.vm("vm-left", "sql-01-replica-staging-66666666", marker=f"abr-replica:{JOB_ID}:s6")

        clean_first_run_leftovers(self.job, {"target_datastore": "ds", "replica_name": "sql-01-replica"}, Node(), self.platform, None)

        self.assertEqual(self.platform.destroyed, ["vm-left"])


class DeleteReplicaTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (HYPERVISORS, JOBS, TASKS)])
        HYPERVISORS["dr"] = {"id": "dr", "name": "vc-dr", "kind": "vcenter", "type": "vsphere"}
        self.platform = DeletingPlatform()
        self.platform.vm("vm-r", "sql-01-replica", marker=f"abr-replica:{JOB_ID}:first")
        self.node = Node()
        for target, value in ((f"{UPDATE}._platform", lambda _inventory: self.platform), (f"{UPDATE}._connect", lambda _inventory: self.node)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _refusal(self, job: dict[str, Any]) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            replica_update.delete_replica(job)
        return caught.exception

    def test_the_replica_is_deleted_and_the_connection_closed(self) -> None:
        result = replica_update.delete_replica(_job())

        self.assertEqual((result["replica_deleted"], self.platform.destroyed, self.node.disconnected), (True, ["vm-r"], True))

    def test_a_job_that_made_no_replica_needs_no_target(self) -> None:
        HYPERVISORS.clear()

        result = replica_update.delete_replica({**_job(), "replica": {}})

        self.assertEqual((result["replica_deleted"], self.platform.destroyed), (False, []))

    def test_a_target_that_is_no_longer_registered_is_refused(self) -> None:
        HYPERVISORS.clear()

        refusal = self._refusal(_job())

        self.assertEqual((refusal.status_code, refusal.code), (409, "replica_not_deleted"))
        self.assertIn("vc-dr is no longer registered", refusal.message)

    def test_an_unreachable_target_is_refused(self) -> None:
        def unreachable(_inventory):
            raise ReplicationError("The target environment vc-dr cannot be reached: [Errno 113] No route to host")

        with patch(f"{UPDATE}._connect", unreachable):
            refusal = self._refusal(_job())

        self.assertEqual(refusal.status_code, 503)
        self.assertIn("cannot be reached", refusal.message)
        self.assertIn("delete the job without --delete-replica to keep the VM", refusal.message)

    def test_a_powered_on_replica_is_refused(self) -> None:
        self.platform.vms["vm-r"]["powered_on"] = True

        refusal = self._refusal(_job())

        self.assertEqual(refusal.status_code, 409)
        self.assertIn("Turn it off first", refusal.message)
        self.assertTrue(self.node.disconnected)

    def test_a_failure_of_the_platform_is_refused_with_its_reason(self) -> None:
        def broken(_node, _state):
            raise RuntimeError("vCenter refused the task")

        self.platform.destroy_replica = broken

        refusal = self._refusal(_job())

        self.assertEqual(refusal.status_code, 502)
        self.assertIn("could not be deleted (vCenter refused the task); the job is kept", refusal.message)


class DeleteJobRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (JOBS, TASKS)])
        JOBS[JOB_ID] = abr_v41.normalize_job_record(_job())
        JOBS["nightly"] = abr_v41.normalize_job_record({"id": "nightly", "name": "Nightly", "job_type": "backup", "vm_ids": ["vm-9"], "vm_names": ["x"]})
        self.deleted: list[str] = []
        self.removed = {"replica_deleted": True, "replica_vm_name": "sql-01-replica", "removed_vms": ["sql-01-replica"], "files_left": []}
        patcher = patch("python.srv.services.replication_service.delete_replica", self._delete_replica)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("python.srv.services.abr_v41.delete_job",):
            patcher = patch(target, lambda job_id: self.deleted.append(job_id) or JOBS.pop(job_id, None))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.asked: list[str] = []

    def _delete_replica(self, job: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(str(job["id"]))
        return dict(self.removed)

    def test_without_the_flag_the_replica_is_kept(self) -> None:
        result = abr_v41.delete_job_record(JOB_ID)

        self.assertEqual(result, {"id": JOB_ID, "deleted": True})
        self.assertEqual((self.asked, self.deleted), ([], [JOB_ID]))

    def test_with_the_flag_the_replica_goes_first_and_the_answer_says_so(self) -> None:
        result = abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertEqual((self.asked, self.deleted), ([JOB_ID], [JOB_ID]))
        self.assertEqual(result["message"], "Deleted the job and its replica sql-01-replica")
        self.assertTrue(result["replica_deleted"])

    def test_a_replica_that_was_gone_is_named(self) -> None:
        self.removed.update(replica_deleted=False, removed_vms=[])

        result = abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertEqual(result["message"], "Deleted the job; its replica sql-01-replica was not on the target any more")

    def test_kept_copies_are_named(self) -> None:
        self.removed["kept_vms"] = ["sql-01-replica-clone"]

        result = abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertIn("Kept sql-01-replica-clone: it carries this job's mark but is a copy of the finished replica", result["message"])
        self.assertEqual(result["kept_vms"], ["sql-01-replica-clone"])

    def test_files_that_could_not_be_removed_are_named(self) -> None:
        self.removed["files_left"] = ["[ds] r/01.vmdk"]

        result = abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertTrue(result["message"].endswith("Could not delete [ds] r/01.vmdk; remove them by hand"))

    def test_a_refused_replica_deletion_keeps_the_job(self) -> None:
        def refuse(_job):
            raise abr_v41.api_error(409, "replica_not_deleted", "Replica sql-01-replica is powered on")

        with patch("python.srv.services.replication_service.delete_replica", refuse), self.assertRaises(ApiError):
            abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertEqual(self.deleted, [])
        self.assertIn(JOB_ID, JOBS)

    def test_an_active_session_is_refused_before_the_replica_is_touched(self) -> None:
        TASKS["s1"] = {"id": "s1", "job_id": JOB_ID, "task_type": "replication", "status": "running"}

        with self.assertRaises(ApiError) as caught:
            abr_v41.delete_job_record(JOB_ID, delete_replica=True)

        self.assertEqual(caught.exception.code, "job_has_active_sessions")
        self.assertEqual(self.asked, [])

    def test_the_flag_is_refused_on_other_jobs(self) -> None:
        with self.assertRaises(ApiError) as caught:
            abr_v41.delete_job_record("nightly", delete_replica=True)

        self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "invalid_job"))
        self.assertIn("--delete-replica applies to replication jobs only", caught.exception.message)
        self.assertEqual(self.deleted, [])


if __name__ == "__main__":
    unittest.main()
