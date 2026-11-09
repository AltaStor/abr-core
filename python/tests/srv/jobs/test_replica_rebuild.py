# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import JOBS, TASKS
from python.srv.core.task_executor import TaskStopRequested
from python.srv.services.api_errors import ApiError
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
)
from python.tests.srv.jobs.test_replica_update import (
    JOB_ID,
    SESSION_ID,
    FakePlatform,
    _layer,
    _UpdateCase,
)

STAGING = "sql-01-replica-staging-abcdef12"


class RebuildPlatform(FakePlatform):
    def __init__(self) -> None:
        super().__init__()
        self.staged: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.swap_error: BaseException | None = None
        self.partial_swap = False

    def find_replica(self, node: Any, vm_id: str) -> dict[str, Any] | None:
        if vm_id in self.staged:
            staged = self.staged[vm_id]
            return {**staged, "disks": [dict(disk) for disk in staged["disks"]]}
        return super().find_replica(node, vm_id)

    def stage(self, name: str, suffix: str, disk_count: int) -> dict[str, Any]:
        staged = {
            "vm_id": "vm-staging",
            "name": name,
            "host": "esx-dr-01",
            "powered_on": False,
            "marker_text": "",
            "snapshots": [],
            "disks": [
                {"slot": f"1000:{index}", "path": f"[ds] {name}/0{index + 1}{suffix}.vmdk", "size_bytes": (index + 1) << 30, "parent_path": ""}
                for index in range(disk_count)
            ],
            "fingerprint": "cv-s",
            "busy": False,
            "consolidation_needed": False,
            "configuration_location": f"[ds] {name}",
        }
        self.staged["vm-staging"] = staged
        return staged

    def release_staging_disks(self, _node: Any, staging: dict[str, Any]) -> None:
        self.calls.append(("release", staging["vm_id"]))
        self.staged[staging["vm_id"]]["disks"] = []

    def swap_disks(self, _node: Any, replica: dict[str, Any], _staging: dict[str, Any], changes: list[dict[str, Any]]) -> None:
        self.calls.append(("swap", [(change["slot"], change["old_path"], change["new_path"]) for change in changes]))
        if self.swap_error is not None:
            if self.partial_swap:
                self._apply(changes[:1])
            raise self.swap_error
        self._apply(changes)

    def _apply(self, changes: list[dict[str, Any]]) -> None:
        disks = {disk["slot"]: disk for disk in self.state["disks"]}
        for change in changes:
            if change["new_path"]:
                disks[change["slot"]] = {"slot": change["slot"], "path": change["new_path"], "size_bytes": 1 << 30, "parent_path": ""}
            else:
                disks.pop(change["slot"], None)
        self.state["disks"] = sorted(disks.values(), key=lambda disk: disk["slot"])
        self.state["fingerprint"] = "cv-swapped"

    def put_back_disks(self, _node: Any, _replica: dict[str, Any], changes: list[dict[str, Any]]) -> None:
        self.calls.append(("put back",))
        self._apply([{**change, "new_path": change["old_path"]} for change in changes])

    def discard_staging_shell(self, _node: Any, staging: dict[str, Any]) -> None:
        self.calls.append(("discard staging", staging["vm_id"]))
        self.staged.pop(staging["vm_id"], None)

    def remove_staging(self, _node: Any, staging: dict[str, Any]) -> list[str]:
        self.calls.append(("remove staging with disks", staging["vm_id"]))
        self.staged.pop(staging["vm_id"], None)
        return []

    def delete_disk_files(self, _node: Any, _host: str, paths: list[str]) -> list[str]:
        self.calls.append(("delete files", list(paths)))
        self.deleted.extend(paths)
        return []


class FakeRestore:
    def __init__(self, platform: RebuildPlatform, disk_count: int, error: BaseException | None = None) -> None:
        self.platform = platform
        self.disk_count = disk_count
        self.error = error
        self.tasks: list[dict[str, Any]] = []

    def restore(self, task: dict[str, Any], progress_callback=None) -> dict[str, Any]:
        self.tasks.append(task)
        parameters = task["parameters"]
        staged = self.platform.stage(parameters["target_vm_name"], parameters["disk_name_suffix"], self.disk_count)
        progress_callback({"stage": "prepare", "progress": 7, "message": "Created VM", "created_vm_id": staged["vm_id"]})
        if self.error is not None:
            raise self.error
        return {
            "target_vm_id": staged["vm_id"],
            "processed_bytes": 3 << 30,
            "transferred_bytes": 3 << 30,
            "warnings": [],
            "disks": [{"index": index + 1, "restored_datastore_path": disk["path"]} for index, disk in enumerate(staged["disks"])],
        }


