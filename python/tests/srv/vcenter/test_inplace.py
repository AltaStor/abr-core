# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyVmomi import vim

from python.srv.plugins.vcenter import vcenter_inplace, vcenter_replica
from python.srv.plugins.vcenter.vcenter_backup import VCenterBackupError

TASK = "1a2b3c4d-0000-0000-0000-000000000000"
GUARD = "abr-in-place-1a2b3c4d"


def controller(key: int, bus: int, kind: type = vim.vm.device.ParaVirtualSCSIController) -> Any:
    return kind(key=key, busNumber=bus)


def disk(key: int, controller_key: int, unit: int, path: str, uuid: str = "", parent: str = "", backing: type | None = None) -> Any:
    if backing is not None:
        made = backing(fileName=path)
    else:
        made = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=path, uuid=uuid, diskMode="persistent")
        if parent:
            made.parent = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=parent)
    return vim.vm.device.VirtualDisk(
        key=key, controllerKey=controller_key, unitNumber=unit, backing=made, deviceInfo=vim.Description(label=f"Hard disk {key - 1999}")
    )


def chain_of(device: Any) -> list[str]:
    return vcenter_inplace._chain(device.backing)


class FakeSnapshot:
    def __init__(self, vm: FakeVm, item: Any, saved: list[tuple[Any, Any, Any, Any]], firmware: str, held: set[str]) -> None:
        self.vm, self.item, self.saved, self.firmware, self.held = vm, item, saved, firmware, held

    def RevertToSnapshot_Task(self) -> Any:
        if self.vm.fail_revert is not None:
            raise self.vm.fail_revert
        devices = []
        for device, controller_key, unit, backing in self.saved:
            if controller_key is not None:
                device.controllerKey, device.unitNumber, device.backing = controller_key, unit, backing
            devices.append(device)
        self.vm.config.hardware.device = devices
        self.vm.config.firmware = self.firmware
        self.vm.reverted.append(self.item.name)
        return "task"

    def RemoveSnapshot_Task(self, removeChildren: bool, consolidate: bool) -> Any:
        self.vm.items.remove(self.item)
        self.vm.removed.append(self.item.name)
        return "task"


class FakeVm:
    def __init__(self, vm_id: str, name: str, devices: list[Any], *, powered_on: bool = True, instance_uuid: str = "inst-1") -> None:
        self._moId = vm_id
        self.name = name
        self.items: list[Any] = []
        self.config = SimpleNamespace(
            hardware=SimpleNamespace(device=list(devices)),
            files=SimpleNamespace(vmPathName=f"[ds1] {name}/{name}.vmx"),
            instanceUuid=instance_uuid,
            firmware="bios",
        )
        self.runtime = SimpleNamespace(
            host=SimpleNamespace(name="esx-01", datastore=[SimpleNamespace(name="ds1"), SimpleNamespace(name="ds2")]),
            powerState=vim.VirtualMachinePowerState.poweredOn if powered_on else vim.VirtualMachinePowerState.poweredOff,
            consolidationNeeded=False,
        )
        self.reconfigured: list[Any] = []
        self.specs: list[Any] = []
        self.next_key = 3000
        self.fail_reconfigure: Exception | None = None
        self.fail_snapshot: Exception | None = None
        self.fail_revert: Exception | None = None
        self.reverted: list[str] = []
        self.removed: list[str] = []

    @property
    def snapshot(self) -> Any:
        return SimpleNamespace(rootSnapshotList=list(self.items)) if self.items else None

    @property
    def layoutEx(self) -> Any:
        names: list[str] = []
        for device in self.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                names.extend(chain_of(device))
        held = sorted({path for item in self.items for path in item.snapshot.held})
        names.extend(held)
        keys = {name: index for index, name in enumerate(dict.fromkeys(names))}
        files = [SimpleNamespace(key=key, name=name) for name, key in keys.items()]
        snapshots = [
            SimpleNamespace(disk=[SimpleNamespace(chain=[SimpleNamespace(fileKey=[keys[path]]) for path in sorted(item.snapshot.held)])])
            for item in self.items
        ]
        return SimpleNamespace(file=files, snapshot=snapshots)

    def hold(self, name: str, files: set[str]) -> None:
        item = SimpleNamespace(name=name, childSnapshotList=[])
        item.snapshot = FakeSnapshot(self, item, [], "bios", files)
        self.items.append(item)

    def CreateSnapshot_Task(self, name: str, description: str, memory: bool, quiesce: bool) -> Any:
        if self.fail_snapshot is not None:
            raise self.fail_snapshot
        saved = [
            (device, device.controllerKey, device.unitNumber, device.backing) if isinstance(device, vim.vm.device.VirtualDisk) else (device, None, None, None)
            for device in self.config.hardware.device
        ]
        held = {path for device in self.config.hardware.device if isinstance(device, vim.vm.device.VirtualDisk) for path in chain_of(device)}
        item = SimpleNamespace(name=name, childSnapshotList=[])
        item.snapshot = FakeSnapshot(self, item, saved, self.config.firmware, held)
        self.items.append(item)
        return "task"

    def ReconfigVM_Task(self, spec: Any) -> Any:
        if self.fail_reconfigure is not None:
            raise self.fail_reconfigure
        self.specs.append(spec)
        if spec.firmware:
            self.config.firmware = spec.firmware
        self.reconfigured.append([(change.operation, change.device.key) for change in spec.deviceChange or []])
        devices = list(self.config.hardware.device)
        renumbered: dict[int, int] = {}
        for change in spec.deviceChange or []:
            if change.operation == vim.vm.device.VirtualDeviceSpec.Operation.remove:
                devices = [device for device in devices if device is not change.device]
            elif change.operation == vim.vm.device.VirtualDeviceSpec.Operation.add:
                self.next_key += 1
                added = change.device
                if added.key < 0:
                    renumbered[added.key] = self.next_key
                added.key = self.next_key
                devices.append(added)
        for device in devices:
            if getattr(device, "controllerKey", None) in renumbered:
                device.controllerKey = renumbered[device.controllerKey]
        self.config.hardware.device = devices
        return "task"


