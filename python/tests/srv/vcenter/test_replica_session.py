# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyVmomi import vim, vmodl

from python.srv.plugins.vcenter import vcenter_replica


class FakeSnapshotTree:
    def __init__(self, name: str) -> None:
        self.name = name
        self.snapshot = SimpleNamespace(name=name)
        self.childSnapshotList: list[Any] = []


class FakeVm:
    def __init__(self) -> None:
        self._moId = "vm-r"
        self.name = "replica"
        self.snapshot = None
        self.created: list[str] = []

    def CreateSnapshot_Task(self, *, name: str, description: str, memory: bool, quiesce: bool) -> str:
        self.created.append(name)
        self.snapshot = SimpleNamespace(rootSnapshotList=[FakeSnapshotTree(name)])
        return "task"


class RemovingSnapshot:
    def __init__(self, vm: FakeVm, name: str, *, already_gone: bool) -> None:
        self.vm = vm
        self.name = name
        self.already_gone = already_gone

    def RemoveSnapshot_Task(self, removeChildren: bool) -> str:
        if self.already_gone:
            self.vm.snapshot = None
            return "failing task"
        return "failing task"


class ExpiringNode:
    def __init__(self, expirations: int) -> None:
        self.host = "vc.example.test"
        self.expirations = expirations
        self.vm = FakeVm()
        self.reauthenticated = 0

    def _get_vm_by_moref(self, vm_id: str) -> Any:
        if self.expirations:
            self.expirations -= 1
            raise vim.fault.NotAuthenticated()
        return self.vm

    def reauthenticate(self) -> None:
        self.reauthenticated += 1

    def _wait_for_task(self, task: Any) -> None:
        pass


STATE = {"vm_id": "vm-r", "name": "replica"}
SNAPPED = {"vm_id": "vm-r", "name": "replica", "disks": [{"parent_path": "[ds] r/01.vmdk"}]}


class ExpiredSessionTests(unittest.TestCase):
    def test_an_expired_session_is_signed_in_again_and_the_call_repeated(self) -> None:
        node = ExpiringNode(expirations=1)

        with patch.object(vcenter_replica, "find_replica", return_value=SNAPPED), self.assertLogs(vcenter_replica.logger, "WARNING"):
            vcenter_replica.create_snapshot(node, STATE, "abr-replica-1", 60)

        self.assertEqual(node.reauthenticated, 1)
        self.assertEqual(node.vm.created, ["abr-replica-1"])

    def test_a_snapshot_already_taken_before_the_session_expired_is_not_taken_twice(self) -> None:
        node = ExpiringNode(expirations=0)
        node.vm.snapshot = SimpleNamespace(rootSnapshotList=[FakeSnapshotTree("abr-replica-1")])

        with patch.object(vcenter_replica, "find_replica", return_value=SNAPPED):
            vcenter_replica.create_snapshot(node, STATE, "abr-replica-1", 60)

        self.assertEqual(node.vm.created, [])

    def test_two_expirations_in_a_row_are_reported(self) -> None:
        node = ExpiringNode(expirations=2)

        with self.assertRaises(vim.fault.NotAuthenticated):
            vcenter_replica.remove_snapshot(node, STATE, "abr-replica-1", 60)
        self.assertEqual(node.reauthenticated, 1)



class RemovalAlreadyDoneTests(unittest.TestCase):
    def _node(self, *, gone_after_failure: bool) -> ExpiringNode:
        node = ExpiringNode(expirations=0)
        tree = FakeSnapshotTree("abr-replica-1")
        tree.snapshot = RemovingSnapshot(node.vm, "abr-replica-1", already_gone=gone_after_failure)
        node.vm.snapshot = SimpleNamespace(rootSnapshotList=[tree])
        node.vm.runtime = SimpleNamespace(consolidationNeeded=False)

        def wait(task: Any) -> None:
            raise RuntimeError("vCenter task failed: The object 'vim.vm.Snapshot:snapshot-1' has already been deleted")

        node._wait_for_task = wait
        return node

    def test_a_snapshot_an_earlier_attempt_already_removed_counts_as_removed(self) -> None:
        vcenter_replica.remove_snapshot(self._node(gone_after_failure=True), STATE, "abr-replica-1", 60)

    def test_a_failed_removal_of_a_snapshot_that_is_still_there_is_reported(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "has already been deleted"):
            vcenter_replica.remove_snapshot(self._node(gone_after_failure=False), STATE, "abr-replica-1", 60)


