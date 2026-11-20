# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

import httpx

from python.srv.plugins.pve.pve import (
    PveAccess,
    PveError,
    PveNode,
    access_from_record,
    parse_property_string,
    parse_size,
    vm_disks,
    vm_network_adapters,
)
from python.tests.srv.pve.fake_api import FakePve

# The test cluster's VM 9002, with a CD-ROM, an empty drive, a disk left out of backups and an unused disk added.
CONFIG_9002 = {
    "name": "uefi-vm",
    "bios": "ovmf",
    "machine": "q35",
    "ostype": "l26",
    "digest": "2365dec717aef5b6",
    "efidisk0": "local-lvm:vm-9002-disk-0,efitype=4m,pre-enrolled-keys=1,size=528K",
    "scsi0": "local-lvm:vm-9002-disk-1,iothread=1,size=5G",
    "scsi1": "local-dir:9002/vm-9002-disk-0.qcow2,size=4G",
    "scsi10": "/dev/disk/by-id/ata-SSD-1,backup=0,size=100G",
    "virtio0": "local-lvm:vm-9002-disk-3,size=1G",
    "tpmstate0": "local-lvm:vm-9002-disk-2,size=4M,version=v2.0",
    "ide2": "local:iso/debian-13.iso,media=cdrom,size=700M",
    "sata0": "none,media=cdrom",
    "unused0": "local-lvm:vm-9002-disk-9",
    "net0": "virtio=BC:24:11:3A:42:7E,bridge=vmbr0,link_down=1",
    "net1": "e1000=bc:24:11:00:00:01,bridge=internal,tag=20,firewall=1",
}


class ConfigurationTests(unittest.TestCase):
    def test_a_property_string_has_a_value_without_a_key(self):
        self.assertEqual(
            parse_property_string("local-dir:9002/vm-9002-disk-0.qcow2,size=4G", "file"),
            {"file": "local-dir:9002/vm-9002-disk-0.qcow2", "size": "4G"},
        )
        self.assertEqual(
            parse_property_string("virtio=BC:24:11:3A:42:7E,bridge=vmbr0"),
            {"virtio": "BC:24:11:3A:42:7E", "bridge": "vmbr0"},
        )

    def test_sizes_are_binary_units(self):
        self.assertEqual(
            [parse_size(text) for text in ("528K", "4M", "5G", "1T", "1.5G", "512", "", None, "big")],
            [528 << 10, 4 << 20, 5 << 30, 1 << 40, 3 << 29, 512, 0, 0, 0],
        )

    def test_the_disks_are_what_vzdump_reads_in_drive_order(self):
        disks = vm_disks("9002", CONFIG_9002)
        self.assertEqual(
            [disk["name"] for disk in disks], ["scsi0", "scsi1", "scsi10", "virtio0", "efidisk0", "tpmstate0"]
        )
        by_name = {disk["name"]: disk for disk in disks}
        self.assertEqual(
            {
                key: by_name["scsi1"][key]
                for key in (
                    "external_id",
                    "drive_id",
                    "datastore_path",
                    "storage",
                    "format",
                    "capacity_bytes",
                    "backup",
                )
            },
            {
                "external_id": "9002:scsi1",
                "drive_id": "drive-scsi1",
                "datastore_path": "local-dir:9002/vm-9002-disk-0.qcow2",
                "storage": "local-dir",
                "format": "qcow2",
                "capacity_bytes": 4 << 30,
                "backup": True,
            },
        )
        self.assertEqual((by_name["efidisk0"]["capacity_bytes"], by_name["efidisk0"]["format"]), (528 << 10, "raw"))
        self.assertEqual((by_name["scsi10"]["storage"], by_name["scsi10"]["backup"]), ("", False))

    def test_network_adapters_keep_model_mac_bridge_vlan_and_link(self):
        adapters = vm_network_adapters("9002", CONFIG_9002)
        self.assertEqual(
            [
                (nic["name"], nic["model"], nic["mac"], nic["network"], nic["vlan"], nic["connected"], nic["firewall"])
                for nic in adapters
            ],
            [
                ("net0", "virtio", "BC:24:11:3A:42:7E", "vmbr0", None, False, False),
                ("net1", "e1000", "BC:24:11:00:00:01", "internal", 20, True, True),
            ],
        )


