# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import Any

from pypsrp.exceptions import WinRMTransportError

from python.srv.plugins.hyperv import hyperv_replica


class DroppingNode:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def _run_script(self, name: str, args: dict[str, Any], host: str) -> list[dict[str, Any]]:
        self.calls.append(f"run {name}")
        if self.failures:
            self.failures -= 1
            raise WinRMTransportError("http", 400, "Bad HTTP response returned from the server. Code: 400, Content: ''")
        return [{"action": args.get("action"), "disks": ["C:\\r\\disk.vhdx"]}]

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def connect(self) -> None:
        self.calls.append("connect")


STATE = {"vm_id": "vm-r", "name": "replica", "host": "HV-01", "disks": []}


class DroppedConnectionTests(unittest.TestCase):
    def test_a_connection_the_host_dropped_during_a_long_write_is_opened_again(self) -> None:
        node = DroppingNode(failures=1)

        hyperv_replica.remove_snapshot(node, STATE, "abr-replica-x", 60)

        self.assertEqual(
            node.calls,
            ["run replica_checkpoint.ps1", "disconnect", "connect", "run replica_checkpoint.ps1"],
        )

    def test_a_second_drop_in_a_row_is_reported(self) -> None:
        node = DroppingNode(failures=2)

        with self.assertRaises(WinRMTransportError):
            hyperv_replica.revert_snapshot(node, STATE, "abr-replica-x", 60)

    def test_an_answer_needs_no_reconnect(self) -> None:
        node = DroppingNode(failures=0)

        hyperv_replica.remove_snapshot(node, STATE, "abr-replica-x", 60)

        self.assertEqual(node.calls, ["run replica_checkpoint.ps1"])


if __name__ == "__main__":
    unittest.main()