class FakeNode:
    def __init__(self, *vms: FakeVm) -> None:
        self.host = "vc.example.test"
        self.vms = {vm._moId: vm for vm in vms}
        self.destroyed: list[str] = []
        self.deleted: list[str] = []
        self.deleted_files: list[str] = []
        self.powered_on: list[str] = []
        self.powered_off: list[str] = []
        self.folders: dict[str, list[str]] = {}
        self.locked: dict[str, int] = {}
        self.found_by_uuid: Any = None
        self.si = SimpleNamespace(RetrieveContent=self._content)

    def _content(self) -> Any:
        return SimpleNamespace(
            searchIndex=SimpleNamespace(FindByUuid=lambda **kwargs: self.found_by_uuid),
            fileManager=SimpleNamespace(DeleteDatastoreFile_Task=self._delete_file),
        )

    def _delete_file(self, name: str, datacenter: Any) -> Any:
        self.deleted_files.append(name)
        return "task"

    def _get_datacenter(self, entity: Any) -> Any:
        return SimpleNamespace(name="dc")

    def _get_vm_by_moref(self, vm_id: str) -> Any:
        return self.vms.get(vm_id)

    def _get_vm(self, name: str) -> Any:
        return next((vm for vm in self.vms.values() if vm.name == name), None)

    def _get_datastore(self, name: str) -> Any:
        def search(datastorePath: str, searchSpec: Any) -> Any:
            folder = datastorePath
            return SimpleNamespace(file=[SimpleNamespace(path=item) for item in self.folders.get(folder, []) if (folder + "/" + item) not in self.deleted])

        return SimpleNamespace(name=name, browser=SimpleNamespace(SearchDatastore_Task=search))

    def _wait_for_task(self, task: Any) -> Any:
        return task

    def destroy_vm(self, name: str, vm_moref: str | None = None) -> None:
        self.destroyed.append(name)
        self.vms.pop(vm_moref or "", None)

    def delete_managed_disk(self, path: str) -> None:
        if self.locked.get(path):
            self.locked[path] -= 1
            raise RuntimeError(f"vCenter task failed: Error caused by file {path}")
        self.deleted.append(path)

    def power_on_vm(self, name: str, vm_moref: str | None = None) -> dict:
        self.powered_on.append(name)
        return {}

    def power_off_vm(self, name: str, vm_moref: str | None = None) -> dict:
        self.powered_off.append(name)
        return {}


def original(*devices: Any, powered_on: bool = True) -> FakeVm:
    return FakeVm("vm-104", "app-01", [controller(1000, 0), controller(1001, 1), *devices], powered_on=powered_on)


def entry(index: int, path: str, unit: int | None = 0, bus: int = 0, uuid: str = "", kind: str = "paravirtual") -> dict:
    placed = {"controller_type": kind, "controller_bus": bus, "unit_number": unit} if unit is not None else {}
    return {"disk_label": f"Hard disk {index}", "datastore_path": path, "uuid": uuid, **placed}


class DescribeTests(unittest.TestCase):
    def test_slots_controllers_home_and_disk_kinds(self) -> None:
        vm = original(
            disk(2000, 1000, 0, "[ds1] app-01/app-01.vmdk", "u1"),
            disk(2001, 1001, 0, "[ds2] app-01/app-01_1-000001.vmdk", parent="[ds2] app-01/app-01_1.vmdk"),
            disk(2002, 1001, 1, "[ds2] gold/clone-000001.vmdk", parent="[ds2] gold/base.vmdk"),
            disk(2003, 1001, 2, "[ds1] app-01/rdm.vmdk", backing=vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo),
        )
        vm.hold("before", {"[ds2] app-01/app-01_1.vmdk"})

        state = vcenter_inplace.describe_vm(vm)

        self.assertEqual(state["controllers"], {"paravirtual:0": 1000, "paravirtual:1": 1001})
        self.assertEqual(
            [(item["slot"], item["kind"], item["base"]) for item in state["disks"]],
            [
                ("paravirtual:0:0", "plain", "[ds1] app-01/app-01.vmdk"),
                ("paravirtual:1:0", "snapshot", "[ds2] app-01/app-01_1.vmdk"),
                ("paravirtual:1:1", "linked", "[ds2] gold/base.vmdk"),
                ("paravirtual:1:2", "other", "[ds1] app-01/rdm.vmdk"),
            ],
        )
        self.assertEqual((state["home_datastore"], state["home_folder"], state["snapshots"], state["powered_on"]), ("ds1", "app-01", ["before"], True))

    def test_snapshots_are_no_reason_to_refuse_but_a_needed_consolidation_is(self) -> None:
        state = vcenter_inplace.describe_vm(original())
        self.assertIsNone(vcenter_inplace.refusal({**state, "snapshots": ["a", "b"]}))
        self.assertIn("needs its disks consolidated", vcenter_inplace.refusal({**state, "consolidation_needed": True}) or "")