class _RebuildCase(_UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        self.platform = RebuildPlatform()
        self.layers[1] = [_layer("rp-3", "full")]
        self.layers[2] = [_layer("rp-3", "full")]
        self.restorer = FakeRestore(self.platform, disk_count=len(self.manifest))
        self.space_error: ApiError | None = None

        self.planned: list[dict[str, Any]] = []

        def plan(payload: dict[str, Any]):
            self.planned.append(payload)
            if self.space_error is not None:
                raise self.space_error
            return (
                {"id": "planned", "hypervisor_id": "dr", "parameters": {"point_id": payload["point_id"], "target_vm_name": payload["target_vm_name"]}},
                {},
                "summary",
            )

        for target, value in (
            ("python.srv.services.internal.replication.replica_update.retry_settings", lambda: (0, 0)),
            ("python.srv.services.abr_v41.plan_restore_session", plan),
            ("python.srv.services.internal.replication.replica_update.build_hypervisor_plugin", lambda _inventory: self.restorer),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _calls(self, kind: str) -> list[tuple]:
        return [call for call in self.platform.calls if call[0] == kind]


class RebuildTests(_RebuildCase):
    def test_a_replica_moved_to_another_host_is_rebuilt_where_it_is_now(self) -> None:
        self.platform.state["host"] = "esx-dr-02"

        result = self._run()

        self.assertEqual(result["mode"], "rebuild")
        self.assertEqual(self.planned[0]["target_host"], "esx-dr-02")
        self.assertEqual(self.planned[0]["target_vm_name"], STAGING)

    def test_an_active_full_rebuilds_every_disk_next_to_the_old_ones_and_switches_once(self) -> None:
        result = self._run()

        self.assertEqual(result["mode"], "rebuild")
        self.assertIn("Rebuilt the disks of replica sql-01-replica", result["message"])
        task = self.restorer.tasks[0]
        self.assertEqual(task["parameters"]["target_vm_name"], STAGING)
        self.assertEqual(task["parameters"]["disk_name_suffix"], "-abr-replica-12345678-a")
        self.assertEqual(
            self._calls("swap"),
            [("swap", [("1000:0", "[ds] r/01.vmdk", f"[ds] {STAGING}/01-abr-replica-12345678-a.vmdk"),
                       ("1000:1", "[ds] r/02.vmdk", f"[ds] {STAGING}/02-abr-replica-12345678-a.vmdk")])],
        )
        self.assertLess(self.platform.calls.index(("release", "vm-staging")), self.platform.calls.index(self._calls("swap")[0]))
        self.assertIn(("discard staging", "vm-staging"), self.platform.calls)
        self.assertEqual(self.platform.deleted, ["[ds] r/01.vmdk", "[ds] r/02.vmdk"])
        replica = self._replica()
        self.assertEqual(replica["point_id"], "rp-3")
        self.assertEqual([disk["path"] for disk in replica["disks"]], [f"[ds] {STAGING}/01-abr-replica-12345678-a.vmdk", f"[ds] {STAGING}/02-abr-replica-12345678-a.vmdk"])
        self.assertEqual(replica["fingerprint"], "cv-swapped")

    def test_disks_written_with_the_a_suffix_are_rebuilt_with_b(self) -> None:
        for disk in self.platform.state["disks"]:
            disk["path"] = disk["path"].replace(".vmdk", "-abr-replica-12345678-a.vmdk")
        for disk in JOBS[JOB_ID]["replica"]["disks"]:
            disk["path"] = disk["path"].replace(".vmdk", "-abr-replica-12345678-a.vmdk")

        self._run()

        self.assertEqual(self.restorer.tasks[0]["parameters"]["disk_name_suffix"], "-abr-replica-12345678-b")

    def test_the_swap_plan_is_recorded_before_the_switch(self) -> None:
        self._run()

        plan_at = next(index for index, update in enumerate(self.updates) if update.get("swap_plan"))
        switch_at = next(index for index, update in enumerate(self.updates) if "Switching" in str(update.get("message")))
        self.assertLess(plan_at, switch_at)
        self.assertEqual(self.updates[plan_at]["swap_plan"]["point_id"], "rp-3")

    def test_an_added_disk_joins_in_the_same_switch(self) -> None:
        self.manifest.append({"index": 3, "uuid": "6000c29a-0003", "capacity_bytes": 1 << 30})
        self.restorer.disk_count = 3

        self._run()

        changes = self._calls("swap")[0][1]
        self.assertEqual(changes[2], ("1000:2", "", f"[ds] {STAGING}/03-abr-replica-12345678-a.vmdk"))
        self.assertEqual(len(self._replica()["disks"]), 3)

    def test_a_removed_disk_leaves_the_replica_in_the_same_switch(self) -> None:
        self.manifest.pop()
        self.restorer.disk_count = 1

        self._run()

        changes = self._calls("swap")[0][1]
        self.assertIn(("1000:1", "[ds] r/02.vmdk", ""), changes)
        self.assertEqual([disk["slot"] for disk in self._replica()["disks"]], ["1000:0"])
        self.assertIn("[ds] r/02.vmdk", self.platform.deleted)

    def test_a_failed_restore_of_the_staging_vm_leaves_the_replica_alone(self) -> None:
        self.restorer.error = RuntimeError("tool failed")

        with self.assertRaisesRegex(RuntimeError, "tool failed"):
            self._run()

        self.assertEqual(self._calls("swap"), [])
        self.assertIn(("remove staging with disks", "vm-staging"), self.platform.calls)
        self.assertEqual(self._replica()["point_id"], "rp-1")
        self.assertEqual(self.platform.deleted, [])

    def test_a_stop_during_the_staging_restore_leaves_the_replica_alone(self) -> None:
        self.restorer.error = TaskStopRequested("Session stop requested")

        with self.assertRaises(TaskStopRequested):
            self._run()

        self.assertEqual(self._calls("swap"), [])
        self.assertEqual(self.platform.staged, {})

    def test_a_failed_switch_keeps_the_old_disks_and_removes_the_rebuilt_ones(self) -> None:
        self.platform.swap_error = RuntimeError("Reconfigure failed")

        with self.assertRaisesRegex(RuntimeError, "Reconfigure failed"):
            self._run()

        self.assertEqual([disk["path"] for disk in self.platform.state["disks"]], ["[ds] r/01.vmdk", "[ds] r/02.vmdk"])
        self.assertEqual(self.platform.deleted, [f"[ds] {STAGING}/01-abr-replica-12345678-a.vmdk", f"[ds] {STAGING}/02-abr-replica-12345678-a.vmdk"])
        self.assertEqual(self._replica()["point_id"], "rp-1")
        self.assertFalse(self.updates[-1].get("swap_plan"))

    def test_a_switch_that_failed_half_way_is_put_back(self) -> None:
        self.platform.swap_error = RuntimeError("second disk failed")
        self.platform.partial_swap = True

        with self.assertRaises(RuntimeError):
            self._run()

        self.assertIn(("put back",), self.platform.calls)
        self.assertEqual([disk["path"] for disk in self.platform.state["disks"]], ["[ds] r/01.vmdk", "[ds] r/02.vmdk"])

    def test_each_run_rebuilds_through_a_staging_vm_of_its_own_name(self) -> None:
        from python.srv.services.internal.replication.replica_update import staging_name

        settings = {"replica_name": "sql-01-replica"}
        self.assertNotEqual(staging_name(settings, "aaaaaaaa-1"), staging_name(settings, "bbbbbbbb-2"))
        self.assertLessEqual(len(staging_name({"replica_name": "x" * 200}, "aaaaaaaa-1")), 80)

    def test_a_staging_name_that_is_the_folder_of_the_live_disks_is_refused_before_any_restore(self) -> None:
        for disk in self.platform.state["disks"]:
            disk["path"] = disk["path"].replace("[ds] r/", f"[ds] {STAGING}/")

        with self.assertRaisesRegex(ReplicationError, "is the folder the disks of sql-01-replica live in"):
            self._run()

        self.assertEqual(self.restorer.tasks, [])

    def test_the_empty_folder_of_an_earlier_staging_vm_is_removed_after_the_switch(self) -> None:
        removed: list[str] = []
        self.platform.remove_folder = lambda _node, _host, folder: removed.append(folder)
        for disk in self.platform.state["disks"]:
            disk["path"] = disk["path"].replace("[ds] r/", "[ds] sql-01-replica-staging-11111111/")

        self._run()

        self.assertEqual(removed, ["[ds] sql-01-replica-staging-11111111"])

    def test_not_enough_room_for_the_rebuild_is_named_and_changes_nothing(self) -> None:
        self.space_error = ApiError(409, "insufficient_space", "Datastore ds-dr has 2.0 GiB free, the restore needs 3.0 GiB")

        with self.assertRaisesRegex(ReplicationError, "cannot be rebuilt next to the current ones: Datastore ds-dr has 2.0 GiB free"):
            self._run()

        self.assertEqual(self.restorer.tasks, [])
        self.assertEqual(self._replica()["point_id"], "rp-1")


class InterruptedSwapTests(_RebuildCase):
    def _interrupted(self, *, switched: list[str]) -> dict[str, Any]:
        changes = [
            {"key": "uuid:6000c29a-0001", "slot": "1000:0", "staging_slot": "1000:0", "old_path": "[ds] r/01.vmdk",
             "new_path": "[ds] s/01-b.vmdk", "source_capacity_bytes": 1 << 30},
            {"key": "uuid:6000c29a-0002", "slot": "1000:1", "staging_slot": "1000:1", "old_path": "[ds] r/02.vmdk",
             "new_path": "[ds] s/02-b.vmdk", "source_capacity_bytes": 2 << 30},
        ]
        plan = {"replica_vm_id": "vm-r", "staging_vm_id": "vm-staging-old", "staging_name": "old-staging", "staging_configuration_location": "",
                "host": "esx-dr-01", "point_id": "rp-3", "point_created_at": "2026-11-03T01:00:00+00:00", "changes": changes}
        TASKS["crashed"] = {"id": "crashed", "job_id": JOB_ID, "task_type": "replication", "status": "failed",
                            "created_at": "2026-11-08T00:00:00", "result": {"replica_touched": True, "swap_plan": plan, "failure_kind": "interrupted"}}
        self.platform._apply([change for change in changes if change["slot"] in switched])
        self.platform.state["fingerprint"] = "cv-1" if not switched else "cv-swapped"
        return plan

    def test_a_switch_that_had_happened_is_finished_and_recorded(self) -> None:
        self._interrupted(switched=["1000:0", "1000:1"])
        self.layers[1] = [_layer("rp-3", "full")]
        self.layers[2] = [_layer("rp-3", "full")]

        result = self._run()

        self.assertEqual(result["mode"], "up_to_date")
        self.assertIn(("delete files", ["[ds] r/01.vmdk", "[ds] r/02.vmdk"]), self.platform.calls)
        self.assertEqual(self._replica()["point_id"], "rp-3")
        self.assertEqual(TASKS["crashed"]["result"]["swap_plan"], {})

    def test_a_switch_that_had_not_happened_removes_the_rebuilt_disks_and_rebuilds_again(self) -> None:
        self._interrupted(switched=[])

        result = self._run()

        self.assertIn(("delete files", ["[ds] s/01-b.vmdk", "[ds] s/02-b.vmdk"]), self.platform.calls)
        self.assertEqual(result["mode"], "rebuild")
        self.assertEqual(TASKS["crashed"]["result"]["swap_plan"], {})

    def test_a_half_done_switch_is_finished_forward(self) -> None:
        self._interrupted(switched=["1000:0"])

        self._run()

        forward = self._calls("swap")[0][1]
        self.assertEqual(forward, [("1000:1", "[ds] r/02.vmdk", "[ds] s/02-b.vmdk")])
        self.assertEqual(TASKS["crashed"]["result"]["swap_plan"], {})

    def test_a_plan_is_handled_once(self) -> None:
        self._interrupted(switched=["1000:0", "1000:1"])
        self._run()
        self.platform.calls.clear()
        TASKS.pop(SESSION_ID)

        self._run()

        self.assertNotIn(("delete files", ["[ds] r/01.vmdk", "[ds] r/02.vmdk"]), self.platform.calls)

    def test_a_plan_for_another_vm_is_dropped(self) -> None:
        plan = self._interrupted(switched=[])
        plan["replica_vm_id"] = "vm-other"
        TASKS["crashed"]["result"]["swap_plan"] = plan

        self._run()

        self.assertEqual(TASKS["crashed"]["result"]["swap_plan"], {})
        self.assertNotIn(("delete files", ["[ds] s/01-b.vmdk", "[ds] s/02-b.vmdk"]), self.platform.calls)
