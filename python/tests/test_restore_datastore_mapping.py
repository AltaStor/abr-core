# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.plugins.vcenter import vcenter_restore
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


def _disk(name: str, datastore: str, *, capacity_bytes: int = 10 * 1024**3) -> dict[str, object]:
    return {
        "id": name,
        "label": name,
        "datastore_path": f"[{datastore}] vm-1/{name}.vmdk",
        "capacity_bytes": capacity_bytes,
        "used_bytes": capacity_bytes,
        "provisioning": "thick",
    }


def _datastore(name: str, free_bytes: int | None = 10 * 1024**4) -> dict[str, object]:
    return {"id": f"inv::datastore::{name}", "name": name, "free_bytes": free_bytes, "host_ids": ["host-1"]}


class RestoreDatastoreMapParsingTests(unittest.TestCase):
    def test_a_cli_style_pair_is_accepted(self) -> None:
        self.assertEqual(abr_v41._parse_restore_datastore_map(["ssd=sata"]), {"ssd": "sata"})

    def test_a_colon_is_accepted_like_the_network_map(self) -> None:
        self.assertEqual(abr_v41._parse_restore_datastore_map(["ssd:sata"]), {"ssd": "sata"})

    def test_the_web_ui_object_form_is_accepted(self) -> None:
        self.assertEqual(
            abr_v41._parse_restore_datastore_map([{"source_datastore": "ssd", "target_datastore": "sata"}]),
            {"ssd": "sata"},
        )

    def test_nothing_maps_to_an_empty_selection(self) -> None:
        self.assertEqual(abr_v41._parse_restore_datastore_map(None), {})
        self.assertEqual(abr_v41._parse_restore_datastore_map([]), {})

    def test_a_pair_without_a_separator_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            abr_v41._parse_restore_datastore_map(["ssd-only"])
        self.assertIn("<source>=<target>", error.exception.message)

    def test_a_pair_with_an_empty_side_is_rejected(self) -> None:
        with self.assertRaises(ApiError):
            abr_v41._parse_restore_datastore_map(["=sata"])


class RestoreDatastorePlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_vm = {
            "name": "vm-1",
            "disks": [_disk("disk-1", "ssd-fast"), _disk("disk-2", "sata-bulk"), _disk("disk-3", "sata-bulk")],
        }
        self.host = {"name": "host-1", "external_id": "host-1"}

    def _resolve(self, requested, restore_mode="new_vm", default="restore-target"):
        with patch.object(abr_v41, "_resolve_restore_target_datastore", side_effect=lambda _i, name, host: _datastore(name)):
            return abr_v41._resolve_restore_datastore_placement(
                self.source_vm,
                inventory_id="inv",
                host=self.host,
                default_datastore=_datastore(default),
                requested=requested,
                restore_mode=restore_mode,
            )

    def test_without_a_mapping_every_disk_lands_on_the_target_datastore(self) -> None:
        placement, _resolved = self._resolve(None)
        self.assertEqual(placement, {"ssd-fast": "restore-target", "sata-bulk": "restore-target"})

    def test_a_mapping_redirects_only_the_datastore_it_names(self) -> None:
        placement, _resolved = self._resolve(["sata-bulk=sata-restore"])
        self.assertEqual(placement, {"ssd-fast": "restore-target", "sata-bulk": "sata-restore"})

    def test_the_source_datastore_is_matched_the_way_the_target_is(self) -> None:
        placement, _resolved = self._resolve(["SATA-BULK=sata-restore"])
        self.assertEqual(placement["sata-bulk"], "sata-restore")

    def test_a_mapping_for_a_datastore_the_vm_does_not_use_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["nvme-other=sata-restore"])
        self.assertIn("does not use", error.exception.message)

    def test_in_place_keeps_every_disk_on_its_own_datastore(self) -> None:
        placement, resolved = self._resolve(None, restore_mode="in_place", default="ssd-fast")
        self.assertEqual(placement, {"ssd-fast": "ssd-fast", "sata-bulk": "sata-bulk"})
        self.assertEqual(sorted(resolved), ["sata-bulk", "ssd-fast"])

    def test_in_place_refuses_an_explicit_mapping(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["sata-bulk=sata-restore"], restore_mode="in_place")
        self.assertIn("cannot take a datastore mapping", error.exception.message)


