# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV
from python.srv.plugins import build_hypervisor_plugin
from python.srv.plugins.pve import inventory as pve_inventory
from python.srv.plugins.pve.pve import PveNode
from python.srv.plugins.pve.setup import PveSetupError
from python.srv.plugins.pve.ssh import SshAuthError, SshError
from python.srv.services import abr_v41, discovery, inventory_management

PAYLOAD = {
    "name": "PVE-Cluster",
    "type": "proxmox-ve",
    "address": "pve1.example.com",
    "username": "root@pam",
    "password": "root-secret",
}


def _setup(token: str = "token-1", key: str = "PRIVATE KEY") -> dict:
    return {
        "pve": {
            "token_id": "abr@pve!altastor-x",
            "ssh_key": key,
            "ssh_port": 22,
            "nodes": [
                {"name": "node1", "address": "pve1.example.com", "host_keys": ["ssh-ed25519 AAAA1"]},
                {"name": "node2", "address": "pve2.example.com", "host_keys": ["ssh-ed25519 AAAA2"]},
            ],
            "certificates": ["CA"],
            "storage": "abr",
        },
        "token": token,
        "cluster_name": "cluster1",
        "nodes": ["node1", "node2"],
        "warnings": [],
    }


class FakeSetUp:
    """Stands in for set_up_cluster: checks the cluster is new as the real one does, before anything else."""

    def __init__(self, cluster: str = "cluster1", failure: Exception | None = None) -> None:
        self.cluster = cluster
        self.failure = failure
        self.calls: list[dict] = []

    def __call__(self, **options):
        self.calls.append(options)
        options["check_new"](self.cluster, ["node1", "node2"])
        if self.failure is not None:
            raise self.failure
        return _setup(token=f"token-{len(self.calls)}")


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        self.addCleanup(HYPERVISORS.clear)
        self.addCleanup(RESOURCES_BY_HV.clear)
        self.set_up = FakeSetUp()
        self.enterContext(patch.object(pve_inventory, "set_up_cluster", self.set_up))
        self.enterContext(patch.object(pve_inventory, "package_path", return_value=None))
        self.enterContext(patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False))

    def add(self, **changes) -> dict:
        return abr_v41.create_inventory_record({**PAYLOAD, **changes})

    def test_adding_sets_the_cluster_up_and_keeps_its_token_and_key_not_the_password(self):
        created = self.add()
        stored = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual(created["status"], "available")
        self.assertEqual(stored["credentials"], {"username": "root@pam", "password": None})
        self.assertEqual(
            (stored["token"], stored["pve"]["ssh_key"], stored["cluster_name"]),
            ("token-1", "PRIVATE KEY", "cluster1"),
        )
        self.assertEqual(
            (created["metadata"]["token"], created["metadata"]["pve"]["ssh_key"]), ("********", "********")
        )
        self.assertTrue(created["connection"]["token_present"])
        call = self.set_up.calls[0]
        self.assertEqual(
            (call["address"], call["root_password"], call["inventory_id"], call["previous"]),
            ("pve1.example.com", "root-secret", created["id"], None),
        )

    def test_only_root_with_a_password_and_no_token_can_add(self):
        for changes, message in (
            ({"username": "admin@pve"}, "added with root's password"),
            ({"password": ""}, "requires root's password"),
            ({"token": "abc"}, "takes no token"),
        ):
            with self.subTest(changes=changes), self.assertRaises(abr_v41.ApiError) as raised:
                self.add(**changes)
            self.assertEqual(raised.exception.status_code, 422)
            self.assertIn(message, raised.exception.message)
        self.assertEqual(self.set_up.calls, [])

    def test_a_failed_setup_says_why_and_registers_nothing(self):
        self.set_up.failure = SshAuthError("root@pve1.example.com:22 refused this password: Authentication failed")
        with self.assertRaises(abr_v41.ApiError) as raised:
            self.add()
        self.assertEqual((raised.exception.status_code, raised.exception.details["reason"]), (422, "auth"))
        self.assertIn("Give the root password of the node at pve1.example.com", raised.exception.message)
        self.set_up.failure = PveSetupError("apt could not install abr-pve-backup")
        with self.assertRaises(abr_v41.ApiError) as raised:
            self.add()
        self.assertEqual(raised.exception.details["reason"], "setup")
        self.assertEqual(HYPERVISORS, {})

    def test_a_cluster_registered_already_is_refused(self):
        first = self.add()
        with self.assertRaises(abr_v41.ApiError) as raised:
            self.add(name="PVE-Again", address="pve2.example.com")
        self.assertEqual((raised.exception.status_code, raised.exception.code), (409, "inventory_already_registered"))
        self.assertIn(
            "Proxmox VE cluster 'cluster1' is already registered as inventory 'PVE-Cluster'",
            raised.exception.message,
        )
        self.assertEqual(raised.exception.details["existing_id"], first["id"])

    def test_a_single_node_registered_already_is_refused_by_its_name(self):
        self.set_up.cluster = None
        first = self.add()
        HYPERVISORS[first["id"]]["metadata"]["cluster_name"] = None
        with self.assertRaises(abr_v41.ApiError) as raised:
            self.add(name="PVE-Again", address="pve2.example.com")
        self.assertIn("Proxmox VE node 'node1' is already registered", raised.exception.message)

    def test_a_hyperv_cluster_of_the_same_name_is_another_cluster(self):
        HYPERVISORS["hv"] = {
            "id": "hv",
            "name": "HV",
            "type": "hyper-v",
            "kind": "hyper-v",
            "metadata": {"cluster_name": "cluster1"},
        }
        self.assertEqual(self.add()["status"], "available")

    def test_editing_with_the_password_sets_the_cluster_up_again_with_the_same_key(self):
        created = self.add()
        edited = abr_v41.edit_inventory_record(created["id"], {"password": "root-secret"})
        stored = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual(
            (edited["status"], stored["token"], stored["credentials"]["password"]), ("available", "token-2", None)
        )
        self.assertEqual(self.set_up.calls[1]["previous"]["ssh_key"], "PRIVATE KEY")

    def test_editing_without_the_password_checks_the_token_and_key(self):
        created = self.add()
        with patch.object(
            pve_inventory, "test_connection", return_value={"cluster": "cluster1", "computer": "node1"}
        ) as checked:
            edited = abr_v41.edit_inventory_record(created["id"], {"port": 8007})
        self.assertEqual((edited["status"], len(self.set_up.calls), checked.call_count), ("available", 1, 1))
        self.assertEqual(HYPERVISORS[created["id"]]["metadata"]["port"], 8007)

    def test_removing_takes_the_token_and_key_back_from_the_first_node_that_answers(self):
        created = self.add()
        reached: list[str] = []

        class Shell:
            def __init__(self, address, **options):
                self.address = address

            def __enter__(self):
                reached.append(self.address)
                if self.address == "pve1.example.com":
                    raise SshError("SSH to pve1.example.com:22 cannot be reached")
                return self

            def __exit__(self, *exc_info):
                pass

        with patch.object(pve_inventory, "NodeShell", Shell), patch.object(
            pve_inventory, "tear_down", return_value=["API token"]
        ) as torn:
            removed = abr_v41.remove_inventory_record(created["id"])
        self.assertEqual(removed, {"id": created["id"], "removed": True})
        self.assertEqual(reached, ["pve1.example.com", "pve2.example.com"])
        self.assertEqual(torn.call_args.args[1], created["id"])

    def test_removing_while_no_node_answers_says_what_to_remove_by_hand(self):
        created = self.add()
        with patch.object(pve_inventory, "NodeShell", side_effect=SshError("unreachable")):
            with self.assertLogs("python.srv.plugins.pve.inventory", "WARNING"):
                removed = abr_v41.remove_inventory_record(created["id"])
        self.assertTrue(removed["removed"])
        self.assertRegex(
            removed["warnings"][0], r"Remove the token abr@pve!altastor-\w+ and the root SSH key marked .* by hand"
        )
        self.assertEqual(HYPERVISORS, {})

    def test_the_plugin_of_a_proxmox_inventory_is_a_pve_node(self):
        created = self.add()
        node = build_hypervisor_plugin(HYPERVISORS[created["id"]])
        self.assertIsInstance(node, PveNode)
        self.assertEqual(node.access.api_addresses, ["pve1.example.com", "pve2.example.com"])


