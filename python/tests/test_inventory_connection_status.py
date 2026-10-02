# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV
from python.srv.plugins import build_hypervisor_plugin
from python.srv.services import abr_v41, inventory_management
from python.srv.services.inventory_management import (
    HyperVAuthError,
    HyperVUnreachableError,
)


HYPERV_HOST = "192.0.2.10"
HYPERV_CLUSTER_HOST = "192.0.2.11"
HYPERV_USERNAME = "EXAMPLE\\Administrator"
HYPERV_PASSWORD = "test-password"
HYPERV_NODE_A = "node-a"
HYPERV_NODE_B = "node-b"
HYPERV_CLUSTER_NAME = "node-cluster"
HYPERV_DNS_SUFFIX = "example.test"
HYPERV_SECOND_HOST = "192.0.2.12"
VCENTER_HOST = "192.0.2.20"
VCENTER_USERNAME = "administrator@vsphere.local"
VCENTER_PASSWORD = "test-password"

CLUSTER_IDENTITY = {
    "computer": HYPERV_NODE_A.upper(),
    "cluster": HYPERV_CLUSTER_NAME,
    "version": "Microsoft Windows Server 2025 Datacenter",
}
OTHER_NODE_IDENTITY = {
    "computer": HYPERV_NODE_B.upper(),
    "cluster": HYPERV_CLUSTER_NAME,
    "version": "Microsoft Windows Server 2025 Datacenter",
}
STANDALONE_IDENTITY = {"computer": "node-solo", "cluster": "", "version": "Windows Server 2022"}

HYPERV_PAYLOAD = {
    "name": "HyperV-01",
    "type": "hyper-v",
    "address": HYPERV_HOST,
    "username": HYPERV_USERNAME,
    "password": HYPERV_PASSWORD,
    "insecure": True,
}

VSPHERE_PAYLOAD = {
    "name": "vCenter-01",
    "type": "vsphere",
    "address": VCENTER_HOST,
    "username": VCENTER_USERNAME,
    "password": VCENTER_PASSWORD,
    "insecure": True,
}


class InventoryStatusAfterRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def test_a_hyperv_host_that_answered_is_registered_as_available(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection", return_value=None
        ):
            created = abr_v41.create_inventory_record(dict(HYPERV_PAYLOAD))

        self.assertEqual(created["status"], "available")
        self.assertTrue(created["status_checked_at"])
        self.assertIsNone(created["status_error"])
        self.assertEqual(HYPERVISORS[created["id"]]["status"], "available")

    def test_a_vcenter_that_answered_is_registered_as_available_too(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider, "test_connection", return_value=None
        ):
            created = abr_v41.create_inventory_record(dict(VSPHERE_PAYLOAD))

        self.assertEqual(created["status"], "available")
        self.assertTrue(created["status_checked_at"])

    def test_a_type_the_server_cannot_prove_stays_unknown(self) -> None:
        payload = {"name": "pve-01", "type": "proxmox-ve", "address": "10.0.0.5", "token": "t"}

        created = abr_v41.create_inventory_record(payload)

        self.assertEqual(created["status"], "unknown")
        self.assertIsNone(created["status_checked_at"])

    def test_a_refused_host_is_not_registered_at_all(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            side_effect=HyperVAuthError(HYPERV_HOST, HYPERV_USERNAME, "401"),
        ):
            with self.assertRaises(abr_v41.ApiError) as caught:
                abr_v41.create_inventory_record(dict(HYPERV_PAYLOAD))

        self.assertEqual(caught.exception.details.get("reason"), "auth")
        self.assertEqual(list(HYPERVISORS), [])


class InventoryStatusAfterAnEditTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection", return_value=None
        ):
            self.created = abr_v41.create_inventory_record(dict(HYPERV_PAYLOAD))
        self.inventory_id = self.created["id"]

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def test_an_edit_that_still_answers_stays_available(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection", return_value=None
        ) as check:
            edited = abr_v41.edit_inventory_record(self.inventory_id, {"password": "new-secret"})

        check.assert_called_once()
        self.assertEqual(edited["status"], "available")
        self.assertIsNone(edited["status_error"])

    def test_an_edit_that_breaks_the_connection_is_saved_but_marked_unavailable(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            side_effect=HyperVAuthError(HYPERV_HOST, HYPERV_USERNAME, "401"),
        ):
            edited = abr_v41.edit_inventory_record(self.inventory_id, {"password": "wrong"})

        self.assertEqual(edited["status"], "unavailable")
        self.assertIn("rejected the credentials", edited["status_error"])
        self.assertTrue(edited["status_checked_at"])
        self.assertEqual(HYPERVISORS[self.inventory_id]["status"], "unavailable")
        self.assertEqual(
            HYPERVISORS[self.inventory_id]["metadata"]["credentials"]["password"], "wrong"
        )

    def test_an_edit_of_the_address_is_re_checked_as_well(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            side_effect=HyperVUnreachableError(HYPERV_SECOND_HOST, [5986, 5985]),
        ):
            edited = abr_v41.edit_inventory_record(self.inventory_id, {"address": HYPERV_SECOND_HOST})

        self.assertEqual(edited["status"], "unavailable")
        self.assertIn("did not answer WinRM", edited["status_error"])

    def test_renaming_an_inventory_does_not_touch_the_host(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection"
        ) as check:
            edited = abr_v41.edit_inventory_record(self.inventory_id, {"name": "HyperV-01-renamed"})

        check.assert_not_called()
        self.assertEqual(edited["name"], "HyperV-01-renamed")
        self.assertEqual(edited["status"], "available")

    def test_editing_only_the_description_does_not_touch_the_host(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection"
        ) as check:
            abr_v41.edit_inventory_record(self.inventory_id, {"description": "lab cluster"})

        check.assert_not_called()


class InventoryStatusFromAnExplicitTestTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection", return_value=None
        ):
            self.inventory_id = abr_v41.create_inventory_record(dict(HYPERV_PAYLOAD))["id"]

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def test_a_failed_test_records_why_the_inventory_is_unavailable(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            side_effect=HyperVUnreachableError(HYPERV_HOST, [5986, 5985]),
        ):
            result = abr_v41.test_inventory(self.inventory_id)

        self.assertEqual(result["status"], "error")
        record = HYPERVISORS[self.inventory_id]
        self.assertEqual(record["status"], "unavailable")
        self.assertIn("did not answer WinRM", record["status_error"])
        self.assertTrue(record["status_checked_at"])

    def test_a_passing_test_clears_the_reason(self) -> None:
        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            side_effect=HyperVUnreachableError(HYPERV_HOST, [5986, 5985]),
        ):
            abr_v41.test_inventory(self.inventory_id)
        with patch.object(
            inventory_management.HyperVInventoryProvider, "test_connection", return_value=None
        ):
            result = abr_v41.test_inventory(self.inventory_id)

        self.assertEqual(result["status"], "success")
        record = HYPERVISORS[self.inventory_id]
        self.assertEqual(record["status"], "available")
        self.assertIsNone(record["status_error"])

    def test_the_inventory_dto_carries_the_status_and_when_it_was_checked(self) -> None:
        item = abr_v41.normalize_inventory_record(HYPERVISORS[self.inventory_id])

        self.assertIn("status_checked_at", item)
        self.assertIn("status_error", item)
        self.assertEqual(item["status"], "available")


if __name__ == "__main__":
    unittest.main()


def _add_hyperv(name: str, address: str, identity: dict) -> dict:
    payload = dict(HYPERV_PAYLOAD)
    payload["name"] = name
    payload["address"] = address
    with patch.object(
        inventory_management.HyperVInventoryProvider, "test_connection", return_value=identity
    ):
        return abr_v41.create_inventory_record(payload)


class OneClusterIsRegisteredOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def test_the_cluster_a_host_belongs_to_is_remembered_on_the_record(self) -> None:
        created = _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)

        metadata = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual(metadata["cluster_name"], HYPERV_CLUSTER_NAME)
        self.assertEqual(metadata["computer_name"], HYPERV_NODE_A.upper())

    def test_the_same_cluster_under_its_cluster_address_is_refused(self) -> None:
        _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)

        with self.assertRaises(abr_v41.ApiError) as caught:
            _add_hyperv("cluster", HYPERV_CLUSTER_HOST, OTHER_NODE_IDENTITY)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "inventory_already_registered")
        self.assertEqual(caught.exception.details.get("scope"), "cluster")
        self.assertEqual(caught.exception.details.get("existing_name"), "node-01")
        self.assertEqual(len(HYPERVISORS), 1)

    def test_another_cluster_is_still_accepted(self) -> None:
        _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)

        _add_hyperv("other", "10.0.0.5", {"computer": "HV-9", "cluster": "other-cluster"})

        self.assertEqual(len(HYPERVISORS), 2)

    def test_the_same_standalone_host_twice_is_refused_by_its_computer_name(self) -> None:
        _add_hyperv("solo", "10.0.0.7", STANDALONE_IDENTITY)

        with self.assertRaises(abr_v41.ApiError) as caught:
            _add_hyperv("solo-again", "10.0.0.8", STANDALONE_IDENTITY)

        self.assertEqual(caught.exception.details.get("scope"), "host")
        self.assertEqual(len(HYPERVISORS), 1)

    def test_a_standalone_host_does_not_collide_with_a_cluster_member(self) -> None:
        _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)

        _add_hyperv("solo", "10.0.0.7", {"computer": HYPERV_NODE_A.upper(), "cluster": ""})

        self.assertEqual(len(HYPERVISORS), 2)

    def test_a_record_registered_before_this_check_does_not_block_anything(self) -> None:
        legacy = _add_hyperv("legacy", HYPERV_HOST, {})
        self.assertNotIn("cluster_name", HYPERVISORS[legacy["id"]]["metadata"])

        _add_hyperv("cluster", HYPERV_CLUSTER_HOST, CLUSTER_IDENTITY)

        self.assertEqual(len(HYPERVISORS), 2)

    def test_an_edit_that_points_at_an_already_registered_cluster_is_refused(self) -> None:
        _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)
        other = _add_hyperv("other", "10.0.0.5", {"computer": "HV-9", "cluster": "other-cluster"})

        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            return_value=OTHER_NODE_IDENTITY,
        ):
            with self.assertRaises(abr_v41.ApiError) as caught:
                abr_v41.edit_inventory_record(other["id"], {"address": HYPERV_CLUSTER_HOST})

        self.assertEqual(caught.exception.code, "inventory_already_registered")
        self.assertEqual(HYPERVISORS[other["id"]]["metadata"]["cluster_name"], "other-cluster")

    def test_re_checking_the_same_inventory_does_not_collide_with_itself(self) -> None:
        created = _add_hyperv("node-01", HYPERV_HOST, CLUSTER_IDENTITY)

        with patch.object(
            inventory_management.HyperVInventoryProvider,
            "test_connection",
            return_value=OTHER_NODE_IDENTITY,
        ):
            edited = abr_v41.edit_inventory_record(created["id"], {"address": HYPERV_CLUSTER_HOST})

        self.assertEqual(edited["status"], "available")
        self.assertEqual(HYPERVISORS[created["id"]]["metadata"]["computer_name"], HYPERV_NODE_B.upper())


class HyperVPluginPortTests(unittest.TestCase):
    @staticmethod
    def _record(**metadata) -> dict:
        return {
            "id": "hv-1",
            "kind": "hyper-v",
            "metadata": {
                "host": HYPERV_HOST,
                "credentials": {"username": "u", "password": "p"},
                **metadata,
            },
        }

    def test_a_port_nobody_asked_for_leaves_both_winrm_ports_open_to_try(self) -> None:
        node = build_hypervisor_plugin(self._record(port=5986, port_explicit=False))

        self.assertIsNone(node.port)
        self.assertEqual(node._candidate_ports(), [5986, 5985])

    def test_a_port_the_operator_set_is_the_only_one_tried(self) -> None:
        node = build_hypervisor_plugin(self._record(port=5985, port_explicit=True))

        self.assertEqual(node.port, 5985)
        self.assertEqual(node._candidate_ports(), [5985])