class EventManager:
    def __init__(self, *, implemented: bool, events: list[Any]) -> None:
        self.implemented = implemented
        self.events = events
        self.collectors: list[Any] = []

    def QueryEvents(self, spec: Any) -> list[Any]:
        if not self.implemented:
            raise vmodl.fault.NotImplemented()
        return self.events

    def CreateCollectorForEvents(self, spec: Any) -> Any:
        collector = SimpleNamespace(latestPage=self.events, destroyed=False, spec=spec)
        collector.DestroyCollector = lambda: setattr(collector, "destroyed", True)
        self.collectors.append(collector)
        return collector


class Browser:
    def __init__(self, logs: list[str]) -> None:
        self.logs = logs
        self.searches: list[tuple[str, list[str]]] = []

    def SearchDatastore_Task(self, *, datastorePath: str, searchSpec: Any) -> Any:
        self.searches.append((datastorePath, list(searchSpec.matchPattern)))
        files = [SimpleNamespace(path=name) for name in self.logs]
        return SimpleNamespace(info=SimpleNamespace(result=SimpleNamespace(file=files)))


def _logged_vm(logs: list[str], *, folder: str = "[ds-01] replica", datastore: str = "ds-01") -> Any:
    store = SimpleNamespace(name=datastore, browser=Browser(logs))
    files = SimpleNamespace(logDirectory=folder, vmPathName=f"{folder}/replica.vmx")
    return SimpleNamespace(name="replica", config=SimpleNamespace(files=files), datastore=[store])


class PoweredOnSinceTests(unittest.TestCase):
    SINCE = "2026-11-08T22:00:00+00:00"

    def _probe(self, manager: EventManager, *, logs: bool) -> tuple[bool, list[Any]]:
        node = SimpleNamespace(si=SimpleNamespace(content=SimpleNamespace(eventManager=manager)))
        checked: list[Any] = []

        def has_run_logs(_node: Any, vm: Any) -> bool:
            checked.append(vm)
            return logs

        with patch.object(vcenter_replica, "_vm", return_value=vim.VirtualMachine("vm-7")), patch.object(vcenter_replica, "_has_run_logs", has_run_logs):
            return vcenter_replica.powered_on_since(node, STATE, self.SINCE), checked

    def test_vcenter_answers_from_its_events_alone(self) -> None:
        self.assertEqual(self._probe(EventManager(implemented=True, events=[object()]), logs=False), (True, []))
        self.assertEqual(self._probe(EventManager(implemented=True, events=[]), logs=True), (False, []))

    def test_a_standalone_host_finds_the_log_a_power_on_writes(self) -> None:
        manager = EventManager(implemented=False, events=[])

        powered_on, checked = self._probe(manager, logs=True)

        self.assertTrue(powered_on)
        self.assertEqual([vm._moId for vm in checked], ["vm-7"])

    def test_a_standalone_host_whose_folder_has_no_log_asks_its_events_too(self) -> None:
        manager = EventManager(implemented=False, events=[object()])

        self.assertTrue(self._probe(manager, logs=False)[0])
        self.assertTrue(manager.collectors[0].destroyed)
        self.assertEqual(manager.collectors[0].spec.eventTypeId, ["VmPoweredOnEvent", "VmStartingEvent"])

    def test_a_replica_never_powered_on_on_a_standalone_host_lets_the_run_go_on(self) -> None:
        manager = EventManager(implemented=False, events=[])

        self.assertFalse(self._probe(manager, logs=False)[0])
        self.assertTrue(manager.collectors[0].destroyed)


class RunLogsTests(unittest.TestCase):
    NODE = SimpleNamespace(_wait_for_task=lambda task: None)

    def test_a_log_in_the_folder_means_the_vm_ran(self) -> None:
        vm = _logged_vm(["vmware.log"])

        self.assertTrue(vcenter_replica._has_run_logs(self.NODE, vm))
        self.assertEqual(vm.datastore[0].browser.searches, [("[ds-01] replica", ["vmware*.log"])])

    def test_a_folder_without_logs_means_it_never_ran(self) -> None:
        self.assertFalse(vcenter_replica._has_run_logs(self.NODE, _logged_vm([])))

    def test_the_folder_is_found_from_the_configuration_file_without_a_log_directory(self) -> None:
        vm = _logged_vm(["vmware-1.log"])
        vm.config.files.logDirectory = None

        self.assertTrue(vcenter_replica._has_run_logs(self.NODE, vm))
        self.assertEqual(vm.datastore[0].browser.searches[0][0], "[ds-01] replica")

    def test_a_folder_on_no_known_datastore_fails_rather_than_passing(self) -> None:
        vm = _logged_vm(["vmware.log"], datastore="ds-02")

        with self.assertRaisesRegex(vcenter_replica.VCenterReplicaError, "Cannot check whether replica was powered on"):
            vcenter_replica._has_run_logs(self.NODE, vm)

