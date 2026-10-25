# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import types
import unittest

from pyVmomi import vim

from python.srv.plugins.vcenter.vcenter import VCenterNode, _boot_order_entries

D = vim.vm.device
B = vim.vm.BootOptions


def _disk(key: int, controller_key: int, unit: int, uuid: str) -> object:
    return D.VirtualDisk(
        key=key,
        controllerKey=controller_key,
        unitNumber=unit,
        capacityInKB=1024 * 1024,
        deviceInfo=vim.Description(label=f"Hard disk {key - 1999}", summary=""),
        backing=D.VirtualDisk.FlatVer2BackingInfo(fileName=f"[ds-01] vm/vm_{key}.vmdk", diskMode="persistent", uuid=uuid),
    )


def _nic(key: int, number: int) -> object:
    return D.VirtualVmxnet3(key=key, deviceInfo=vim.Description(label=f"Network adapter {number}", summary=""))


def _source_devices() -> list[object]:
    return [
        D.ParaVirtualSCSIController(key=1000, busNumber=0),
        D.VirtualAHCIController(key=15000, busNumber=0),
        D.VirtualIDEController(key=200, busNumber=0),
        _disk(2000, 1000, 0, "uuid-os"),
        _disk(2001, 15000, 0, "uuid-data"),
        _nic(4000, 1),
        _nic(4001, 2),
        D.VirtualCdrom(key=3000, controllerKey=200, unitNumber=0, deviceInfo=vim.Description(label="CD/DVD drive 1", summary="")),
    ]


def _config(devices: list[object], order: list[object] | None) -> object:
    return types.SimpleNamespace(
        hardware=types.SimpleNamespace(device=devices),
        bootOptions=types.SimpleNamespace(bootOrder=order),
    )


OS_DISK = {"type": "disk", "label": "Hard disk 1", "uuid": "uuid-os", "controller_type": "paravirtual", "controller_bus": 0, "unit_number": 0}
DATA_DISK = {"type": "disk", "label": "Hard disk 2", "uuid": "uuid-data", "controller_type": "sata", "controller_bus": 0, "unit_number": 0}


class BootOrderCaptureTests(unittest.TestCase):
    def test_each_boot_device_is_recorded_by_what_it_is_not_by_its_key(self) -> None:
        order = [
            B.BootableDiskDevice(deviceKey=2000),
            B.BootableCdromDevice(),
            B.BootableEthernetDevice(deviceKey=4001),
            B.BootableFloppyDevice(),
            B.BootableDiskDevice(deviceKey=2001),
        ]

        entries = _boot_order_entries(_config(_source_devices(), order))

        self.assertEqual(
            entries,
            [OS_DISK, {"type": "cdrom"}, {"type": "network", "label": "Network adapter 2"}, {"type": "floppy"}, DATA_DISK],
        )

    def test_no_boot_order_records_nothing(self) -> None:
        self.assertEqual(_boot_order_entries(_config(_source_devices(), None)), [])
        self.assertEqual(_boot_order_entries(types.SimpleNamespace(hardware=None, bootOptions=None)), [])

    def test_a_key_the_vm_no_longer_has_is_recorded_without_a_device(self) -> None:
        entries = _boot_order_entries(_config(_source_devices(), [B.BootableDiskDevice(deviceKey=2999)]))

        self.assertEqual(
            entries,
            [{"type": "disk", "label": None, "uuid": None, "controller_type": None, "controller_bus": None, "unit_number": None}],
        )


