# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import RESOURCES_BY_HV
from python.srv.plugins.hyperv import HyperVNode
from python.srv.services.discovery import build_hyperv_resources


HYPERV_HOST = "192.0.2.10"
HYPERV_CLUSTER_HOST = "192.0.2.11"
HYPERV_USERNAME = "EXAMPLE\\Administrator"
HYPERV_PASSWORD = "test-password"
HYPERV_NODE_A = "node-a"
HYPERV_NODE_B = "node-b"
HYPERV_CLUSTER_NAME = "node-cluster"
HYPERV_DNS_SUFFIX = "example.test"

INVENTORY_RECORD = {
    "id": "inv-1",
    "name": "HyperV-test",
    "kind": "hyper-v",
    "endpoint": HYPERV_HOST,
    "metadata": {
        "host": HYPERV_HOST,
        "port": 5986,
        "port_explicit": False,
        "verify_ssl": False,
        "credentials": {"username": HYPERV_USERNAME, "password": HYPERV_PASSWORD},
    },
}

HOSTS = [
    {
        "name": HYPERV_NODE_A.upper(),
        "dns_name": f"{HYPERV_NODE_A}.{HYPERV_DNS_SUFFIX}",
        "cluster": HYPERV_CLUSTER_NAME,
        "datacenter": None,
        "version": "Microsoft Windows Server 2025 Datacenter",
        "datastores": ["C:", "C:\\ClusterStorage\\Volume1"],
        "datastore_capacity": {
            "C:": {"capacity_bytes": 106295193600, "free_bytes": 70866038784, "shared": False},
            "C:\\ClusterStorage\\Volume1": {
                "capacity_bytes": 1099511627776,
                "free_bytes": 1000000000000,
                "shared": True,
            },
        },
        "networks": ["Management", "Production"],
    },
    {
        "name": HYPERV_NODE_B.upper(),
        "dns_name": f"{HYPERV_NODE_B}.{HYPERV_DNS_SUFFIX}",
        "cluster": HYPERV_CLUSTER_NAME,
        "datacenter": None,
        "version": "Microsoft Windows Server 2025 Datacenter",
        "datastores": ["C:"],
        "datastore_capacity": {
            "C:": {"capacity_bytes": 106295193600, "free_bytes": 89970679808, "shared": False}
        },
        "networks": ["Management"],
    },
]

STANDALONE_VM = {
    "vm_id": "bcf894b0-0074-4462-a469-0aaabf19a4c0",
    "name": HYPERV_NODE_A,
    "power_state": "Off",
    "num_cpu": 2,
    "memory_mb": 2048,
    "guest_os": "",
    "firmware": "efi",
    "generation": 2,
    "version": "12.0",
    "checkpoints": 2,
    "config_path": "C:\\ProgramData\\Microsoft\\Windows\\Hyper-V",
    "checkpoint_path": "C:\\ProgramData\\Microsoft\\Windows\\Hyper-V",
    "host": HYPERV_NODE_A,
    "clustered": False,
    "owner_node": HYPERV_NODE_A,
}

CLUSTERED_VM = {
    "vm_id": "f6b63a36-f5e6-4697-9c94-c118ffb277c8",
    "name": f"{HYPERV_CLUSTER_NAME}-vm",
    "power_state": "Running",
    "num_cpu": 4,
    "memory_mb": 4096,
    "guest_os": "Windows Server 2025",
    "firmware": "efi",
    "generation": 2,
    "version": "12.0",
    "checkpoints": 0,
    "config_path": f"C:\\ClusterStorage\\Volume1\\{HYPERV_CLUSTER_NAME}-vm",
    "checkpoint_path": f"C:\\ClusterStorage\\Volume1\\{HYPERV_CLUSTER_NAME}-vm",
    "host": HYPERV_NODE_A,
    "clustered": True,
    "owner_node": HYPERV_NODE_B,
}

