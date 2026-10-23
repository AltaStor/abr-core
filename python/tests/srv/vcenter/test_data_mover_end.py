# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace

from python.srv.core.subprocess_util import describe_process_end
from python.srv.plugins.vcenter import vcenter_backup

INFO_LINE = "Disk '[ds] vm/vm.vmdk' opened using transport mode 'nbd'"
REPOSITORY = {"id": "repo-1", "name": "Local", "type": "local", "path": "/backups"}


def _ended(returncode: int, stderr: str = "", stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)


class DescribeProcessEndTests(unittest.TestCase):
    def test_signals_are_named(self) -> None:
        self.assertEqual(describe_process_end("tool", -9), "tool was terminated by signal 9 (SIGKILL)")
        self.assertEqual(describe_process_end("tool", -15), "tool was terminated by signal 15 (SIGTERM)")
        self.assertEqual(describe_process_end("tool", -6), "tool was terminated by signal 6 (SIGABRT)")

    def test_a_signal_without_a_name_keeps_its_number(self) -> None:
        self.assertEqual(describe_process_end("tool", -200), "tool was terminated by signal 200")

    def test_an_exit_code_is_given_as_is(self) -> None:
        self.assertEqual(describe_process_end("tool", 3), "tool exited with code 3")


class DiskToolFailureTests(unittest.TestCase):
    def _failure(self, completed: SimpleNamespace, **context) -> str:
        context = {"needed_bytes": None, "needed_is_disk_size": True, "free_before": None, **context}
        return vcenter_backup._disk_tool_failure(completed, REPOSITORY, **context)

    def test_a_killed_data_mover_names_the_signal_and_keeps_its_last_line_as_detail(self) -> None:
        message = self._failure(_ended(-9, stderr=f"VixDiskLib: starting\n{INFO_LINE}\n"))

        self.assertTrue(message.startswith("the data mover ended unexpectedly: abr-vmdk-tool was terminated by signal 9 (SIGKILL)"))
        self.assertIn("out-of-memory killer", message)
        self.assertTrue(message.endswith(f"Its last output: {INFO_LINE}"))
        self.assertNotIn("VixDiskLib: starting", message)

    def test_another_signal_has_no_memory_hint(self) -> None:
        message = self._failure(_ended(-15, stderr=INFO_LINE))

        self.assertIn("terminated by signal 15 (SIGTERM)", message)
        self.assertNotIn("out-of-memory", message)

    def test_a_killed_data_mover_wins_over_whatever_its_output_said(self) -> None:
        message = self._failure(_ended(-9, stderr="Error: connection reset by peer"), data_host="198.51.100.52")

        self.assertIn("terminated by signal 9 (SIGKILL)", message)
        self.assertIn("Its last output: Error: connection reset by peer", message)
        self.assertNotIn("Lost the connection", message)

    def test_a_silent_end_says_so(self) -> None:
        self.assertTrue(self._failure(_ended(-9)).endswith(". It printed nothing"))
        self.assertTrue(self._failure(_ended(4)).endswith("abr-vmdk-tool exited with code 4. It printed nothing"))

    def test_an_exit_code_after_an_info_line_is_unexpected(self) -> None:
        message = self._failure(_ended(2, stderr=INFO_LINE))

        self.assertEqual(message, f"the data mover ended unexpectedly: abr-vmdk-tool exited with code 2. Its last output: {INFO_LINE}")

    def test_a_tool_error_stays_the_cause(self) -> None:
        for output in ("Error: failed to read disk data", "disk copy failed", "query changed areas failed"):
            with self.subTest(output=output):
                self.assertEqual(self._failure(_ended(1, stderr=output)), output)

    def test_a_full_repository_and_a_lost_connection_are_still_recognised(self) -> None:
        full = self._failure(_ended(1, stderr="write error: No space left on device"), needed_bytes=10, free_before=1)
        self.assertIn("ran out of space", full)
        lost = self._failure(_ended(1, stderr="Error: The server refused connection"), data_host="198.51.100.52")
        self.assertTrue(lost.startswith("Lost the connection to ESXi host 198.51.100.52"))


if __name__ == "__main__":
    unittest.main()
