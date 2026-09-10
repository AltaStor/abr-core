# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import TASKS
from python.srv.core.storage_tool import StorageToolError
from python.srv.core.subprocess_util import ProcessCancelled
from python.srv.core.task_executor import TaskStopRequested, cancel_requested
from python.srv.plugins.vcenter import vcenter_backup
from python.srv.plugins.vcenter.vcenter_backup import (
    _backup_still_stored,
    _discard_task_backups,
)
from python.srv.plugins.vcenter.vcenter_restore import _run_restore_layer


REPOSITORY = {"id": "repo-1", "type": "s3"}


class CancelRequestedTests(unittest.TestCase):
    def setUp(self) -> None:
        TASKS.clear()
        self.addCleanup(TASKS.clear)

    def test_a_task_asked_to_stop_reports_a_cancellation(self) -> None:
        TASKS["task-1"] = {"id": "task-1", "cancel_requested": True}
        self.assertTrue(cancel_requested("task-1"))

    def test_a_running_task_reports_no_cancellation(self) -> None:
        TASKS["task-1"] = {"id": "task-1"}
        self.assertFalse(cancel_requested("task-1"))

    def test_an_unknown_task_reports_no_cancellation(self) -> None:
        self.assertFalse(cancel_requested("task-missing"))


class DiscardTaskBackupsTests(unittest.TestCase):
    def test_every_backup_the_task_wrote_is_removed_once(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup"
        ) as remove_mock, tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-1"
            task_root.mkdir()
            _discard_task_backups(
                REPOSITORY,
                "prefix/demo-vm/task-1",
                ["01-demo-vm.vmdk", "02-demo-vm_1.vmdk", "01-demo-vm.vmdk", ""],
                task_root,
            )

        removed = [call.args[2] for call in remove_mock.call_args_list]
        self.assertEqual(removed, ["01-demo-vm.vmdk", "02-demo-vm_1.vmdk"])

    def test_the_task_directory_is_cleared(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup"
        ), tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-1"
            task_root.mkdir()
            (task_root / "01-demo-vm.vmdk.abrd").write_text("data", encoding="utf-8")
            (task_root / "01-demo-vm.vmdk.abri").write_text("index", encoding="utf-8")

            _discard_task_backups(REPOSITORY, str(task_root), ["01-demo-vm.vmdk"], task_root)

            self.assertFalse(task_root.exists())

    def test_a_backup_object_lock_keeps_is_reported_as_left_behind(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup",
            side_effect=StorageToolError("object is locked"),
        ), patch(
            "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
            return_value={"name": "01-demo-vm.vmdk"},
        ), tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-1"
            task_root.mkdir()
            with self.assertLogs("python.srv.plugins.vcenter.vcenter_backup", level="ERROR") as logs:
                _discard_task_backups(
                    REPOSITORY, "prefix/demo-vm/task-1", ["01-demo-vm.vmdk"], task_root
                )

        self.assertTrue(
            any("01-demo-vm.vmdk" in message and "Object Lock" in message for message in logs.output),
            f"expected the stranded backup to be named in the log, got: {logs.output}",
        )

    def test_a_backup_that_was_never_written_is_not_reported_as_left_behind(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup",
            side_effect=StorageToolError("backup not found"),
        ), patch(
            "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
            side_effect=StorageToolError("backup not found"),
        ), tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-1"
            task_root.mkdir()
            with patch.object(vcenter_backup.logger, "error") as error_mock:
                _discard_task_backups(
                    REPOSITORY, "prefix/demo-vm/task-1", ["01-demo-vm.vmdk"], task_root
                )

        error_mock.assert_not_called()

    def test_a_task_with_no_repository_record_still_has_its_directory_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-1"
            task_root.mkdir()
            (task_root / "01-demo-vm.vmdk.abrd").write_text("data", encoding="utf-8")

            _discard_task_backups(None, None, ["01-demo-vm.vmdk"], task_root)

            self.assertFalse(task_root.exists())

    def test_a_task_directory_that_was_never_created_is_not_an_error(self) -> None:
        with patch("python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup"):
            with tempfile.TemporaryDirectory() as temp_dir:
                _discard_task_backups(
                    REPOSITORY, "prefix", [], pathlib.Path(temp_dir) / "never-created"
                )


class BackupStillStoredTests(unittest.TestCase):
    def test_a_readable_backup_is_still_stored(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
            return_value={"name": "01-demo-vm.vmdk"},
        ):
            self.assertTrue(_backup_still_stored(REPOSITORY, "prefix", "01-demo-vm.vmdk"))

    def test_a_backup_the_tool_cannot_read_is_gone(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
            side_effect=StorageToolError("backup not found"),
        ):
            self.assertFalse(_backup_still_stored(REPOSITORY, "prefix", "01-demo-vm.vmdk"))


class RestoreLayerCancellationTests(unittest.TestCase):
    def test_a_cancelled_layer_becomes_a_stop_rather_than_a_failure(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_restore.run_capped",
            side_effect=ProcessCancelled("cancellation requested"),
        ):
            with self.assertRaises(TaskStopRequested):
                _run_restore_layer(["abr-vmdk-tool", "restore"], check=False)

    def test_an_uninterrupted_layer_returns_the_completed_process(self) -> None:
        with patch(
            "python.srv.plugins.vcenter.vcenter_restore.run_capped",
            return_value="completed",
        ):
            self.assertEqual(_run_restore_layer(["abr-vmdk-tool", "restore"]), "completed")


if __name__ == "__main__":
    unittest.main()