DISKS = [
    {
        "external_id": f"{STANDALONE_VM['vm_id']}:C:\\Hyper-V\\node-a_AA7.avhdx",
        "name": "node-a_AA7.avhdx",
        "vm_id": STANDALONE_VM["vm_id"],
        "capacity_gb": 10.0,
        "datastore_path": "C:\\Hyper-V\\node-a_AA7.avhdx",
        "uuid": "821007B8-FFD3-4306-B69E-63FAEDAF585D",
        "source_disk_type_code": "VHDX",
        "source_disk_type_name": "Differencing",
        "source_disk_eager_zeroed": False,
        "controller": "SCSI0:0",
        "file_bytes": 300941312,
        "parent_path": "C:\\Hyper-V\\node-a_43B.avhdx",
    },
    {
        "external_id": f"{CLUSTERED_VM['vm_id']}:C:\\ClusterStorage\\Volume1\\disk1.vhdx",
        "name": "disk1.vhdx",
        "vm_id": CLUSTERED_VM["vm_id"],
        "capacity_gb": 2.0,
        "datastore_path": "C:\\ClusterStorage\\Volume1\\disk1.vhdx",
        "uuid": "758E41A8-BB77-4718-A9B2-3BB5C42BD4B7",
        "source_disk_type_code": "VHDX",
        "source_disk_type_name": "Dynamic",
        "source_disk_eager_zeroed": False,
        "controller": "SCSI0:0",
        "file_bytes": 4194304,
        "parent_path": "",
    },
    {
        "external_id": f"{CLUSTERED_VM['vm_id']}:C:\\ClusterStorage\\Volume1\\disk2.vhdx",
        "name": "disk2.vhdx",
        "vm_id": CLUSTERED_VM["vm_id"],
        "capacity_gb": 3.0,
        "datastore_path": "C:\\ClusterStorage\\Volume1\\disk2.vhdx",
        "uuid": "9E1C0B2A-0000-4000-8000-000000000002",
        "source_disk_type_code": "VHDX",
        "source_disk_type_name": "Fixed",
        "source_disk_eager_zeroed": True,
        "controller": "SCSI0:1",
        "file_bytes": 3221225472,
        "parent_path": "",
    },
    {
        "external_id": "ghost-vm:C:\\Hyper-V\\orphan.vhdx",
        "name": "orphan.vhdx",
        "vm_id": "ghost-vm",
        "capacity_gb": 1.0,
        "datastore_path": "C:\\Hyper-V\\orphan.vhdx",
        "uuid": "",
        "source_disk_type_code": "VHDX",
        "source_disk_type_name": "Dynamic",
        "source_disk_eager_zeroed": False,
        "controller": "SCSI0:0",
        "file_bytes": 0,
        "parent_path": "",
    },
]

NICS = [
    {
        "external_id": f"{STANDALONE_VM['vm_id']}:nic-1",
        "name": "Network Adapter",
        "vm_id": STANDALONE_VM["vm_id"],
        "mac": "00155D0C1C00",
        "network": "Management",
        "connected": True,
        "dynamic_mac": True,
    },
    {
        "external_id": f"{CLUSTERED_VM['vm_id']}:nic-1",
        "name": "Network Adapter",
        "vm_id": CLUSTERED_VM["vm_id"],
        "mac": "00155D0C1C01",
        "network": "Management",
        "connected": True,
        "dynamic_mac": True,
    },
    {
        "external_id": f"{CLUSTERED_VM['vm_id']}:nic-2",
        "name": "second",
        "vm_id": CLUSTERED_VM["vm_id"],
        "mac": "00155D0C1C02",
        "network": "Production",
        "connected": True,
        "dynamic_mac": False,
    },
    {
        "external_id": f"{CLUSTERED_VM['vm_id']}:nic-3",
        "name": "disconnected",
        "vm_id": CLUSTERED_VM["vm_id"],
        "mac": "000000000000",
        "network": "",
        "connected": False,
        "dynamic_mac": True,
    },
]


class FakeHyperVNode:
    def __init__(self, host, user, password, port=None, verify_ssl=False) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl
        self.cluster_name: str | None = HYPERV_CLUSTER_NAME
        self.hosts = [dict(item) for item in HOSTS]
        self.inventory = {
            "virtual_machines": [dict(STANDALONE_VM), dict(CLUSTERED_VM)],
            "disks": [dict(item) for item in DISKS if item["vm_id"] != "ghost-vm"],
            "network_adapters": [dict(item) for item in NICS],
        }

    def __enter__(self) -> "FakeHyperVNode":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def get_inventory(self) -> dict:
        return self.inventory

    def list_hosts(self) -> list:
        return self.hosts


