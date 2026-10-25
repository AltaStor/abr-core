# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.mongo_db_wrapper import REPOSITORIES
from python.srv.core.storage_tool import StorageToolError, run_disk_tool
from python.srv.core.subprocess_util import CompletedProcess
from python.srv.services import abr_v41

TOOL_TAIL = "Failed to initialize backup manager!\nError: failed to initialize 'file' backup storage at '{path}'"
REAL_SCANDIR = os.scandir


def _tool_says_missing(*paths: str) -> str:
    lines = [f"Directory does not exist '{path}'!" for path in paths]
    return "\n".join(lines) + "\n" + TOOL_TAIL.format(path=paths[-1])


def _scandir_failing_for(failing: str, error: int):
    def scandir(path):
        if str(path) == failing:
            raise OSError(error, os.strerror(error), str(path))
        return REAL_SCANDIR(path)

    return scandir


class UnreadableDirectoryMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.root = Path(workdir.name)
        self.tool = self.root / "abr-vmdk-tool"
        self.tool.write_text("#!/bin/sh\n", encoding="utf-8")
        self.session = self.root / "repo" / "job" / "vm" / "session"
        self.session.mkdir(parents=True)

    def _fail(self, args: list[str], stderr: str) -> StorageToolError:
        completed = CompletedProcess(args=args, returncode=1, stdout="", stderr=stderr)
        with patch.object(storage_tool, "run_disk_tool_process", return_value=completed), self.assertRaises(
            StorageToolError
        ) as ctx:
            run_disk_tool(args, tool_path=self.tool)
        return ctx.exception

    def test_a_folder_the_disk_cannot_read_is_named_as_a_read_error(self) -> None:
        with patch.object(storage_tool.os, "scandir", _scandir_failing_for(str(self.session), errno.EIO)):
            message = str(self._fail(["verify", "--name", "01-disk.vmdk"], _tool_says_missing(str(self.session))))

        self.assertTrue(message.startswith(f"cannot read {self.session}: Input/output error\n"), message)
        self.assertNotIn("Directory does not exist", message)
        self.assertIn("Failed to initialize backup manager!", message)
        self.assertIn(f"failed to initialize 'file' backup storage at '{self.session}'", message)

    def test_a_folder_that_is_really_gone_keeps_the_tool_message(self) -> None:
        gone = self.root / "repo" / "job" / "vm" / "deleted-session"
        stderr = _tool_says_missing(str(gone))

        self.assertEqual(str(self._fail(["verify", "--name", "01-disk.vmdk"], stderr)), stderr)

    def test_a_folder_under_a_missing_parent_keeps_the_tool_message(self) -> None:
        stderr = _tool_says_missing(str(self.session / "01-disk.vmdk.abrd" / "inner"))
        (self.session / "01-disk.vmdk.abrd").write_text("not a folder", encoding="utf-8")

        self.assertEqual(str(self._fail(["list"], stderr)), stderr)

    def test_a_readable_folder_keeps_the_tool_message(self) -> None:
        stderr = _tool_says_missing(str(self.session))

        self.assertEqual(str(self._fail(["verify", "--name", "01-disk.vmdk"], stderr)), stderr)

    @unittest.skipIf(os.geteuid() == 0, "root reads a folder whatever its mode")
    def test_a_folder_the_server_may_not_read_says_so(self) -> None:
        self.session.chmod(0)
        self.addCleanup(self.session.chmod, 0o750)

        message = str(self._fail(["list"], _tool_says_missing(str(self.session))))

        self.assertTrue(message.startswith(f"cannot read {self.session}: Permission denied\n"), message)

    def test_each_named_folder_is_checked_on_its_own(self) -> None:
        gone = self.root / "repo" / "gone"
        with patch.object(storage_tool.os, "scandir", _scandir_failing_for(str(self.session), errno.EIO)):
            message = str(self._fail(["list"], _tool_says_missing(str(self.session), str(gone))))

        self.assertIn(f"cannot read {self.session}: Input/output error", message)
        self.assertIn(f"Directory does not exist '{gone}'!", message)

    def test_any_other_tool_failure_is_raised_unchanged(self) -> None:
        stderr = "Error: failed to read disk data\nchecksum mismatch in block 12"

        self.assertEqual(str(self._fail(["verify", "--name", "01-disk.vmdk"], stderr)), stderr)

    def test_removing_from_an_unreadable_folder_is_a_warning_not_an_absent_backup(self) -> None:
        args = ["remove", "--name", "01-disk.vmdk", "--force"]
        with patch.object(storage_tool.os, "scandir", _scandir_failing_for(str(self.session), errno.EIO)), self.assertLogs(
            "python.srv.core.storage_tool", level="WARNING"
        ) as logs:
            self._fail(args, _tool_says_missing(str(self.session)))

        self.assertIn(f"cannot read {self.session}: Input/output error", "\n".join(logs.output))

    def test_removing_from_a_folder_that_is_gone_stays_a_quiet_absent_backup(self) -> None:
        gone = self.root / "repo" / "gone"
        with self.assertNoLogs("python.srv.core.storage_tool", level="WARNING"):
            self._fail(["remove", "--name", "01-disk.vmdk", "--force"], _tool_says_missing(str(gone)))


class VerifyArtifactReadErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.root = Path(workdir.name)
        self.tool = self.root / "abr-vmdk-tool"
        self.tool.write_text("#!/bin/sh\n", encoding="utf-8")
        self.session = self.root / "repo" / "job" / "vm" / "session"
        self.session.mkdir(parents=True)
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "local", "type": "local", "path": str(self.root / "repo")}

    def test_verify_of_a_disk_on_an_unreadable_folder_names_the_read_error(self) -> None:
        completed = CompletedProcess(args=[], returncode=1, stdout="", stderr=_tool_says_missing(str(self.session)))
        point = {"id": "point-1", "repository_id": "repo-1"}
        disk = {"index": 1, "abr_path": str(self.session), "abr_backup_name": "01-disk.vmdk"}
        with patch.object(storage_tool, "default_disk_tool_path", return_value=self.tool), patch.object(
            storage_tool, "run_disk_tool_process", return_value=completed
        ), patch.object(storage_tool.os, "scandir", _scandir_failing_for(str(self.session), errno.EIO)):
            result = abr_v41._verify_disk_artifact(point, disk)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["message"].startswith(f"cannot read {self.session}: Input/output error"), result["message"])


if __name__ == "__main__":
    unittest.main()