class RestoreDatastoreCapacityTests(unittest.TestCase):
    def test_only_the_disks_landing_on_a_datastore_count_against_it(self) -> None:
        small = _datastore("sata-restore", free_bytes=15 * 1024**3)
        abr_v41._ensure_restore_target_capacity([_disk("disk-1", "ssd-fast")], small, "new_vm", None)

        with self.assertRaises(ApiError) as error:
            abr_v41._ensure_restore_target_capacity(
                [_disk("disk-2", "sata-bulk"), _disk("disk-3", "sata-bulk")], small, "new_vm", None
            )
        self.assertEqual(error.exception.code, "insufficient_datastore_space")

    def test_the_refusal_states_both_the_required_and_the_free_space(self) -> None:
        small = _datastore("sata-restore", free_bytes=15 * 1024**3)

        with self.assertRaises(ApiError) as error:
            abr_v41._ensure_restore_target_capacity(
                [_disk("disk-2", "sata-bulk"), _disk("disk-3", "sata-bulk")], small, "new_vm", None
            )

        message = error.exception.message
        self.assertIn("sata-restore", message)
        self.assertIn("required", message)
        self.assertIn("15.00 GiB free", message)
        self.assertEqual(error.exception.details["free_bytes"], 15 * 1024**3)
        self.assertIsNotNone(error.exception.details["required_bytes"])


class RestoreMappingConflictTests(unittest.TestCase):
    def test_a_datastore_mapped_twice_to_the_same_target_is_accepted(self) -> None:
        self.assertEqual(abr_v41._parse_restore_datastore_map(["ssd=sata", "ssd=sata"]), {"ssd": "sata"})

    def test_a_datastore_mapped_twice_to_different_targets_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            abr_v41._parse_restore_datastore_map(["ssd=sata", "ssd=nvme"])
        self.assertIn("--datastore-map", error.exception.message)
        self.assertIn("different targets", error.exception.message)

    def test_a_network_mapped_twice_to_different_targets_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            abr_v41._parse_restore_network_map(["VM Network=DMZ", "VM Network=Storage"])
        self.assertIn("--network-map", error.exception.message)

    def test_networks_that_differ_only_in_letter_case_are_two_sources(self) -> None:
        self.assertEqual(
            abr_v41._parse_restore_network_map(["DMZ=Recovery", "dmz=Isolated"]),
            {"DMZ": "Recovery", "dmz": "Isolated"},
        )

    def test_a_network_mapped_twice_with_the_same_spelling_still_conflicts(self) -> None:
        with self.assertRaises(ApiError):
            abr_v41._parse_restore_network_map(["DMZ=Recovery", "DMZ=Isolated"])

    def test_the_same_source_in_different_letter_case_still_conflicts(self) -> None:
        with self.assertRaises(ApiError):
            abr_v41._parse_restore_datastore_map(["ssd=sata", "SSD=nvme"])


class RestoreDiskPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_vm = {
            "name": "vm-1",
            "disks": [
                dict(_disk("disk-1", "ssd-fast"), label="Hard disk 1"),
                dict(_disk("disk-2", "ssd-fast"), label="Hard disk 2"),
            ],
        }
        self.host = {"name": "host-1", "external_id": "host-1"}

    def _resolve(self, requested, restore_mode="new_vm"):
        with patch.object(abr_v41, "_resolve_restore_target_datastore", side_effect=lambda _i, name, host: _datastore(name)):
            return abr_v41._resolve_restore_disk_placement(
                self.source_vm,
                inventory_id="inv",
                host=self.host,
                requested=requested,
                restore_mode=restore_mode,
            )

    def test_without_an_override_nothing_is_placed_per_disk(self) -> None:
        self.assertEqual(self._resolve(None), ([], {}))

    def test_a_disk_label_places_only_that_disk(self) -> None:
        targets, resolved = self._resolve(["Hard disk 2=nvme"])
        self.assertEqual(targets, [{"index": 2, "disk_id": "disk-2", "label": "Hard disk 2", "target_datastore": "nvme"}])
        self.assertEqual(list(resolved), ["nvme"])

    def test_a_disk_id_places_the_same_disk(self) -> None:
        targets, _resolved = self._resolve(["disk-2=nvme"])
        self.assertEqual([item["index"] for item in targets], [2])

    def test_the_web_ui_object_form_is_accepted(self) -> None:
        targets, _resolved = self._resolve([{"source_disk": "Hard disk 1", "target_datastore": "nvme"}])
        self.assertEqual([item["index"] for item in targets], [1])

    def test_a_disk_the_restore_point_does_not_carry_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["Hard disk 9=nvme"])
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("does not carry", error.exception.message)

    def test_in_place_refuses_a_disk_placement(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["Hard disk 2=nvme"], restore_mode="in_place")
        self.assertEqual(error.exception.status_code, 422)

    def test_a_disk_placed_twice_to_different_targets_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["Hard disk 2=nvme", "Hard disk 2=sata"])
        self.assertIn("--disk-datastore", error.exception.message)

    def test_a_disk_override_wins_over_the_datastore_map(self) -> None:
        disks = self.source_vm["disks"]
        placement = {"ssd-fast": "sata-restore"}
        self.assertEqual(
            abr_v41._restore_disk_target_name(disks[0], 1, {2: "nvme"}, placement, "restore-target"),
            "sata-restore",
        )
        self.assertEqual(
            abr_v41._restore_disk_target_name(disks[1], 2, {2: "nvme"}, placement, "restore-target"),
            "nvme",
        )

    def test_without_either_the_default_datastore_is_used(self) -> None:
        self.assertEqual(
            abr_v41._restore_disk_target_name(self.source_vm["disks"][0], 1, {}, {}, "restore-target"),
            "restore-target",
        )


class RestoreNicNetworkPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_vm = {
            "nics": [
                {"id": "nic-1", "label": "Network adapter 1", "source_network": "VM Network"},
                {"id": "nic-2", "label": "Network adapter 2", "source_network": "VM Network"},
            ]
        }
        self.host = {"name": "host-1", "external_id": "host-1"}

    def _resolve(self, requested):
        with patch.object(
            abr_v41,
            "_resolve_restore_target_network",
            side_effect=lambda _i, name, source_networks, host: {"id": f"inv::network::{name}", "name": name},
        ):
            return abr_v41._resolve_restore_nic_network_mappings(
                self.source_vm, inventory_id="inv", host=self.host, requested=requested
            )

    def test_without_an_override_nothing_is_mapped(self) -> None:
        self.assertEqual(self._resolve(None), [])

    def test_two_adapters_on_one_source_network_reach_different_targets(self) -> None:
        self.assertEqual(
            self._resolve(["Network adapter 1=DMZ", "Network adapter 2=Storage"]),
            [
                {"source_nic_id": "nic-1", "target_network_id": "inv::network::DMZ"},
                {"source_nic_id": "nic-2", "target_network_id": "inv::network::Storage"},
            ],
        )

    def test_an_adapter_id_names_the_same_adapter(self) -> None:
        self.assertEqual(self._resolve(["nic-2=DMZ"]), [{"source_nic_id": "nic-2", "target_network_id": "inv::network::DMZ"}])

    def test_an_adapter_the_restore_point_does_not_carry_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["Network adapter 9=DMZ"])
        self.assertEqual(error.exception.code, "invalid_restore")
        self.assertEqual(error.exception.status_code, 422)

    def test_an_adapter_placed_twice_to_different_targets_is_refused(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._resolve(["nic-1=DMZ", "nic-1=Storage"])
        self.assertIn("--nic-network", error.exception.message)


class RestoreDiskLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            {"disk_label": "Hard disk 1", "datastore_path": "[ssd-fast] vm-1/vm-1.vmdk", "capacity_gb": 40},
            {"disk_label": "Hard disk 2", "datastore_path": "[sata-bulk] vm-1/vm-1_1.vmdk", "capacity_gb": 500},
        ]

    def test_without_a_map_every_disk_carries_the_default_datastore(self) -> None:
        layout = vcenter_restore._build_disk_layout(self.entries, {}, "restore-target")
        self.assertEqual([item["datastore_name"] for item in layout], ["restore-target", "restore-target"])

    def test_a_mapped_disk_carries_its_own_datastore(self) -> None:
        layout = vcenter_restore._build_disk_layout(self.entries, {"sata-bulk": "sata-restore"}, "restore-target")
        self.assertEqual([item["datastore_name"] for item in layout], ["restore-target", "sata-restore"])

    def test_a_per_disk_target_wins_over_the_datastore_map(self) -> None:
        layout = vcenter_restore._build_disk_layout(
            self.entries, {"sata-bulk": "sata-restore"}, "restore-target", disk_targets={1: "nvme"}
        )
        self.assertEqual([item["datastore_name"] for item in layout], ["nvme", "sata-restore"])

    def test_a_per_disk_target_applies_without_any_datastore_map(self) -> None:
        layout = vcenter_restore._build_disk_layout(self.entries, {}, "restore-target", disk_targets={2: "nvme"})
        self.assertEqual([item["datastore_name"] for item in layout], ["restore-target", "nvme"])

    def test_a_disk_without_a_readable_source_path_falls_back_to_the_default(self) -> None:
        name = vcenter_restore._disk_target_datastore_name(
            {"datastore_path": "not-a-datastore-path"}, {"sata-bulk": "sata-restore"}, "restore-target"
        )
        self.assertEqual(name, "restore-target")


