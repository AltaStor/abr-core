# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import tempfile
import types
import threading
import time
import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES, TASKS
from python.srv.services import backup_catalog

REPO_ID = "repo-1"
HV_ID = "hv-1"


def _task(task_id: str, status: str, **parameters) -> dict:
    return {
        "id": task_id,
        "task_type": "backup",
        "status": status,
        "job_id": "job-1",
        "hypervisor_id": HV_ID,
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "parameters": {"repository_id": REPO_ID, "vm_name": "web01", **parameters},
    }


class FakeNode:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []
        self.raise_on_remove: Exception | None = None
        self.connected = 0
        self.lock_releases: list[tuple[str, str | None]] = []
        self.raise_on_release: Exception | None = None
        self.lock_held = True

    def __enter__(self) -> "FakeNode":
        self.connected += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        if self.raise_on_remove is not None:
            raise self.raise_on_remove
        self.removed.append((vm_name, snapshot_name))

    def release_access_lock(self, vm_name: str, vm_moref: str | None = None) -> bool:
        self.lock_releases.append((vm_name, vm_moref))
        if self.raise_on_release is not None:
            raise self.raise_on_release
        return self.lock_held


class SweepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, REPOSITORIES, TASKS):
            store.clear()
        self._root = tempfile.mkdtemp(prefix="abr-interrupted-")
        self.addCleanup(lambda: None)
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": self._root}
        HYPERVISORS[HV_ID] = {
            "id": HV_ID,
            "kind": "vcenter",
            "metadata": {"host": "vc.local", "credentials": {"username": "a", "password": "b"}},
        }
        self.node = FakeNode()
        build_plugin = patch(
            "python.srv.plugins.build_hypervisor_plugin",
            return_value=self.node,
        )
        self.build_plugin = build_plugin.start()
        self.addCleanup(build_plugin.stop)
        self._real_sweep_spawn = backup_catalog._start_interrupted_task_sweep
        sweep_now = patch(
            "python.srv.services.backup_catalog._start_interrupted_task_sweep",
            side_effect=backup_catalog._sweep_interrupted_tasks,
        )
        self._sweep_now = sweep_now.start()
        self.addCleanup(sweep_now.stop)

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, REPOSITORIES, TASKS):
            store.clear()

    def _output_dir(self, task_id: str) -> pathlib.Path:
        folder = pathlib.Path(self._root) / "web01" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01-disk.vmdk.abri").write_bytes(b"partial")
        return folder

class InterruptedTaskReconciliationTests(SweepTestCase):
    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_a_task_left_running_is_failed_and_its_output_discarded(self, list_mock, remove_mock) -> None:
        TASKS["task-a"] = _task("task-a", "running")
        self._output_dir("task-a")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-a"]["status"], "failed")
        self.assertEqual(TASKS["task-a"]["result"]["failure_kind"], "interrupted")
        self.assertIn("server stopped", TASKS["task-a"]["result"]["message"])
        self.assertTrue(TASKS["task-a"]["result"]["finished_at"])
        self.assertEqual(remove_mock.call_count, 1)
        self.assertEqual(remove_mock.call_args.args[2], "01-disk.vmdk")

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value=set())
    def test_tasks_that_are_not_running_are_left_alone(self, list_mock, remove_mock) -> None:
        for status in ("completed", "failed", "stopped", "queued"):
            TASKS[f"task-{status}"] = _task(f"task-{status}", status)

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 0)
        self.assertEqual({task["status"] for task in TASKS.values()},
                         {"completed", "failed", "stopped", "queued"})
        remove_mock.assert_not_called()

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_output_a_restore_point_already_references_is_kept(self, list_mock, remove_mock) -> None:
        TASKS["task-b"] = _task("task-b", "running")
        folder = self._output_dir("task-b")
        BACKUPS["point-1"] = {
            "id": "point-1",
            "vm_id": "vm-1",
            "vm_name": "web01",
            "artifact": {
                "manifest_path": str(folder / "manifest.json"),
                "disks": [{"abr_path": str(folder)}],
            },
        }

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-b"]["status"], "failed")
        remove_mock.assert_not_called()
        list_mock.assert_not_called()

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", side_effect=RuntimeError("offline"))
    def test_a_repository_that_cannot_be_listed_still_fails_the_task(self, list_mock, remove_mock) -> None:
        TASKS["task-c"] = _task("task-c", "running")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-c"]["status"], "failed")
        remove_mock.assert_not_called()

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_an_s3_repository_is_swept_by_its_prefix(self, list_mock, remove_mock) -> None:
        REPOSITORIES[REPO_ID] = {
            "id": REPO_ID,
            "name": "S3",
            "type": "s3",
            "metadata": {"s3": {"bucket": "abr-dev", "prefix": "chains"}},
        }
        TASKS["task-d"] = _task("task-d", "running")

        backup_catalog.fail_interrupted_tasks()

        self.assertEqual(list_mock.call_args.args[1], "chains/web01/task-d")
        self.assertEqual(remove_mock.call_args.args[1], "chains/web01/task-d")

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup", side_effect=RuntimeError("object is locked"))
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk", "02-disk.vmdk"})
    def test_output_that_cannot_be_removed_still_fails_the_task(self, list_mock, remove_mock) -> None:
        TASKS["task-e"] = _task("task-e", "running")
        self._output_dir("task-e")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-e"]["status"], "failed")
        self.assertEqual(remove_mock.call_count, 2)

    def test_a_slow_sweep_does_not_hold_up_the_caller(self) -> None:
        self._sweep_now.side_effect = self._real_sweep_spawn
        release = threading.Event()
        entered = threading.Event()

        def _slow(*args, **kwargs):
            entered.set()
            release.wait(10)
            return {"01-disk.vmdk"}

        TASKS["task-f"] = _task("task-f", "running")
        with patch("python.srv.services.backup_catalog.list_disk_tool_backups", side_effect=_slow), \
                patch("python.srv.services.backup_catalog.remove_disk_tool_backup"):
            started = time.monotonic()
            abandoned = backup_catalog.fail_interrupted_tasks()
            elapsed = time.monotonic() - started
            self.assertTrue(entered.wait(5))
            release.set()
            for thread in threading.enumerate():
                if thread.name == "interrupted-task-sweep":
                    thread.join(5)

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-f"]["status"], "failed")
        self.assertLess(elapsed, 2)