def _record(**pve) -> dict:
    return {
        "id": "inv-1",
        "kind": "proxmox-ve",
        "metadata": {
            "host": "pve1.example.com",
            "port": 8006,
            "token": "secret",
            "cluster_name": "cluster1",
            "pve": {
                "token_id": "abr@pve!altastor-inv1",
                "ssh_key": "key",
                "certificates": ["CA"],
                "nodes": [
                    {"name": "node1", "address": "pve1.example.com", "host_keys": ["ssh-ed25519 AAAA1"]},
                    {"name": "node2", "address": "pve2.example.com", "host_keys": ["ssh-ed25519 AAAA2"]},
                ],
                **pve,
            },
        },
    }


class AccessTests(unittest.TestCase):
    def test_the_api_tries_the_given_address_then_every_node(self):
        access = access_from_record(_record())
        self.assertEqual(access.api_addresses, ["pve1.example.com", "pve2.example.com"])
        self.assertEqual(access.node("NODE2")["address"], "pve2.example.com")

    def test_a_node_abr_core_has_not_set_up_is_named_in_the_error(self):
        with self.assertRaisesRegex(PveError, "Node node3 is not one AltaStor has set up"):
            access_from_record(_record()).node("node3")

    def test_an_inventory_without_token_or_key_has_to_be_set_up_again(self):
        record = _record()
        del record["metadata"]["pve"]["ssh_key"]
        with self.assertRaisesRegex(PveError, "has not been set up.*inventory edit with the root password"):
            access_from_record(record)


def _cluster(node2_online: bool = True) -> FakePve:
    node2 = "online" if node2_online else "offline"
    resources = [
        {"type": "node", "node": "node1", "status": "online", "maxcpu": 8, "maxmem": 16 << 30},
        {"type": "node", "node": "node2", "status": node2, "maxcpu": 6, "maxmem": 8 << 30},
        {
            "type": "storage",
            "node": "node1",
            "storage": "local-lvm",
            "content": "images,rootdir",
            "plugintype": "lvmthin",
            "maxdisk": 100 << 30,
            "disk": 30 << 30,
            "shared": 0,
            "status": "available",
        },
        {
            "type": "storage",
            "node": "node1",
            "storage": "local",
            "content": "iso,vztmpl,backup",
            "plugintype": "dir",
            "maxdisk": 50 << 30,
            "disk": 1 << 30,
            "shared": 0,
            "status": "available",
        },
        {
            "type": "storage",
            "node": "node1",
            "storage": "abr",
            "content": "backup",
            "plugintype": "abr",
            "status": "available",
        },
        {
            "type": "storage",
            "node": "node2",
            "storage": "local-zfs",
            "content": "images",
            "plugintype": "zfspool",
            "maxdisk": 200 << 30,
            "disk": 50 << 30,
            "shared": 0,
            "status": "available",
        },
        {
            "type": "qemu",
            "vmid": 9004,
            "name": "mig-vm",
            "node": "node2",
            "status": "running",
            "maxcpu": 1,
            "maxmem": 256 << 20,
        },
        {
            "type": "qemu",
            "vmid": 9002,
            "name": "uefi-vm",
            "node": "node1",
            "status": "running",
            "maxcpu": 1,
            "maxmem": 512 << 20,
            "tags": "prod;db",
        },
        {"type": "lxc", "vmid": 300, "name": "ct", "node": "node1", "status": "running"},
    ]
    status = [
        {"type": "cluster", "name": "cluster1", "nodes": 2, "quorate": 1},
        {"type": "node", "name": "node1", "ip": "pve1.example.com", "online": 1, "local": 1},
        {"type": "node", "name": "node2", "ip": "pve2.example.com", "online": int(node2_online), "local": 0},
    ]
    routes = {
        ("GET", "/cluster/status"): {"data": status},
        ("GET", "/cluster/resources"): {"data": resources},
        ("GET", "/nodes/node1/version"): {"data": {"version": "9.2.26"}},
        ("GET", "/nodes/node2/version"): {"data": {"version": "9.2.26"}},
        ("GET", "/nodes/node1/network"): {"data": [{"iface": "vmbr1"}, {"iface": "vmbr0"}]},
        ("GET", "/nodes/node2/network"): {"data": [{"iface": "vmbr0"}]},
        ("GET", "/nodes/node1/qemu/9002/config"): {"data": CONFIG_9002},
        ("GET", "/nodes/node2/qemu/9004/config"): (
            {
                "data": {
                    "name": "mig-vm",
                    "scsi0": "local-zfs:vm-9004-disk-0,size=2G",
                    "net0": "virtio=BC:24:11:00:00:04,bridge=vmbr0",
                }
            }
            if node2_online
            else httpx.Response(595, json={"data": None}, extensions={"reason_phrase": b"No route to host"})
        ),
    }
    return FakePve(routes)