class HyperVDiscoveryTests(unittest.TestCase):
    def _build(self, node: FakeHyperVNode | None = None, record: dict | None = None) -> list[dict]:
        built = node or FakeHyperVNode(HYPERV_HOST, "u", "p")
        with patch("python.srv.plugins.hyperv.HyperVNode", return_value=built):
            return build_hyperv_resources(record or dict(INVENTORY_RECORD))

    @staticmethod
    def _of_type(resources: list[dict], resource_type: str) -> list[dict]:
        return [item for item in resources if item["resource_type"] == resource_type]

    @staticmethod
    def _by_name(resources: list[dict], resource_type: str, name: str) -> dict:
        return next(
            item
            for item in resources
            if item["resource_type"] == resource_type and item["name"] == name
        )

    def test_every_kind_of_object_reaches_the_inventory(self) -> None:
        resources = self._build()

        counts = {
            kind: len(self._of_type(resources, kind))
            for kind in ("hypervisor", "cluster", "host", "virtual_machine", "disk", "network_adapter")
        }
        self.assertEqual(
            counts,
            {
                "hypervisor": 1,
                "cluster": 1,
                "host": 2,
                "virtual_machine": 2,
                "disk": 3,
                "network_adapter": 4,
            },
        )

    def test_a_vm_is_identified_by_its_hyperv_guid(self) -> None:
        resources = self._build()

        vm = self._by_name(resources, "virtual_machine", HYPERV_NODE_A)
        self.assertEqual(vm["external_id"], "bcf894b0-0074-4462-a469-0aaabf19a4c0")

    def test_a_vm_names_the_host_it_runs_on(self) -> None:
        resources = self._build()

        self.assertEqual(
            self._by_name(resources, "virtual_machine", HYPERV_NODE_A)["payload"]["host_name"],
            HYPERV_NODE_A,
        )
        self.assertEqual(
            [item["external_id"] for item in self._of_type(resources, "host")],
            [HYPERV_NODE_A.upper(), HYPERV_NODE_B.upper()],
        )

    def test_a_clustered_vm_is_filed_under_its_owner_node_and_its_cluster(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "virtual_machine", f"{HYPERV_CLUSTER_NAME}-vm")["payload"]
        self.assertEqual(payload["host_name"], HYPERV_NODE_B)
        self.assertTrue(payload["clustered"])
        self.assertEqual(payload["cluster_name"], HYPERV_CLUSTER_NAME)
        self.assertEqual(payload["cluster_external_id"], HYPERV_CLUSTER_NAME)

    def test_a_vm_that_is_not_a_cluster_role_carries_no_cluster(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "virtual_machine", HYPERV_NODE_A)["payload"]
        self.assertFalse(payload["clustered"])
        self.assertIsNone(payload["cluster_name"])

    def test_the_fields_a_backup_needs_are_carried_over(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "virtual_machine", HYPERV_NODE_A)["payload"]
        self.assertEqual(payload["power_state"], "Off")
        self.assertEqual(payload["num_cpu"], 2)
        self.assertEqual(payload["memory_mb"], 2048)
        self.assertEqual(payload["generation"], 2)
        self.assertEqual(payload["firmware"], "efi")
        self.assertEqual(payload["version"], "12.0")
        self.assertEqual(payload["checkpoints"], 2)
        self.assertEqual(payload["config_path"], "C:\\ProgramData\\Microsoft\\Windows\\Hyper-V")
        self.assertEqual(payload["checkpoint_path"], "C:\\ProgramData\\Microsoft\\Windows\\Hyper-V")

    def test_a_vm_with_several_disks_keeps_each_one_with_its_controller_slot(self) -> None:
        resources = self._build()

        disks = [
            item
            for item in self._of_type(resources, "disk")
            if item["payload"]["vm_id"] == CLUSTERED_VM["vm_id"]
        ]
        self.assertEqual(
            sorted((item["name"], item["payload"]["controller"]) for item in disks),
            [("disk1.vhdx", "SCSI0:0"), ("disk2.vhdx", "SCSI0:1")],
        )
        self.assertEqual(len({item["external_id"] for item in disks}), 2)

    def test_a_disk_of_a_checkpointed_vm_names_its_parent(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "disk", "node-a_AA7.avhdx")["payload"]
        self.assertEqual(payload["source_disk_type_name"], "Differencing")
        self.assertEqual(payload["parent_path"], "C:\\Hyper-V\\node-a_43B.avhdx")
        self.assertEqual(payload["capacity_gb"], 10.0)
        self.assertEqual(payload["file_bytes"], 300941312)

    def test_a_fixed_disk_is_reported_as_preallocated(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "disk", "disk2.vhdx")["payload"]
        self.assertEqual(payload["source_disk_type_name"], "Fixed")
        self.assertTrue(payload["source_disk_eager_zeroed"])

    def test_a_vm_with_several_adapters_keeps_each_one(self) -> None:
        resources = self._build()

        nics = [
            item
            for item in self._of_type(resources, "network_adapter")
            if item["payload"]["vm_id"] == CLUSTERED_VM["vm_id"]
        ]
        self.assertEqual(len(nics), 3)
        self.assertEqual(len({item["external_id"] for item in nics}), 3)
        self.assertEqual(
            sorted((item["payload"]["mac"], item["payload"]["network"]) for item in nics),
            [
                ("000000000000", ""),
                ("00155D0C1C01", "Management"),
                ("00155D0C1C02", "Production"),
            ],
        )

    def test_an_adapter_says_whether_its_mac_is_assigned_by_the_host(self) -> None:
        resources = self._build()

        self.assertTrue(self._by_name(resources, "network_adapter", "Network Adapter")["payload"]["dynamic_mac"])
        self.assertFalse(self._by_name(resources, "network_adapter", "second")["payload"]["dynamic_mac"])

    def test_a_disconnected_adapter_is_still_discovered(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "network_adapter", "disconnected")["payload"]
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["network"], "")

    def test_the_networks_and_stores_of_a_host_are_reported(self) -> None:
        resources = self._build()

        payload = self._by_name(resources, "host", HYPERV_NODE_A.upper())["payload"]
        self.assertEqual(payload["networks"], ["Management", "Production"])
        self.assertEqual(payload["datastores"], ["C:", "C:\\ClusterStorage\\Volume1"])
        self.assertTrue(payload["datastore_capacity"]["C:\\ClusterStorage\\Volume1"]["shared"])
        self.assertEqual(payload["cluster_name"], HYPERV_CLUSTER_NAME)
        self.assertEqual(payload["dns_name"], f"{HYPERV_NODE_A}.{HYPERV_DNS_SUFFIX}")

    def test_a_standalone_host_produces_no_cluster_object(self) -> None:
        node = FakeHyperVNode(HYPERV_CLUSTER_HOST, "u", "p")
        node.cluster_name = None
        node.hosts = [dict(HOSTS[0], cluster=None)]
        node.inventory = {
            "virtual_machines": [dict(STANDALONE_VM)],
            "disks": [dict(DISKS[0])],
            "network_adapters": [dict(NICS[0])],
        }

        resources = self._build(node)

        self.assertEqual(self._of_type(resources, "cluster"), [])
        self.assertIsNone(self._of_type(resources, "hypervisor")[0]["payload"]["cluster_name"])

    def test_a_registration_without_credentials_is_refused_before_connecting(self) -> None:
        record = dict(INVENTORY_RECORD)
        record["metadata"] = dict(record["metadata"], credentials={})

        with self.assertRaises(ValueError):
            self._build(record=record)


