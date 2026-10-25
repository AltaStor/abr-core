# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import types
import unittest

from pyVmomi import vim

from python.srv.plugins.vcenter.vcenter import VCenterNode
from python.srv.plugins.vcenter.vcenter_backup import _disk_placement_fields
from python.srv.plugins.vcenter.vcenter_restore import (
    _disk_placements,
    _report_disk_placements,
)

D = vim.vm.device
_Datastore = type("FakeDatastore", (vim.Datastore,), {"name": "ds-01"})


def _disk(key: int, controller_key: int, unit: int) -> object:
    return D.VirtualDisk(
        key=key,
        controllerKey=controller_key,
        unitNumber=unit,
        capacityInKB=1024 * 1024,
        deviceInfo=vim.Description(label=f"Hard disk {key - 1999}", summary=""),
        backing=D.VirtualDisk.FlatVer2BackingInfo(
            fileName=f"[ds-01] demo-vm/demo-vm_{key}.vmdk", diskMode="persistent", thinProvisioned=True
        ),
    )


def _source_devices() -> list[object]:
    return [
        D.ParaVirtualSCSIController(key=1000, busNumber=0),
        D.VirtualLsiLogicController(key=1001, busNumber=1),
        D.VirtualAHCIController(key=15000, busNumber=0),
        D.VirtualNVMEController(key=31000, busNumber=0),
        D.VirtualIDEController(key=200, busNumber=0),
        D.VirtualIDEController(key=201, busNumber=1),
        _disk(2000, 1000, 3),
        _disk(2001, 1001, 2),
        _disk(2002, 15000, 5),
        _disk(2003, 31000, 1),
        _disk(2004, 201, 1),
    ]


def _vm(devices: list[object]) -> object:
    vm = types.SimpleNamespace(
        name="demo-vm",
        config=types.SimpleNamespace(hardware=types.SimpleNamespace(device=devices)),
        reconfig_specs=[],
    )
    vm.ReconfigVM_Task = lambda spec: vm.reconfig_specs.append(spec)
    return vm


def _placement(label: str, type_name: str, bus: object, unit: object) -> dict[str, object]:
    return {"label": label, "controller_type": type_name, "controller_bus": bus, "unit_number": unit}


SOURCE_PLACEMENTS = [
    _placement("Hard disk 1", "paravirtual", 0, 3),
    _placement("Hard disk 2", "lsilogic", 1, 2),
    _placement("Hard disk 3", "sata", 0, 5),
    _placement("Hard disk 4", "nvme", 0, 1),
    _placement("Hard disk 5", "ide", 1, 1),
]


class _VmFolder:
    def __init__(self) -> None:
        self.configs: list[object] = []

    def CreateVM_Task(self, *, config: object, pool: object, host: object) -> object:
        self.configs.append(config)
        return object()


def _shell_node(wait_for_task=None) -> tuple[VCenterNode, _VmFolder]:
    node = VCenterNode("vc.local", "administrator", "secret")
    folder = _VmFolder()
    host = types.SimpleNamespace(
        name="esx-01.local",
        datastore=[_Datastore("datastore-1")],
        parent=types.SimpleNamespace(resourcePool=object()),
    )
    created = types.SimpleNamespace(_moId="vm-restore-001", config=types.SimpleNamespace(firmware="bios"))
    node._get_host = lambda host_name: host  # type: ignore[method-assign]
    node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
    node._get_datacenter = lambda obj: types.SimpleNamespace(vmFolder=folder)  # type: ignore[method-assign]
    node._wait_for_task = wait_for_task or (lambda task: created)  # type: ignore[method-assign]
    return node, folder


def _create_shell(node: VCenterNode, *, disk_count: int, placements, metadata=None) -> dict[str, object]:
    return node.create_vm_shell(
        vm_name="demo-vm-restored",
        host_name="esx-01.local",
        datastore_name="ds-01",
        config_metadata={"firmware": "bios", "network_adapters": [], **(metadata or {})},
        disk_layout=[{"label": f"Hard disk {index}", "capacity_gb": 1} for index in range(1, disk_count + 1)],
        disk_placements=placements,
    )


