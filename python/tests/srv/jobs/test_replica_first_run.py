# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services.internal.replication import replication_session
from python.srv.services.internal.replication.replica_update import run_replication
from python.srv.services.internal.replication.replication_session import (
    ReplicationError,
    ReplicationValidationError,
)
from python.srv.services.internal.replication.replication_settings import (
    replication_view,
)

JOB_ID = "12345678-aaaa-bbbb-cccc-000000000001"
SESSION_ID = "session-1"


POINT_CONFIG = {"num_cpu": 2, "memory_mb": 4096, "network_adapters": []}


class ConfigFakes:
    HARDWARE_KEYS = ("num_cpu", "memory_mb")
    replica_config: dict[str, Any] | None = None
    available_networks: frozenset[str] = frozenset()
    unsynced_items: tuple[str, ...] = ()
    apply_error: Exception | None = None
    rename_error: Exception | None = None

    def config_calls(self) -> list[tuple]:
        if "_config_calls" not in self.__dict__:
            self._config_calls: list[tuple] = []
        return self._config_calls

    def read_config(self, _node: Any, _state: dict[str, Any]) -> dict[str, Any]:
        if self.replica_config is None:
            self.replica_config = copy.deepcopy(POINT_CONFIG)
        return copy.deepcopy(self.replica_config)

    def networks(self, _node: Any, _state: dict[str, Any], names: list[str]) -> set[str]:
        return {name for name in names if name in self.available_networks}

    def apply_config(self, _node: Any, state: dict[str, Any], hardware: dict[str, Any], *, remove, change, add) -> list[str]:
        self.config_calls().append(("apply", dict(hardware), list(remove), [dict(item) for item in change], [dict(item) for item in add]))
        if self.apply_error is not None:
            raise self.apply_error
        config = self.read_config(None, state)
        config.update(hardware)
        adapters = [adapter for adapter in config["network_adapters"] if adapter["id"] not in remove]
        for item in change:
            adapter = next(adapter for adapter in adapters if adapter["id"] == item["id"])
            adapter.update(network=item["network"] or adapter["network"], connected=item["connected"], vlan_id=item["vlan_id"])
        added = []
        for item in add:
            adapter_id = f"nic-{len(self.config_calls())}-{len(added) + 1}"
            adapters.append({"id": adapter_id, "label": item["label"], "network": item["network"], "connected": item["connected"], "vlan_id": item["vlan_id"]})
            added.append(adapter_id)
        config["network_adapters"] = adapters
        self.replica_config = config
        self._config_changed(state)
        return added

    def rename(self, _node: Any, state: dict[str, Any], new_name: str) -> None:
        self.config_calls().append(("rename", state["name"], new_name))
        if self.rename_error is not None:
            raise self.rename_error
        self._renamed(state, new_name)

    def unsynced(self, _current: dict[str, Any], _wanted: dict[str, Any]) -> list[str]:
        return list(self.unsynced_items)

    def _config_changed(self, state: dict[str, Any]) -> None:
        pass

    def _renamed(self, state: dict[str, Any], new_name: str) -> None:
        pass


