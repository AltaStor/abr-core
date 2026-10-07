# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv
from python.srv.plugins.hyperv.hyperv import HyperVError, HyperVNode


class NodeSnapshotTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        timeout = patch.object(hyperv, "NODE_SNAPSHOT_TIMEOUT_SECONDS", 0.3)
        timeout.start()
        self.addCleanup(timeout.stop)

    def _node(self, hung: set[str], failing: set[str] = frozenset()) -> HyperVNode:
        node = HyperVNode("hv.example.test", "user", "secret")
        node._nodes = ["NODE-A", "NODE-B"]

        def snapshot(name: str):
            if name in failing:
                raise HyperVError(f"{name} refused the connection")
            if name in hung:
                self.release.wait(5)
            record = {
                "host": {**node._host_record(name, {"name": name}), "status": "connected"},
                "virtual_machines": [{"vm_id": f"vm-{name}", "name": f"vm-{name}"}],
                "disks": [],
                "network_adapters": [],
            }
            with node._lock:
                node._snapshots[name.lower()] = record
            return record

        node._node_snapshot = snapshot
        return node

    def test_a_node_that_does_not_answer_is_unavailable_and_the_rest_still_lists(self) -> None:
        node = self._node(hung={"NODE-B"})

        with self.assertLogs(hyperv.logger, level="WARNING"):
            hosts = node.list_hosts()
        vms = node.get_inventory()["virtual_machines"]

        self.assertEqual([(host["name"], host["status"]) for host in hosts], [("NODE-A", "connected"), ("NODE-B", "unavailable")])
        self.assertEqual([vm["vm_id"] for vm in vms], ["vm-NODE-A"])

    def test_no_node_answering_is_an_error_instead_of_a_hang(self) -> None:
        node = self._node(hung={"NODE-A", "NODE-B"})

        with self.assertRaisesRegex(HyperVError, "No Hyper-V node of hv.example.test listed its VMs"):
            node.list_hosts()

    def test_a_node_that_fails_still_fails_the_listing(self) -> None:
        node = self._node(hung=set(), failing={"NODE-B"})

        with self.assertRaisesRegex(HyperVError, "NODE-B refused the connection"):
            node.list_hosts()


if __name__ == "__main__":
    unittest.main()