class InterruptedBackupSnapshotTests(SweepTestCase):
    @staticmethod
    def _running_with_snapshot(task_id: str, name: str = "FullBackup_1790942477") -> dict:
        task = _task(task_id, "running")
        task["result"] = {
            "stage": "transfer",
            "progress": 44,
            "message": "Backing up disk 1/2",
            "backup_snapshot": {"vm_name": "web01", "name": name},
        }
        return task

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_the_snapshot_an_abandoned_backup_left_is_removed_at_startup(self, list_mock, remove_mock) -> None:
        TASKS["task-s1"] = self._running_with_snapshot("task-s1")
        self._output_dir("task-s1")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(self.node.removed, [("web01", "FullBackup_1790942477")])
        self.assertEqual(TASKS["task-s1"]["status"], "failed")
        self.assertEqual(TASKS["task-s1"]["result"]["failure_kind"], "interrupted")
        self.assertNotIn("backup_snapshot", TASKS["task-s1"]["result"])
        self.assertEqual(remove_mock.call_count, 1)

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_a_snapshot_that_cannot_be_removed_yet_is_kept_for_the_next_backup(self, list_mock, remove_mock) -> None:
        self.node.raise_on_remove = RuntimeError("vCenter is still booting")
        TASKS["task-s2"] = self._running_with_snapshot("task-s2")
        self._output_dir("task-s2")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-s2"]["status"], "failed")
        self.assertEqual(
            TASKS["task-s2"]["result"]["backup_snapshot"],
            {"vm_name": "web01", "name": "FullBackup_1790942477"},
        )
        self.assertEqual(remove_mock.call_count, 1)
        self.assertEqual(
            backup_catalog.stranded_backup_snapshots(HV_ID, "web01"),
            [("task-s2", "FullBackup_1790942477")],
        )

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value=set())
    def test_a_backup_that_recorded_no_snapshot_never_reaches_the_hypervisor(self, list_mock, remove_mock) -> None:
        TASKS["task-s3"] = _task("task-s3", "running")

        backup_catalog.fail_interrupted_tasks()

        self.build_plugin.assert_not_called()
        self.assertEqual(self.node.removed, [])

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_a_hypervisor_that_is_gone_leaves_the_snapshot_recorded(self, list_mock, remove_mock) -> None:
        HYPERVISORS.clear()
        TASKS["task-s4"] = self._running_with_snapshot("task-s4")
        self._output_dir("task-s4")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 1)
        self.build_plugin.assert_not_called()
        self.assertIn("backup_snapshot", TASKS["task-s4"]["result"])
        self.assertEqual(remove_mock.call_count, 1)

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value={"01-disk.vmdk"})
    def test_each_abandoned_backup_gets_its_own_snapshot_removed(self, list_mock, remove_mock) -> None:
        TASKS["task-s5"] = self._running_with_snapshot("task-s5", "FullBackup_1")
        TASKS["task-s6"] = self._running_with_snapshot("task-s6", "FullBackup_2")
        self._output_dir("task-s5")
        self._output_dir("task-s6")

        abandoned = backup_catalog.fail_interrupted_tasks()

        self.assertEqual(abandoned, 2)
        self.assertEqual(
            sorted(name for _vm, name in self.node.removed),
            ["FullBackup_1", "FullBackup_2"],
        )

    def test_only_snapshots_of_the_same_vm_and_hypervisor_are_offered_to_a_backup(self) -> None:
        TASKS["task-done"] = self._running_with_snapshot("task-done", "FullBackup_mine")
        TASKS["task-done"]["status"] = "failed"
        TASKS["task-live"] = self._running_with_snapshot("task-live", "FullBackup_live")
        other_vm = self._running_with_snapshot("task-other-vm", "FullBackup_other")
        other_vm["status"] = "failed"
        other_vm["result"]["backup_snapshot"]["vm_name"] = "db01"
        TASKS["task-other-vm"] = other_vm
        other_hv = self._running_with_snapshot("task-other-hv", "FullBackup_hv")
        other_hv["status"] = "failed"
        other_hv["hypervisor_id"] = "hv-2"
        TASKS["task-other-hv"] = other_hv

        self.assertEqual(
            backup_catalog.stranded_backup_snapshots(HV_ID, "web01"),
            [("task-done", "FullBackup_mine")],
        )
        self.assertEqual(
            backup_catalog.stranded_backup_snapshots(HV_ID, "web01", exclude_task_id="task-done"),
            [],
        )

    def test_forgetting_a_snapshot_leaves_the_rest_of_the_result_alone(self) -> None:
        TASKS["task-s7"] = self._running_with_snapshot("task-s7")

        backup_catalog.forget_task_backup_snapshot("task-s7")

        self.assertNotIn("backup_snapshot", TASKS["task-s7"]["result"])
        self.assertEqual(TASKS["task-s7"]["result"]["progress"], 44)
        backup_catalog.forget_task_backup_snapshot("task-s7")
        backup_catalog.forget_task_backup_snapshot("no-such-task")


