# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import tempfile
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

class InterruptedTaskReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, REPOSITORIES, TASKS):
            store.clear()
        self._root = tempfile.mkdtemp(prefix="abr-interrupted-")
        self.addCleanup(lambda: None)
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": self._root}
        self._real_sweep_spawn = backup_catalog._start_interrupted_task_sweep
        sweep_now = patch(
            "python.srv.services.backup_catalog._start_interrupted_task_sweep",
            side_effect=backup_catalog._sweep_interrupted_tasks,
        )
        self._sweep_now = sweep_now.start()
        self.addCleanup(sweep_now.stop)

    def tearDown(self) -> None:
        for store in (BACKUPS, REPOSITORIES, TASKS):
            store.clear()

    def _output_dir(self, task_id: str) -> pathlib.Path:
        folder = pathlib.Path(self._root) / "web01" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01-disk.vmdk.abri").write_bytes(b"partial")
        return folder

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

        self.assertEqual(abandoned, 1)
        self.assertEqual(TASKS["task-f"]["status"], "failed")
        self.assertLess(elapsed, 2)


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
