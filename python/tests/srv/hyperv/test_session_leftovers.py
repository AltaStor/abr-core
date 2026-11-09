# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import task_pool
from python.srv.core.mongo_db_wrapper import TASKS
from python.srv.plugins.hyperv import hyperv, hyperv_backup
from python.srv.plugins.hyperv.hyperv import HyperVNode, tool_location
from python.srv.services import backup_catalog

BUSY = OSError(errno.EBUSY, "Device or resource busy", "/mnt/share/vm/task-1/01-disk.vhdx.abrd")


class ToolLocationTests(unittest.TestCase):
    def test_the_session_folder_or_s3_path_identifies_the_tool_run(self) -> None:
        self.assertEqual(tool_location(["backup", "--mode", "file", "--folder", r"\\nas\share\vm\task-1"]), r"\\nas\share\vm\task-1")
        self.assertEqual(tool_location(["backup", "--mode", "s3", "--folder", "bucket", "--path", "prefix/vm/task-1"]), "prefix/vm/task-1")
        self.assertEqual(tool_location(["info", "--json"]), "")
        self.assertEqual(tool_location(["backup", "--folder"]), "")

    def test_a_stopped_run_ends_the_tool_that_works_on_its_folder(self) -> None:
        node = HyperVNode.__new__(HyperVNode)
        with patch.object(HyperVNode, "stop_disk_tool", return_value=["abr-vhdx-tool (process 42)"]) as stop:
            node._end_tool("HV-NODE-01", ["backup", "--folder", r"\\nas\share\vm\task-1"])

        stop.assert_called_once_with("HV-NODE-01", [r"\\nas\share\vm\task-1"])

    def test_a_host_that_cannot_end_the_tool_does_not_hide_the_stop(self) -> None:
        node = HyperVNode.__new__(HyperVNode)
        with patch.object(HyperVNode, "stop_disk_tool", side_effect=ConnectionError("gone")), self.assertLogs(hyperv.logger, "WARNING"):
            node._end_tool("HV-NODE-01", ["backup", "--folder", r"\\nas\share\vm\task-1"])


class DiscardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task_root = self.root / "vm" / "task-1"
        self.task_root.mkdir(parents=True)
        (self.task_root / "01-disk.vhdx.abrd").write_bytes(b"data")
        self.repository = {"id": "repo-1", "name": "Shared", "type": "smb", "path": str(self.root)}
        self.enterContext(patch.object(hyperv_backup, "_discard_task_backups", return_value=[]))
        self.enterContext(patch.object(hyperv_backup.time, "sleep"))
        self.updates: list[dict] = []

    def test_a_folder_the_host_still_holds_is_removed_once_it_lets_go(self) -> None:
        real = hyperv_backup.shutil.rmtree
        calls = [BUSY, BUSY]

        def rmtree(path):
            if calls:
                raise calls.pop(0)
            real(path)

        with patch.object(hyperv_backup.shutil, "rmtree", side_effect=rmtree):
            message = hyperv_backup._stopped(self.updates.append, self.repository, str(self.task_root), [], self.task_root, self.root)

        self.assertEqual(message, "Session stop requested")
        self.assertFalse((self.root / "vm").exists())
        self.assertEqual(self.updates, [])

    def test_a_folder_that_stays_is_named_in_the_stop_and_kept_for_the_next_backup(self) -> None:
        with patch.dict("os.environ", {"ALTASTOR_HYPERV_DISCARD_RETRY_SECONDS": "0"}), patch.object(
            hyperv_backup.shutil, "rmtree", side_effect=BUSY
        ), self.assertLogs(hyperv_backup.logger, "ERROR"):
            message = hyperv_backup._stopped(self.updates.append, self.repository, str(self.task_root), [], self.task_root, self.root)

        self.assertEqual(
            message,
            f"Session stop requested. What this session had written could not be removed from {self.task_root}: "
            "Device or resource busy (/mnt/share/vm/task-1/01-disk.vhdx.abrd). The next backup of this job removes it",
        )
        self.assertEqual(
            self.updates[0]["left_session_data"], {
                "path": str(self.task_root),
                "repository_id": "repo-1",
                "reason": "Device or resource busy (/mnt/share/vm/task-1/01-disk.vhdx.abrd)",
            }
        )
        self.assertFalse(self.updates[0].get("log_entry", True))

    def test_a_failed_backup_that_leaves_its_folder_warns_in_the_session_log(self) -> None:
        with self.assertLogs(hyperv_backup.logger, "ERROR"):
            hyperv_backup._record_left_data(self.updates.append, self.repository, self.task_root, "busy", warn=True)

        self.assertEqual(self.updates[0]["log_level"], "warn")
        self.assertIn(f"could not be removed from {self.task_root}: busy", self.updates[0]["message"])
        self.assertEqual(self.updates[0]["left_session_data"]["path"], str(self.task_root))

    def test_an_s3_session_keeps_the_object_lock_report_and_no_folder_record(self) -> None:
        s3 = {"id": "repo-2", "type": "s3", "metadata": {"s3": {"bucket": "b"}}}
        with patch.object(hyperv_backup, "is_s3_repository", return_value=True):
            stranded, reason = hyperv_backup._discard_session(s3, "prefix/vm/task-1", ["01-disk.vhdx"], self.task_root, self.root)

        self.assertEqual((stranded, reason), ([], ""))


class NextBackupSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        TASKS.clear()
        self.addCleanup(TASKS.clear)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repository = {"id": "repo-1", "name": "Shared", "type": "smb", "path": str(self.root)}
        self.notes: list[tuple[str, str]] = []
        self.enterContext(patch.object(hyperv_backup, "_emit", side_effect=lambda cb, stage, progress, message, **kw: self.notes.append(("info", message))))
        self.enterContext(patch.object(hyperv_backup, "_warn", side_effect=lambda cb, stage, progress, message, **kw: self.notes.append(("warn", message))))

    def _left(self, task_id: str, folder: Path, *, job_id: str = "job-1", repository_id: str = "repo-1", status: str = "stopped") -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01-disk.vhdx.abrd").write_bytes(b"data")
        TASKS[task_id] = {
            "id": task_id,
            "job_id": job_id,
            "status": status,
            "result": {"message": "Session stop requested", "left_session_data": {"path": str(folder), "repository_id": repository_id}},
        }

    def _sweep(self) -> None:
        hyperv_backup._remove_left_session_data({"id": "task-new", "job_id": "job-1"}, self.repository, None)

    def test_the_next_backup_of_the_job_removes_the_folder_and_forgets_it(self) -> None:
        folder = self.root / "vm" / "task-1"
        self._left("task-1", folder)

        self._sweep()

        self.assertFalse((self.root / "vm").exists())
        self.assertNotIn("left_session_data", TASKS["task-1"]["result"])
        self.assertEqual(self.notes, [("info", f"Removed what session task-1 left in {folder} when it ended")])

    def test_a_folder_it_still_cannot_remove_stays_recorded_with_a_warning(self) -> None:
        folder = self.root / "vm" / "task-1"
        self._left("task-1", folder)

        with patch.object(hyperv_backup.shutil, "rmtree", side_effect=BUSY):
            self._sweep()

        self.assertTrue(folder.exists())
        self.assertIn("left_session_data", TASKS["task-1"]["result"])
        self.assertEqual(self.notes[0][0], "warn")
        self.assertIn("The next backup tries again", self.notes[0][1])

    def test_only_the_session_folder_inside_this_repository_is_ever_removed(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "vm" / "task-1"
        self._left("task-1", outside)
        misnamed = self.root / "vm" / "something-else"
        self._left("task-2", misnamed)
        other_repository = self.root / "vm" / "task-3"
        self._left("task-3", other_repository, repository_id="repo-9")
        other_job = self.root / "vm" / "task-4"
        self._left("task-4", other_job, job_id="job-2")
        running = self.root / "vm" / "task-5"
        self._left("task-5", running, status="running")

        self._sweep()

        for folder in (outside, misnamed, other_repository, other_job, running):
            self.assertTrue(folder.exists(), folder)
        self.assertEqual(self.notes, [])

    def test_a_record_whose_folder_is_already_gone_is_forgotten(self) -> None:
        folder = self.root / "vm" / "task-1"
        self._left("task-1", folder)
        hyperv_backup.shutil.rmtree(folder)

        self._sweep()

        self.assertNotIn("left_session_data", TASKS["task-1"]["result"])


class FinishedSessionTests(unittest.TestCase):
    def test_the_record_survives_the_end_of_the_session(self) -> None:
        result: dict = {"message": "Session stop requested"}
        entry = {"path": "/mnt/share/vm/task-1", "repository_id": "repo-1"}

        task_pool._keep_backup_snapshot(result, {"left_session_data": entry, "progress": 40})

        self.assertEqual(result["left_session_data"], entry)

    def test_the_catalog_lists_only_finished_sessions_of_the_job(self) -> None:
        TASKS.clear()
        self.addCleanup(TASKS.clear)
        entry = {"path": "/mnt/share/vm/task-1", "repository_id": "repo-1"}
        TASKS["task-1"] = {"id": "task-1", "job_id": "job-1", "status": "stopped", "result": {"left_session_data": entry}}
        TASKS["task-2"] = {"id": "task-2", "job_id": "job-1", "status": "running", "result": {"left_session_data": entry}}
        TASKS["task-3"] = {"id": "task-3", "job_id": "job-2", "status": "failed", "result": {"left_session_data": entry}}

        self.assertEqual(backup_catalog.left_session_data("job-1", exclude_task_id="task-new"), [("task-1", entry)])


if __name__ == "__main__":
    unittest.main()
