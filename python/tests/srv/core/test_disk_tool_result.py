# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.storage_tool import StorageToolError, parse_disk_tool_result
from python.srv.core.subprocess_util import CompletedProcess

COMMAND = ["copy", "--json", "--name", "01-disk.vmdk", "--key", "enc-key-123456", "--source-key", "src-key-654321"]


def _completed(stdout: str, stderr: str = "", returncode: int = 0) -> CompletedProcess:
    return CompletedProcess(args=COMMAND, returncode=returncode, stdout=stdout, stderr=stderr)


class UnreadableToolResultTests(unittest.TestCase):
    def test_a_readable_document_is_returned_as_before(self) -> None:
        document = parse_disk_tool_result(_completed('12%\n100%\n{"command": "copy", "status": "ok"}\n'), COMMAND)
        self.assertEqual(document["status"], "ok")

    def test_a_missing_document_logs_the_exit_code_command_and_both_tails(self) -> None:
        completed = _completed("47%\n98%\n100%\n", "Warning: slow storage\nmerged 2 layers\n")
        with self.assertLogs("python.srv.core.storage_tool", level="ERROR") as logs, self.assertRaises(StorageToolError) as ctx:
            parse_disk_tool_result(completed, COMMAND)
        logged = "\n".join(logs.output)
        self.assertIn("exited with code 0", logged)
        self.assertIn("copy --json --name 01-disk.vmdk", logged)
        self.assertIn("merged 2 layers", logged)
        self.assertIn("100%", logged)
        message = str(ctx.exception)
        self.assertIn("no JSON result document", message)
        self.assertIn("exit code 0", message)
        self.assertIn("the tool said: merged 2 layers", message)

    def test_secrets_never_reach_the_log_or_the_message(self) -> None:
        completed = _completed("opening with enc-key-123456\n", "bad source key src-key-654321\n")
        with self.assertLogs("python.srv.core.storage_tool", level="ERROR") as logs, self.assertRaises(StorageToolError) as ctx:
            parse_disk_tool_result(completed, COMMAND)
        text = "\n".join(logs.output) + str(ctx.exception)
        self.assertNotIn("enc-key-123456", text)
        self.assertNotIn("src-key-654321", text)
        self.assertIn(storage_tool.SECRET_MASK, text)

    def test_an_empty_stderr_is_said_plainly(self) -> None:
        with self.assertLogs("python.srv.core.storage_tool", level="ERROR"), self.assertRaises(StorageToolError) as ctx:
            parse_disk_tool_result(_completed(""), COMMAND)
        self.assertIn("the tool wrote nothing to stderr", str(ctx.exception))

    def test_a_long_output_is_logged_only_by_its_tail(self) -> None:
        completed = _completed("x" * 50000 + "\nEND-OF-OUTPUT\n")
        with self.assertLogs("python.srv.core.storage_tool", level="ERROR") as logs, self.assertRaises(StorageToolError):
            parse_disk_tool_result(completed, COMMAND)
        self.assertIn("END-OF-OUTPUT", logs.output[0])
        self.assertLess(len(logs.output[0]), 6000)

    def test_a_merge_that_returns_no_document_reports_the_exit_code(self) -> None:
        with patch.object(storage_tool, "run_disk_tool", return_value=_completed("100%\n", "")), self.assertLogs(
            "python.srv.core.storage_tool", level="ERROR"
        ), self.assertRaises(StorageToolError) as ctx:
            storage_tool.copy_disk_tool_chain(
                source_repository={"type": "local", "id": "repo-1", "path": "/srv/backups"},
                source_abr_path="/srv/backups/job/vm",
                source_names=["task-a/01-disk.vmdk", "task-b/01-disk.vmdk"],
                destination_repository={"type": "local", "id": "repo-1", "path": "/srv/backups"},
                destination_abr_path="/srv/backups/job/vm/task-c",
                backup_name="01-disk.vmdk",
                encryption_key="enc-key-123456",
            )
        self.assertIn("no JSON result document (exit code 0", str(ctx.exception))
        self.assertNotIn("enc-key-123456", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