if __name__ == "__main__":
    unittest.main()


class RestoreDiskProvisioningTests(unittest.TestCase):
    THIN = {"source_disk_type_code": 7, "source_disk_type_name": "vmfs_thin"}
    THICK_LAZY = {
        "source_disk_type_code": 5,
        "source_disk_type_name": "vmfs_flat",
        "source_disk_eager_zeroed": False,
    }
    THICK_EAGER = {
        "source_disk_type_code": 5,
        "source_disk_type_name": "vmfs_flat",
        "source_disk_eager_zeroed": True,
    }
    THICK_UNRECORDED = {"source_disk_type_code": 5, "source_disk_type_name": "vmfs_flat"}

    def test_original_keeps_a_thin_disk_thin(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(parameters={}, disk_entry=dict(self.THIN))

        self.assertTrue(decision["thin_provisioned"])
        self.assertFalse(decision["eager_zeroed"])

    def test_original_keeps_a_lazy_zeroed_thick_disk_thick(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(parameters={}, disk_entry=dict(self.THICK_LAZY))

        self.assertFalse(decision["thin_provisioned"])
        self.assertFalse(decision["eager_zeroed"])

    def test_original_keeps_an_eager_zeroed_thick_disk_eager_zeroed(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(parameters={}, disk_entry=dict(self.THICK_EAGER))

        self.assertFalse(decision["thin_provisioned"])
        self.assertTrue(decision["eager_zeroed"])

    def test_a_point_taken_before_eager_zeroed_was_recorded_restores_lazy_zeroed_thick(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(parameters={}, disk_entry=dict(self.THICK_UNRECORDED))

        self.assertFalse(decision["thin_provisioned"])
        self.assertFalse(decision["eager_zeroed"])

    def test_an_explicit_thin_request_overrides_a_thick_source(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(
            parameters={"disk_type": "thin"}, disk_entry=dict(self.THICK_EAGER)
        )

        self.assertTrue(decision["thin_provisioned"])
        self.assertFalse(decision["eager_zeroed"])

    def test_an_explicit_thick_request_does_not_borrow_the_source_eager_flag(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(
            parameters={"disk_type": "thick"}, disk_entry=dict(self.THICK_EAGER)
        )

        self.assertFalse(decision["thin_provisioned"])
        self.assertFalse(decision["eager_zeroed"])

    def test_a_per_disk_override_wins_over_the_global_choice(self) -> None:
        decision = vcenter_restore._resolve_disk_provisioning(
            parameters={"disk_type": "thin", "disk_types": {"2": "thick"}},
            disk_entry=dict(self.THIN),
            disk_index=2,
        )

        self.assertFalse(decision["thin_provisioned"])

    def test_the_layout_carries_each_disk_its_own_decision(self) -> None:
        entries = [
            {"disk_label": "Hard disk 1", "datastore_path": "[ssd] vm-1/vm-1.vmdk", "capacity_gb": 40, **self.THIN},
            {"disk_label": "Hard disk 2", "datastore_path": "[ssd] vm-1/vm-1_1.vmdk", "capacity_gb": 500, **self.THICK_EAGER},
        ]
        provisioning = [
            vcenter_restore._resolve_disk_provisioning(parameters={}, disk_entry=entry, disk_index=index)
            for index, entry in enumerate(entries, start=1)
        ]

        layout = vcenter_restore._build_disk_layout(entries, {}, "restore-target", provisioning=provisioning)

        self.assertEqual([item["thin_provisioned"] for item in layout], [True, False])
        self.assertEqual([item["eager_zeroed"] for item in layout], [False, True])

    def test_a_layout_built_without_decisions_leaves_them_unset(self) -> None:
        entries = [{"disk_label": "Hard disk 1", "datastore_path": "[ssd] vm-1/vm-1.vmdk", "capacity_gb": 40}]

        layout = vcenter_restore._build_disk_layout(entries, {}, "restore-target")

        self.assertIsNone(layout[0]["thin_provisioned"])
        self.assertFalse(layout[0]["eager_zeroed"])

    def test_the_session_result_reports_the_eager_zeroed_decision(self) -> None:
        details = vcenter_restore._disk_type_details("original", 5, True)

        self.assertEqual(details["resolved_disk_type_name"], "vmfs_flat")
        self.assertTrue(details["resolved_disk_eager_zeroed"])
