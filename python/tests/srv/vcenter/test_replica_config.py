# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from pyVmomi import vim

from python.srv.plugins.vcenter import vcenter_replica


def _nic(key: int, network: str, *, connected: bool = True) -> Any:
    nic = vim.vm.device.VirtualVmxnet3()
    nic.key = key
    nic.deviceInfo = vim.Description(label=f"Network adapter {key - 3999}", summary=network)
    nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=network)
    nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=connected, connected=False)
    return nic


class ReconfiguringVm:
    def __init__(self, devices: list[Any]) -> None:
        self._moId = "vm-r"
        self.name = "replica"
        self.runtime = SimpleNamespace(host=SimpleNamespace(name="esx-dr-01", network=[], _moId="host-1"))
        self.config = SimpleNamespace(
            hardware=SimpleNamespace(device=list(devices), numCPU=2, memoryMB=4096),
            firmware="efi",
            bootOptions=SimpleNamespace(efiSecureBootEnabled=True, bootOrder=[]),
            changeVersion="cv-1",
        )
        self.specs: list[Any] = []

    def ReconfigVM_Task(self, spec: Any) -> str:
        self.specs.append(spec)
        devices = self.config.hardware.device
        for change in spec.deviceChange or []:
            if change.operation == "remove":
                devices.remove(change.device)
            elif change.operation == "add":
                change.device.key = 4000 + len(devices) + 10
                devices.append(change.device)
        if spec.numCPUs:
            self.config.hardware.numCPU = spec.numCPUs
        if spec.memoryMB:
            self.config.hardware.memoryMB = spec.memoryMB
        return "task"


class ConfigNode:
    def __init__(self, vm: ReconfiguringVm, networks: tuple[str, ...] = ("dr-lan", "dr-dmz"), phantoms: tuple[str, ...] = ()) -> None:
        self.host = "vc.example.test"
        self.vm = vm
        host = vm.runtime.host
        self.networks = {
            name: SimpleNamespace(name=name, summary=SimpleNamespace(accessible=True), host=[host]) for name in networks
        }
        self.networks.update({name: SimpleNamespace(name=name, summary=SimpleNamespace(accessible=False), host=[]) for name in phantoms})

    def _get_vm_by_moref(self, _vm_id: str) -> Any:
        return self.vm

    def _get_datacenter(self, _entity: Any) -> Any:
        return SimpleNamespace(networkFolder=None)

    def _find_network(self, _datacenter: Any, _host: Any, name: str) -> Any:
        return self.networks.get(name)

    def _build_nic_spec(self, _datacenter: Any, _host: Any, adapter: dict[str, Any]) -> Any:
        if adapter["network"] not in self.networks:
            return None
        backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=adapter["network"], network=vim.Network(f"network-{adapter['network']}"))
        return SimpleNamespace(device=SimpleNamespace(backing=backing))

    def _wait_for_task(self, _task: Any) -> None:
        pass


STATE = {"vm_id": "vm-r", "name": "replica"}


def _item(**values: Any) -> dict[str, Any]:
    return {"label": "Network adapter 9", "network": "", "source_network": "", "connected": True, "vlan_id": 0, "legacy": False, **values}