class FakeNode:
    cluster_name = "cluster1"

    def __init__(self, config_read: bool = True) -> None:
        self.config_read = config_read

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def list_hosts(self):
        return [
            {
                "name": "node1",
                "address": "pve1.example.com",
                "version": "9.2.26",
                "datastores": ["local-lvm"],
                "datastore_capacity": {},
                "networks": ["vmbr0"],
                "status": "connected",
            }
        ]

    def get_inventory(self):
        vm = {
            "vm_id": "9002",
            "name": "web",
            "power_state": "running",
            "num_cpu": 2,
            "memory_mb": 2048,
            "guest_os": "Linux",
            "host": "node1",
            "config_read": self.config_read,
        }
        disks = (
            [
                {
                    "external_id": "9002:scsi0",
                    "name": "scsi0",
                    "vm_id": "9002",
                    "capacity_gb": 32.0,
                    "datastore_path": "local-lvm:vm-9002-disk-0",
                }
            ]
            if self.config_read
            else []
        )
        return {"virtual_machines": [vm], "disks": disks, "network_adapters": []}


class DiscoveryTests(unittest.TestCase):
    RECORD = {
        "id": "inv-1",
        "name": "PVE-Cluster",
        "kind": "proxmox-ve",
        "endpoint": "pve1.example.com",
        "metadata": {"host": "pve1.example.com"},
    }

    def setUp(self):
        RESOURCES_BY_HV.clear()
        self.addCleanup(RESOURCES_BY_HV.clear)

    def resources(self, node: FakeNode) -> dict:
        with patch.object(PveNode, "from_record", return_value=node):
            items = discovery.build_pve_resources(self.RECORD)
        return {(item["resource_type"], item["external_id"]): item for item in items}

    def test_discovery_lists_the_cluster_its_nodes_and_vms_with_their_disks(self):
        found = self.resources(FakeNode())
        self.assertEqual(
            sorted(found),
            [
                ("cluster", "cluster1"),
                ("disk", "9002:scsi0"),
                ("host", "node1"),
                ("hypervisor", "inv-1"),
                ("virtual_machine", "9002"),
            ],
        )
        vm = found[("virtual_machine", "9002")]["payload"]
        self.assertEqual((vm["host_name"], vm["cluster_name"], vm["memory_mb"]), ("node1", "cluster1", 2048))
        self.assertEqual(found[("hypervisor", "inv-1")]["payload"]["inventory"], "proxmox-ve")

    def test_a_vm_whose_configuration_cannot_be_read_keeps_the_disks_found_last_time(self):
        RESOURCES_BY_HV["inv-1"] = {
            ("disk", "9002:scsi0"): {
                "resource_type": "disk",
                "external_id": "9002:scsi0",
                "name": "scsi0",
                "payload": {"vm_id": "9002"},
            },
            ("disk", "9100:scsi0"): {
                "resource_type": "disk",
                "external_id": "9100:scsi0",
                "name": "scsi0",
                "payload": {"vm_id": "9100"},
            },
        }
        found = self.resources(FakeNode(config_read=False))
        self.assertIn(("disk", "9002:scsi0"), found)
        self.assertNotIn(("disk", "9100:scsi0"), found)

    def test_the_inventory_provider_rescans_through_this_discovery(self):
        provider = inventory_management.InventoryProviderRegistry.resolve({"type": "proxmox-ve"})
        with patch.object(inventory_management, "build_pve_resources", return_value=["resources"]) as built:
            self.assertEqual(provider.rescan(self.RECORD), ["resources"])
        built.assert_called_once_with(self.RECORD)


if __name__ == "__main__":
    unittest.main()
