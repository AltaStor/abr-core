# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.mongo_db_wrapper import BACKUPS
from python.srv.core.storage_tool import StorageToolError, timed_out_message
from python.srv.plugins.vcenter.vcenter_backup import (
    _disk_timeout_seconds,
    disk_entry_timeout_seconds,
)
from python.srv.services import backup_copy, synthetic_full
from python.tests.srv.backup.test_synthetic_full import SyntheticFullRunTests
from python.tests.srv.jobs.test_backup_copy_texts import _CopyRun

PROGRESS = "\n".join(f"{percent}%" for percent in range(29))


class DiskTimeoutTests(unittest.TestCase):
    def test_the_configured_timeout_is_a_floor_scaled_by_the_disk_capacity(self) -> None:
        scaled = disk_entry_timeout_seconds(60, {"capacity_gb": 100})

        self.assertEqual(scaled, _disk_timeout_seconds(60, 100))
        self.assertGreater(scaled, 100 * 1024 // 20)

    def test_capacity_in_bytes_counts_when_gigabytes_are_missing(self) -> None:
        self.assertEqual(disk_entry_timeout_seconds(60, {"capacity_bytes": 100 * 1024**3}), _disk_timeout_seconds(60, 100))

    def test_an_unknown_size_keeps_the_configured_timeout(self) -> None:
        for disk in ({}, {"capacity_gb": None, "capacity_bytes": None}, {"capacity_gb": ""}, {"capacity_bytes": "x"}, {"capacity_gb": 0}):
            with self.subTest(disk):
                self.assertEqual(disk_entry_timeout_seconds(60, disk), 60)

    def test_a_large_configured_timeout_is_kept_for_a_small_disk(self) -> None:
        self.assertEqual(disk_entry_timeout_seconds(86400, {"capacity_gb": 1}), 86400)


class TimedOutMessageTests(unittest.TestCase):
    def test_progress_lines_become_how_far_the_disk_got(self) -> None:
        self.assertEqual(timed_out_message(60, PROGRESS), "backup tool timed out after 60s with the disk 28% done")

    def test_other_output_is_kept_without_the_progress(self) -> None:
        self.assertEqual(
            timed_out_message(60, f"{PROGRESS}\nwaiting for the repository lock"),
            "backup tool timed out after 60s with the disk 28% done: waiting for the repository lock",
        )

    def test_no_output_says_only_the_timeout(self) -> None:
        self.assertEqual(timed_out_message(60, ""), "backup tool timed out after 60s")

    def test_run_disk_tool_reports_a_timeout_that_way(self) -> None:
        tool = Path(self.enterContext(tempfile.TemporaryDirectory())) / "abr-vmdk-tool"
        tool.write_text("")
        expired = subprocess.TimeoutExpired(["abr-vmdk-tool"], 60, output="", stderr=PROGRESS)
        with patch.object(storage_tool, "run_disk_tool_process", side_effect=expired), self.assertLogs(storage_tool.logger, "WARNING"), self.assertRaises(StorageToolError) as caught:
            storage_tool.run_disk_tool(["copy"], tool_path=tool, timeout=60)

        self.assertEqual(str(caught.exception), "backup tool timed out after 60s with the disk 28% done")


class SyntheticMergeTimeoutTests(SyntheticFullRunTests):
    def _merge_timeout(self, **disk) -> int:
        for point in BACKUPS.values():
            for entry in point["artifact"]["disks"]:
                entry.update(disk)
        task = self._task()
        task["parameters"]["tool_timeout_seconds"] = 60
        with patch.object(synthetic_full, "copy_disk_tool_chain", return_value={"raw_data_size": 1024, "stored_data_size": 512}) as copy_mock:
            synthetic_full.run_synthetic_full(task, progress_callback=lambda update: None)
        return copy_mock.call_args.kwargs["timeout"]

    def test_a_merge_gets_the_timeout_a_backup_of_the_disk_gets(self) -> None:
        self.assertEqual(self._merge_timeout(capacity_gb=100), _disk_timeout_seconds(60, 100))

    def test_a_merge_of_a_disk_of_unknown_size_keeps_the_configured_timeout(self) -> None:
        self.assertEqual(self._merge_timeout(), 60)


class CopyDiskTimeoutTests(_CopyRun):
    def _copy_timeout(self, **disk) -> int:
        task = self.start()
        for point in BACKUPS.values():
            for entry in point["artifact"]["disks"]:
                entry.update(disk)
        task["parameters"]["tool_timeout_seconds"] = 60
        with (
            patch.object(backup_copy, "copy_disk_tool_chain", side_effect=StorageToolError("stop here")) as copy_mock,
            patch.object(backup_copy, "_free_bytes_at_start", return_value=None),
            self.assertRaises(backup_copy.BackupCopyError),
        ):
            backup_copy.run_backup_copy(task)
        return copy_mock.call_args.kwargs["timeout"]

    def test_a_copy_gets_the_timeout_a_backup_of_the_disk_gets(self) -> None:
        self.assertEqual(self._copy_timeout(capacity_gb=100), _disk_timeout_seconds(60, 100))

    def test_a_copy_of_a_disk_of_unknown_size_keeps_the_configured_timeout(self) -> None:
        self.assertEqual(self._copy_timeout(), 60)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for case in (DiskTimeoutTests, TimedOutMessageTests, SyntheticMergeTimeoutTests, CopyDiskTimeoutTests):
        suite.addTests(case(name) for name in vars(case) if name.startswith("test"))
    return suite

if __name__ == "__main__":
    unittest.main()
