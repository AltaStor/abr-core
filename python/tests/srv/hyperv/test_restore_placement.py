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


class InPlaceAdapterRestoreTests(unittest.TestCase):
    VM_CONFIG = {
        "network_adapters": [
            {
                "label": "Network Adapter",
                "network": "Management",
                "vlan_mode": "Untagged",
                "vlan_id": 0,
                "adapter_id": "F48CCA05-161D-499E-8C8A-ACC26AB3D0E3",
                "mac": "00155D0A0B0C",
                "dynamic_mac": False,
                "legacy": False,
            },
            {"label": "nic2", "network": "Isolated", "vlan_mode": "Access", "vlan_id": 9, "mac": "000000000000", "dynamic_mac": True},
        ]
    }

    def _run(self, rows=None, error=None, vm_config=None):
        sent: list = []

        class Node:
            def restore_vm_adapters(self, vm_id, adapters):
                sent.append((vm_id, adapters))
                if error:
                    raise error
                return rows or []

        notes: list[tuple[str, str]] = []
        warnings: list[str] = []
        with patch.object(hyperv_restore, "_emit", side_effect=lambda cb, stage, progress, message, **kw: notes.append(("info", message))), patch.object(
            hyperv_restore, "_warn", side_effect=lambda cb, stage, progress, message, **kw: notes.append(("warn", message))
        ):
            hyperv_restore._restore_adapters(Node(), "vm-1", "vm", self.VM_CONFIG if vm_config is None else vm_config, warnings, None)
        return sent, notes, warnings

    def test_every_adapter_of_the_backup_is_sent_with_what_identifies_it(self) -> None:
        sent, _, _ = self._run()

        self.assertEqual(
            sent,
            [
                (
                    "vm-1",
                    [
                        {
                            "label": "Network Adapter",
                            "switch": "Management",
                            "vlan_mode": "Untagged",
                            "vlan_id": 0,
                            "adapter_id": "F48CCA05-161D-499E-8C8A-ACC26AB3D0E3",
                            "mac": "00155D0A0B0C",
                            "dynamic_mac": False,
                            "legacy": False,
                        },
                        {
                            "label": "nic2",
                            "switch": "Isolated",
                            "vlan_mode": "Access",
                            "vlan_id": 9,
                            "adapter_id": "",
                            "mac": "000000000000",
                            "dynamic_mac": True,
                            "legacy": False,
                        },
                    ],
                )
            ],
        )

    def test_removed_recreated_and_reconnected_adapters_are_each_logged(self) -> None:
        rows = [
            {"label": "Network Adapter", "status": "removed", "previous": "vs-b", "switch": ""},
            {"label": "Network Adapter", "status": "created", "previous": "", "switch": "Management", "vlan_changed": False},
            {"label": "nic2", "status": "reconnected", "previous": "", "switch": "Isolated", "vlan_changed": True, "previous_vlan": "VLAN 5"},
        ]

        _, notes, warnings = self._run(rows=rows)

        self.assertEqual(
            notes,
            [
                ("info", "Network adapter Network Adapter on vs-b removed from vm: the backup does not have it"),
                ("info", "Network adapter Network Adapter of vm recreated on Management with its MAC address 00155D0A0B0C, as in the backup"),
                ("info", "Network adapter nic2 of vm reconnected from no switch to Isolated, as in the backup"),
                ("info", "Network adapter nic2 of vm set back from VLAN 5 to VLAN 9, as in the backup"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_a_recreated_adapter_with_a_dynamic_mac_names_no_address(self) -> None:
        _, notes, _ = self._run(rows=[{"label": "nic2", "status": "created", "switch": "Isolated"}])

        self.assertEqual(notes, [("info", "Network adapter nic2 of vm recreated on Isolated, as in the backup")])

    def test_a_backup_without_adapters_still_removes_the_ones_added_since(self) -> None:
        sent, notes, _ = self._run(rows=[{"label": "Network Adapter", "status": "removed", "previous": "vs-b"}], vm_config={"network_adapters": []})

        self.assertEqual(sent, [("vm-1", [])])
        self.assertEqual(notes, [("info", "Network adapter Network Adapter on vs-b removed from vm: the backup does not have it")])

    def test_a_backup_that_does_not_record_adapters_leaves_them_and_says_so(self) -> None:
        sent, notes, warnings = self._run(vm_config={"num_cpu": 1})

        self.assertEqual(sent, [])
        self.assertEqual(warnings, ["The backup does not record the network adapters of vm, so they are left as they are"])
        self.assertEqual(notes[0][0], "warn")

    def test_a_failing_adapter_restore_warns_instead_of_failing_the_restored_vm(self) -> None:
        _, notes, warnings = self._run(error=HyperVError("Add-VMNetworkAdapter failed"))

        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be set back to the backup's: Add-VMNetworkAdapter failed", warnings[0])
        self.assertEqual(notes[0][0], "warn")


class InPlaceBootOrderTests(unittest.TestCase):
    def _restore(self, vm_config, restored_slots, error=None):
        calls: list = []

        class Node:
            def set_boot_disk(self, vm_id, slot, boot_order):
                calls.append((vm_id, slot, boot_order))
                if error:
                    raise error

        warnings: list[str] = []
        with patch.object(hyperv_restore, "_warn"):
            restored = hyperv_restore._restore_boot_order(Node(), "vm-1", "vm", vm_config, restored_slots, warnings, None)
        return restored, calls, warnings

    def test_a_generation_2_vm_that_booted_from_a_restored_disk_boots_from_it_again(self) -> None:
        order = [{"boot_type": "Drive", "device": "SCSI0:0"}, {"boot_type": "Network", "device": "Network Adapter"}]

        restored, calls, _ = self._restore({"generation": 2, "boot_order": order}, {"SCSI0:0": "SCSI0:0"})

        self.assertTrue(restored)
        self.assertEqual(calls, [("vm-1", "SCSI0:0", order)])

    def test_a_generation_2_vm_that_booted_from_something_else_is_not_touched(self) -> None:
        order = [{"boot_type": "Network", "device": "Network Adapter"}, {"boot_type": "Drive", "device": "SCSI0:0"}]

        restored, calls, _ = self._restore({"generation": 2, "boot_order": order}, {"SCSI0:0": "SCSI0:0"})

        self.assertFalse(restored)
        self.assertEqual(calls, [])

    def test_a_generation_1_vm_gets_the_backup_startup_order(self) -> None:
        order = [{"boot_type": kind, "device": ""} for kind in ("IDE", "CD", "LegacyNetworkAdapter", "Floppy")]

        restored, calls, _ = self._restore({"generation": 1, "boot_order": order}, {"IDE0:0": "IDE0:0"})

        self.assertTrue(restored)
        self.assertEqual(calls, [("vm-1", "", order)])

    def test_a_boot_order_that_cannot_be_set_is_a_warning(self) -> None:
        order = [{"boot_type": "Drive", "device": "SCSI0:0"}]

        restored, _, warnings = self._restore({"generation": 2, "boot_order": order}, {"SCSI0:0": "SCSI0:0"}, HyperVError("denied"))

        self.assertFalse(restored)
        self.assertEqual(warnings, ["vm is restored, but its boot order could not be set back to the backup's: denied"])


class InPlaceKeptSettingsTests(unittest.TestCase):
    BACKUP = {
        "version": "12.0",
        "secure_boot_enabled": True,
        "secure_boot_template": "MicrosoftWindows",
        "tpm_enabled": False,
        "boot_order": [{"boot_type": "Drive", "device": "SCSI0:0"}],
        "dvd_drives": [{"slot": "SCSI0:1", "path": ""}],
        "hard_drives": [{"slot": "SCSI0:0", "path": "C:\\VMs\\vm.vhdx"}],
    }

    def test_a_vm_that_matches_the_backup_has_nothing_to_report(self) -> None:
        self.assertEqual(hyperv_restore._kept_as_they_are(dict(self.BACKUP), self.BACKUP, True), [])

    def test_every_setting_left_as_it_is_that_differs_is_named(self) -> None:
        current = {
            **self.BACKUP,
            "version": "12.1",
            "secure_boot_enabled": False,
            "tpm_enabled": True,
            "boot_order": [{"boot_type": "Network", "device": "Network Adapter"}],
            "dvd_drives": [{"slot": "SCSI0:1", "path": "D:\\iso\\setup.iso"}, {"slot": "SCSI0:3", "path": ""}],
            "hard_drives": [*self.BACKUP["hard_drives"], {"slot": "SCSI0:2", "path": "C:\\VMs\\data.vhdx"}],
        }

        kept = hyperv_restore._kept_as_they_are(current, self.BACKUP, False)

        self.assertEqual(
            kept,
            [
                "Secure Boot is off (the backup: on)",
                "the virtual TPM is on (the backup: off)",
                "the DVD drive SCSI0:1 holds D:\\iso\\setup.iso (the backup: no media)",
                "the DVD drive SCSI0:3 was added after the backup",
                "the hard disk SCSI0:2 (C:\\VMs\\data.vhdx) was attached after the backup",
                "the configuration version is 12.1 (the backup: 12.0)",
                "it boots first from Network Network Adapter (the backup: Drive SCSI0:0)",
            ],
        )

    def test_a_secure_boot_template_change_is_named(self) -> None:
        current = {**self.BACKUP, "secure_boot_template": "MicrosoftUEFICertificateAuthority"}

        self.assertEqual(
            hyperv_restore._kept_as_they_are(current, self.BACKUP, True),
            ["the Secure Boot template is MicrosoftUEFICertificateAuthority (the backup: MicrosoftWindows)"],
        )

    def test_a_boot_order_set_back_is_not_reported(self) -> None:
        current = {**self.BACKUP, "boot_order": [{"boot_type": "Network", "device": "Network Adapter"}]}

        self.assertEqual(hyperv_restore._kept_as_they_are(current, self.BACKUP, True), [])

    def test_a_backup_that_did_not_record_a_setting_reports_nothing_about_it(self) -> None:
        current = {"tpm_enabled": True, "dvd_drives": [{"slot": "SCSI0:1", "path": "x.iso"}], "hard_drives": [{"slot": "SCSI0:2", "path": "d.vhdx"}]}

        self.assertEqual(hyperv_restore._kept_as_they_are(current, {}, False), [])

    def test_a_configuration_that_could_not_be_read_is_reported(self) -> None:
        self.assertEqual(
            hyperv_restore._kept_as_they_are(None, self.BACKUP, True),
            ["the rest of its configuration, which could not be read to compare it with the backup"],
        )

    def test_a_changed_setting_is_shown_with_both_values_and_notes_by_name_only(self) -> None:
        self.assertEqual(
            hyperv_restore._setting_change({"setting": "processor count", "before": "2", "after": "1"}), "processor count (2 -> 1)"
        )
        self.assertEqual(hyperv_restore._setting_change({"setting": "notes", "before": "long text", "after": ""}), "notes")


if __name__ == "__main__":
    unittest.main()
