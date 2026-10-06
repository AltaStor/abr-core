# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import RESOURCES_BY_HV
from python.srv.services import abr_v41


def _hosts(nodes: list[dict]) -> list[dict]:
    found = []
    for node in nodes:
        if node.get("kind") == "host":
            found.append(node)
        found.extend(_hosts(node.get("children") or []))
    return found


class InventoryTreeHostMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        RESOURCES_BY_HV.clear()

    def test_a_hyperv_vm_lands_under_its_host_whatever_the_name_case(self) -> None:
        RESOURCES_BY_HV["inv-1"] = {
            "host-a": {"resource_type": "host", "external_id": "AS-HV-TEST-01", "name": "AS-HV-TEST-01", "payload": {}},
            "host-b": {"resource_type": "host", "external_id": "AS-HV-TEST-02", "name": "AS-HV-TEST-02", "payload": {}},
            "vm-1": {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "vm",
                "payload": {"host_name": "as-hv-test-01"},
            },
        }
        inventory = {"id": "inv-1", "name": "HyperV-test", "kind": "hyper-v", "status": "available"}

        with patch.object(abr_v41, "list_inventory_records", return_value=[inventory]):
            tree = abr_v41.list_inventory_vm_tree()

        hosts = _hosts(tree)
        self.assertEqual([(host["host_id"], host["host_name"]) for host in hosts], [("AS-HV-TEST-01", "AS-HV-TEST-01")])


if __name__ == "__main__":
    unittest.main()