class VsphereConfigTests(unittest.TestCase):
    def test_the_configuration_is_read_with_its_adapters(self) -> None:
        node = ConfigNode(ReconfiguringVm([_nic(4000, "dr-lan"), _nic(4001, "dr-dmz", connected=False)]))

        config = vcenter_replica.read_config(node, STATE)

        self.assertEqual((config["num_cpu"], config["memory_mb"], config["firmware"], config["secure_boot_enabled"]), (2, 4096, "efi", True))
        self.assertEqual(
            [(item["id"], item["network"], item["connected"]) for item in config["network_adapters"]],
            [("4000", "dr-lan", True), ("4001", "dr-dmz", False)],
        )

    def test_cpu_memory_and_every_adapter_change_go_in_one_reconfiguration(self) -> None:
        vm = ReconfiguringVm([_nic(4000, "dr-lan"), _nic(4001, "dr-lan")])
        node = ConfigNode(vm)

        added = vcenter_replica.apply_config(
            node,
            STATE,
            {"num_cpu": 4, "memory_mb": 8192},
            remove=["4001"],
            change=[_item(network="dr-dmz", connected=True, id="4000")],
            add=[_item(network="dr-lan"), _item(source_network="backup-net", connected=False)],
        )

        self.assertEqual(len(vm.specs), 1)
        spec = vm.specs[0]
        self.assertEqual((spec.numCPUs, spec.memoryMB), (4, 8192))
        self.assertEqual([change.operation for change in spec.deviceChange], ["remove", "edit", "add", "add"])
        self.assertEqual(len(added), 2)
        adapters = {item["id"]: item for item in vcenter_replica.read_config(node, STATE)["network_adapters"]}
        self.assertEqual((adapters["4000"]["network"], adapters["4000"]["connected"]), ("dr-dmz", True))
        self.assertEqual([(adapters[key]["network"], adapters[key]["connected"]) for key in added], [("dr-lan", True), ("backup-net", False)])

    def test_an_adapter_left_disconnected_keeps_its_network(self) -> None:
        vm = ReconfiguringVm([_nic(4000, "dr-lan")])

        vcenter_replica.apply_config(ConfigNode(vm), STATE, {}, remove=[], change=[_item(id="4000", connected=False)], add=[])

        device = vm.config.hardware.device[0]
        self.assertEqual((device.backing.deviceName, device.connectable.startConnected), ("dr-lan", False))
        self.assertIsNone(vm.specs[0].numCPUs)

    def test_a_network_the_host_lacks_is_refused(self) -> None:
        vm = ReconfiguringVm([])

        with self.assertRaisesRegex(vcenter_replica.VCenterReplicaError, "Network dr-gone is not on host esx-dr-01"):
            vcenter_replica.apply_config(ConfigNode(vm), STATE, {}, remove=[], change=[], add=[_item(network="dr-gone")])

    def test_only_the_networks_on_the_host_are_offered(self) -> None:
        node = ConfigNode(ReconfiguringVm([]))

        self.assertEqual(vcenter_replica.networks(node, STATE, ["dr-lan", "lan"]), {"dr-lan"})

    def test_a_network_vcenter_only_lists_for_a_vm_that_points_at_it_is_not_offered(self) -> None:
        node = ConfigNode(ReconfiguringVm([]), phantoms=("backup-net",))

        self.assertEqual(vcenter_replica.networks(node, STATE, ["dr-lan", "backup-net"]), {"dr-lan"})
        with self.assertRaisesRegex(vcenter_replica.VCenterReplicaError, "Network backup-net is not on host esx-dr-01"):
            vcenter_replica.apply_config(node, STATE, {}, remove=[], change=[], add=[_item(network="backup-net")])

    def test_a_network_of_another_host_is_not_offered(self) -> None:
        node = ConfigNode(ReconfiguringVm([]))
        node.networks["dr-other"] = SimpleNamespace(name="dr-other", summary=SimpleNamespace(accessible=True), host=[SimpleNamespace(_moId="host-2")])

        self.assertEqual(vcenter_replica.networks(node, STATE, ["dr-other"]), set())

    def test_settings_replication_does_not_change_are_named(self) -> None:
        current = {"firmware": "bios", "secure_boot_enabled": False, "boot_order": []}
        wanted = {"firmware": "efi", "secure_boot_enabled": True, "boot_order": [{"type": "cdrom"}]}

        self.assertEqual(
            vcenter_replica.unsynced(current, wanted),
            [
                "its firmware is bios (the source VM: efi)",
                "Secure Boot is off (the source VM: on)",
                "it boots first from the default order (the source VM: cdrom)",
            ],
        )
        self.assertEqual(vcenter_replica.unsynced(wanted, wanted), [])


if __name__ == "__main__":
    unittest.main()