class TheLastKnownGuestOsIsRememberedTests(unittest.TestCase):
    def setUp(self) -> None:
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        RESOURCES_BY_HV.clear()

    @staticmethod
    def _scan(guest_os: str) -> list[dict]:
        node = FakeHyperVNode(HYPERV_HOST, "u", "p")
        node.inventory = {
            "virtual_machines": [dict(STANDALONE_VM, guest_os=guest_os)],
            "disks": [],
            "network_adapters": [],
        }
        with patch("python.srv.plugins.hyperv.HyperVNode", return_value=node):
            return build_hyperv_resources(dict(INVENTORY_RECORD))

    @staticmethod
    def _guest_os(resources: list[dict]) -> str:
        vm = next(item for item in resources if item["resource_type"] == "virtual_machine")
        return vm["payload"]["guest_os"]

    @staticmethod
    def _remember(resources: list[dict]) -> None:
        RESOURCES_BY_HV[INVENTORY_RECORD["id"]] = {
            (item["resource_type"], item["external_id"]): item for item in resources
        }

    def test_a_running_guest_reports_its_os(self) -> None:
        self.assertEqual(self._guest_os(self._scan("Debian GNU/Linux")), "Debian GNU/Linux")

    def test_a_vm_that_was_never_seen_running_reports_nothing(self) -> None:
        self.assertEqual(self._guest_os(self._scan("")), "")

    def test_powering_a_vm_off_does_not_lose_the_os_it_reported(self) -> None:
        self._remember(self._scan("Debian GNU/Linux"))

        self.assertEqual(self._guest_os(self._scan("")), "Debian GNU/Linux")

    def test_a_guest_that_reports_a_different_os_replaces_the_remembered_one(self) -> None:
        self._remember(self._scan("Debian GNU/Linux"))

        self.assertEqual(
            self._guest_os(self._scan("Windows Server 2025 Datacenter")),
            "Windows Server 2025 Datacenter",
        )

    def test_what_another_inventory_remembers_is_not_borrowed(self) -> None:
        remembered = self._scan("Debian GNU/Linux")
        RESOURCES_BY_HV["another-inventory"] = {
            (item["resource_type"], item["external_id"]): item for item in remembered
        }

        self.assertEqual(self._guest_os(self._scan("")), "")