class AccessLockReleaseTests(SweepTestCase):
    def _with_lock(self, task_id: str, status: str, **parameters) -> dict:
        task = _task(task_id, status, **parameters)
        task["access_lock"] = {"vm_name": "web01", "vm_moref": "vm-42"}
        return task

    def _messages(self, task_id: str) -> list[tuple[str, str]]:
        return [(entry["level"], entry["message"]) for entry in TASKS[task_id].get("logs") or []]

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value=set())
    def test_the_relocation_block_an_abandoned_backup_left_is_lifted_at_startup(self, list_mock, remove_mock) -> None:
        TASKS["task-l1"] = self._with_lock("task-l1", "running")

        backup_catalog.fail_interrupted_tasks()

        self.assertEqual(self.node.lock_releases, [("web01", "vm-42")])
        self.assertIsNone(TASKS["task-l1"]["access_lock"])
        self.assertEqual(TASKS["task-l1"]["status"], "failed")
        self.assertIn("Lifted the block on relocating VM web01", self._messages("task-l1")[-1][1])

    def test_a_lock_record_on_a_direct_esxi_host_is_dropped_without_a_warning(self) -> None:
        from python.srv.plugins.vcenter.vcenter import VCenterNode

        class _DirectEsxiNode(VCenterNode):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def _enable_methods(self, entity, methods):
                raise AssertionError("EnableMethods must not be sent to an ESXi host")

        esxi = _DirectEsxiNode("esxi.local", "root", "secret")
        esxi.si = types.SimpleNamespace(RetrieveContent=lambda: types.SimpleNamespace(about=types.SimpleNamespace(apiType="HostAgent")))
        esxi._vm_holding_access_lock = lambda vm_name, vm_moref: types.SimpleNamespace(
            disabledMethod=["CloneVM_Task", "MigrateVM_Task", "RelocateVM_Task"]
        )
        self.build_plugin.return_value = esxi
        TASKS["task-e1"] = self._with_lock("task-e1", "completed")

        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

        self.assertIsNone(TASKS["task-e1"]["access_lock"])
        self.assertFalse([message for level, message in self._messages("task-e1") if level == "warn"])
        self.assertFalse(TASKS["task-e1"].get("warnings"))
        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

    @patch("python.srv.services.backup_catalog.remove_disk_tool_backup")
    @patch("python.srv.services.backup_catalog.list_disk_tool_backups", return_value=set())
    def test_a_block_that_cannot_be_lifted_yet_is_reported_once_and_retried_until_it_is(self, list_mock, remove_mock) -> None:
        self.node.raise_on_release = RuntimeError("vCenter is unreachable")
        TASKS["task-l2"] = self._with_lock("task-l2", "running")

        backup_catalog.fail_interrupted_tasks()
        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

        self.assertEqual(len(self.node.lock_releases), 2)
        self.assertTrue(TASKS["task-l2"]["access_lock"]["retry_noted"])
        warnings = [message for level, message in self._messages("task-l2") if level == "warn"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("vCenter is unreachable", warnings[0])
        self.assertIn("keeps retrying", warnings[0])
        self.assertEqual([warning["message"] for warning in TASKS["task-l2"]["warnings"]], warnings)

        self.node.raise_on_release = None
        self.assertEqual(backup_catalog.release_pending_access_locks(), 1)

        self.assertIsNone(TASKS["task-l2"]["access_lock"])
        self.assertIn("Lifted the block on relocating VM web01", self._messages("task-l2")[-1][1])
        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)
        self.assertEqual(len(self.node.lock_releases), 3)

    def test_the_retry_leaves_the_blocks_of_sessions_still_in_progress_alone(self) -> None:
        for status in ("queued", "running", "stopping"):
            TASKS[f"task-{status}"] = self._with_lock(f"task-{status}", status)

        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

        self.build_plugin.assert_not_called()
        self.assertTrue(all("access_lock" in task for task in TASKS.values()))

    def test_the_retry_waits_while_another_session_works_on_the_vm(self) -> None:
        TASKS["task-l3"] = self._with_lock("task-l3", "failed")
        TASKS["restore-1"] = {
            "id": "restore-1",
            "task_type": "restore",
            "status": "running",
            "hypervisor_id": HV_ID,
            "parameters": {"target_vm_name": "web01"},
        }

        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)
        self.build_plugin.assert_not_called()
        self.assertIn("access_lock", TASKS["task-l3"])

        TASKS["restore-1"]["status"] = "completed"
        self.assertEqual(backup_catalog.release_pending_access_locks(), 1)
        self.assertIsNone(TASKS["task-l3"]["access_lock"])

    def test_a_vm_that_carries_no_block_is_forgotten_without_a_log_line(self) -> None:
        self.node.lock_held = False
        TASKS["task-l4"] = self._with_lock("task-l4", "failed")

        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

        self.assertEqual(self.node.lock_releases, [("web01", "vm-42")])
        self.assertIsNone(TASKS["task-l4"]["access_lock"])
        self.assertEqual(self._messages("task-l4"), [])

    def test_a_hypervisor_that_is_gone_forgets_the_block(self) -> None:
        HYPERVISORS.clear()
        TASKS["task-l5"] = self._with_lock("task-l5", "failed")

        self.assertEqual(backup_catalog.release_pending_access_locks(), 0)

        self.build_plugin.assert_not_called()
        self.assertIsNone(TASKS["task-l5"]["access_lock"])

    def test_another_vm_or_hypervisor_does_not_count_as_in_use(self) -> None:
        TASKS["other-vm"] = {"id": "other-vm", "status": "running", "hypervisor_id": HV_ID, "parameters": {"vm_name": "db01"}}
        TASKS["other-hv"] = {"id": "other-hv", "status": "running", "hypervisor_id": "hv-2", "parameters": {"vm_name": "web01"}}
        TASKS["mine"] = {"id": "mine", "status": "running", "hypervisor_id": HV_ID, "parameters": {"vm_name": "web01"}}

        self.assertFalse(backup_catalog.vm_in_use_by_another_task(HV_ID, "web01", exclude_task_id="mine"))
        self.assertTrue(backup_catalog.vm_in_use_by_another_task(HV_ID, "web01"))

    def test_a_vcenter_that_cannot_be_reached_is_named_in_the_message(self) -> None:
        self.assertEqual(
            backup_catalog.access_lock_failure_reason("vc.local", OSError(22, "Invalid argument")),
            "vCenter vc.local cannot be reached ([Errno 22] Invalid argument)",
        )
        self.assertEqual(
            backup_catalog.access_lock_failure_reason("vc.local", RuntimeError("Permission denied")),
            "Permission denied",
        )

    def test_a_cleared_record_is_written_as_none_so_a_restart_does_not_bring_it_back(self) -> None:
        TASKS["task-l6"] = self._with_lock("task-l6", "failed")
        with patch("python.srv.services.backup_catalog.persist_task", wraps=backup_catalog.persist_task) as persist:
            backup_catalog.release_pending_access_locks()
        self.assertIn("access_lock", persist.call_args.args[0])
        self.assertIsNone(persist.call_args.args[0]["access_lock"])