def _disk(path: str, uuid: str, size: int = 1 << 30, unit: int = 0) -> Any:
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=path, uuid=uuid, diskMode="persistent")
    return vim.vm.device.VirtualDisk(key=2000 + unit, controllerKey=1000, unitNumber=unit, capacityInBytes=size, backing=backing)


def _vm_with(*disks: Any, change_version: str = "cv-1", cpus: int = 1) -> Any:
    hardware = SimpleNamespace(device=list(disks), numCPU=cpus)
    return SimpleNamespace(config=SimpleNamespace(hardware=hardware, changeVersion=change_version))


class FingerprintTests(unittest.TestCase):
    BASE = "6000c29a-0000-0000-0000-000000000001"

    def _print(self, *disks: Any, **config: Any) -> str:
        return vcenter_replica._fingerprint(_vm_with(*disks, **config))

    def test_a_configuration_change_alone_keeps_the_fingerprint(self) -> None:
        before = self._print(_disk("[ds-01] r/r.vmdk", self.BASE))

        self.assertEqual(self._print(_disk("[ds-01] r/r.vmdk", self.BASE), change_version="cv-9", cpus=4), before)

    def test_a_move_to_another_datastore_keeps_the_fingerprint(self) -> None:
        before = self._print(_disk("[ds-01] r/r.vmdk", self.BASE))

        self.assertEqual(self._print(_disk("[ds-02] r/r.vmdk", self.BASE)), before)

    def test_a_replaced_or_resized_disk_changes_the_fingerprint(self) -> None:
        before = self._print(_disk("[ds-01] r/r.vmdk", self.BASE))

        self.assertNotEqual(self._print(_disk("[ds-01] r/r.vmdk", "6000c29a-0000-0000-0000-000000000002")), before)
        self.assertNotEqual(self._print(_disk("[ds-01] r/r.vmdk", self.BASE, size=2 << 30)), before)

    def test_a_disk_without_a_uuid_is_known_by_its_file_name(self) -> None:
        self.assertEqual(self._print(_disk("[ds-01] r/r.vmdk", "")), "disks:1000:0=r.vmdk:1073741824")

    def test_the_replica_state_carries_the_disk_fingerprint_not_the_change_version(self) -> None:
        vm = _vm_with(_disk("[ds-01] r/r.vmdk", self.BASE), change_version="cv-7")
        vm._moId = "vm-9"
        vm.name = "replica"
        vm.snapshot = None
        vm.recentTask = []
        vm.config.annotation = ""
        vm.runtime = SimpleNamespace(host=SimpleNamespace(name="esx-1"), powerState=vim.VirtualMachinePowerState.poweredOff, consolidationNeeded=False)

        state = vcenter_replica._state(vm)

        self.assertEqual(state["fingerprint"], f"disks:1000:0={self.BASE}:1073741824")

    def test_the_disks_are_listed_in_a_stable_order(self) -> None:
        first, second = _disk("[ds] r/a.vmdk", "u-a", unit=0), _disk("[ds] r/b.vmdk", "u-b", unit=1)

        self.assertEqual(self._print(second, first), self._print(first, second))


REBUILD_SETTINGS = {"target_host": "esx-dr-01", "target_datastore": "ds-01"}


class RebuildLocationTests(unittest.TestCase):

    def test_the_rebuild_goes_to_the_host_and_datastore_the_replica_is_on_now(self) -> None:
        state = {"host": "esx-dr-02", "disks": [{"path": "[ds-02] replica/01.vmdk"}]}

        self.assertEqual(vcenter_replica.rebuild_location(state, REBUILD_SETTINGS), ("esx-dr-02", "ds-02"))

    def test_a_replica_without_disks_or_host_falls_back_to_the_job_settings(self) -> None:
        self.assertEqual(vcenter_replica.rebuild_location({"host": "", "disks": []}, REBUILD_SETTINGS), ("esx-dr-01", "ds-01"))


if __name__ == "__main__":
    unittest.main()