class FindTests(unittest.TestCase):
    def test_the_vm_is_found_by_its_id_then_by_its_instance_uuid(self) -> None:
        vm = original()
        node = FakeNode(vm)

        self.assertIs(vcenter_inplace.find_original(node, vm_id="vm-104", instance_uuid="inst-1", name="app-01"), vm)
        node.found_by_uuid = vm
        self.assertIs(vcenter_inplace.find_original(node, vm_id="vm-1", instance_uuid="inst-1", name="renamed-in-the-point"), vm)

    def test_an_id_that_now_names_another_vm_is_not_taken(self) -> None:
        node = FakeNode(original())

        with self.assertRaises(VCenterBackupError) as caught:
            vcenter_inplace.find_original(node, vm_id="vm-104", instance_uuid="other-uuid", name="app-01")
        self.assertIn("VM app-01 is not on vc.example.test", str(caught.exception))
        self.assertIn("id vm-104 or instance UUID other-uuid", str(caught.exception))
        with self.assertRaises(VCenterBackupError):
            vcenter_inplace.find_original(node, vm_id="vm-104", instance_uuid="", name="db-01")


class PlanTests(unittest.TestCase):
    def state(self, *devices: Any, held: set[str] | None = None) -> dict:
        vm = original(*devices)
        if held:
            vm.hold("before", held)
        return vcenter_inplace.describe_vm(vm)

    def test_each_point_disk_replaces_the_disk_in_its_slot_next_to_it(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/app-01.vmdk"), disk(2001, 1001, 0, "[ds2] data/app-01_1-rdeadbeef.vmdk"))

        planned, kept = vcenter_inplace.plan_disks(
            state, [entry(1, "[ds1] app-01/app-01.vmdk"), entry(2, "[ds2] data/app-01_1-rdeadbeef.vmdk", unit=0, bus=1)], TASK
        )

        self.assertEqual(
            [(item["slot"], item["old_path"], item["new_path"], item["controller"], item["old_controller_key"]) for item in planned],
            [
                ("paravirtual:0:0", "[ds1] app-01/app-01.vmdk", "[ds1] app-01/app-01-r1a2b3c4d.vmdk", "paravirtual:0", 1000),
                ("paravirtual:1:0", "[ds2] data/app-01_1-rdeadbeef.vmdk", "[ds2] data/app-01_1-r1a2b3c4d.vmdk", "paravirtual:1", 1001),
            ],
        )
        self.assertEqual(kept, [])

    def test_a_disk_under_the_vms_own_snapshot_is_replaced_next_to_its_delta(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/a-000002.vmdk", parent="[ds1] app-01/a.vmdk"), held={"[ds1] app-01/a.vmdk"})

        [planned], _kept = vcenter_inplace.plan_disks(state, [entry(1, "[ds1] app-01/a.vmdk")], TASK)

        self.assertEqual((planned["old_path"], planned["old_base"], planned["new_path"]), ("[ds1] app-01/a-000002.vmdk", "[ds1] app-01/a.vmdk", "[ds1] app-01/a-r1a2b3c4d.vmdk"))

    def test_a_disk_removed_after_the_backup_is_added_back_and_one_added_after_it_is_kept(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/app-01.vmdk"), disk(2001, 1000, 2, "[ds1] app-01/scratch.vmdk"))

        planned, kept = vcenter_inplace.plan_disks(
            state, [entry(1, "[ds1] app-01/app-01.vmdk"), entry(2, "[ds2] old/app-01_1.vmdk", unit=1), entry(3, "[ds9] old/app-01_2.vmdk", unit=3)], TASK
        )

        self.assertEqual(planned[1]["slot"], "paravirtual:0:1")
        self.assertEqual((planned[1]["old_path"], planned[1]["new_path"], planned[1]["old_controller_key"]), ("", "[ds2] app-01/app-01_1-r1a2b3c4d.vmdk", None))
        self.assertEqual(planned[2]["new_path"], "[ds1] app-01/app-01_2-r1a2b3c4d.vmdk")
        self.assertEqual([item["path"] for item in kept], ["[ds1] app-01/scratch.vmdk"])

    def test_a_disk_on_a_controller_the_vm_no_longer_has_goes_on_that_controller(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/app-01.vmdk"))

        planned, _kept = vcenter_inplace.plan_disks(state, [entry(1, "[ds1] app-01/app-01.vmdk"), entry(2, "[ds1] app-01/b.vmdk", unit=0, bus=3)], TASK)

        self.assertEqual((planned[1]["controller"], planned[1]["unit_number"], planned[1]["old_path"]), ("paravirtual:3", 0, ""))

    def test_a_disk_on_another_controller_type_on_the_same_bus_is_still_the_one_replaced(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/app-01.vmdk"))

        [planned], _kept = vcenter_inplace.plan_disks(state, [entry(1, "[ds1] app-01/app-01.vmdk", kind="lsilogicsas")], TASK)

        self.assertEqual((planned["controller"], planned["old_path"], planned["old_controller_key"]), ("lsilogicsas:0", "[ds1] app-01/app-01.vmdk", 1000))

    def test_a_raw_mapping_and_a_linked_clone_are_refused(self) -> None:
        for device, text in (
            (disk(2000, 1000, 0, "[ds1] app-01/rdm.vmdk", backing=vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo), "raw device mapping"),
            (disk(2000, 1000, 0, "[ds1] app-01/c-000001.vmdk", parent="[ds1] gold/base.vmdk"), "is a linked clone: it is built on [ds1] gold/base.vmdk"),
        ):
            with self.subTest(text=text), self.assertRaises(VCenterBackupError) as caught:
                vcenter_inplace.plan_disks(self.state(device), [entry(1, "[ds1] app-01/app-01.vmdk")], TASK)
            self.assertIn(text, str(caught.exception))

    def test_points_without_slots_match_by_uuid_then_in_order(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk", "uuid-a"), disk(2001, 1000, 1, "[ds1] app-01/b.vmdk", "uuid-b"))

        planned, kept = vcenter_inplace.plan_disks(
            state, [entry(1, "[ds1] app-01/x.vmdk", unit=None, uuid="uuid-new"), entry(2, "[ds1] app-01/b.vmdk", unit=None, uuid="uuid-b")], TASK
        )

        self.assertEqual([item["old_path"] for item in planned], ["[ds1] app-01/a.vmdk", "[ds1] app-01/b.vmdk"])
        self.assertEqual([item["controller"] for item in planned], ["paravirtual:0", "paravirtual:0"])
        self.assertEqual(kept, [])

    def test_a_long_name_and_repeated_restores_keep_the_file_name_short(self) -> None:
        state = self.state(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk"))
        name = "n" * 200 + ".vmdk"
        for _ in range(5):
            planned, _kept = vcenter_inplace.plan_disks(state, [entry(1, f"[ds1] app-01/{name}")], TASK)
            name = planned[0]["new_path"].rsplit("/", 1)[1]

        self.assertLessEqual(len(name), vcenter_inplace.DISK_FILE_NAME_LIMIT)
        self.assertTrue(name.endswith("-r1a2b3c4d.vmdk"))
        self.assertEqual(name.count("-r1a2b3c4d"), 1)


def planned_disk(old_path: str, new_path: str, *, controller: str = "paravirtual:0", old_key: int | None = 1000, unit: int = 0, base: str = "") -> dict:
    return {
        "label": "Hard disk 1",
        "controller": controller,
        "unit_number": unit,
        "old_controller_key": old_key,
        "old_unit_number": unit if old_key is not None else None,
        "old_path": old_path,
        "old_base": base or old_path,
        "new_path": new_path,
    }


def paths(vm: FakeVm) -> list[str]:
    return [device.backing.fileName for device in vm.config.hardware.device if isinstance(device, vim.vm.device.VirtualDisk)]


def controller_types(vm: FakeVm) -> list[str]:
    return sorted(f"{vcenter_inplace._disk_controller_type_name(d)}:{d.busNumber}" for d in vm.config.hardware.device if vcenter_inplace._disk_controller_type_name(d))


class SwitchSpecTests(unittest.TestCase):
    def apply(self, vm: FakeVm, disks: list[dict]) -> list[str]:
        changes, messages = vcenter_inplace.switch_spec(vm, disks)
        vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=changes))
        return messages

    def test_a_disk_on_the_same_controller_is_edited_onto_the_new_file(self) -> None:
        vm = original(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk"))

        messages = self.apply(vm, [planned_disk("[ds1] app-01/a.vmdk", "[ds1] app-01/a-r1.vmdk")])

        self.assertEqual(vm.reconfigured, [[(vim.vm.device.VirtualDeviceSpec.Operation.edit, 2000)]])
        self.assertEqual(paths(vm), ["[ds1] app-01/a-r1.vmdk"])
        self.assertEqual(messages, [])

    def test_another_controller_type_on_the_bus_is_replaced_and_its_other_disks_move_with_it(self) -> None:
        vm = original(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk"), disk(2001, 1000, 1, "[ds1] app-01/scratch.vmdk"))

        messages = self.apply(vm, [planned_disk("[ds1] app-01/a.vmdk", "[ds1] app-01/a-r1.vmdk", controller="lsilogicsas:0")])

        self.assertIn("lsilogicsas:0", controller_types(vm))
        self.assertNotIn("paravirtual:0", controller_types(vm))
        new_key = next(d.key for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualLsiLogicSASController))
        self.assertEqual({d.backing.fileName: d.controllerKey for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)}, {"[ds1] app-01/a-r1.vmdk": new_key, "[ds1] app-01/scratch.vmdk": new_key})
        self.assertEqual(messages, ["PVSCSI 0 replaced by LSI Logic SAS 0"])

    def test_a_missing_controller_is_added_and_the_others_stay(self) -> None:
        cdrom = vim.vm.device.VirtualCdrom(key=3002, controllerKey=15000, unitNumber=0)
        vm = FakeVm("vm-104", "app-01", [controller(1000, 0), controller(1001, 1), vim.vm.device.VirtualAHCIController(key=15000, busNumber=0), cdrom, disk(2000, 1000, 0, "[ds1] app-01/a.vmdk")])

        messages = self.apply(
            vm,
            [
                planned_disk("[ds1] app-01/a.vmdk", "[ds1] app-01/a-r1.vmdk"),
                planned_disk("", "[ds1] app-01/b-r1.vmdk", controller="nvme:0", old_key=None),
            ],
        )

        self.assertEqual(controller_types(vm), ["nvme:0", "paravirtual:0", "paravirtual:1", "sata:0"])
        self.assertEqual(sorted(paths(vm)), ["[ds1] app-01/a-r1.vmdk", "[ds1] app-01/b-r1.vmdk"])
        self.assertEqual(messages, ["NVMe 0 added"])


def plan_for(node: FakeNode, vm: FakeVm, *, staging_holds: bool, was_on: bool = True, disks: list[dict] | None = None) -> dict:
    staging_devices = [controller(1000, 0)]
    if staging_holds:
        staging_devices.append(disk(2000, 1000, 0, "[ds1] app-01/a-r1a2b3c4d.vmdk"))
    node.vms["vm-200"] = FakeVm("vm-200", "app-01-restoring-1a2b3c4d", staging_devices)
    disks = disks or [planned_disk("[ds1] app-01/a.vmdk", "[ds1] app-01/a-r1a2b3c4d.vmdk")]
    return {
        "vm_id": vm._moId,
        "name": "app-01",
        "was_powered_on": was_on,
        "staging_vm_id": "vm-200",
        "staging_name": "app-01-restoring-1a2b3c4d",
        "guard_snapshot": GUARD,
        "old_bases": [item["old_base"] for item in disks if item["old_base"]],
        "disks": disks,
    }


WANTED = {"num_cpu": 2, "firmware": "efi"}


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = original(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk"))
        self.node = FakeNode(self.vm)
        self.node.folders["[ds1] app-01"] = ["a.vmdk"]
        self.reports: list[tuple[int, str, str]] = []
        self.saved: list[dict] = []
        self.settle = patch.object(vcenter_inplace, "settle_config", side_effect=self.fake_settle)
        self.settle.start()
        self.addCleanup(self.settle.stop)
        self.settle_error: Exception | None = None

    def fake_settle(self, node: Any, vm_id: str, name: str, wanted: dict, point_id: str) -> tuple[list[str], list[str]]:
        if self.settle_error is not None:
            raise self.settle_error
        self.vm.config.firmware = wanted["firmware"]
        return ["2 vCPU (was 1)"], []

    def report(self, progress: int, message: str, level: str) -> None:
        self.reports.append((progress, message, level))

    def run_switch(self, plan: dict) -> dict:
        return vcenter_inplace.switch(self.node, plan, WANTED, "p1", self.report, lambda saved: self.saved.append(dict(saved)))

    def test_the_vm_is_guarded_switched_configured_and_the_old_files_removed(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=True)

        outcome = self.run_switch(plan)

        self.assertEqual(outcome["problems"], [])
        self.assertEqual(outcome["changes"], ["2 vCPU (was 1)"])
        self.assertEqual(self.node.powered_off, ["app-01"])
        self.assertEqual(paths(self.vm), ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual((self.vm.removed, self.vm.reverted, self.vm.items), ([GUARD], [], []))
        self.assertEqual(self.vm.config.firmware, "efi")
        self.assertEqual(self.node.deleted, ["[ds1] app-01/a.vmdk"])
        self.assertEqual(self.node.destroyed, ["app-01-restoring-1a2b3c4d"])
        self.assertTrue(self.saved and self.saved[-1]["committed"])
        self.assertEqual(self.reports[0][1], "Turning off app-01 to replace its disks")

    def test_user_snapshots_keep_their_disks_and_only_unused_old_files_go(self) -> None:
        self.vm = original(disk(2000, 1000, 0, "[ds1] app-01/a-000001.vmdk", parent="[ds1] app-01/a.vmdk"))
        self.vm.hold("before-upgrade", {"[ds1] app-01/a.vmdk"})
        self.node = FakeNode(self.vm)
        self.node.folders["[ds1] app-01"] = ["a.vmdk", "a-000001.vmdk", "a-000002.vmdk", "a-r1a2b3c4d.vmdk", "a-flat.vmdk"]
        plan = plan_for(self.node, self.vm, staging_holds=False, disks=[planned_disk("[ds1] app-01/a-000001.vmdk", "[ds1] app-01/a-r1a2b3c4d.vmdk", base="[ds1] app-01/a.vmdk")])

        outcome = self.run_switch(plan)

        self.assertEqual(paths(self.vm), ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual([item.name for item in self.vm.items], ["before-upgrade"])
        self.assertEqual(sorted(self.node.deleted), ["[ds1] app-01/a-000001.vmdk", "[ds1] app-01/a-000002.vmdk"])
        self.assertEqual(
            outcome["held"],
            "VM app-01 keeps its snapshot(s) before-upgrade. Reverting to one of them brings back the disks it was taken with and "
            "leaves the restored disk(s) [ds1] app-01/a-r1a2b3c4d.vmdk unused; deleting the snapshots instead leaves the disk(s) "
            "the VM had before the restore, which they keep: [ds1] app-01/a.vmdk. vCenter does not remove such files, so delete "
            "them by hand then",
        )

    def test_tracking_files_of_removed_disks_go_and_those_of_used_disks_stay(self) -> None:
        self.node.folders["[ds1] app-01"] = ["a.vmdk", "a-ctk.vmdk", "a-r1a2b3c4d-ctk.vmdk"]
        plan = plan_for(self.node, self.vm, staging_holds=False)

        outcome = self.run_switch(plan)

        self.assertEqual(self.node.deleted, ["[ds1] app-01/a.vmdk"])
        self.assertEqual(self.node.deleted_files, ["[ds1] app-01/a-ctk.vmdk"])
        self.assertEqual(outcome["held"], "")

    def test_a_failed_switch_goes_back_to_the_guard_and_removes_the_new_disks(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=True)
        self.settle_error = RuntimeError("vCenter task failed: busy")

        with self.assertRaises(RuntimeError):
            self.run_switch(plan)

        self.assertTrue(plan["undone"])
        self.assertEqual((self.vm.reverted, self.vm.removed, self.vm.items), ([GUARD], [GUARD], []))
        self.assertEqual(paths(self.vm), ["[ds1] app-01/a.vmdk"])
        self.assertEqual(self.vm.config.firmware, "bios")
        self.assertEqual(self.node.deleted, ["[ds1] app-01/a-r1a2b3c4d.vmdk"])

    def test_the_delta_the_guard_left_is_removed_after_going_back_to_it(self) -> None:
        self.node.folders["[ds1] app-01"] = ["a.vmdk", "a-000001.vmdk", "a-000001-ctk.vmdk", "a-ctk.vmdk"]
        plan = plan_for(self.node, self.vm, staging_holds=True)
        self.settle_error = RuntimeError("vCenter task failed: busy")

        with self.assertRaises(RuntimeError):
            self.run_switch(plan)

        self.assertEqual(paths(self.vm), ["[ds1] app-01/a.vmdk"])
        self.assertEqual(sorted(self.node.deleted), ["[ds1] app-01/a-000001.vmdk", "[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual(self.node.deleted_files, ["[ds1] app-01/a-000001-ctk.vmdk"])
        self.assertEqual(self.node.destroyed, ["app-01-restoring-1a2b3c4d"])
        self.assertEqual(self.node.powered_on, ["app-01"])
        self.assertIn("it is back as it was and powered on again", self.reports[-1][1])
        self.assertFalse(any(saved.get("committed") for saved in self.saved))

    def test_a_guard_that_cannot_be_taken_leaves_the_vm_untouched(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=True)
        self.vm.fail_snapshot = RuntimeError("vCenter task failed: snapshot limit")

        with self.assertRaises(RuntimeError):
            self.run_switch(plan)

        self.assertEqual(paths(self.vm), ["[ds1] app-01/a.vmdk"])
        self.assertEqual(self.vm.reconfigured, [])
        self.assertEqual(self.node.powered_on, ["app-01"])

    def test_a_guard_that_cannot_be_reverted_is_kept_and_named(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=True)
        self.settle_error = RuntimeError("config failed")
        self.vm.fail_revert = RuntimeError("revert failed")

        with self.assertRaises(RuntimeError), self.assertLogs(vcenter_inplace.logger, "ERROR"):
            self.run_switch(plan)

        self.assertFalse(plan["undone"])
        self.assertEqual([item.name for item in self.vm.items], [GUARD])
        self.assertIn("the snapshot is kept so that it can be reverted in vCenter", self.reports[-1][1])
        self.assertEqual(self.node.deleted, [])


class RecoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vm = original(disk(2000, 1000, 0, "[ds1] app-01/a.vmdk"), powered_on=False)
        self.node = FakeNode(self.vm)
        self.node.folders["[ds1] app-01"] = ["a.vmdk"]

    def test_a_restore_that_stopped_while_writing_removes_its_temporary_vm(self) -> None:
        self.node.vms["vm-200"] = FakeVm("vm-200", "app-01-restoring-1a2b3c4d", [])

        outcome, settled, _completed = vcenter_inplace.recover(self.node, {"staging_vm_id": "vm-200"}, "app-01-restoring-1a2b3c4d")

        self.assertTrue(settled)
        self.assertIn("Removed the temporary VM app-01-restoring-1a2b3c4d", outcome or "")
        self.assertEqual(self.node.destroyed, ["app-01-restoring-1a2b3c4d"])

    def test_a_temporary_vm_whose_id_was_never_recorded_is_found_by_its_name(self) -> None:
        self.node.vms["vm-201"] = FakeVm("vm-201", "app-01-restoring-1a2b3c4d", [])

        outcome, _settled, _completed = vcenter_inplace.recover(self.node, {}, "app-01-restoring-1a2b3c4d")

        self.assertEqual(self.node.destroyed, ["app-01-restoring-1a2b3c4d"])
        self.assertIsNotNone(outcome)
        self.assertEqual(vcenter_inplace.recover(self.node, {}, "app-01-restoring-1a2b3c4d"), (None, True, False))

    def test_disks_written_before_the_temporary_vm_held_them_are_removed(self) -> None:
        outcome, settled, _completed = vcenter_inplace.recover(self.node, {"in_place_new_paths": ["[ds1] app-01/a-r1a2b3c4d.vmdk"]}, "app-01-restoring-1a2b3c4d")

        self.assertTrue(settled)
        self.assertEqual(self.node.deleted, ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual(outcome, "Removed the disks the interrupted restore had written; the VM itself was not touched")

    def test_a_disk_still_locked_by_the_cut_off_tool_is_removed_once_the_lock_goes(self) -> None:
        self.node.locked = {"[ds1] app-01/a-r1a2b3c4d.vmdk": 2}

        with patch.object(vcenter_inplace.time, "sleep") as pause, self.assertLogs(vcenter_inplace.logger, "WARNING"):
            outcome, _settled, _completed = vcenter_inplace.recover(self.node, {"in_place_new_paths": ["[ds1] app-01/a-r1a2b3c4d.vmdk"]}, "")

        self.assertEqual(self.node.deleted, ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual(pause.call_count, 2)
        self.assertNotIn("could not be removed", outcome or "")

    def test_a_disk_that_stays_locked_is_named_for_removal_by_hand(self) -> None:
        self.node.locked = {"[ds1] app-01/a-r1a2b3c4d.vmdk": 10**6}

        with (
            patch.object(vcenter_inplace, "RECOVERY_PATIENCE_SECONDS", 0),
            self.assertLogs(vcenter_inplace.logger, "WARNING"),
        ):
            outcome, _settled, _completed = vcenter_inplace.recover(self.node, {"in_place_new_paths": ["[ds1] app-01/a-r1a2b3c4d.vmdk"]}, "")

        self.assertIn("could not be removed; delete them by hand", outcome or "")

    def test_a_switch_that_never_happened_leaves_the_vm_as_it_was(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=False)

        outcome, settled, completed = vcenter_inplace.recover(self.node, {"swap_plan": plan}, "")

        self.assertEqual((settled, completed), (True, False))
        self.assertEqual(paths(self.vm), ["[ds1] app-01/a.vmdk"])
        self.assertEqual(self.node.deleted, ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertEqual(self.node.powered_on, ["app-01"])
        self.assertIn("was not changed and powered on again", outcome or "")

    def test_a_switch_cut_off_while_guarded_goes_back_to_the_guard(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=False)
        self.vm.CreateSnapshot_Task(name=GUARD, description="", memory=False, quiesce=False)
        changes, _messages = vcenter_inplace.switch_spec(self.vm, plan["disks"])
        self.vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=changes))

        outcome, settled, completed = vcenter_inplace.recover(self.node, {"swap_plan": plan}, "")

        self.assertEqual((settled, completed), (True, False))
        self.assertEqual(paths(self.vm), ["[ds1] app-01/a.vmdk"])
        self.assertEqual((self.vm.reverted, self.vm.items), ([GUARD], []))
        self.assertEqual(self.node.deleted, ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
        self.assertIn(f"is back as it was before the restore and powered on again: its snapshot {GUARD} was reverted and removed", outcome or "")

    def test_a_committed_switch_is_finished(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=False, was_on=False)
        changes, _messages = vcenter_inplace.switch_spec(self.vm, plan["disks"])
        self.vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=changes))

        for stored in ({**plan, "committed": True}, plan):
            self.node.deleted.clear()
            with self.subTest(committed=stored.get("committed", False)):
                outcome, settled, completed = vcenter_inplace.recover(self.node, {"swap_plan": stored}, "")

                self.assertEqual((settled, completed), (True, True))
                self.assertEqual(paths(self.vm), ["[ds1] app-01/a-r1a2b3c4d.vmdk"])
                self.assertEqual(self.node.deleted, ["[ds1] app-01/a.vmdk"])
                self.assertIn("so the restore was completed instead of rolled back", outcome or "")
                self.assertEqual(self.node.powered_on, [])

    def test_a_guard_that_cannot_be_reverted_keeps_the_plan(self) -> None:
        plan = plan_for(self.node, self.vm, staging_holds=False)
        self.vm.CreateSnapshot_Task(name=GUARD, description="", memory=False, quiesce=False)
        self.vm.fail_revert = RuntimeError("vCenter task failed: busy")

        with self.assertLogs(vcenter_inplace.logger, "ERROR"):
            outcome, settled, _completed = vcenter_inplace.recover(self.node, {"swap_plan": plan}, "")

        self.assertFalse(settled)
        self.assertIn("could not be taken back", outcome or "")
        self.assertEqual(self.node.deleted, [])


class ConfigTests(unittest.TestCase):
    def run_config(self, current: dict, wanted: dict, available: set[str]) -> tuple[list[str], list[str], list[dict]]:
        applied: list[dict] = []
        self.vm = FakeVm("vm-104", "app-01", [])
        node = SimpleNamespace(host="vc.example.test", apply_boot_order=lambda *args, **kwargs: [], _get_vm_by_moref=lambda vm_id: self.vm, _wait_for_task=lambda task: task)
        with (
            patch.object(vcenter_replica, "read_config", return_value=current),
            patch.object(vcenter_replica, "networks", return_value=available),
            patch.object(vcenter_replica, "apply_config", side_effect=lambda node, state, hardware, **plan: applied.append({"hardware": hardware, **plan}) or []),
        ):
            messages, warnings = vcenter_inplace.settle_config(node, "vm-104", "app-01", wanted, "p1")
        return messages, warnings, applied

    def test_adapters_are_paired_by_label_and_set_back_to_the_point(self) -> None:
        current = {
            "num_cpu": 4,
            "memory_mb": 4096,
            "firmware": "bios",
            "network_adapters": [
                {"id": "4000", "label": "Network adapter 1", "network": "Data", "connected": True},
                {"id": "4001", "label": "Network adapter 2", "network": "VM Network", "connected": True},
                {"id": "4002", "label": "Network adapter 3", "network": "VM Network", "connected": True},
            ],
        }
        wanted = {
            "num_cpu": 2,
            "memory_mb": 4096,
            "firmware": "bios",
            "network_adapters": [
                {"label": "Network adapter 1", "network": "VM Network", "connected": True},
                {"label": "Network adapter 2", "network": "VM Network", "connected": False},
            ],
        }

        messages, warnings, applied = self.run_config(current, wanted, {"VM Network"})

        [call] = applied
        self.assertEqual(call["hardware"], {"num_cpu": 2})
        self.assertEqual([item["id"] for item in call["change"]], ["4000", "4001"])
        self.assertEqual(call["remove"], ["4002"])
        self.assertEqual(call["add"], [])
        self.assertEqual(messages[0], "2 vCPU (was 4)")
        self.assertIn("Network adapter 3 on VM Network removed: restore point p1 has no such adapter", messages)
        self.assertEqual(warnings, [])

    def test_nothing_is_reconfigured_when_the_vm_matches_the_point(self) -> None:
        adapters = [{"id": "4000", "label": "Network adapter 1", "network": "VM Network", "connected": True}]
        current = {"num_cpu": 2, "memory_mb": 2048, "firmware": "bios", "network_adapters": adapters}
        wanted = {"num_cpu": 2, "memory_mb": 2048, "firmware": "bios", "network_adapters": [{"label": "Network adapter 1", "network": "VM Network"}]}

        messages, warnings, applied = self.run_config(current, wanted, {"VM Network"})

        self.assertEqual((messages, warnings, applied), ([], [], []))

    def test_a_missing_adapter_is_added_and_an_unknown_network_leaves_it_disconnected(self) -> None:
        current = {"num_cpu": 2, "memory_mb": 2048, "firmware": "efi", "secure_boot_enabled": True, "network_adapters": []}
        wanted = {
            "num_cpu": 2,
            "memory_mb": 2048,
            "firmware": "bios",
            "secure_boot_enabled": False,
            "network_adapters": [{"label": "Network adapter 1", "network": "Gone"}],
        }

        messages, warnings, applied = self.run_config(current, wanted, set())

        self.assertEqual(applied[0]["add"][0]["connected"], False)
        self.assertEqual(messages, ["Network adapter 1 added on no network, disconnected", "bios firmware (was efi)"])
        [spec] = self.vm.specs
        self.assertEqual((spec.firmware, spec.bootOptions.efiSecureBootEnabled), ("bios", False))
        self.assertEqual(len(warnings), 1)
        self.assertIn("is on Gone, which is not on host", warnings[0])

    def test_firmware_and_secure_boot_go_back_to_the_point(self) -> None:
        current = {"num_cpu": 2, "memory_mb": 2048, "firmware": "bios", "secure_boot_enabled": None, "network_adapters": []}
        wanted = {"num_cpu": 2, "memory_mb": 2048, "firmware": "efi", "secure_boot_enabled": True, "network_adapters": []}

        messages, warnings, applied = self.run_config(current, wanted, set())

        self.assertEqual(messages, ["efi firmware (was bios)", "Secure Boot on (was off)"])
        [spec] = self.vm.specs
        self.assertEqual((spec.firmware, spec.bootOptions.efiSecureBootEnabled), ("efi", True))
        self.assertEqual((warnings, applied), ([], []))


class ReplicaKeyTests(unittest.TestCase):
    def test_disks_added_on_two_controllers_get_their_own_keys(self) -> None:
        template = vim.vm.device.VirtualDisk(backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(diskMode="persistent"))
        first = vcenter_replica._addition(template, 1000, 1, "[ds] r/a.vmdk", -101)
        second = vcenter_replica._addition(template, 1001, 0, "[ds] r/b.vmdk", -102)

        self.assertNotEqual(first.device.key, second.device.key)


if __name__ == "__main__":
    unittest.main()


class CompletedAfterInterruptionTests(unittest.TestCase):
    def test_a_completed_switch_gets_the_points_configuration_and_power(self) -> None:
        import json
        import tempfile

        from python.srv.core.mongo_db_wrapper import TASKS
        from python.srv.plugins.vcenter import vcenter_restore

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as manifest:
            json.dump({"vm_config": {"num_cpu": 2}}, manifest)
        TASKS["task-cut"] = {
            "id": "task-cut",
            "task_type": "restore",
            "status": "failed",
            "parameters": {"restore_mode": "in_place", "target_vm_name": "app-01", "point_id": "p1", "manifest_path": manifest.name, "power_on": True},
            "result": {"swap_plan": {"vm_id": "vm-104", "name": "app-01"}, "staging_vm_id": "vm-200"},
            "logs": [],
        }
        self.addCleanup(TASKS.pop, "task-cut", None)
        node = FakeNode(original())
        with (
            patch.object(vcenter_inplace, "recover", return_value=("VM app-01 ... so the restore was completed instead of rolled back", True, True)),
            patch.object(vcenter_inplace, "settle_config", return_value=(["2 vCPU (was 4)"], ["VM app-01 keeps its efi firmware; restore point p1 has bios"])) as settle,
        ):
            outcome = vcenter_restore._settle_leftover(node, TASKS["task-cut"])

        settle.assert_called_once_with(node, "vm-104", "app-01", {"num_cpu": 2}, "p1")
        self.assertEqual(node.powered_on, ["app-01"])
        self.assertIn("app-01 set back to restore point p1: 2 vCPU (was 4); VM app-01 keeps its efi firmware", outcome or "")
        self.assertIn("app-01 was powered on", outcome or "")
        self.assertEqual((TASKS["task-cut"]["result"]["swap_plan"], TASKS["task-cut"]["result"]["staging_vm_id"]), ({}, ""))
        self.assertTrue(TASKS["task-cut"]["logs"][-1]["message"].startswith("After the interruption: VM app-01"))