class InterruptedOutputLocationTests(SweepTestCase):
    def test_the_recorded_task_folder_wins_over_the_vms_current_name(self) -> None:
        task = _task("task-r1", "running", vm_name="web01-renamed")
        task["result"] = {"backup_root": str(pathlib.Path(self._root) / "web01" / "task-r1")}
        self.assertEqual(
            backup_catalog._interrupted_task_output_location(task),
            (REPOSITORIES[REPO_ID], str(pathlib.Path(self._root) / "web01" / "task-r1")),
        )

    def test_a_task_that_recorded_no_folder_is_found_by_its_vm_name(self) -> None:
        task = _task("task-r2", "running", vm_name="web01-renamed")
        self.assertEqual(
            backup_catalog._interrupted_task_output_location(task),
            (REPOSITORIES[REPO_ID], str(pathlib.Path(self._root) / "web01-renamed" / "task-r2")),
        )

    def test_a_recorded_folder_of_another_task_is_not_trusted(self) -> None:
        task = _task("task-r3", "running", vm_name="web01")
        task["result"] = {"backup_root": str(pathlib.Path(self._root) / "elsewhere" / "task-other")}
        self.assertEqual(
            backup_catalog._interrupted_task_output_location(task),
            (REPOSITORIES[REPO_ID], str(pathlib.Path(self._root) / "web01" / "task-r3")),
        )

    def test_an_id_layout_task_folder_is_found_under_its_job_and_vm(self) -> None:
        task = _task("task-r5", "running", vm_name="web01")
        task["result"] = {"backup_root": str(pathlib.Path(self._root) / "job-1" / "vm-1" / "task-r5")}
        self.assertEqual(
            backup_catalog._interrupted_task_output_location(task),
            (REPOSITORIES[REPO_ID], str(pathlib.Path(self._root) / "job-1" / "vm-1" / "task-r5")),
        )

    def test_an_id_layout_s3_task_keeps_both_levels_in_its_prefix(self) -> None:
        REPOSITORIES["repo-s3"] = {"id": "repo-s3", "name": "S3", "type": "s3",
                                   "metadata": {"s3": {"bucket": "b", "prefix": "tenant", "region": "us-east-1"}}}
        cache_root = pathlib.Path(self._root) / "cache"
        task = _task("task-r6", "running", vm_name="web01", repository_id="repo-s3")
        task["result"] = {"backup_root": str(cache_root / ".catalog" / "repositories" / "repo-s3" / "job-1" / "vm-1" / "task-r6")}
        with patch("python.srv.services.backup_catalog.is_s3_repository", return_value=True), patch(
            "python.srv.services.backup_catalog.extract_s3_config", return_value={"prefix": "tenant"}
        ), patch("python.srv.plugins.vcenter.vcenter_backup._default_backup_root", return_value=cache_root):
            location = backup_catalog._interrupted_task_output_location(task)
        self.assertEqual(location[1], "tenant/job-1/vm-1/task-r6")

    def test_an_s3_task_uses_the_folder_it_recorded_in_its_prefix(self) -> None:
        REPOSITORIES["repo-s3"] = {"id": "repo-s3", "name": "S3", "type": "s3",
                                   "metadata": {"s3": {"bucket": "b", "prefix": "tenant", "region": "us-east-1"}}}
        task = _task("task-r4", "running", vm_name="web01-renamed", repository_id="repo-s3")
        task["result"] = {"backup_root": "/var/cache/abr/repo-s3/web01/task-r4"}
        with patch("python.srv.services.backup_catalog.is_s3_repository", return_value=True), patch(
            "python.srv.services.backup_catalog.extract_s3_config", return_value={"prefix": "tenant"}
        ):
            location = backup_catalog._interrupted_task_output_location(task)
        self.assertEqual(location[1], "tenant/web01/task-r4")