def _snapshot(node_name: str, vms: list[dict], disks: list[dict], nics: list[dict]) -> dict:
    return {
        "host": dict(HOSTS[0], name=node_name),
        "virtual_machines": vms,
        "disks": disks,
        "network_adapters": nics,
    }


class HyperVNodeInventoryTests(unittest.TestCase):
    @staticmethod
    def _node(snapshots: dict[str, dict]) -> HyperVNode:
        node = HyperVNode(HYPERV_HOST, "u", "p")
        node._nodes = [name for name in snapshots]
        node._snapshots = {name.lower(): snapshot for name, snapshot in snapshots.items()}
        return node

    def test_a_disk_of_a_vm_that_is_no_longer_there_is_dropped(self) -> None:
        node = self._node(
            {HYPERV_NODE_A: _snapshot(HYPERV_NODE_A.upper(), [dict(STANDALONE_VM)], [dict(item) for item in DISKS], [])}
        )

        inventory = node.get_inventory()

        self.assertNotIn("orphan.vhdx", [item["name"] for item in inventory["disks"]])
        self.assertEqual([item["name"] for item in inventory["disks"]], ["node-a_AA7.avhdx"])

    def test_an_adapter_of_a_vm_that_is_no_longer_there_is_dropped(self) -> None:
        stray = dict(NICS[0], external_id="ghost:nic", vm_id="ghost-vm", name="stray")
        node = self._node(
            {HYPERV_NODE_A: _snapshot(HYPERV_NODE_A.upper(), [dict(STANDALONE_VM)], [], [dict(NICS[0]), stray])}
        )

        inventory = node.get_inventory()

        self.assertEqual([item["name"] for item in inventory["network_adapters"]], ["Network Adapter"])

    def test_a_vm_seen_on_two_nodes_at_once_is_reported_once(self) -> None:
        node = self._node(
            {
                HYPERV_NODE_A: _snapshot(HYPERV_NODE_A.upper(), [dict(CLUSTERED_VM)], [dict(DISKS[1])], [dict(NICS[1])]),
                HYPERV_NODE_B: _snapshot(HYPERV_NODE_B.upper(), [dict(CLUSTERED_VM)], [dict(DISKS[1])], [dict(NICS[1])]),
            }
        )

        inventory = node.get_inventory()

        self.assertEqual([item["vm_id"] for item in inventory["virtual_machines"]], [CLUSTERED_VM["vm_id"]])
        self.assertEqual(len(inventory["disks"]), 1)
        self.assertEqual(len(inventory["network_adapters"]), 1)

    def test_the_vms_of_every_node_are_gathered(self) -> None:
        node = self._node(
            {
                HYPERV_NODE_A: _snapshot(HYPERV_NODE_A.upper(), [dict(STANDALONE_VM)], [], []),
                HYPERV_NODE_B: _snapshot(HYPERV_NODE_B.upper(), [dict(CLUSTERED_VM)], [], []),
            }
        )

        inventory = node.get_inventory()

        self.assertEqual(
            sorted(item["name"] for item in inventory["virtual_machines"]),
            sorted([f"{HYPERV_CLUSTER_NAME}-vm", HYPERV_NODE_A]),
        )


if __name__ == "__main__":
    unittest.main()
