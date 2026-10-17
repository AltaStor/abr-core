# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.srv.plugins.vcenter.vcenter_restore import _resolve_chain_disk_entry


def _manifest(disks: list[dict[str, object]]) -> dict[str, object]:
    return {"disks": disks}


class ResolveChainDiskEntryTests(unittest.TestCase):
    def test_placeholder_uuid_does_not_collide_across_disks(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "N/A",
                    "datastore_path": "[data1] LNX-EFI-M/LNX-EFI-M.vmdk",
                    "capacity_gb": 32,
                    "abr_backup_name": "01-LNX-EFI-M.vmdk",
                },
                {
                    "index": 2,
                    "uuid": "N/A",
                    "datastore_path": "[data1] LNX-EFI-M/LNX-EFI-M_1.vmdk",
                    "capacity_gb": 64,
                    "abr_backup_name": "02-LNX-EFI-M_1.vmdk",
                },
            ]
        )
        disk_2 = manifest["disks"][1]

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_2, disk_index=2
        )

        self.assertEqual(resolved["abr_backup_name"], "02-LNX-EFI-M_1.vmdk")
        self.assertEqual(resolved["capacity_gb"], 64)

    def test_placeholder_uuid_same_size_disks_still_resolve_correctly(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "N/A",
                    "datastore_path": "[data1] demo/demo.vmdk",
                    "capacity_gb": 50,
                    "abr_backup_name": "01-demo.vmdk",
                },
                {
                    "index": 2,
                    "uuid": "N/A",
                    "datastore_path": "[data1] demo/demo_1.vmdk",
                    "capacity_gb": 50,
                    "abr_backup_name": "02-demo_1.vmdk",
                },
            ]
        )
        disk_1, disk_2 = manifest["disks"]

        resolved_1 = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_1, disk_index=1
        )
        resolved_2 = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_2, disk_index=2
        )

        self.assertEqual(resolved_1["abr_backup_name"], "01-demo.vmdk")
        self.assertEqual(resolved_2["abr_backup_name"], "02-demo_1.vmdk")

    def test_placeholder_variants_are_all_rejected(self) -> None:
        for placeholder in ("N/A", "n/a", "None", "NULL", "Unknown", " N/A "):
            with self.subTest(placeholder=placeholder):
                manifest = _manifest(
                    [
                        {
                            "index": 1,
                            "uuid": placeholder,
                            "datastore_path": "[ds] a.vmdk",
                            "abr_backup_name": "01-a.vmdk",
                        },
                        {
                            "index": 2,
                            "uuid": placeholder,
                            "datastore_path": "[ds] b.vmdk",
                            "abr_backup_name": "02-b.vmdk",
                        },
                    ]
                )
                disk_2 = manifest["disks"][1]

                resolved = _resolve_chain_disk_entry(
                    manifest, selected_disk=disk_2, disk_index=2
                )

                self.assertEqual(resolved["abr_backup_name"], "02-b.vmdk")

    def test_real_distinct_uuid_still_matches_by_identity(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "6000c298-abcd-1111",
                    "datastore_path": "[ds] a.vmdk",
                    "abr_backup_name": "01-a.vmdk",
                },
                {
                    "index": 2,
                    "uuid": "6000c298-abcd-2222",
                    "datastore_path": "[ds] b.vmdk",
                    "abr_backup_name": "02-b.vmdk",
                },
            ]
        )
        disk_2 = manifest["disks"][1]

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_2, disk_index=2
        )

        self.assertEqual(resolved["abr_backup_name"], "02-b.vmdk")

    def test_real_uuid_wins_even_if_list_order_changes(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 2,
                    "uuid": "6000c298-abcd-2222",
                    "datastore_path": "[ds] b.vmdk",
                    "abr_backup_name": "02-b.vmdk",
                },
                {
                    "index": 1,
                    "uuid": "6000c298-abcd-1111",
                    "datastore_path": "[ds] a.vmdk",
                    "abr_backup_name": "01-a.vmdk",
                },
            ]
        )
        selected_disk = {"uuid": "6000c298-abcd-1111", "datastore_path": "[ds] a.vmdk"}

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=selected_disk, disk_index=1
        )

        self.assertEqual(resolved["abr_backup_name"], "01-a.vmdk")

    def test_mixed_real_and_placeholder_uuid_resolve_independently(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "6000c298-abcd-1111",
                    "datastore_path": "[ds] a.vmdk",
                    "abr_backup_name": "01-a.vmdk",
                },
                {
                    "index": 2,
                    "uuid": "N/A",
                    "datastore_path": "[ds] b.vmdk",
                    "abr_backup_name": "02-b.vmdk",
                },
            ]
        )
        disk_1, disk_2 = manifest["disks"]

        resolved_1 = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_1, disk_index=1
        )
        resolved_2 = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_2, disk_index=2
        )

        self.assertEqual(resolved_1["abr_backup_name"], "01-a.vmdk")
        self.assertEqual(resolved_2["abr_backup_name"], "02-b.vmdk")

    def test_missing_uuid_field_entirely_still_falls_back_to_datastore_path(
        self,
    ) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "datastore_path": "[ds] a.vmdk",
                    "abr_backup_name": "01-a.vmdk",
                },
                {
                    "index": 2,
                    "datastore_path": "[ds] b.vmdk",
                    "abr_backup_name": "02-b.vmdk",
                },
            ]
        )
        disk_2 = manifest["disks"][1]

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_2, disk_index=2
        )

        self.assertEqual(resolved["abr_backup_name"], "02-b.vmdk")

    def test_single_disk_manifest_resolves_via_shortcut_despite_placeholder_uuid(
        self,
    ) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "N/A",
                    "datastore_path": "[ds] only.vmdk",
                    "abr_backup_name": "01-only.vmdk",
                }
            ]
        )
        disk_1 = manifest["disks"][0]

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=disk_1, disk_index=1
        )

        self.assertEqual(resolved["abr_backup_name"], "01-only.vmdk")

    def test_no_identity_available_falls_back_to_stamped_index(self) -> None:
        manifest = _manifest(
            [
                {"index": 1, "abr_backup_name": "01-a.vmdk"},
                {"index": 2, "abr_backup_name": "02-b.vmdk"},
            ]
        )
        selected_disk: dict[str, object] = {}

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=selected_disk, disk_index=2
        )

        self.assertEqual(resolved["abr_backup_name"], "02-b.vmdk")

    def test_no_match_at_all_returns_none(self) -> None:
        manifest = _manifest(
            [
                {
                    "index": 1,
                    "uuid": "real-1",
                    "datastore_path": "[ds] a.vmdk",
                    "abr_backup_name": "01-a.vmdk",
                },
                {
                    "index": 2,
                    "uuid": "real-2",
                    "datastore_path": "[ds] b.vmdk",
                    "abr_backup_name": "02-b.vmdk",
                },
            ]
        )
        selected_disk = {
            "uuid": "real-nonexistent",
            "datastore_path": "[ds] nonexistent.vmdk",
        }

        resolved = _resolve_chain_disk_entry(
            manifest, selected_disk=selected_disk, disk_index=99
        )

        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
