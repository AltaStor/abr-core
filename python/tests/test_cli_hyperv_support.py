# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.commands.backup_commands import BackupCommands
from python.cli.commands.inventory_commands import INVENTORY_SCAN_TIMEOUT_SECONDS, InventoryCommands
from python.cli.commands.restore_commands import RestoreCommands
from python.cli.commands.session_commands import SessionCommands


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _args(**values: object) -> SimpleNamespace:
    return SimpleNamespace(base_url="http://example.test", log_level="info", **values)


HYPERV_CONTEXT = {
    "source_vm": {
        "disks": [
            {"label": "SCSI0:0", "datastore_path": "C:\\et\\vm\\disk1.vhdx"},
            {"label": "SCSI0:1", "datastore_path": "E:\\et\\vm\\disk2.vhdx"},
        ],
        "nics": [{"id": "nic-1", "label": "Network Adapter", "source_network": "Management"}],
    },
    "targets": {
        "hosts": [
            {"id": "NODE-01", "name": "NODE-01", "status": "connected"},
            {"id": "node-02", "name": "node-02", "status": "unavailable"},
        ],
        "networks": [{"id": "inv::network::Management", "name": "Management", "host_ids": ["NODE-01"]}],
        "datastores": [
            {"id": "inv::datastore::C:", "name": "C:", "host_ids": ["NODE-01"], "free_bytes": 64 * 1024**3, "capacity_bytes": 100 * 1024**3},
            {"id": "inv::datastore::E:", "name": "E:", "host_ids": ["NODE-01"], "free_bytes": 900 * 1024**2, "capacity_bytes": 1024**3},
        ],
    },
    "defaults": {
        "target_datastore_id": "inv::datastore::C:",
        "datastore_mappings": [
            {"source_datastore": "C:", "target_datastore_id": "inv::datastore::C:"},
            {"source_datastore": "E:", "target_datastore_id": "inv::datastore::E:"},
        ],
        "network_mappings": [{"source_nic_id": "nic-1", "target_network_id": "inv::network::Management"}],
    },
}


class InventoryRescanTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_a_rescan_waits_for_a_slow_cluster_instead_of_the_default_thirty_seconds(self, request_mock) -> None:
        request_mock.side_effect = [
            _FakeResponse({"items": [{"id": "inv-1", "name": "HyperV-test"}]}),
            _FakeResponse({"id": "inv-1", "name": "HyperV-test", "status": "available", "last_scan_at": "now"}),
        ]

        with redirect_stdout(io.StringIO()):
            self.assertEqual(InventoryCommands().rescan(_args(output="json", name="HyperV-test", id=None)), 0)

        method, url = request_mock.call_args.args
        self.assertEqual((method, url), ("POST", "http://example.test/api/v1/inventories/inv-1/rescan"))
        self.assertEqual(request_mock.call_args.kwargs["timeout"], INVENTORY_SCAN_TIMEOUT_SECONDS)
        self.assertGreaterEqual(INVENTORY_SCAN_TIMEOUT_SECONDS, 300)


