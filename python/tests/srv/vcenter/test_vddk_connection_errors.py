# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.plugins.vcenter.disk_transport import (
    CONNECTION_FAILURES,
    is_connection_failure,
)
from python.srv.plugins.vcenter.vcenter_backup import _disk_failure_message
from python.srv.services.internal.replication.replica_update import is_temporary

DISK = "[ds-02] replica/01-vm-000001.vmdk"


def _tool_error(text: str) -> str:
    return f"Error: failed to open disk '{DISK}': {text}"


class VddkConnectionErrorTests(unittest.TestCase):
    def test_every_connection_failure_of_both_libraries_is_a_temporary_replication_error(self) -> None:
        for code, texts in CONNECTION_FAILURES.items():
            for library, text in zip(("VMware VDDK", "oVDDK"), texts):
                with self.subTest(code=code, library=library):
                    error = RuntimeError(f"Updating disk {DISK} of replica failed: {_tool_error(text)} (abr-vmdk-tool.sh restore --name)")
                    self.assertTrue(is_temporary(error))

    def test_the_ticket_wordings(self) -> None:
        self.assertTrue(is_temporary(RuntimeError(_tool_error("The server refused the connection"))))
        self.assertTrue(is_temporary(RuntimeError(_tool_error("The server refused connection"))))

    def test_other_library_errors_stay_permanent(self) -> None:
        for text in (
            "The file or disk is read-only",
            "Access to the file was denied",
            "An encryption key is required",
            "Insufficient permissions on the host",
            "The disk to mount was not found",
            "This operation is not supported here",
        ):
            with self.subTest(text):
                self.assertFalse(is_connection_failure(text))
                self.assertFalse(is_temporary(RuntimeError(_tool_error(text))))

    def test_a_backup_names_the_lost_host_whichever_library_answered(self) -> None:
        for library, text in zip(("VMware VDDK", "oVDDK"), CONNECTION_FAILURES["VIX_E_HOST_NETWORK_CONN_REFUSED"]):
            with self.subTest(library):
                message = _disk_failure_message(
                    None, _tool_error(text), needed_bytes=None, needed_is_disk_size=False, free_before=None, data_host="192.168.13.52"
                )
                self.assertEqual(message, f"Lost the connection to ESXi host 192.168.13.52 (failed to open disk '{DISK}': {text})")


if __name__ == "__main__":
    unittest.main()
