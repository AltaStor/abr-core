# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.storage_tool import StorageToolError, run_disk_tool
from python.srv.core.subprocess_util import CompletedProcess

PROGRESS = "4%\n8%\n12%\n70%\n"


class ToolDeathMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.tool = Path(workdir.name) / "abr-vmdk-tool"
        self.tool.write_text("#!/bin/sh\n", encoding="utf-8")

    def _fail(self, returncode: int, stderr: str, stdout: str = "") -> str:
        completed = CompletedProcess(args=["copy"], returncode=returncode, stdout=stdout, stderr=stderr)
        with patch.object(storage_tool, "run_disk_tool_process", return_value=completed), self.assertRaises(StorageToolError) as caught:
            run_disk_tool(["copy", "--name", "01-disk.vmdk"], tool_path=self.tool)
        return str(caught.exception)

    def test_a_tool_killed_mid_copy_is_named_with_its_signal_not_its_progress(self) -> None:
        message = self._fail(-9, PROGRESS)

        self.assertEqual(
            message,
            "the backup tool ended unexpectedly: abr-vmdk-tool was terminated by signal 9 (SIGKILL) "
            f"({storage_tool.OUT_OF_MEMORY_HINT}). It printed no error",
        )

    def test_other_signals_and_a_silent_non_zero_exit_say_what_ended_it(self) -> None:
        self.assertIn("terminated by signal 15 (SIGTERM). It printed no error", self._fail(-15, PROGRESS))
        self.assertEqual(self._fail(3, PROGRESS), "the backup tool ended unexpectedly: abr-vmdk-tool exited with code 3. It printed no error")
        self.assertEqual(self._fail(1, ""), "the backup tool ended unexpectedly: abr-vmdk-tool exited with code 1. It printed no error")

    def test_a_tool_killed_after_printing_an_error_keeps_that_line(self) -> None:
        message = self._fail(-9, PROGRESS + "Error: lost the source backup\n")

        self.assertIn("terminated by signal 9 (SIGKILL)", message)
        self.assertTrue(message.endswith("Its last output: Error: lost the source backup"), message)

    def test_an_error_the_tool_describes_is_kept_without_its_progress(self) -> None:
        self.assertEqual(self._fail(1, PROGRESS + "Error: no space left on device\n"), "Error: no space left on device")
        self.assertEqual(self._fail(2, "", "Error: backup 'x' does not exist"), "Error: backup 'x' does not exist")

    def test_progress_with_byte_counts_is_dropped_too(self) -> None:
        self.assertEqual(storage_tool.without_progress_lines("  45% (123456)\nError: x\n100%"), "Error: x")


if __name__ == "__main__":
    unittest.main()