class RestoreContextTests(unittest.TestCase):
    def test_a_hyperv_disk_path_is_read_as_its_storage_location(self) -> None:
        self.assertEqual(RestoreCommands._source_datastore("[ds1] vm/vm.vmdk"), "ds1")
        self.assertEqual(RestoreCommands._source_datastore("C:\\Hyper-V\\vm\\disk.vhdx"), "C:")
        self.assertEqual(RestoreCommands._source_datastore("e:\\vm\\disk.vhdx"), "E:")
        self.assertEqual(RestoreCommands._source_datastore("\\\\nas\\share\\vm\\disk.vhdx"), "\\\\nas\\share")
        self.assertEqual(RestoreCommands._source_datastore(""), "")

    def test_each_hyperv_disk_shows_the_target_of_its_own_storage_location(self) -> None:
        disks = [row for row in RestoreCommands._mappable_rows(HYPERV_CONTEXT) if row["kind"] == "disk"]
        self.assertEqual(disks, [
            {"kind": "disk", "source": "SCSI0:0", "target": "C:"},
            {"kind": "disk", "source": "SCSI0:1", "target": "E:"},
        ])

    def test_the_targets_list_hosts_with_status_networks_and_storage_with_free_space(self) -> None:
        rows = RestoreCommands._target_rows(HYPERV_CONTEXT)
        self.assertEqual([(row["kind"], row["name"]) for row in rows], [
            ("host", "NODE-01"), ("host", "node-02"), ("network", "Management"), ("datastore", "C:"), ("datastore", "E:"),
        ])
        self.assertEqual(rows[1]["status"], "unavailable")
        self.assertEqual(rows[2]["hosts"], "NODE-01")
        self.assertEqual(rows[4]["free"], "900.0 MB")
        self.assertEqual(rows[4]["capacity"], "1.0 GB")

    @patch("python.cli.api.client.requests.request")
    def test_the_table_prints_the_mappings_and_then_the_targets(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(HYPERV_CONTEXT)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            RestoreCommands().context(_args(output="table", restore_point="p-1"))
        text = stdout.getvalue()
        self.assertLess(text.index("DEFAULT TARGET"), text.index("TARGET  "))
        self.assertIn("unavailable", text)
        self.assertIn("900.0 MB", text)

    @patch("python.cli.api.client.requests.request")
    def test_json_output_is_the_unchanged_context(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(HYPERV_CONTEXT)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            RestoreCommands().context(_args(output="json", restore_point="p-1"))
        self.assertNotIn("DEFAULT TARGET", stdout.getvalue())
        self.assertIn('"free_bytes"', stdout.getvalue())


class SessionOutputTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_the_session_log_is_one_line_per_entry_in_a_table(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse({
            "id": "sess-1",
            "logs": [
                {"timestamp": "2026-10-07T18:12:37Z", "level": "info", "message": "Network mapping: nic -> et-test-sw"},
                {"timestamp": "2026-10-07T18:13:00Z", "level": "error", "message": "Storage location E: has 0.10 GiB free"},
            ],
        })
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(SessionCommands().log(_args(output="table", id="sess-1")), 0)
        lines = stdout.getvalue().splitlines()
        self.assertIn("LEVEL", lines[0])
        self.assertTrue(any("info" in line and "Network mapping" in line for line in lines))
        self.assertTrue(any("error" in line and "Storage location E:" in line for line in lines))
        self.assertNotIn('"component"', stdout.getvalue())

    @patch("python.cli.api.client.requests.request")
    def test_the_session_list_names_the_vm(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse({"items": [
            {"id": "sess-1", "state": "completed", "type": "restore", "vm_name": "et-test-hvc-r3", "job": None},
        ]})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            SessionCommands().list(_args(output="table", job=None))
        header, _rule, row = stdout.getvalue().splitlines()[:3]
        self.assertIn("VM", header)
        self.assertIn("et-test-hvc-r3", row)


class BackupInfoTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_each_restore_point_shows_its_gfs_marks_and_object_lock_date(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse({
            "id": "backup-1",
            "name": "job / vm / s3",
            "restore_points": [
                {"id": "p-1", "execution_type": "ActiveFull", "size_bytes": 1024,
                 "gfs_marks": {"weekly": True, "monthly": True, "yearly": False},
                 "immutability_expires_at": "2026-11-05T23:00:00+00:00"},
                {"id": "p-2", "execution_type": "Incremental", "size_bytes": 1024,
                 "gfs_marks": {"weekly": False, "monthly": False, "yearly": False},
                 "immutability_expires_at": None},
            ],
        })
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            BackupCommands().info(_args(output="table", id="backup-1"))
        text = stdout.getvalue()
        self.assertIn("IMMUTABLE UNTIL", text)
        point_line = next(line for line in text.splitlines() if line.startswith("p-1"))
        self.assertIn("weekly,monthly", point_line)
        self.assertIn("2026-11-05T23:00:00+00:00", point_line)


if __name__ == "__main__":
    unittest.main()