class NodeTests(unittest.TestCase):
    def node(self, fake: FakePve) -> PveNode:
        node = PveNode(PveAccess(host="pve1", token_id="abr@pve!abr", token_secret="s", ssh_key="k"), api=fake.api())
        self.addCleanup(node.disconnect)
        return node

    def test_the_identity_is_the_cluster_and_its_nodes(self):
        node = self.node(_cluster())
        self.assertEqual(
            node.identity(),
            {"cluster": "cluster1", "computer": "node1", "nodes": ["node1", "node2"]},
        )
        self.assertEqual(node.get_service_type(), "cluster")

    def test_hosts_offer_the_storages_that_hold_vm_disks(self):
        hosts = self.node(_cluster()).list_hosts()
        first = hosts[0]
        self.assertEqual(
            (first["name"], first["address"], first["version"], first["status"]),
            ("node1", "pve1.example.com", "9.2.26", "connected"),
        )
        self.assertEqual(first["datastores"], ["local-lvm"])
        self.assertEqual(
            first["datastore_capacity"]["local-lvm"],
            {
                "capacity_bytes": 100 << 30,
                "free_bytes": 70 << 30,
                "shared": False,
                "type": "lvmthin",
                "content": ["images", "rootdir"],
                "active": True,
            },
        )
        self.assertEqual(first["networks"], ["vmbr0", "vmbr1"])
        self.assertTrue(first["backup_storage_active"])
        self.assertFalse(hosts[1]["backup_storage_active"])

    def test_a_node_that_is_down_is_listed_as_unavailable_without_asking_it(self):
        fake = _cluster(node2_online=False)
        hosts = self.node(fake).list_hosts()
        self.assertEqual((hosts[1]["status"], hosts[1]["version"], hosts[1]["networks"]), ("unavailable", "", []))
        self.assertNotIn("/api2/json/nodes/node2/version", [request.url.path for request in fake.requests])

    def test_the_inventory_has_the_vms_with_their_disks_and_adapters(self):
        inventory = self.node(_cluster()).get_inventory()
        vms = inventory["virtual_machines"]
        self.assertEqual([vm["vm_id"] for vm in vms], ["9002", "9004"])
        self.assertEqual(
            {
                key: vms[0][key]
                for key in (
                    "name",
                    "power_state",
                    "num_cpu",
                    "memory_mb",
                    "firmware",
                    "guest_os",
                    "host",
                    "tags",
                    "config_digest",
                    "config_read",
                )
            },
            {
                "name": "uefi-vm",
                "power_state": "running",
                "num_cpu": 1,
                "memory_mb": 512,
                "firmware": "efi",
                "guest_os": "Linux 2.6 - 6.x kernel",
                "host": "node1",
                "tags": ["prod", "db"],
                "config_digest": "2365dec717aef5b6",
                "config_read": True,
            },
        )
        self.assertEqual(len([disk for disk in inventory["disks"] if disk["vm_id"] == "9002"]), 6)
        self.assertEqual(
            [nic["external_id"] for nic in inventory["network_adapters"]], ["9002:net0", "9002:net1", "9004:net0"]
        )

    def test_a_vm_on_a_node_that_is_down_is_listed_without_its_disks(self):
        with self.assertLogs("python.srv.plugins.pve.pve", "WARNING"):
            inventory = self.node(_cluster(node2_online=False)).get_inventory()
        vm = inventory["virtual_machines"][1]
        self.assertEqual((vm["vm_id"], vm["config_read"], vm["host"]), ("9004", False, "node2"))
        self.assertFalse([disk for disk in inventory["disks"] if disk["vm_id"] == "9004"])

    def test_a_vm_is_found_where_it_runs_now(self):
        fake = _cluster()
        node = self.node(fake)
        self.assertEqual(node.locate_vm("9004")["node"], "node2")
        with self.assertRaisesRegex(PveError, "VM 9999 is not in the PVE cluster any more"):
            node.locate_vm("9999")
        self.assertEqual(fake.requests[-1].url.params["type"], "vm")

    def test_backup_and_restore_are_not_there_yet(self):
        node = self.node(_cluster())
        with self.assertRaisesRegex(PveError, "not available in this build yet"):
            node.backup({})


if __name__ == "__main__":
    unittest.main()
