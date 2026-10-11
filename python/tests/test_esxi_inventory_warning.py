# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.commands.inventory_commands import InventoryCommands
from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV
from python.srv.services import abr_v41, inventory_management

ESXI_WARNING = "Backups through a direct ESXi connection are not supported yet."


class EsxiInventoryWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, RESOURCES_BY_HV):
            store.clear()
            self.addCleanup(store.clear)
        auto = patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False)
        auto.start()
        self.addCleanup(auto.stop)

    def _add(self, service_type: str) -> dict:
        with patch.object(abr_v41, "assert_inventory_registrable", return_value=service_type):
            return abr_v41.create_inventory_record(
                {"name": f"inv-{service_type}", "type": "vsphere", "address": "host.local", "username": "root", "password": "secret"}
            )

    def test_registering_an_esxi_host_directly_warns_that_backups_are_not_supported_yet(self) -> None:
        self.assertEqual(self._add("HostAgent")["warnings"], [ESXI_WARNING])

    def test_registering_a_vcenter_carries_no_warning(self) -> None:
        self.assertNotIn("warnings", self._add("VirtualCenter"))

    def test_the_connection_test_of_an_esxi_host_warns_too(self) -> None:
        record = self._add("VirtualCenter")
        for service_type, expected in (("HostAgent", [ESXI_WARNING]), ("VirtualCenter", None)):
            with self.subTest(service_type=service_type):
                with patch.object(inventory_management.VsphereInventoryProvider, "test_connection", return_value=service_type):
                    result = abr_v41.test_inventory(record["id"])
                self.assertEqual(result["status"], "success")
                self.assertEqual(result.get("warnings"), expected)

    def test_the_cli_prints_the_warning_after_add_and_test(self) -> None:
        response = {"id": "i1", "name": "esxi52", "status": "success", "message": "Connection successful", "warnings": [ESXI_WARNING]}
        client = SimpleNamespace(post=lambda path, **kwargs: response)
        context = SimpleNamespace(api_client=client, print_output=lambda payload, columns=None: None)
        args = SimpleNamespace(token=None, password="secret", username="root", type="vsphere", name="esxi52", address="h", port=None, insecure=True)
        with patch("python.cli.commands.inventory_commands.CommandDependencies", lambda a: SimpleNamespace(context=context)), patch(
            "python.cli.commands.inventory_commands.resolve_secret", side_effect=lambda value, **kwargs: value
        ), patch.object(InventoryCommands, "_resolve_id", staticmethod(lambda deps, args: "i1")):
            for handler in (InventoryCommands().add, InventoryCommands().test):
                errors = io.StringIO()
                with redirect_stderr(errors), redirect_stdout(io.StringIO()):
                    handler(args)
                self.assertIn(f"Warning: {ESXI_WARNING}", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
