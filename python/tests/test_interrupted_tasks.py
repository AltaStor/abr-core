# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, REPOSITORIES, TASKS
from python.srv.services import backup_catalog

REPO_ID = "repo-1"


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
