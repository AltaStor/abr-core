# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.plugins.vcenter import vcenter_backup


REPOSITORY = {"id": "repo-1", "name": "small", "type": "local", "path": "/srv/backups/small"}
WRITE_ERROR = (
    "Disk '[ds] vm/vm.vmdk' opened using transport mode 'nbd'\n"
    "Write error, expected 4194410 bytes, written 4164455307 bytes!\n"
    "Error: failed to save data range at offset 759169024"
)
MIB = 1024 * 1024


class DiskFailureMessageTests(unittest.TestCase):
    def test_an_incremental_that_outgrows_the_free_space_says_so(self) -> None:
        message = vcenter_backup._disk_failure_message(
            REPOSITORY, WRITE_ERROR, needed_bytes=512 * MIB, needed_is_disk_size=False, free_before=128 * MIB
        )

        self.assertEqual(
            message,
            "The repository 'small' at /srv/backups/small ran out of space: this disk needs about 512.0 MiB of "
            "changed data and 128.0 MiB were free before it was written. Free space in that filesystem, or point "
            "the job at another repository",
        )

    def test_a_full_names_the_disk_size_as_the_upper_bound(self) -> None:
        message = vcenter_backup._disk_failure_message(
            REPOSITORY, WRITE_ERROR, needed_bytes=2048 * MIB, needed_is_disk_size=True, free_before=128 * MIB
        )

        self.assertIn("needs up to 2.0 GiB (the size of the disk) and 128.0 MiB were free", message)

    def test_a_storage_that_says_it_is_full_is_reported_even_without_the_amounts(self) -> None:
        with patch.object(vcenter_backup, "filesystem_free_bytes", return_value=4096):
            message = vcenter_backup._disk_failure_message(
                REPOSITORY, "write failed: No space left on device", needed_bytes=None, needed_is_disk_size=False, free_before=None
            )

        self.assertEqual(
            message,
            "The repository 'small' at /srv/backups/small ran out of space (4.0 KiB free). Free space in that "
            "filesystem, or point the job at another repository",
        )

    def test_a_write_error_with_room_to_spare_keeps_the_tool_message(self) -> None:
        message = vcenter_backup._disk_failure_message(
            REPOSITORY, WRITE_ERROR, needed_bytes=64 * MIB, needed_is_disk_size=False, free_before=10 * 1024 * MIB
        )

        self.assertEqual(message, WRITE_ERROR)

    def test_other_failures_keep_the_tool_message(self) -> None:
        error = "VixDiskLib_Open failed: NBD_ERR_INSUFFICIENT_RESOURCES"

        message = vcenter_backup._disk_failure_message(
            REPOSITORY, error, needed_bytes=512 * MIB, needed_is_disk_size=False, free_before=128 * MIB
        )

        self.assertEqual(message, error)

    def test_an_s3_repository_keeps_the_tool_message(self) -> None:
        repository = {"id": "repo-2", "name": "bucket", "type": "s3", "s3": {"bucket": "b"}}

        message = vcenter_backup._disk_failure_message(
            repository, WRITE_ERROR, needed_bytes=512 * MIB, needed_is_disk_size=False, free_before=None
        )

        self.assertEqual(message, WRITE_ERROR)


if __name__ == "__main__":
    unittest.main()