def _split_changes(config: object) -> tuple[dict[int, object], list[object]]:
    controllers = {}
    disks = []
    for change in config.deviceChange:
        if isinstance(change.device, D.VirtualDisk):
            disks.append(change.device)
        else:
            controllers[change.device.key] = change.device
    return controllers, disks


class DiskPlacementCaptureTests(unittest.TestCase):
    def test_each_disk_records_the_controller_type_bus_and_slot_it_is_on(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")

        disks = node._collect_disks(_vm(_source_devices()))

        self.assertEqual(
            [(disk["controller_type"], disk["controller_bus"], disk["unit_number"]) for disk in disks],
            [("paravirtual", 0, 3), ("lsilogic", 1, 2), ("sata", 0, 5), ("nvme", 0, 1), ("ide", 1, 1)],
        )

    def test_a_disk_on_a_controller_restore_does_not_know_records_that_controller_by_name(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")

        disks = node._collect_disks(_vm([D.VirtualPCIController(key=100, busNumber=0), _disk(2000, 100, 0)]))

        self.assertEqual(disks[0]["controller_type"], "vim.vm.device.VirtualPCIController")

    def test_the_backup_takes_the_first_source_that_knows_the_placement(self) -> None:
        live = {"controller_type": "sata", "controller_bus": 0, "unit_number": 5}

        self.assertEqual(_disk_placement_fields(live, {"controller_type": "paravirtual"}), live)
        self.assertEqual(
            _disk_placement_fields({}, live, {}),
            live,
        )
        self.assertEqual(
            _disk_placement_fields({}),
            {"controller_type": None, "controller_bus": None, "unit_number": None},
        )


class CreateShellPlacementTests(unittest.TestCase):
    def test_each_disk_goes_back_on_its_own_controller_and_slot(self) -> None:
        node, folder = _shell_node()

        result = _create_shell(node, disk_count=5, placements=SOURCE_PLACEMENTS)

        controllers, disks = _split_changes(folder.configs[0])
        self.assertEqual(
            sorted((type(device).__name__, device.busNumber) for device in controllers.values()),
            [
                ("vim.vm.device.ParaVirtualSCSIController", 0),
                ("vim.vm.device.VirtualAHCIController", 0),
                ("vim.vm.device.VirtualLsiLogicController", 1),
                ("vim.vm.device.VirtualNVMEController", 0),
            ],
        )
        placed = []
        for disk in disks:
            controller = controllers.get(disk.controllerKey)
            where = type(controller).__name__ if controller is not None else f"default key {disk.controllerKey}"
            placed.append((where, disk.unitNumber))
        self.assertEqual(
            placed,
            [
                ("vim.vm.device.ParaVirtualSCSIController", 3),
                ("vim.vm.device.VirtualLsiLogicController", 2),
                ("vim.vm.device.VirtualAHCIController", 5),
                ("vim.vm.device.VirtualNVMEController", 1),
                ("default key 201", 1),
            ],
        )
        self.assertEqual(result["disk_controllers"], ["PVSCSI 0", "LSI Logic 1", "SATA 0", "NVMe 0", "IDE 1"])
        self.assertIsNone(result["requested_scsi_controller_type"])

    def test_a_shell_whose_disks_are_attached_later_still_gets_their_controllers(self) -> None:
        node, folder = _shell_node()

        result = _create_shell(node, disk_count=0, placements=SOURCE_PLACEMENTS[2:4])

        controllers, disks = _split_changes(folder.configs[0])
        self.assertEqual(disks, [])
        self.assertEqual(
            sorted(type(device).__name__ for device in controllers.values()),
            ["vim.vm.device.VirtualAHCIController", "vim.vm.device.VirtualNVMEController"],
        )
        self.assertEqual(result["disk_controllers"], ["SATA 0", "NVMe 0"])

    def test_a_point_without_placements_keeps_one_scsi_controller_with_disks_in_order(self) -> None:
        node, folder = _shell_node()

        result = _create_shell(node, disk_count=9, placements=None, metadata={"scsi_controller_type": "lsilogicsas"})

        controllers, disks = _split_changes(folder.configs[0])
        self.assertEqual(list(controllers), [1000])
        self.assertIsInstance(controllers[1000], D.VirtualLsiLogicSASController)
        self.assertEqual([disk.controllerKey for disk in disks], [1000] * 9)
        self.assertEqual([disk.unitNumber for disk in disks], [0, 1, 2, 3, 4, 5, 6, 8, 9])
        self.assertEqual(result["requested_scsi_controller_type"], "lsilogicsas")
        self.assertEqual(result["disk_controllers"], ["LSI Logic SAS 0"])

    def test_a_bus_that_cannot_be_recreated_is_refused_before_the_vm_is_made(self) -> None:
        cases = {
            "unknown controller": (
                [_placement("Hard disk 1", "vim.vm.device.VirtualFutureController", 0, 0)],
                (
                    "Hard disk 1 was on a vim.vm.device.VirtualFutureController controller, "
                    "which this restore cannot recreate."
                ),
            ),
            "no bus recorded": (
                [_placement("Hard disk 1", "sata", None, 0)],
                "Hard disk 1 was on a SATA controller, but the restore point records no valid bus and slot",
            ),
            "IDE bus a VM does not have": (
                [_placement("Hard disk 1", "ide", 2, 0)],
                "Hard disk 1 was on IDE bus 2, which cannot be recreated",
            ),
            "one SCSI bus for two controller types": (
                [_placement("Hard disk 1", "paravirtual", 0, 0), _placement("Hard disk 2", "lsilogic", 0, 1)],
                (
                    "Hard disk 2 needs a LSI Logic controller on SCSI bus 0, which another disk of the restore "
                    "point needs for a PVSCSI controller."
                ),
            ),
            "two disks in one slot": (
                [_placement("Hard disk 1", "sata", 0, 2), _placement("Hard disk 2", "sata", 0, 2)],
                "Hard disk 2 and Hard disk 1 are both recorded at SATA 0:2.",
            ),
        }
        for name, (placements, message) in cases.items():
            with self.subTest(name):
                node, folder = _shell_node()

                with self.assertRaises(ValueError) as caught:
                    _create_shell(node, disk_count=len(placements), placements=placements)

                self.assertIn(message, str(caught.exception))
                self.assertEqual(folder.configs, [])

    def test_buses_vcenter_refuses_are_named_in_the_error(self) -> None:
        def refuse(task: object) -> object:
            raise RuntimeError("vCenter task failed: A specified parameter was not correct: unitNumber")

        node, _ = _shell_node(wait_for_task=refuse)

        with self.assertRaises(RuntimeError) as caught:
            _create_shell(node, disk_count=2, placements=SOURCE_PLACEMENTS[:2])

        self.assertEqual(
            str(caught.exception),
            "Could not create VM 'demo-vm-restored' with the disk controllers it was backed up with "
            "(PVSCSI 0, LSI Logic 1): vCenter task failed: A specified parameter was not correct: unitNumber",
        )

    def test_without_placements_a_vcenter_error_is_raised_as_before(self) -> None:
        def refuse(task: object) -> object:
            raise RuntimeError("vCenter task failed: Insufficient disk space on datastore")

        node, _ = _shell_node(wait_for_task=refuse)

        with self.assertRaises(RuntimeError) as caught:
            _create_shell(node, disk_count=1, placements=None)

        self.assertEqual(str(caught.exception), "vCenter task failed: Insufficient disk space on datastore")


class AttachPlacementTests(unittest.TestCase):
    def _node(self, devices: list[object]) -> tuple[VCenterNode, object]:
        node = VCenterNode("vc.local", "administrator", "secret")
        vm = _vm(devices)
        node._get_vm = lambda vm_name: vm  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]
        return node, vm

    def _shell_devices(self) -> list[object]:
        return [
            D.ParaVirtualSCSIController(key=1000, busNumber=0),
            D.VirtualAHCIController(key=15000, busNumber=0),
            D.VirtualIDEController(key=200, busNumber=0),
            D.VirtualIDEController(key=201, busNumber=1),
        ]

    def test_a_disk_is_attached_at_its_recorded_controller_and_slot(self) -> None:
        node, vm = self._node(self._shell_devices())

        sata = node.attach_existing_disk("demo-vm", "[ds-01] demo-vm/02.vmdk", placement=SOURCE_PLACEMENTS[2])
        ide = node.attach_existing_disk("demo-vm", "[ds-01] demo-vm/05.vmdk", placement=SOURCE_PLACEMENTS[4])

        self.assertEqual((sata["controller_key"], sata["unit_number"]), (15000, 5))
        self.assertEqual((ide["controller_key"], ide["unit_number"]), (201, 1))
        attached = [spec.deviceChange[0].device for spec in vm.reconfig_specs]
        self.assertEqual([(disk.controllerKey, disk.unitNumber) for disk in attached], [(15000, 5), (201, 1)])

    def test_a_slot_already_taken_is_refused(self) -> None:
        node, vm = self._node([*self._shell_devices(), _disk(2000, 15000, 5)])

        with self.assertRaises(ValueError) as caught:
            node.attach_existing_disk(
                "demo-vm", "[ds-01] demo-vm/03.vmdk", disk_label="Hard disk 3", placement=SOURCE_PLACEMENTS[2]
            )

        self.assertEqual(
            str(caught.exception),
            "SATA 0:5 on VM 'demo-vm' is already taken, so Hard disk 3 cannot go back there.",
        )
        self.assertEqual(vm.reconfig_specs, [])

    def test_a_controller_the_vm_lacks_is_refused(self) -> None:
        node, vm = self._node(self._shell_devices())

        with self.assertRaises(ValueError) as caught:
            node.attach_existing_disk(
                "demo-vm", "[ds-01] demo-vm/04.vmdk", disk_label="Hard disk 4", placement=SOURCE_PLACEMENTS[3]
            )

        self.assertEqual(
            str(caught.exception),
            "VM 'demo-vm' has no NVMe controller on bus 0 for Hard disk 4, so it cannot go back to NVMe 0:1.",
        )
        self.assertEqual(vm.reconfig_specs, [])

    def test_without_a_placement_the_first_free_scsi_slot_is_used_as_before(self) -> None:
        node, _ = self._node(
            [
                D.ParaVirtualSCSIController(key=1000, busNumber=0),
                *[_disk(2000 + unit, 1000, unit) for unit in range(7)],
            ]
        )

        attachment = node.attach_existing_disk("demo-vm", "[ds-01] demo-vm/08.vmdk")

        self.assertEqual((attachment["controller_key"], attachment["unit_number"]), (1000, 8))


class RestorePlacementTests(unittest.TestCase):
    def test_placements_come_from_the_manifest_disks_in_order(self) -> None:
        disks = [
            {"disk_label": "Hard disk 1", "controller_type": "sata", "controller_bus": 0, "unit_number": 5},
            {"controller_type": "nvme", "controller_bus": 0, "unit_number": 1},
        ]

        self.assertEqual(
            _disk_placements(disks),
            [_placement("Hard disk 1", "sata", 0, 5), _placement("Hard disk 2", "nvme", 0, 1)],
        )

    def test_a_manifest_that_misses_any_disks_placement_falls_back_to_the_old_layout(self) -> None:
        disks = [
            {"disk_label": "Hard disk 1", "controller_type": "sata", "controller_bus": 0, "unit_number": 5},
            {"disk_label": "Hard disk 2"},
        ]

        self.assertIsNone(_disk_placements(disks))
        self.assertIsNone(_disk_placements([]))

    def test_the_session_says_where_each_disk_went(self) -> None:
        progress: list[dict[str, object]] = []

        _report_disk_placements(progress.append, SOURCE_PLACEMENTS[2:4], ["SATA 0", "NVMe 0"])
        _report_disk_placements(progress.append, None, ["PVSCSI 0"])

        self.assertEqual(
            [update["message"] for update in progress],
            [
                "Hard disk 3 placed on SATA 0:5, as it was backed up",
                "Hard disk 4 placed on NVMe 0:1, as it was backed up",
                (
                    "The restore point does not record which controller and slot each disk was on "
                    "(it was made before that was recorded): the disks go on PVSCSI 0 in disk order"
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
