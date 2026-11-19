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

    def test_a_lost_smb_server_is_named_instead_of_a_full_share(self) -> None:
        repository = {"id": "repo-3", "name": "nas", "type": "smb", "path": "/run/altastor-backup-mounts/repo-3", "metadata": {"server": "nas.example", "managed_mount": True}}

        with patch("python.srv.services.repository_mounts.server_answers", return_value=False) as asked:
            message = vcenter_backup._disk_failure_message(
                repository, "write failed: No space left on device", needed_bytes=None, needed_is_disk_size=False, free_before=None
            )

        asked.assert_called_once_with("nas.example")
        self.assertIn("failed because the server nas.example of its share stopped answering (port 445 or 139)", message)
        self.assertIn("not a full share", message)
        self.assertNotIn("ran out of space", message)

    def test_an_smb_share_whose_server_answers_is_reported_full(self) -> None:
        repository = {"id": "repo-3", "name": "nas", "type": "smb", "path": "/run/altastor-backup-mounts/repo-3", "metadata": {"server": "nas.example"}}

        with (
            patch("python.srv.services.repository_mounts.server_answers", return_value=True),
            patch.object(vcenter_backup, "filesystem_free_bytes", return_value=0),
        ):
            message = vcenter_backup._disk_failure_message(
                repository, "write failed: No space left on device", needed_bytes=None, needed_is_disk_size=False, free_before=None
            )

        self.assertIn("ran out of space (0 B free)", message)

    def test_an_s3_repository_keeps_the_tool_message(self) -> None:
        repository = {"id": "repo-2", "name": "bucket", "type": "s3", "s3": {"bucket": "b"}}

        message = vcenter_backup._disk_failure_message(
            repository, WRITE_ERROR, needed_bytes=512 * MIB, needed_is_disk_size=False, free_before=None
        )

        self.assertEqual(message, WRITE_ERROR)


if __name__ == "__main__":
    unittest.main()