class FakePlatform(ConfigFakes):
    PLATFORM = "vsphere"

    def _renamed(self, state: dict[str, Any], new_name: str) -> None:
        self.vms[state["vm_id"]]["name"] = new_name

    def __init__(self) -> None:
        self.vms: dict[str, dict[str, Any]] = {}
        self.destroyed: list[str] = []
        self.files: list[dict[str, Any]] = []
        self.removed_files: list[str] = []

    def vm(self, vm_id: str, name: str, *, marker: str = "", powered_on: bool = False) -> dict[str, Any]:
        state = {
            "vm_id": vm_id,
            "name": name,
            "host": "esx-dr-01",
            "powered_on": powered_on,
            "marker_text": marker,
            "snapshots": [],
            "disks": [{"slot": "1000:0", "path": f"[ds] {name}/01-disk.vmdk", "size_bytes": 1 << 30, "parent_path": ""}],
            "fingerprint": "cv-1",
            "busy": False,
            "consolidation_needed": False,
        }
        self.vms[vm_id] = state
        return state

    def rebuild_location(self, state: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
        return state["host"] or settings["target_host"], settings["target_datastore"]

    def find_replica(self, _node: Any, vm_id: str) -> dict[str, Any] | None:
        return self.vms.get(vm_id)

    def replicas_with_marker(self, _node: Any, prefix: str) -> list[dict[str, Any]]:
        return [state for state in self.vms.values() if prefix in state["marker_text"]]

    def destroy_replica(self, _node: Any, state: dict[str, Any]) -> list[str]:
        self.destroyed.append(state["vm_id"])
        self.vms.pop(state["vm_id"], None)
        return []

    def replica_folder(self, datastore: str, name: str) -> str:
        return f"{datastore}\\{name}"

    def tagged_files(self, _node: Any, _host: str, _folder: str, tag: str) -> list[dict[str, Any]]:
        return [item for item in self.files if tag in item["path"]]

    def detached_staging_files(self, _node: Any, _host: str, _datastore: str, tag: str) -> list[str]:
        self.staging_scans = getattr(self, "staging_scans", 0) + 1
        return [item["path"] for item in getattr(self, "staging_files", []) if tag in item["path"] and not item.get("attached")]

    def delete_disk_files(self, _node: Any, _host: str, paths: list[str]) -> list[str]:
        self.deleted_staging = getattr(self, "deleted_staging", []) + list(paths)
        return []

    def remove_files(self, _node: Any, _host: str, paths: list[str]) -> list[str]:
        self.removed_files.extend(paths)
        return []


class FakeNode:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


class FakeRestorePlugin:
    def __init__(self, platform: FakePlatform, *, fail_after_create: BaseException | None = None) -> None:
        self.platform = platform
        self.fail_after_create = fail_after_create
        self.tasks: list[dict[str, Any]] = []

    def restore(self, task: dict[str, Any], progress_callback=None) -> dict[str, Any]:
        self.tasks.append(task)
        parameters = task["parameters"]
        self.platform.vm("vm-new", parameters["target_vm_name"], marker=parameters["replica_marker"])
        progress_callback({"stage": "prepare", "progress": 7, "message": "Created VM", "created_vm_id": "vm-new"})
        if self.fail_after_create is not None:
            self.platform.destroy_replica(None, self.platform.vms["vm-new"])
            raise self.fail_after_create
        return {
            "target_vm_id": "vm-new",
            "processed_bytes": 2 << 30,
            "transferred_bytes": 1 << 30,
            "warnings": [],
            "disks": [{"index": 1, "restored_datastore_path": f"[ds] {parameters['target_vm_name']}/01-disk.vmdk"}],
        }


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.point_config = copy.deepcopy(POINT_CONFIG)
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
                "target_host_name": "esx-dr-01",
                "target_datastore": "ds-dr",
                "target_datastore_name": "ds-dr",
                "replica_name": "sql-01-replica",
            },
            "replica": {},
        }
        for point_id, created in (("rp-1", "2026-11-01T01:00:00+00:00"), ("rp-2", "2026-11-02T01:00:00+00:00")):
            BACKUPS[point_id] = {
                "id": point_id,
                "job_id": "src",
                "vm_id": "vm-1",
                "vm_name": "sql-01",
                "status": "available",
                "created_at": created,
                "execution_type": "ActiveFull",
            }
        self.platform = FakePlatform()
        self.updates: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        original_save = replication_session.save_replica

        def record_save(job_id: str, replica: dict[str, Any], **kwargs: Any) -> None:
            self.saved.append(dict(replica))
            original_save(job_id, replica, **kwargs)

        for target, value in (
            ("python.srv.services.internal.replication.replica_update.retry_settings", lambda: (0, 0)),
            ("python.srv.services.internal.replication.replica_config.point_config", lambda _point: copy.deepcopy(self.point_config)),
            ("python.srv.services.internal.replication.replica_update._platform", lambda _inventory: self.platform),
            ("python.srv.services.internal.replication.replica_update._connect", lambda _inventory: FakeNode()),
            ("python.srv.services.internal.replication.replica_update.transport_problem", lambda _transport: None),
            ("python.srv.services.internal.replication.replication_session.persist_job", lambda job: JOBS.__setitem__(job["id"], job)),
            ("python.srv.services.internal.replication.replica_update.save_replica", record_save),
            ("python.srv.services.internal.replication.replica_leftovers.save_replica", record_save),
            (
                "python.srv.services.internal.replication.replica_update.manifest_disks",
                lambda _point: [{"index": 1, "uuid": "6000c29a-0001", "capacity_bytes": 1 << 30}],
            ),
            (
                "python.srv.services.abr_v41.plan_restore_session",
                lambda payload: (
                    {"id": "planned", "hypervisor_id": "dr", "parameters": {"point_id": payload["point_id"], "target_vm_name": payload["target_vm_name"]}},
                    {},
                    "summary",
                ),
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, plugin: FakeRestorePlugin | None = None) -> dict[str, Any]:
        self.plugin = plugin or FakeRestorePlugin(self.platform)
        with patch("python.srv.services.internal.replication.replica_update.build_hypervisor_plugin", return_value=self.plugin):
            return run_replication({"id": SESSION_ID, "job_id": JOB_ID}, progress_callback=self.updates.append)

    def test_the_first_run_creates_the_replica_from_the_newest_point(self) -> None:
        result = self._run()

        replica = JOBS[JOB_ID]["replica"]
        self.assertEqual(replica["vm_id"], "vm-new")
        self.assertEqual(replica["point_id"], "rp-2")
        self.assertTrue(replica["last_success_at"])
        self.assertEqual(replica["fingerprint"], "cv-1")
        self.assertEqual(
            replica["disks"],
            [
                {
                    "key": "uuid:6000c29a-0001",
                    "slot": "1000:0",
                    "path": "[ds] sql-01-replica/01-disk.vmdk",
                    "size_bytes": 1 << 30,
                    "source_capacity_bytes": 1 << 30,
                }
            ],
        )
        self.assertEqual(result["mode"], "create")
        self.assertEqual(result["restore_point_id"], "rp-2")
        self.assertIn("Created replica sql-01-replica", result["message"])

    def test_the_adapters_of_a_new_replica_are_paired_and_its_notes_are_only_logged(self) -> None:
        self.point_config = {"num_cpu": 2, "memory_mb": 4096, "network_adapters": [{"label": "Network adapter 1", "network": "backup-net", "connected": True}]}
        self.platform.replica_config = {
            "num_cpu": 2,
            "memory_mb": 4096,
            "network_adapters": [{"id": "4000", "label": "Network adapter 1", "network": "backup-net", "connected": False, "vlan_id": 0}],
        }
        self.platform.unsynced_items = ("its firmware is bios (the source VM: efi)",)

        result = self._run()

        self.assertEqual(result["mode"], "create")
        replica = JOBS[JOB_ID]["replica"]
        self.assertEqual(replica["adapters"], [{"source": "label:network adapter 1", "replica": "4000"}])
        self.assertEqual(len(replica["config_warnings"]), 2)
        configure = [update for update in self.updates if update.get("stage") == "configure"]
        self.assertEqual(len(configure), 2)
        self.assertFalse(any(update.get("log_level") == "warn" for update in configure))
        self.assertEqual(self.platform.config_calls(), [])

    def test_the_restore_runs_as_this_session_with_the_marker_and_the_disk_suffix(self) -> None:
        self._run()

        task = self.plugin.tasks[0]
        self.assertEqual(task["id"], SESSION_ID)
        self.assertEqual(task["parameters"]["replica_marker"], f"abr-replica:{JOB_ID}:{SESSION_ID}")
        self.assertEqual(task["parameters"]["disk_name_suffix"], "-abr-replica-12345678-a")

    def test_the_vm_id_is_saved_as_soon_as_the_vm_exists_and_the_session_is_marked_touched(self) -> None:
        self._run()

        self.assertEqual(self.saved[0], {"vm_id": "vm-new", "vm_name": "sql-01-replica", "created_by_session": SESSION_ID})
        self.assertNotIn("point_id", self.saved[0])
        self.assertTrue(any(update.get("replica_touched") for update in self.updates))
        touched_at = next(index for index, update in enumerate(self.updates) if update.get("replica_touched"))
        created_at = next(index for index, update in enumerate(self.updates) if update.get("created_vm_id"))
        self.assertLess(touched_at, created_at)

    def test_a_chosen_start_point_is_used(self) -> None:
        JOBS[JOB_ID]["replication"]["restore_point_id"] = "rp-1"

        self.assertEqual(self._run()["restore_point_id"], "rp-1")

    def test_a_failed_creation_forgets_the_vm_the_restore_removed(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run(FakeRestorePlugin(self.platform, fail_after_create=RuntimeError("tool failed")))

        self.assertEqual(JOBS[JOB_ID]["replica"], {})

    def test_a_cancelled_creation_forgets_it_too(self) -> None:
        from python.srv.core.task_executor import TaskStopRequested

        with self.assertRaises(TaskStopRequested):
            self._run(FakeRestorePlugin(self.platform, fail_after_create=TaskStopRequested("stop")))

        self.assertEqual(JOBS[JOB_ID]["replica"], {})

    def test_an_unfinished_replica_with_our_marker_is_removed_before_a_new_one_is_made(self) -> None:
        self.platform.vm("vm-old", "sql-01-replica", marker=f"abr-replica:{JOB_ID}:old-session")
        JOBS[JOB_ID]["replica"] = {"vm_id": "vm-old", "vm_name": "sql-01-replica", "created_by_session": "old-session"}

        self._run()

        self.assertIn("vm-old", self.platform.destroyed)
        self.assertEqual(JOBS[JOB_ID]["replica"]["vm_id"], "vm-new")

    def test_an_unfinished_replica_without_our_marker_is_not_touched(self) -> None:
        self.platform.vm("vm-old", "sql-01-replica", marker="someone edited the notes")
        JOBS[JOB_ID]["replica"] = {"vm_id": "vm-old"}

        with self.assertRaisesRegex(ReplicationError, "Replica creation was not completed. Delete VM sql-01-replica and create a new Replication Job"):
            self._run()

        self.assertEqual(self.platform.destroyed, [])

    def test_a_powered_on_unfinished_replica_is_not_touched(self) -> None:
        self.platform.vm("vm-old", "sql-01-replica", marker=f"abr-replica:{JOB_ID}:old", powered_on=True)
        JOBS[JOB_ID]["replica"] = {"vm_id": "vm-old"}

        with self.assertRaisesRegex(ReplicationError, "is powered on"):
            self._run()

        self.assertEqual(self.platform.destroyed, [])

    def test_an_unfinished_replica_that_is_gone_is_forgotten(self) -> None:
        JOBS[JOB_ID]["replica"] = {"vm_id": "vm-gone"}

        self._run()

        self.assertEqual(JOBS[JOB_ID]["replica"]["vm_id"], "vm-new")

    def test_a_marked_vm_the_metadata_does_not_know_is_removed_when_its_session_did_not_complete(self) -> None:
        TASKS["crashed"] = {"id": "crashed", "status": "failed"}
        self.platform.vm("vm-crash", "sql-01-replica", marker=f"note\nabr-replica:{JOB_ID}:crashed")

        self._run()

        self.assertIn("vm-crash", self.platform.destroyed)

    def test_a_clone_of_a_finished_replica_is_kept(self) -> None:
        TASKS["done"] = {"id": "done", "status": "completed"}
        self.platform.vm("vm-clone", "clone-of-replica", marker=f"abr-replica:{JOB_ID}:done")

        self._run()

        self.assertNotIn("vm-clone", self.platform.destroyed)

    def test_hyperv_disk_files_an_interrupted_first_run_left_are_removed_only_when_detached(self) -> None:
        self.platform.PLATFORM = "hyper-v"
        self.platform.files = [
            {"path": "D:\\Hyper-V\\sql-01-replica\\Virtual Hard Disks\\disk-abr-replica-12345678-a.vhdx", "attached": False},
            {"path": "D:\\Hyper-V\\sql-01-replica\\Virtual Hard Disks\\other-abr-replica-12345678-a.vhdx", "attached": True},
        ]

        self._run()

        self.assertEqual(self.platform.removed_files, [self.platform.files[0]["path"]])

    def test_hyperv_disks_an_interrupted_rebuild_left_detached_are_removed(self) -> None:
        self.platform.PLATFORM = "hyper-v"
        old = "C:\\Hyper-V\\sql-01-replica-staging-11111111\\Virtual Hard Disks\\disk-1-abr-replica-12345678-b.vhdx"
        self.platform.staging_files = [{"path": old}, {"path": "C:\\Hyper-V\\x-staging-22222222\\disk-abr-replica-99999999-a.vhdx"}]

        self._run()

        self.assertEqual(self.platform.deleted_staging, [old])
        self.assertTrue(
            any(
                "an interrupted rebuild of this job left in C:\\Hyper-V\\sql-01-replica-staging-11111111\\Virtual Hard Disks"
                in str(update.get("message"))
                for update in self.updates
            )
        )

    def test_vsphere_needs_no_staging_sweep(self) -> None:
        self._run()

        self.assertEqual(getattr(self.platform, "staging_scans", 0), 0)

    def test_a_source_vm_that_left_the_job_fails_with_its_reason(self) -> None:
        JOBS["src"]["vm_ids"] = ["vm-9"]

        with self.assertRaisesRegex(ReplicationValidationError, "is no longer in source backup job"):
            self._run()

    def test_a_reassigned_source_vm_fails_with_the_advice(self) -> None:
        JOBS["src"]["vm_ids"] = ["vm-9"]
        JOBS["src"]["vm_reassignments"] = [{"previous_vm_id": "vm-1", "vm_id": "vm-9"}]

        with self.assertRaisesRegex(ReplicationValidationError, "now backs up another VM.*Create a new Replication Job"):
            self._run()

    def test_a_reassigned_source_vm_is_named_by_its_new_vm(self) -> None:
        JOBS["src"]["vm_ids"] = ["vm-9"]
        JOBS["src"]["vm_reassignments"] = [{"previous_vm_id": "vm-1", "vm_id": "vm-9", "vm_name": "sql-01-new"}]

        with self.assertRaisesRegex(ReplicationValidationError, "under the name sql-01-new; this replica belongs to the old one"):
            self._run()

    def test_a_renamed_source_vm_and_job_are_named_as_they_are_now(self) -> None:
        JOBS["src"].update(name="sql-nightly-v2", inventory_id="src-inv")
        RESOURCES_BY_HV["src-inv"] = {("virtual_machine", "vm-1"): {"name": "sql-01-renamed"}}
        self.addCleanup(RESOURCES_BY_HV.clear)

        result = self._run()
        session = replication_session.create_replication_session(JOBS[JOB_ID], trigger="manual", run_id="run-2")

        self.assertIn("of sql-01-renamed (backup job sql-nightly-v2)", result["message"])
        self.assertIn("of sql-01-renamed (backup job sql-nightly-v2) on vc-dr", session["logs"][0]["message"])
        self.assertEqual(JOBS[JOB_ID]["replication"]["source_vm_name"], "sql-01")
        view = replication_view(JOBS[JOB_ID])
        self.assertEqual((view["source_vm"]["name"], view["source_job"]["name"]), ("sql-01-renamed", "sql-nightly-v2"))

    def test_no_valid_point_fails_with_its_reason(self) -> None:
        for point in BACKUPS.values():
            point["status"] = "deleting"

        with self.assertRaisesRegex(ReplicationValidationError, "has no restore point of sql-01 that can be replicated"):
            self._run()

    def test_an_unreachable_target_is_named(self) -> None:
        def refuse(_inventory: Any) -> Any:
            raise ReplicationError("The target environment vc-dr cannot be reached: timed out")

        with patch("python.srv.services.internal.replication.replica_update._connect", refuse), self.assertRaisesRegex(ReplicationError, "cannot be reached"):
            self._run()


if __name__ == "__main__":
    unittest.main()
