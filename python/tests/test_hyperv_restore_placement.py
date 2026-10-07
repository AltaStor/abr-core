# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv_restore
from python.srv.plugins.hyperv.hyperv import HyperVError


class DiskPlacementTests(unittest.TestCase):
    def test_each_requested_disk_keeps_its_target_whatever_the_manifest_order(self) -> None:
        manifest_disks = [{"slot": "SCSI0:1"}, {"slot": "SCSI0:0"}, {"slot": "IDE0:0"}]
        parameters = {
            "disk_datastore_map": [
                {"index": 1, "disk_id": "b", "label": "SCSI0:1", "target_datastore": "E:"},
                {"index": 3, "disk_id": "c", "label": "IDE0:0", "target_datastore": "F:"},
            ]
        }

        self.assertEqual(hyperv_restore._disk_placements(parameters, manifest_disks), {"SCSI0:1": "E:", "IDE0:0": "F:"})

    def test_entries_that_name_no_disk_are_ignored(self) -> None:
        parameters = {
            "disk_datastore_map": [
                {"index": 0, "target_datastore": "E:"},
                {"index": 5, "target_datastore": "E:"},
                {"index": True, "target_datastore": "E:"},
                {"index": 1, "target_datastore": ""},
                "not an entry",
            ]
        }

        self.assertEqual(hyperv_restore._disk_placements(parameters, [{"slot": "SCSI0:0"}]), {})

    def test_no_placement_requested(self) -> None:
        self.assertEqual(hyperv_restore._disk_placements({}, [{"slot": "SCSI0:0"}]), {})


class AdapterPlacementTests(unittest.TestCase):
    def test_two_adapters_on_one_network_can_land_on_different_switches(self) -> None:
        vm_config = {"network_adapters": [{"label": "nic1", "network": "Management"}, {"label": "nic2", "network": "Management"}]}
        parameters = {
            "network_map": {"Management": "Management"},
            "network_adapter_map": [{"index": 1, "target_network": "Isolated"}],
        }

        targets = [adapter["target_switch"] for adapter in hyperv_restore._adapter_targets(vm_config, parameters)]

        self.assertEqual(targets, ["Management", "Isolated"])


class InPlaceAdapterReconnectTests(unittest.TestCase):
    VM_CONFIG = {
        "network_adapters": [
            {"label": "Network Adapter", "network": "Management", "vlan_mode": "Untagged", "vlan_id": 0},
            {"label": "nic2", "network": "Isolated", "vlan_mode": "Access", "vlan_id": 9},
        ]
    }

    def _run(self, rows=None, error=None):
        sent: list = []

        class Node:
            def connect_vm_adapters(self, vm_id, adapters):
                sent.append((vm_id, adapters))
                if error:
                    raise error
                return rows or []

        notes: list[tuple[str, str]] = []
        warnings: list[str] = []
        with patch.object(hyperv_restore, "_emit", side_effect=lambda cb, stage, progress, message, **kw: notes.append(("info", message))), patch.object(
            hyperv_restore, "_warn", side_effect=lambda cb, stage, progress, message, **kw: notes.append(("warn", message))
        ):
            hyperv_restore._reconnect_adapters(Node(), "vm-1", "vm", self.VM_CONFIG, warnings, None)
        return sent, notes, warnings

    def test_the_backup_switches_and_vlans_are_sent_for_every_adapter(self) -> None:
        sent, _, _ = self._run()

        self.assertEqual(
            sent,
            [
                (
                    "vm-1",
                    [
                        {"label": "Network Adapter", "switch": "Management", "vlan_mode": "Untagged", "vlan_id": 0},
                        {"label": "nic2", "switch": "Isolated", "vlan_mode": "Access", "vlan_id": 9},
                    ],
                )
            ],
        )

    def test_what_changed_is_logged_and_a_missing_adapter_is_a_warning(self) -> None:
        rows = [
            {"label": "Network Adapter", "status": "reconnected", "previous": "", "switch": "Management", "vlan_changed": False},
            {"label": "nic2", "status": "unchanged", "previous": "Isolated", "switch": "Isolated", "vlan_changed": True, "previous_vlan": "VLAN 5"},
            {"label": "nic3", "status": "missing", "switch": "Management"},
        ]

        _, notes, warnings = self._run(rows=rows)

        self.assertEqual(
            notes,
            [
                ("info", "Network adapter Network Adapter of vm reconnected from no switch to Management, as in the backup"),
                ("info", "Network adapter nic2 of vm set back from VLAN 5 to VLAN 9, as in the backup"),
                ("warn", "vm no longer has the network adapter nic3 the backup recorded on Management"),
            ],
        )
        self.assertEqual(warnings, ["vm no longer has the network adapter nic3 the backup recorded on Management"])

    def test_a_failing_reconnect_warns_instead_of_failing_the_restored_vm(self) -> None:
        _, notes, warnings = self._run(error=HyperVError("Connect-VMNetworkAdapter failed"))

        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be set back to the backup's switches: Connect-VMNetworkAdapter failed", warnings[0])
        self.assertEqual(notes[0][0], "warn")

    def test_a_vm_without_adapters_is_left_alone(self) -> None:
        class Node:
            def connect_vm_adapters(self, vm_id, adapters):
                raise AssertionError("not called")

        hyperv_restore._reconnect_adapters(Node(), "vm-1", "vm", {"network_adapters": []}, [], None)


if __name__ == "__main__":
    unittest.main()