class BootOrderApplyTests(unittest.TestCase):
    def _node(self, devices: list[object]) -> tuple[VCenterNode, object]:
        node = VCenterNode("vc.local", "administrator", "secret")
        vm = types.SimpleNamespace(config=types.SimpleNamespace(hardware=types.SimpleNamespace(device=devices)), specs=[])
        vm.ReconfigVM_Task = lambda spec: vm.specs.append(spec)
        node._get_vm = lambda name: vm  # type: ignore[method-assign]
        node._get_vm_by_moref = lambda moref: None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]
        return node, vm

    def _applied(self, vm: object) -> list[tuple[str, int | None]]:
        return [
            (type(entry).__name__.split(".")[-1], getattr(entry, "deviceKey", None))
            for entry in vm.specs[-1].bootOptions.bootOrder
        ]

    def test_the_source_order_is_rebuilt_on_the_restored_devices(self) -> None:
        restored = [
            D.ParaVirtualSCSIController(key=-101, busNumber=0),
            D.VirtualAHCIController(key=-102, busNumber=0),
            _disk(2500, -101, 0, "uuid-os"),
            _disk(2600, -102, 0, "uuid-data"),
            _nic(4100, 1),
            _nic(4101, 2),
            D.VirtualCdrom(key=3100, controllerKey=200, unitNumber=0),
        ]
        node, vm = self._node(restored)
        adapters = [{"label": "Network adapter 1", "attached": True}, {"label": "Network adapter 2", "attached": True}]

        report = node.apply_boot_order(
            "vm-restored",
            [OS_DISK, {"type": "cdrom"}, {"type": "network", "label": "Network adapter 2"}, DATA_DISK],
            network_adapters=adapters,
        )

        self.assertEqual(
            self._applied(vm),
            [("BootableDiskDevice", 2500), ("BootableCdromDevice", None), ("BootableEthernetDevice", 4101), ("BootableDiskDevice", 2600)],
        )
        self.assertEqual(
            [item["device"] for item in report],
            ["Hard disk 1 (PVSCSI 0:0)", "CD/DVD drive", "Network adapter 2", "Hard disk 2 (SATA 0:0)"],
        )
        self.assertTrue(all(item["restored"] for item in report))

    def test_a_disk_restored_elsewhere_is_found_by_its_uuid(self) -> None:
        node, vm = self._node([D.ParaVirtualSCSIController(key=1000, busNumber=0), _disk(2500, 1000, 3, "uuid-os")])

        node.apply_boot_order("vm-restored", [OS_DISK])

        self.assertEqual(self._applied(vm), [("BootableDiskDevice", 2500)])

    def test_devices_the_restored_vm_lacks_are_left_out_and_said(self) -> None:
        restored = [D.ParaVirtualSCSIController(key=1000, busNumber=0), _disk(2500, 1000, 0, "uuid-os"), _nic(4100, 1)]
        node, vm = self._node(restored)
        adapters = [{"label": "Network adapter 1", "attached": False}, {"label": "Network adapter 2", "attached": True}]

        report = node.apply_boot_order(
            "vm-restored",
            [
                {"type": "network", "label": "Network adapter 1"},
                {"type": "network", "label": "Network adapter 3"},
                DATA_DISK,
                {"type": "cdrom"},
                {"type": "floppy"},
                {"type": "usb"},
                {"type": "network", "label": "Network adapter 2"},
                OS_DISK,
            ],
            network_adapters=adapters,
        )

        self.assertEqual(self._applied(vm), [("BootableEthernetDevice", 4100), ("BootableDiskDevice", 2500)])
        self.assertEqual(
            [(item["device"], item["reason"]) for item in report if not item["restored"]],
            [
                ("Network adapter 1", "the adapter was not restored because its network is missing on the target host"),
                ("Network adapter 3", "the restored VM has no such network adapter"),
                ("Hard disk 2 (SATA 0:0)", "the restored VM has no such disk"),
                ("CD/DVD drive", "the restored VM has no CD/DVD drive"),
                ("floppy drive", "the restored VM has no floppy drive"),
                ("a device the source VM no longer had", "boot device type 'usb' is not known"),
            ],
        )

    def test_nothing_is_changed_when_no_entry_can_be_restored(self) -> None:
        node, vm = self._node([D.ParaVirtualSCSIController(key=1000, busNumber=0)])

        report = node.apply_boot_order("vm-restored", [OS_DISK, {"type": "cdrom"}])

        self.assertEqual(vm.specs, [])
        self.assertEqual([item["restored"] for item in report], [False, False])


if __name__ == "__main__":
    unittest.main()
