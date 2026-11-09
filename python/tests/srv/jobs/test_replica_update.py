# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import copy
import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, TASKS
from python.srv.core.task_executor import TaskStopRequested
from python.srv.services.internal.replication import replica_update
from python.srv.services.internal.replication.replica_update import run_replication
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
    ReplicationValidationError,
)
from python.tests.srv.jobs.test_replica_first_run import POINT_CONFIG, ConfigFakes

JOB_ID = "12345678-aaaa-bbbb-cccc-000000000002"
SESSION_ID = "abcdef12-session"
OUR_SNAPSHOT = "abr-replica-12345678-abcdef12"


class SizeTextTests(unittest.TestCase):
    def test_less_than_a_gibibyte_is_shown_in_mebibytes(self) -> None:
        self.assertEqual(replica_update._size(16 * 1024**2), "16.0 MiB")
        self.assertEqual(replica_update._size(None), "0.0 MiB")

    def test_a_gibibyte_or_more_is_shown_in_gibibytes(self) -> None:
        self.assertEqual(replica_update._size(1024**3), "1.0 GiB")
        self.assertEqual(replica_update._size(42 * 1024**3), "42.0 GiB")


class FakePlatform(ConfigFakes):
    PLATFORM = "vsphere"

    def _config_changed(self, _state: dict[str, Any]) -> None:
        self.state["fingerprint"] = f"{self.state['fingerprint']}+config"

    def _renamed(self, _state: dict[str, Any], new_name: str) -> None:
        self.state["name"] = new_name

    def __init__(self) -> None:
        self.reported_bytes = 100
        self.state = {
            "vm_id": "vm-r",
            "name": "sql-01-replica",
            "host": "esx-dr-01",
            "powered_on": False,
            "marker_text": f"abr-replica:{JOB_ID}:first",
            "snapshots": [],
            "disks": [
                {"slot": "1000:0", "path": "[ds] r/01.vmdk", "size_bytes": 1 << 30, "parent_path": ""},
                {"slot": "1000:1", "path": "[ds] r/02.vmdk", "size_bytes": 2 << 30, "parent_path": ""},
            ],
            "fingerprint": "cv-1",
            "busy": False,
            "busy_tasks": [],
            "consolidation_needed": False,
        }
        self.calls: list[tuple] = []
        self.write_error: BaseException | None = None
        self.revert_error: Exception | None = None
        self.powered_on_since_result = False
        self.busy_rounds = 0

    def rebuild_location(self, state: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
        return state["host"] or settings["target_host"], settings["target_datastore"]

    def find_replica(self, _node: Any, vm_id: str) -> dict[str, Any] | None:
        if self.state is None or vm_id != self.state["vm_id"]:
            return None
        if self.busy_rounds:
            self.busy_rounds -= 1
            self.state["busy"] = self.busy_rounds > 0
        return {**self.state, "disks": [dict(disk) for disk in self.state["disks"]], "snapshots": list(self.state["snapshots"])}

    def replicas_with_marker(self, _node: Any, _prefix: str) -> list[dict[str, Any]]:
        return []

    def create_snapshot(self, _node: Any, state: dict[str, Any], name: str, _timeout: int) -> dict[str, Any]:
        self.calls.append(("snapshot", name))
        self.state["snapshots"].append(name)
        self.state["fingerprint"] = "cv-2"
        return {**self.state, "disks": [dict(disk, path=disk["path"].replace(".vmdk", "-000001.vmdk")) for disk in self.state["disks"]]}

    def write_increments(self, _node, _state, disk, layers, _repository, _key, *, report, should_cancel, timeout_seconds):
        self.calls.append(("write", disk["path"], [layer["point_id"] for layer in layers]))
        if self.write_error is not None:
            raise self.write_error
        return {"written_bytes": self.reported_bytes}

    def revert_snapshot(self, _node: Any, _state: dict[str, Any], name: str, _timeout: int) -> None:
        self.calls.append(("revert", name))
        if self.revert_error is not None:
            raise self.revert_error
        self.state["snapshots"].remove(name)

    def remove_snapshot(self, _node: Any, _state: dict[str, Any], name: str, _timeout: int) -> None:
        self.calls.append(("remove", name))
        self.state["snapshots"].remove(name)
        self.state["fingerprint"] = "cv-3"

    def consolidate(self, _node: Any, _state: dict[str, Any], _timeout: int) -> None:
        self.calls.append(("consolidate",))
        self.state["consolidation_needed"] = False

    def powered_on_since(self, _node: Any, _state: dict[str, Any], _since: str) -> bool:
        return self.powered_on_since_result


class FakeNode:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


class KeptAliveNode(FakeNode):
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextlib.contextmanager
    def session_keepalive(self):
        self.events.append("keepalive on")
        try:
            yield
        finally:
            self.events.append("keepalive off")


def _layer(point_id: str, store_mode: str = "incremental") -> dict[str, Any]:
    return {"point_id": point_id, "store_mode": store_mode, "abr_path": "/r", "abr_backup_name": point_id}


MANIFEST = [
    {"index": 1, "uuid": "6000c29a-0001", "capacity_bytes": 1 << 30},
    {"index": 2, "uuid": "6000c29a-0002", "capacity_bytes": 2 << 30},
]


class _UpdateCase(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, HYPERVISORS, JOBS, TASKS)])
        HYPERVISORS["dr"] = {"id": "dr", "name": "vc-dr", "kind": "vcenter", "type": "vsphere"}
        JOBS["src"] = {"id": "src", "name": "sql-nightly", "job_type": "backup", "vm_ids": ["vm-1"], "vm_names": ["sql-01"]}
        JOBS[JOB_ID] = {
            "id": JOB_ID,
            "name": "sql-dr",
            "job_type": "replication",
            "replication": {
                "source_job_id": "src",
                "source_job_name": "sql-nightly",
                "source_vm_id": "vm-1",
                "source_vm_name": "sql-01",
                "target_inventory_id": "dr",
                "target_inventory_name": "vc-dr",
                "target_host": "esx-dr-01",
                "target_datastore": "ds-dr",
                "replica_name": "sql-01-replica",
            },
            "replica": {
                "vm_id": "vm-r",
                "vm_name": "sql-01-replica",
                "host": "esx-dr-01",
                "point_id": "rp-1",
                "point_created_at": "2026-11-01T01:00:00+00:00",
                "last_success_at": "2026-11-01T01:30:00+00:00",
                "fingerprint": "cv-1",
                "disks": [
                    {"key": "uuid:6000c29a-0001", "slot": "1000:0", "path": "[ds] r/01.vmdk", "size_bytes": 1 << 30, "source_capacity_bytes": 1 << 30},
                    {"key": "uuid:6000c29a-0002", "slot": "1000:1", "path": "[ds] r/02.vmdk", "size_bytes": 2 << 30, "source_capacity_bytes": 2 << 30},
                ],
            },
        }
        for point_id, day, kind in (("rp-1", "01", "ActiveFull"), ("rp-2", "02", "Incremental"), ("rp-3", "03", "Incremental")):
            BACKUPS[point_id] = {
                "id": point_id,
                "job_id": "src",
                "vm_id": "vm-1",
                "status": "available",
                "created_at": f"2026-11-{day}T01:00:00+00:00",
                "execution_type": kind,
            }
        self.platform = FakePlatform()
        self.point_config = copy.deepcopy(POINT_CONFIG)
        self.layers = {1: [_layer("rp-1", "full"), _layer("rp-2"), _layer("rp-3")], 2: [_layer("rp-1", "full"), _layer("rp-2"), _layer("rp-3")]}
        self.manifest = [dict(disk) for disk in MANIFEST]
        self.updates: list[dict[str, Any]] = []
        for target, value in (
            ("python.srv.services.internal.replication.replica_update.retry_settings", lambda: (0, 0)),
            ("python.srv.services.internal.replication.replica_config.point_config", lambda _point: copy.deepcopy(self.point_config)),
            ("python.srv.services.internal.replication.replica_update._platform", lambda _inventory: self.platform),
            ("python.srv.services.internal.replication.replica_update._connect", lambda _inventory: FakeNode()),
            ("python.srv.services.internal.replication.replication_session.persist_job", lambda job: JOBS.__setitem__(job["id"], job)),
            ("python.srv.services.internal.replication.replica_update.manifest_disks", lambda _point: self.manifest),
            ("python.srv.services.internal.replication.replica_update._repository_and_key", lambda _point: ({"id": "repo", "type": "local"}, "")),
            ("python.srv.services.internal.replication.replica_update.wait_seconds", lambda: 60),
            ("python.srv.services.internal.replication.replica_leftovers.wait_seconds", lambda: 60),
            ("python.srv.services.internal.replication.replica_leftovers.time.sleep", lambda _seconds: None),
            (
                "python.srv.plugins.vcenter.vcenter_restore._resolve_abr_restore_layers",
                lambda *, selected_point, selected_disk, disk_index, repository: [dict(layer) for layer in self.layers[disk_index]],
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self) -> dict[str, Any]:
        TASKS[SESSION_ID] = {"id": SESSION_ID, "job_id": JOB_ID, "task_type": "replication", "status": "running", "created_at": "2026-11-09T00:00:00"}
        return run_replication({"id": SESSION_ID, "job_id": JOB_ID}, progress_callback=self.updates.append)

    def _replica(self) -> dict[str, Any]:
        return JOBS[JOB_ID]["replica"]


class UpdateTests(_UpdateCase):
    def test_a_tool_that_reports_no_bytes_is_counted_by_the_restore_points_applied(self) -> None:
        self.platform.reported_bytes = 0
        for point_id, sizes in (("rp-2", (8 << 20, 4 << 20)), ("rp-3", (2 << 20, 1 << 20))):
            BACKUPS[point_id]["artifact"] = {"disks": [{"index": 1, "logical_bytes": sizes[0]}, {"index": 2, "logical_bytes": sizes[1]}]}

        result = self._run()

        self.assertEqual(result["transferred_bytes"], 15 << 20)
        self.assertIn("with 2 incremental(s): 15.0 MiB written, in ", result["message"])

    def test_newer_incrementals_are_written_under_a_snapshot_that_is_then_removed(self) -> None:
        result = self._run()

        self.assertEqual(
            self.platform.calls,
            [
                ("snapshot", OUR_SNAPSHOT),
                ("write", "[ds] r/01-000001.vmdk", ["rp-2", "rp-3"]),
                ("write", "[ds] r/02-000001.vmdk", ["rp-2", "rp-3"]),
                ("remove", OUR_SNAPSHOT),
            ],
        )
        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(result["restore_point_id"], "rp-3")
        self.assertIn("Updated replica sql-01-replica on vc-dr/esx-dr-01 to restore point rp-3", result["message"])
        self.assertIn("with 2 incremental(s): 0.0 MiB written, in ", result["message"])
        self.assertEqual(result["transferred_bytes"], 200)
        self.assertEqual(result["applied_point_ids"], ["rp-2", "rp-3"])
        self.assertEqual(self._replica()["point_id"], "rp-3")
        self.assertEqual(self._replica()["fingerprint"], "cv-3")
        self.assertNotEqual(self._replica()["last_success_at"], "2026-11-01T01:30:00+00:00")
        self.assertEqual([disk["path"] for disk in self._replica()["disks"]], ["[ds] r/01.vmdk", "[ds] r/02.vmdk"])

    def test_the_session_is_marked_touched_before_the_snapshot(self) -> None:
        self._run()

        touched = next(index for index, update in enumerate(self.updates) if update.get("replica_touched"))
        snapshot = next(index for index, update in enumerate(self.updates) if "Taking snapshot" in str(update.get("message")))
        self.assertLess(touched, snapshot)

    def test_a_disabled_source_job_is_named_and_the_replica_still_updates_to_its_newest_point(self) -> None:
        JOBS["src"].update({"enabled": False, "status": "disabled"})

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        line = next(update for update in self.updates if "is disabled" in str(update.get("message")))
        self.assertEqual(
            line["message"],
            "Source backup job sql-nightly is disabled, so it takes no new backups; the replica stays at the newest "
            "restore point it has (rp-3 of 2026-11-03T01:00:00+00:00)",
        )
        self.assertNotEqual(line.get("log_level"), "warn")

    def test_an_enabled_source_job_is_not_mentioned(self) -> None:
        JOBS["src"].update({"enabled": True, "status": "active"})

        self._run()

        self.assertFalse(any("is disabled" in str(update.get("message")) for update in self.updates))

    def test_up_to_date_writes_nothing(self) -> None:
        for point_id in ("rp-2", "rp-3"):
            BACKUPS.pop(point_id)

        result = self._run()

        self.assertEqual(result["mode"], "up_to_date")
        self.assertIn("Replica is already up to date", result["message"])
        self.assertEqual(self.platform.calls, [])
        self.assertFalse(result["replica_touched"])

    def test_a_failed_write_reverts_the_snapshot_and_keeps_the_point(self) -> None:
        self.platform.write_error = RuntimeError("Write error on disk")

        with self.assertRaisesRegex(RuntimeError, "Write error on disk"):
            self._run()

        self.assertEqual(self.platform.calls[-1], ("revert", OUR_SNAPSHOT))
        self.assertNotIn(("remove", OUR_SNAPSHOT), self.platform.calls)
        self.assertEqual(self._replica()["point_id"], "rp-1")
        self.assertEqual(self.platform.state["snapshots"], [])

    def test_a_stop_reverts_too_and_is_reported_as_a_stop(self) -> None:
        self.platform.write_error = TaskStopRequested("Session stop requested")

        with self.assertRaises(TaskStopRequested):
            self._run()

        self.assertEqual(self.platform.calls[-1], ("revert", OUR_SNAPSHOT))
        self.assertTrue(any("Stopped: reverting" in str(update.get("message")) for update in self.updates))
        self.assertEqual(self._replica()["point_id"], "rp-1")

    def test_a_stop_reverts_even_though_every_progress_report_now_raises_the_stop(self) -> None:
        self.platform.write_error = TaskStopRequested("Session stop requested")
        stopping = {"on": False}
        original = self.platform.write_increments

        def write_then_stop(*args, **kwargs):
            stopping["on"] = True
            return original(*args, **kwargs)

        def callback(update: dict[str, Any]) -> None:
            self.updates.append(update)
            if stopping["on"]:
                raise TaskStopRequested("Session stop requested")

        self.platform.write_increments = write_then_stop
        TASKS[SESSION_ID] = {"id": SESSION_ID, "job_id": JOB_ID, "task_type": "replication", "status": "running", "created_at": "2026-11-09T00:00:00"}
        with self.assertRaises(TaskStopRequested):
            run_replication({"id": SESSION_ID, "job_id": JOB_ID}, progress_callback=callback)

        self.assertEqual(self.platform.calls[-1], ("revert", OUR_SNAPSHOT))
        self.assertEqual(self.platform.state["snapshots"], [])

    def test_a_failed_revert_says_the_next_run_reverts_it(self) -> None:
        self.platform.write_error = RuntimeError("tool failed")
        self.platform.revert_error = RuntimeError("vCenter busy")

        with self.assertRaisesRegex(ReplicationError, "reverting sql-01-replica to snapshot .* failed too .*next run reverts it"):
            self._run()

    def test_our_leftover_snapshot_is_reverted_first_and_the_fingerprint_is_not_held_against_it(self) -> None:
        self.platform.state["snapshots"] = ["abr-replica-12345678-oldsessi"]
        self.platform.state["fingerprint"] = "cv-crashed"

        result = self._run()

        self.assertEqual(self.platform.calls[0], ("revert", "abr-replica-12345678-oldsessi"))
        self.assertEqual(result["mode"], "incremental")

    def test_an_interrupted_previous_run_lets_the_fingerprint_pass(self) -> None:
        TASKS["before"] = {
            "id": "before",
            "job_id": JOB_ID,
            "task_type": "replication",
            "status": "failed",
            "created_at": "2026-11-08T00:00:00",
            "result": {"replica_touched": True, "failure_kind": "interrupted"},
        }
        self.platform.state["fingerprint"] = "cv-consolidated"

        self.assertEqual(self._run()["mode"], "incremental")

    def test_a_changed_fingerprint_after_a_clean_run_is_a_changed_replica(self) -> None:
        self.platform.state["fingerprint"] = "cv-edited"

        with self.assertRaisesRegex(ReplicationValidationError, "Replica was changed: the configuration or the disks .*Create a new Replication Job"):
            self._run()
        self.assertEqual(self.platform.calls, [])

    def test_each_kind_of_change_is_refused_with_its_reason(self) -> None:
        cases = (
            ("powered_on", True, "is powered on"),
            ("snapshots", ["user-snap"], "has snapshot\\(s\\) or checkpoint\\(s\\) user-snap"),
            ("disks", [{"slot": "1000:0", "path": "a", "size_bytes": 1 << 30}], "are not the ones the last run left"),
            ("disks", [{"slot": "1000:0", "path": "a", "size_bytes": 5 << 30}, {"slot": "1000:1", "path": "b", "size_bytes": 2 << 30}], "changed size"),
        )
        for field, value, text in cases:
            with self.subTest(field=field, text=text):
                self.platform = FakePlatform()
                self.platform.state[field] = value
                with self.assertRaisesRegex(ReplicationValidationError, f"Replica was changed: .*{text}"):
                    self._run()

    def test_a_power_on_between_runs_is_a_changed_replica(self) -> None:
        self.platform.powered_on_since_result = True

        with self.assertRaisesRegex(ReplicationValidationError, "was powered on since the last run"):
            self._run()

    def test_a_hyperv_start_since_the_last_run_is_a_changed_replica(self) -> None:
        for recorded, current in ((None, 408), (400, 408)):
            with self.subTest(recorded=recorded):
                self.setUp()
                JOBS[JOB_ID]["replica"]["start_marker"] = recorded
                self.platform.state["start_marker"] = current
                with self.assertRaisesRegex(ReplicationValidationError, "was powered on since the last run"):
                    self._run()
                self.assertEqual(self.platform.calls, [])

    def test_the_same_start_record_is_no_change(self) -> None:
        JOBS[JOB_ID]["replica"]["start_marker"] = 408
        self.platform.state["start_marker"] = 408

        self.assertEqual(self._run()["mode"], "incremental")
        self.assertEqual(self._replica()["start_marker"], 408)

    def test_a_power_on_is_caught_even_after_an_interrupted_run(self) -> None:
        TASKS["before"] = {
            "id": "before",
            "job_id": JOB_ID,
            "task_type": "replication",
            "status": "failed",
            "created_at": "2026-11-08T00:00:00",
            "result": {"replica_touched": True},
        }
        JOBS[JOB_ID]["replica"]["start_marker"] = None
        self.platform.state["start_marker"] = 12

        with self.assertRaisesRegex(ReplicationValidationError, "was powered on since the last run"):
            self._run()

    def test_a_replica_recorded_before_start_markers_existed_is_not_held_to_one(self) -> None:
        self.platform.state["start_marker"] = 12

        self.assertEqual(self._run()["mode"], "incremental")

    def test_a_missing_replica_fails_with_the_advice(self) -> None:
        self.platform.state = None

        with self.assertRaisesRegex(ReplicationError, "Replica not found. Create a new Replication Job"):
            self._run()

    def test_a_replica_on_another_host_is_updated_with_a_warning(self) -> None:
        self.platform.state["host"] = "esx-dr-02"

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        warning = next(update for update in self.updates if "moved from host" in str(update.get("message")))
        self.assertEqual(warning["log_level"], "warn")
        self.assertEqual(self._replica()["host"], "esx-dr-02")

    def test_a_task_still_running_on_the_replica_is_waited_for(self) -> None:
        self.platform.state["busy"] = True
        self.platform.state["busy_tasks"] = ["RemoveSnapshot_Task"]
        self.platform.busy_rounds = 2

        self.assertEqual(self._run()["mode"], "incremental")
        self.assertTrue(any("Waiting for RemoveSnapshot_Task" in str(update.get("message")) for update in self.updates))

    def test_a_task_that_never_ends_fails_with_its_name(self) -> None:
        self.platform.state["busy"] = True
        self.platform.state["busy_tasks"] = ["RemoveSnapshot_Task"]
        with patch("python.srv.services.internal.replication.replica_leftovers.wait_seconds", lambda: 0), self.assertRaisesRegex(ReplicationError, "still busy with RemoveSnapshot_Task"):
            self._run()

    def test_an_unfinished_consolidation_is_finished_first(self) -> None:
        self.platform.state["consolidation_needed"] = True

        self._run()

        self.assertEqual(self.platform.calls[0], ("consolidate",))

    def test_cases_that_need_the_disks_rebuilt_go_to_the_rebuild_and_write_no_incrementals(self) -> None:
        cases = {
            "an active full": lambda: self.layers.__setitem__(2, [_layer("rp-3", "full")]),
            "a full of one disk": lambda: self.layers.__setitem__(1, [_layer("rp-1", "full"), _layer("rp-2"), _layer("rp-3", "full")]),
            "a new disk": lambda: self.manifest.append({"index": 3, "uuid": "6000c29a-0003", "capacity_bytes": 1 << 30}),
            "a removed disk": lambda: self.manifest.pop(),
            "a grown disk": lambda: self.manifest[0].__setitem__("capacity_bytes", 3 << 30),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                change()
                rebuild = patch("python.srv.services.internal.replication.replica_update._update_mode_two", side_effect=ReplicationError("rebuild chosen"))
                with rebuild, self.assertRaisesRegex(ReplicationError, "rebuild chosen"):
                    self._run()
                self.assertEqual(self.platform.calls, [])


class KeepaliveTests(_UpdateCase):
    def _run_on(self, node: KeptAliveNode) -> None:
        with patch("python.srv.services.internal.replication.replica_update._connect", lambda _inventory: node):
            self._run()

    def test_the_vcenter_session_is_kept_alive_for_the_whole_run(self) -> None:
        node = KeptAliveNode()
        original = self.platform.write_increments

        def write(*args, **kwargs):
            node.events.append("write")
            return original(*args, **kwargs)

        self.platform.write_increments = write
        self._run_on(node)

        self.assertEqual(node.events[0], "keepalive on")
        self.assertEqual(node.events[-1], "keepalive off")
        self.assertIn("write", node.events)

    def test_the_keepalive_stops_when_the_run_fails(self) -> None:
        node = KeptAliveNode()
        self.platform.write_error = RuntimeError("tool failed")

        with self.assertRaises(RuntimeError):
            self._run_on(node)

        self.assertEqual(node.events, ["keepalive on", "keepalive off"])


if __name__ == "__main__":
    unittest.main()


class StagingSweepTests(_UpdateCase):
    FOLDER = "C:\\Hyper-V\\sql-01-replica-staging-aaaaaaaa\\Virtual Hard Disks"
    OTHER = "C:\\Hyper-V\\sql-01-replica-staging-bbbbbbbb\\Virtual Hard Disks"

    def setUp(self) -> None:
        super().setUp()
        self.platform.PLATFORM = "hyper-v"
        self.own = f"{self.FOLDER}\\disk-1-abr-replica-12345678-b.vhdx"
        JOBS[JOB_ID]["replica"]["disks"][0]["path"] = self.own
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.platform.detached_staging_files = lambda _node, _host, _datastore, _tag: list(self.listed)
        self.platform.delete_disk_files = lambda _node, _host, paths: self.deleted.extend(paths) or []

    def test_only_leftovers_of_an_interrupted_rebuild_are_removed(self) -> None:
        planned = f"{self.OTHER}\\disk-2-abr-replica-12345678-a.vhdx"
        TASKS["old"] = {
            "id": "old",
            "job_id": JOB_ID,
            "task_type": "replication",
            "status": "failed",
            "created_at": "2026-11-08T00:00:00",
            "result": {"swap_plan": {"changes": [{"new_path": planned, "old_path": ""}]}},
        }
        leftover = f"{self.OTHER}\\disk-1-abr-replica-12345678-b.vhdx"
        self.listed = [
            self.own,
            f"{self.FOLDER}\\disk-1-abr-replica-12345678-b_7F626267-90AC.avhdx",
            leftover,
            planned,
        ]

        self._run()

        self.assertEqual(self.deleted, [leftover])

    def test_nothing_left_means_nothing_removed(self) -> None:
        self._run()

        self.assertEqual(self.deleted, [])
        self.assertFalse(any("interrupted rebuild" in str(update.get("message")) for update in self.updates))
