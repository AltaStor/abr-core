# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import types
from contextlib import contextmanager
from typing import Any
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

try:
    from python.srv.plugins.vcenter.vcenter import VCenterNode, _normalize_disk_uuid
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.plugins.vcenter.vcenter import VCenterNode, _normalize_disk_uuid

from pyVmomi import vim, vmodl


class _FakeContainer(vim.view.ContainerView):
    """A real ContainerView MoRef, so the property-collector specs accept it."""

    @property
    def view(self) -> list:
        return self._rows

    def Destroy(self) -> None:
        return None


class _FakeViewManager:
    def __init__(self, view: list[object]) -> None:
        self._view = view
        self.created = 0

    def CreateContainerView(self, root: object, types: object, recursive: bool) -> _FakeContainer:
        self.created += 1
        container = _FakeContainer("session[fake]view-1")
        object.__setattr__(container, "_rows", list(self._view))
        return container


class _FakePropertyCollector:
    """Stands in for the real collector: one call, names for the whole view.

    An object whose name cannot be read comes back without a propSet, the way
    vCenter reports one that was destroyed or is still being created.
    """

    def __init__(self, view: list[object]) -> None:
        self._view = view
        self.calls = 0

    def RetrievePropertiesEx(self, filter_specs: object, options: object) -> object:
        self.calls += 1
        entries = []
        for member in self._view:
            name = getattr(member, "_collector_name", _MISSING)
            if name is _MISSING:
                try:
                    name = member.name
                except Exception:
                    entries.append(types.SimpleNamespace(obj=member, propSet=[]))
                    continue
            if name is None:
                entries.append(types.SimpleNamespace(obj=member, propSet=[]))
                continue
            entries.append(
                types.SimpleNamespace(
                    obj=member, propSet=[types.SimpleNamespace(name="name", val=name)]
                )
            )
        return types.SimpleNamespace(objects=entries, token=None)

    def ContinueRetrievePropertiesEx(self, token: object) -> object:
        return None


_MISSING = object()


class _FakeContent:
    def __init__(self, view: list[object]) -> None:
        self.rootFolder = object()
        self.viewManager = _FakeViewManager(view)
        self.propertyCollector = _FakePropertyCollector(view)


class _FakeServiceInstance:
    def __init__(self, view: list[object]) -> None:
        self._view = view

    def RetrieveContent(self) -> _FakeContent:
        return _FakeContent(self._view)


def _host_in_cluster(name: str = "esx-clustered.local") -> object:
    # vim.ClusterComputeResource(moId) is a bare MoRef but still passes the
    # isinstance() check the production code relies on.
    return types.SimpleNamespace(name=name, parent=vim.ClusterComputeResource("domain-c7"))


def _host_standalone(name: str = "esx-standalone.local") -> object:
    return types.SimpleNamespace(name=name, parent=vim.ComputeResource("domain-s9"))


class _FakeVm:
    def __init__(self, *, name: str, moid: str, connection_state: str = "orphaned", power_state: str = "poweredOff") -> None:
        self.name = name
        self._moId = moid
        self.runtime = types.SimpleNamespace(
            powerState=power_state,
            connectionState=connection_state,
        )
        self.unregister_calls = 0
        self.destroy_calls = 0
        self.power_off_calls = 0
        self.power_on_calls = 0
        self.rename_calls: list[str] = []

    def Destroy_Task(self) -> object:
        self.destroy_calls += 1
        return object()

    def Unregister(self) -> None:
        self.unregister_calls += 1

    def PowerOffVM_Task(self) -> object:
        self.power_off_calls += 1
        self.runtime.powerState = vim.VirtualMachinePowerState.poweredOff
        return _FakeTask()

    def PowerOnVM_Task(self) -> object:
        self.power_on_calls += 1
        self.runtime.powerState = vim.VirtualMachinePowerState.poweredOn
        return _FakeTask()

    def Rename_Task(self, newName: str) -> object:
        self.rename_calls.append(newName)
        self.name = newName
        return _FakeTask()


class _FakeTask:
    info = types.SimpleNamespace(state="success", result=None)


class _FakeDisk:
    def __init__(self, controller_key: int, unit_number: int) -> None:
        self.controllerKey = controller_key
        self.unitNumber = unit_number
        self.backing = types.SimpleNamespace(fileName="[datastore1] demo-vm/demo-vm.vmdk")


class _FakeController:
    def __init__(self, key: int, bus_number: int) -> None:
        self.key = key
        self.busNumber = bus_number


class _FakeOption:
    def __init__(self, key: str, value: object) -> None:
        self.key = key
        self.value = value


class _FakeCbtVm:
    def __init__(self, *, change_tracking_enabled: bool, extra_config: list[object]) -> None:
        self.name = "demo-vm"
        self._moId = "vm-201"
        self.config = types.SimpleNamespace(
            changeTrackingEnabled=change_tracking_enabled,
            extraConfig=extra_config,
            hardware=types.SimpleNamespace(
                device=[
                    _FakeController(1000, 0),
                    _FakeDisk(1000, 0),
                ]
            ),
        )
        self.reconfig_specs: list[object] = []

    def ReconfigVM_Task(self, spec: object) -> object:
        self.reconfig_specs.append(spec)
        return _FakeTask()


def _fake_restore_metadata_vm(
    *,
    firmware: str,
    devices: list[object] | None = None,
    secure_boot_enabled: bool | None = None,
) -> object:
    boot_options = (
        types.SimpleNamespace(efiSecureBootEnabled=secure_boot_enabled)
        if secure_boot_enabled is not None
        else None
    )
    return types.SimpleNamespace(
        summary=types.SimpleNamespace(
            config=types.SimpleNamespace(
                name="demo-vm",
                guestFullName="Ubuntu Linux (64-bit)",
                numCpu=2,
                memorySizeMB=4096,
            ),
            runtime=types.SimpleNamespace(
                powerState="poweredOff",
                host=types.SimpleNamespace(name="esx-src.local"),
            ),
        ),
        config=types.SimpleNamespace(
            instanceUuid="instance-uuid-1",
            guestId="ubuntu64Guest",
            firmware=firmware,
            hardware=types.SimpleNamespace(device=devices or []),
            bootOptions=boot_options,
        ),
    )


class _FakeVmFolder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def CreateVM_Task(self, *, config: object, pool: object, host: object) -> object:
        self.calls.append({"config": config, "pool": pool, "host": host})
        return object()


def _fake_vm_create_context(
    *,
    resulting_firmware: str,
) -> tuple[object, object, _FakeVmFolder, object]:
    vm_folder = _FakeVmFolder()
    datacenter = types.SimpleNamespace(vmFolder=vm_folder)
    host = types.SimpleNamespace(
        name="esx-01.local",
        datastore=[types.SimpleNamespace(name="ds-01")],
        parent=types.SimpleNamespace(resourcePool=object()),
    )
    created_vm = types.SimpleNamespace(
        _moId="vm-restore-001",
        config=types.SimpleNamespace(firmware=resulting_firmware),
    )
    return host, datacenter, vm_folder, created_vm


class VCenterNodeHostConnectionTests(unittest.TestCase):
    def test_ensure_vm_host_connected_raises_actionable_error_when_host_disconnected(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = types.SimpleNamespace(
            runtime=types.SimpleNamespace(
                host=types.SimpleNamespace(
                    name="esx-01.local",
                    runtime=types.SimpleNamespace(connectionState="disconnected"),
                )
            )
        )
        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError) as ctx:
            node.ensure_vm_host_connected("demo-vm")

        self.assertIn("esx-01.local", str(ctx.exception))
        self.assertIn("disconnected", str(ctx.exception))

    def test_ensure_vm_host_connected_is_noop_when_host_is_connected(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = types.SimpleNamespace(
            runtime=types.SimpleNamespace(
                host=types.SimpleNamespace(
                    name="esx-01.local",
                    runtime=types.SimpleNamespace(connectionState="connected"),
                )
            )
        )
        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        node.ensure_vm_host_connected("demo-vm")

    def test_ensure_vm_host_connected_is_noop_when_host_cannot_be_determined(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = types.SimpleNamespace(runtime=types.SimpleNamespace(host=None))
        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        node.ensure_vm_host_connected("demo-vm")


class VCenterNodeWaitForTaskTests(unittest.TestCase):
    def test_wait_for_task_prefers_localized_message(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        error = types.SimpleNamespace(localizedMessage="Nice readable message", msg="raw msg")
        task = types.SimpleNamespace(info=types.SimpleNamespace(state=vim.TaskInfo.State.error, error=error))

        with self.assertRaises(RuntimeError) as ctx:
            node._wait_for_task(task)

        self.assertEqual(str(ctx.exception), "vCenter task failed: Nice readable message")

    def test_wait_for_task_falls_back_to_fault_msg_instead_of_raw_repr(self) -> None:
        # Mirrors vim.fault.HostNotConnected raised when a VM's ESXi host has
        # dropped its connection to vCenter: no localizedMessage, only msg.
        # Before the _fault_message fallback chain, this leaked as the fault's
        # full "(vmodl.fault.HostNotConnected) { dynamicType = ... }" repr.
        node = VCenterNode("vc.local", "administrator", "secret")
        error = vmodl.fault.HostNotConnected()
        error.msg = "Unable to communicate with the remote host, since it is disconnected."
        task = types.SimpleNamespace(info=types.SimpleNamespace(state=vim.TaskInfo.State.error, error=error))

        with self.assertRaises(RuntimeError) as ctx:
            node._wait_for_task(task)

        self.assertEqual(
            str(ctx.exception),
            "vCenter task failed: Unable to communicate with the remote host, since it is disconnected.",
        )


class VCenterNodeDestroyVmTests(unittest.TestCase):
    def test_destroy_vm_falls_back_to_unregister_for_orphaned_vcenter_vm(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeVm(name="demo-vm", moid="vm-101", connection_state="orphaned")

        node.get_service_type = lambda: "VirtualCenter"  # type: ignore[method-assign]
        node._get_vm_by_moref = lambda vm_moref: fake_vm if vm_moref == "vm-101" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: (_ for _ in ()).throw(  # type: ignore[method-assign]
            RuntimeError("vCenter task failed: File not found")
        )

        node.destroy_vm("demo-vm", vm_moref="vm-101")

        self.assertEqual(fake_vm.unregister_calls, 1)

    def test_destroy_vm_is_idempotent_when_vm_is_already_absent(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")

        node.get_service_type = lambda: "VirtualCenter"  # type: ignore[method-assign]
        node._get_vm_by_moref = lambda vm_moref: None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]

        node.destroy_vm("missing-vm", vm_moref="vm-404")


class VCenterNodeDestroyByMorefTests(unittest.TestCase):
    def test_a_moref_that_is_gone_never_falls_back_to_a_vm_of_the_same_name(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        stranger = _FakeVm(name="existing-vm", moid="vm-999")
        lookups: list[str] = []

        node.get_service_type = lambda: "VirtualCenter"  # type: ignore[method-assign]
        node._get_vm_by_moref = lambda vm_moref: None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: lookups.append(vm_name) or stranger  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        node.destroy_vm("existing-vm", vm_moref="vm-restore-001")

        self.assertEqual(lookups, [])
        self.assertEqual(stranger.destroy_calls, 0)

    def test_without_a_moref_the_vm_is_still_found_by_name(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        original = _FakeVm(name="demo-vm", moid="vm-101")

        node.get_service_type = lambda: "VirtualCenter"  # type: ignore[method-assign]
        node._get_vm_by_moref = lambda vm_moref: None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: original if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        node.destroy_vm("demo-vm")

        self.assertEqual(original.destroy_calls, 1)


class VCenterNodePowerOffVmTests(unittest.TestCase):
    def test_power_off_vm_issues_task_when_running(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeVm(name="demo-vm", moid="vm-101", power_state="poweredOn")
        node._get_vm_by_moref = lambda vm_moref: fake_vm if vm_moref == "vm-101" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        result = node.power_off_vm("demo-vm", vm_moref="vm-101")

        self.assertEqual(fake_vm.power_off_calls, 1)
        self.assertEqual(result, {"vm_name": "demo-vm", "vm_moref": "vm-101", "power_state": "poweredOff", "changed": True})

    def test_power_off_vm_is_noop_when_already_off(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeVm(name="demo-vm", moid="vm-101", power_state="poweredOff")
        node._get_vm_by_moref = lambda vm_moref: fake_vm if vm_moref == "vm-101" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]

        result = node.power_off_vm("demo-vm", vm_moref="vm-101")

        self.assertEqual(fake_vm.power_off_calls, 0)
        self.assertEqual(result["changed"], False)

    def test_power_off_vm_raises_when_vm_not_found(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node._get_vm_by_moref = lambda vm_moref: None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]

        with self.assertRaises(ValueError):
            node.power_off_vm("missing-vm")


class VCenterNodeRenameVmTests(unittest.TestCase):
    def test_rename_vm_issues_task_and_returns_new_name(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeVm(name="demo-vm-restoring-abc123", moid="vm-101")
        node._get_vm_by_moref = lambda vm_moref: fake_vm if vm_moref == "vm-101" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        result = node.rename_vm("demo-vm-restoring-abc123", "demo-vm", vm_moref="vm-101")

        self.assertEqual(fake_vm.rename_calls, ["demo-vm"])
        self.assertEqual(
            result,
            {"vm_name": "demo-vm", "vm_moref": "vm-101", "previous_name": "demo-vm-restoring-abc123", "changed": True},
        )

    def test_rename_vm_raises_when_vm_not_found(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node._get_vm_by_moref = lambda vm_moref: None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]

        with self.assertRaises(ValueError):
            node.rename_vm("missing-vm", "new-name")


class VCenterNodeCbtTests(unittest.TestCase):
    def test_ensure_cbt_enabled_repairs_missing_per_disk_flags(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeCbtVm(
            change_tracking_enabled=True,
            extra_config=[_FakeOption("ctkEnabled", "TRUE")],
        )

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        changed = node.ensure_cbt_enabled("demo-vm")

        self.assertTrue(changed)
        self.assertEqual(len(fake_vm.reconfig_specs), 1)
        spec = fake_vm.reconfig_specs[0]
        self.assertTrue(spec.changeTrackingEnabled)
        keys = [getattr(item, "key", None) for item in (spec.extraConfig or [])]
        self.assertIn("ctkEnabled", keys)
        self.assertIn("scsi0:0.ctkEnabled", keys)

    def test_ensure_cbt_enabled_is_noop_when_global_and_disk_flags_exist(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeCbtVm(
            change_tracking_enabled=True,
            extra_config=[
                _FakeOption("ctkEnabled", "TRUE"),
                _FakeOption("scsi0:0.ctkEnabled", "true"),
            ],
        )

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        changed = node.ensure_cbt_enabled("demo-vm")

        self.assertFalse(changed)
        self.assertEqual(fake_vm.reconfig_specs, [])


_CBT_CORRUPTION_ERROR = (
    "vCenter task failed: (vim.fault.GenericVmConfigFault) { msg = 'An error "
    "occurred while taking a snapshot: Invalid change tracker error code.' }"
)


class _FakeSnapshotTask:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeReconfigTask:
    pass


class _FakeSnapshotVm:
    def __init__(self) -> None:
        self.name = "demo-vm"
        self._moId = "vm-301"
        self.created_snapshots: list[str] = []
        self.reconfig_specs: list[object] = []

    def CreateSnapshot_Task(self, *, name: str, description: str, memory: bool, quiesce: bool) -> object:
        self.created_snapshots.append(name)
        return _FakeSnapshotTask(name)

    def ReconfigVM_Task(self, spec: object) -> object:
        self.reconfig_specs.append(spec)
        return _FakeReconfigTask()


class VCenterNodeCbtResetTests(unittest.TestCase):
    def test_create_backup_snapshot_resets_cbt_and_retries_once_on_corruption(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeSnapshotVm()
        removed_snapshots: list[str] = []
        cbt_reenabled: list[str] = []

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        node.remove_snapshot = lambda vm_name, snapshot_name: removed_snapshots.append(snapshot_name)  # type: ignore[method-assign]
        node.ensure_cbt_enabled = lambda vm_name: cbt_reenabled.append(vm_name) or False  # type: ignore[method-assign]

        # Only the very first CreateSnapshot_Task (the real backup snapshot)
        # fails; the CBT-reset flush snapshot and the retried backup
        # snapshot both succeed.
        first_attempt = {"done": False}

        def fake_wait_for_task(task: object) -> object:
            if isinstance(task, _FakeSnapshotTask) and task.name.startswith("FullBackup_") and not first_attempt["done"]:
                first_attempt["done"] = True
                raise RuntimeError(_CBT_CORRUPTION_ERROR)
            return None

        node._wait_for_task = fake_wait_for_task  # type: ignore[method-assign]

        snapshot_name = node.create_backup_snapshot("demo-vm")

        self.assertTrue(snapshot_name.startswith("FullBackup_"))
        # First (failed) backup snapshot + flush snapshot + retried backup snapshot.
        self.assertEqual(len(fake_vm.created_snapshots), 3)
        self.assertTrue(fake_vm.created_snapshots[1].startswith("CBTReset_"))
        self.assertEqual(len(fake_vm.reconfig_specs), 1)
        self.assertFalse(fake_vm.reconfig_specs[0].changeTrackingEnabled)
        self.assertEqual(removed_snapshots, [fake_vm.created_snapshots[1]])
        self.assertEqual(cbt_reenabled, ["demo-vm"])

    def test_create_backup_snapshot_raises_actionable_error_when_reset_does_not_help(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeSnapshotVm()

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        node.remove_snapshot = lambda vm_name, snapshot_name: None  # type: ignore[method-assign]
        node.ensure_cbt_enabled = lambda vm_name: False  # type: ignore[method-assign]

        def fake_wait_for_task(task: object) -> object:
            if isinstance(task, _FakeSnapshotTask) and task.name.startswith("FullBackup_"):
                raise RuntimeError(_CBT_CORRUPTION_ERROR)
            return None

        node._wait_for_task = fake_wait_for_task  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError) as ctx:
            node.create_backup_snapshot("demo-vm")

        self.assertIn("power cycle", str(ctx.exception))
        self.assertIn("KB 2048201", str(ctx.exception))
        # Exactly two backup-snapshot attempts: the original and the single retry.
        backup_attempts = [n for n in fake_vm.created_snapshots if n.startswith("FullBackup_")]
        self.assertEqual(len(backup_attempts), 2)


_QUIESCE_FAULT_TEXT = (
    "vCenter task failed: An error occurred while quiescing the virtual machine. "
    "See the virtual machine's event log for details."
)
_GUEST_REASON = (
    "The guest OS has reported an error during quiescing. The error code was: 2 "
    "The error message was: Custom quiesce script failed."
)


class VCenterNodeSnapshotNameTests(unittest.TestCase):
    def test_a_given_name_is_used_for_the_snapshot_and_for_its_cbt_retry(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeSnapshotVm()
        node._get_vm = lambda vm_name: fake_vm  # type: ignore[method-assign]
        node.remove_snapshot = lambda vm_name, snapshot_name: None  # type: ignore[method-assign]
        node.ensure_cbt_enabled = lambda vm_name: False  # type: ignore[method-assign]
        failed = {"done": False}

        def fake_wait_for_task(task: object) -> object:
            if isinstance(task, _FakeSnapshotTask) and task.name == "FullBackup_42" and not failed["done"]:
                failed["done"] = True
                raise RuntimeError(_CBT_CORRUPTION_ERROR)
            return None

        node._wait_for_task = fake_wait_for_task  # type: ignore[method-assign]

        self.assertEqual(node.create_backup_snapshot("demo-vm", snapshot_name="FullBackup_42"), "FullBackup_42")
        self.assertEqual([fake_vm.created_snapshots[0], fake_vm.created_snapshots[2]], ["FullBackup_42", "FullBackup_42"])

    def test_without_a_name_the_snapshot_gets_a_new_backup_name(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _FakeSnapshotVm()
        node._get_vm = lambda vm_name: fake_vm  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        with patch("python.srv.plugins.vcenter.vcenter.time.time", return_value=1794386036.7):
            self.assertEqual(node.create_backup_snapshot("demo-vm"), "FullBackup_1794386036")


class _RecentTask:
    def __init__(self, description_id: str, states: list[object]) -> None:
        self._states = states
        self._description_id = description_id

    @property
    def info(self) -> object:
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return types.SimpleNamespace(descriptionId=self._description_id, state=state)


class VCenterNodeSnapshotCreationWaitTests(unittest.TestCase):
    def _node(self, tasks: list[_RecentTask]) -> VCenterNode:
        node = VCenterNode("vc.local", "administrator", "secret")
        vm = types.SimpleNamespace(recentTask=tasks)
        node._get_vm = lambda vm_name: vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]
        return node

    def test_it_returns_once_vcenter_has_finished_creating_the_snapshot(self) -> None:
        running, done = vim.TaskInfo.State.running, vim.TaskInfo.State.success
        task = _RecentTask("VirtualMachine.createSnapshot", [running, running, done])
        node = self._node([task])

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep") as sleep:
            node.wait_for_snapshot_creation("demo-vm")

        self.assertEqual(sleep.call_count, 2)

    def test_other_tasks_on_the_vm_are_not_waited_for(self) -> None:
        node = self._node([_RecentTask("VirtualMachine.reconfigure", [vim.TaskInfo.State.running])])

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep") as sleep:
            node.wait_for_snapshot_creation("demo-vm")

        sleep.assert_not_called()

    def test_a_creation_that_never_finishes_is_given_up_on(self) -> None:
        node = self._node([_RecentTask("VirtualMachine.createSnapshot", [vim.TaskInfo.State.queued])])

        with (
            patch("python.srv.plugins.vcenter.vcenter.time.sleep"),
            patch("python.srv.plugins.vcenter.vcenter.env_int", return_value=0),
            self.assertRaisesRegex(RuntimeError, "still creating a snapshot of VM 'demo-vm'"),
        ):
            node.wait_for_snapshot_creation("demo-vm")

    def test_a_vm_that_is_gone_has_nothing_to_wait_for(self) -> None:
        self._node([]).wait_for_snapshot_creation("other-vm")


class _FailingSnapshotTask:
    def __init__(self, error: object) -> None:
        self.info = types.SimpleNamespace(error=error, startTime=datetime(2026, 10, 14, tzinfo=timezone.utc))


class _QuiesceSnapshotVm(_FakeSnapshotVm):
    def __init__(self, error: object | None) -> None:
        super().__init__()
        self.error = error
        self.quiesce_requests: list[bool] = []
        self.snapshot = None
        self.guest = types.SimpleNamespace(toolsStatus="toolsOk")

    def CreateSnapshot_Task(self, *, name: str, description: str, memory: bool, quiesce: bool) -> object:
        self.created_snapshots.append(name)
        self.quiesce_requests.append(quiesce)
        return _FailingSnapshotTask(self.error if quiesce else None)


def _event_source(events: list[object], *, visible_from_query: int = 1) -> object:
    queries: list[object] = []

    def query(spec: object) -> list[object]:
        queries.append(spec)
        return list(events) if len(queries) >= visible_from_query else []

    manager = types.SimpleNamespace(QueryEvents=query, queries=queries)
    return types.SimpleNamespace(RetrieveContent=lambda: types.SimpleNamespace(eventManager=manager), manager=manager)


class VCenterNodeQuiesceTests(unittest.TestCase):
    def _node(self, vm: object, fails: bool) -> VCenterNode:
        node = VCenterNode("vc.local", "administrator", "secret")
        node._get_vm = lambda vm_name: vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        def wait(task: object) -> None:
            if fails and task.info.error is not None:
                raise RuntimeError(_QUIESCE_FAULT_TEXT)

        node._wait_for_task = wait  # type: ignore[method-assign]
        return node

    def test_a_failed_quiesce_names_the_guest_reason_and_where_to_look(self) -> None:
        from python.srv.plugins.hypervisor import QuiesceFailedError

        vm = _QuiesceSnapshotVm(vim.fault.ApplicationQuiesceFault(msg="An error occurred while quiescing"))
        node = self._node(vm, fails=True)
        node._quiesce_failure_reason = lambda vm, since: _GUEST_REASON  # type: ignore[method-assign]

        with self.assertRaises(QuiesceFailedError) as caught:
            node.create_backup_snapshot("demo-vm")

        self.assertEqual(caught.exception.reason, _GUEST_REASON)
        self.assertIn("Custom quiesce script failed", str(caught.exception))
        self.assertIn("/etc/vmware-tools/backupScripts.d/", str(caught.exception))
        self.assertIn("vssadmin list writers", str(caught.exception))
        self.assertNotIn("event log", str(caught.exception))
        self.assertEqual(vm.quiesce_requests, [True])

    def test_any_other_snapshot_failure_is_raised_as_before(self) -> None:
        from python.srv.plugins.hypervisor import QuiesceFailedError

        vm = _QuiesceSnapshotVm(vim.fault.GenericVmConfigFault(msg="Exceeded the maximum number of permitted snapshots."))
        node = self._node(vm, fails=True)

        with self.assertRaises(RuntimeError) as caught:
            node.create_backup_snapshot("demo-vm")

        self.assertNotIsInstance(caught.exception, QuiesceFailedError)
        self.assertEqual(str(caught.exception), _QUIESCE_FAULT_TEXT)

    def test_a_snapshot_can_be_taken_without_quiescing(self) -> None:
        vm = _QuiesceSnapshotVm(vim.fault.ApplicationQuiesceFault(msg="never raised"))
        node = self._node(vm, fails=True)

        snapshot_name = node.create_backup_snapshot("demo-vm", quiesce=False)

        self.assertTrue(snapshot_name.startswith("FullBackup_"))
        self.assertEqual(vm.quiesce_requests, [False])

    def test_the_reason_is_read_from_the_vms_quiesce_event(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        events = [
            types.SimpleNamespace(key=10, message="Some other warning"),
            types.SimpleNamespace(key=12, message=_GUEST_REASON + "  "),
        ]
        node.si = _event_source(events)

        reason = node._quiesce_failure_reason(vim.VirtualMachine("vm-301"), datetime(2026, 10, 14, tzinfo=timezone.utc))

        self.assertEqual(reason, _GUEST_REASON)
        self.assertEqual(len(node.si.manager.queries), 1)

    def test_the_reason_is_waited_for_until_vcenter_lists_the_event(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _event_source([types.SimpleNamespace(key=12, message=_GUEST_REASON)], visible_from_query=6)

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep"), patch.dict(
            "os.environ", {"BACKUPSERVER_QUIESCE_REASON_WAIT_SECONDS": "15"}
        ), patch("python.srv.plugins.vcenter.vcenter.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]):
            reason = node._quiesce_failure_reason(vim.VirtualMachine("vm-301"), None)

        self.assertEqual(reason, _GUEST_REASON)
        self.assertEqual(len(node.si.manager.queries), 6)

    def test_without_a_quiesce_event_the_reason_stays_unknown_once_the_wait_is_over(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _event_source([types.SimpleNamespace(key=1, message="Some other warning")])

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep"), patch.dict(
            "os.environ", {"BACKUPSERVER_QUIESCE_REASON_WAIT_SECONDS": "3"}
        ), patch("python.srv.plugins.vcenter.vcenter.time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]):
            reason = node._quiesce_failure_reason(vim.VirtualMachine("vm-301"), None)

        self.assertIsNone(reason)
        self.assertEqual(len(node.si.manager.queries), 3)

    def test_the_consistency_comes_from_the_snapshot_itself(self) -> None:
        for quiesced, tools, expected in (
            (True, "toolsOk", "quiesced"),
            (False, "toolsNotRunning", "crash_consistent"),
            (False, "toolsNotInstalled", "crash_consistent"),
        ):
            with self.subTest(tools=tools):
                vm = _QuiesceSnapshotVm(None)
                vm.guest = types.SimpleNamespace(toolsStatus=tools)
                node_entry = types.SimpleNamespace(name="FullBackup_1", quiesced=quiesced, childSnapshotList=[])
                vm.snapshot = types.SimpleNamespace(rootSnapshotList=[node_entry])
                node = self._node(vm, fails=False)

                state = node.snapshot_consistency("demo-vm", "FullBackup_1")

                self.assertEqual(state, {"consistency": expected, "tools_status": tools})


class VCenterNodeFirmwareTests(unittest.TestCase):
    def test_get_vm_restore_metadata_captures_efi_firmware(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="efi")

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertEqual(metadata["firmware"], "efi")

    def test_get_vm_restore_metadata_captures_bios_firmware(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="bios")

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertEqual(metadata["firmware"], "bios")

    def test_get_vm_restore_metadata_captures_lsi_logic_sas_controller(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(
            firmware="efi",
            devices=[vim.vm.device.VirtualLsiLogicSASController(busNumber=0)],
        )

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertEqual(metadata["scsi_controller_type"], "lsilogicsas")

    def test_get_vm_restore_metadata_prefers_lowest_bus_number_controller(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(
            firmware="bios",
            devices=[
                vim.vm.device.ParaVirtualSCSIController(busNumber=1),
                vim.vm.device.VirtualLsiLogicSASController(busNumber=0),
            ],
        )

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertEqual(metadata["scsi_controller_type"], "lsilogicsas")

    def test_get_vm_restore_metadata_reports_no_controller_type_when_none_recognized(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="bios", devices=[])

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertIsNone(metadata["scsi_controller_type"])

    def test_get_vm_restore_metadata_captures_secure_boot_enabled(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="efi", secure_boot_enabled=True)

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertTrue(metadata["secure_boot_enabled"])

    def test_get_vm_restore_metadata_captures_secure_boot_disabled(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="efi", secure_boot_enabled=False)

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertFalse(metadata["secure_boot_enabled"])

    def test_get_vm_restore_metadata_secure_boot_is_none_when_boot_options_missing(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="bios")

        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertIsNone(metadata["secure_boot_enabled"])

    def test_create_vm_shell_sets_efi_firmware_in_config_spec(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="efi")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "ubuntu64Guest",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "efi",
                "network_adapters": [],
            },
            disk_layout=[],
        )

        self.assertEqual(vm_folder.calls[0]["config"].firmware, "efi")
        self.assertEqual(result["requested_firmware"], "efi")
        self.assertEqual(result["restored_firmware"], "efi")

    def _shell_over_folders(self, existing: set[str]) -> tuple[VCenterNode, Any, list[str]]:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")
        searched: list[str] = []

        class Browser:
            def __init__(self, datastore_name: str) -> None:
                self.datastore_name = datastore_name

            def SearchDatastore_Task(self, datastorePath: str, searchSpec: Any) -> Any:
                folder = searchSpec.matchPattern[0]
                searched.append(f"{datastorePath} {folder}")
                found = [types.SimpleNamespace(path=folder)] if f"[{self.datastore_name}] {folder}" in existing else []
                return types.SimpleNamespace(info=types.SimpleNamespace(result=types.SimpleNamespace(file=found)))

        host.datastore = [
            types.SimpleNamespace(name="ds-01", browser=Browser("ds-01")),
            types.SimpleNamespace(name="ds-02", browser=Browser("ds-02")),
        ]
        node._get_host = lambda host_name: host  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]
        return node, vm_folder, searched

    def _create(self, node: VCenterNode, disks: list[dict]) -> dict:
        return node.create_vm_shell(
            vm_name="demo vm",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={"guest_id": "otherGuest64", "num_cpu": 1, "memory_mb": 512, "firmware": "bios", "network_adapters": []},
            disk_layout=disks,
        )

    def test_create_vm_shell_refuses_a_folder_that_already_exists(self) -> None:
        node, vm_folder, _searched = self._shell_over_folders({"[ds-01] demo-vm"})

        with self.assertRaisesRegex(ValueError, r"Folder \[ds-01\] demo-vm already exists, so VM 'demo vm' is not created there"):
            self._create(node, [{"capacity_gb": 1}])

        self.assertEqual(vm_folder.calls, [])

    def test_create_vm_shell_checks_the_folder_on_every_datastore_a_disk_goes_to(self) -> None:
        node, vm_folder, searched = self._shell_over_folders({"[ds-02] demo-vm"})

        with self.assertRaisesRegex(ValueError, r"Folder \[ds-02\] demo-vm already exists"):
            self._create(node, [{"capacity_gb": 1}, {"capacity_gb": 1, "datastore_name": "ds-02"}])

        self.assertEqual(searched, ["[ds-01] demo-vm", "[ds-02] demo-vm"])
        self.assertEqual(vm_folder.calls, [])

    def test_create_vm_shell_goes_ahead_when_no_folder_has_the_name(self) -> None:
        node, vm_folder, searched = self._shell_over_folders({"[ds-01] other-vm"})

        self._create(node, [])

        self.assertEqual(searched, ["[ds-01] demo-vm"])
        self.assertEqual(len(vm_folder.calls), 1)

    def test_create_vm_shell_sets_bios_firmware_in_config_spec(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "ubuntu64Guest",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "bios",
                "network_adapters": [],
            },
            disk_layout=[],
        )

        self.assertEqual(vm_folder.calls[0]["config"].firmware, "bios")
        self.assertEqual(result["requested_firmware"], "bios")
        self.assertEqual(result["restored_firmware"], "bios")

    def test_create_vm_shell_fails_when_resulting_firmware_does_not_match(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")
        removed: list[dict[str, object]] = []

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]
        node.destroy_vm = lambda vm_name, vm_moref=None: removed.append(  # type: ignore[method-assign]
            {"vm_name": vm_name, "vm_moref": vm_moref}
        )

        with self.assertRaisesRegex(RuntimeError, "requested efi, got bios"):
            node.create_vm_shell(
                vm_name="demo-vm-restored",
                host_name="esx-01.local",
                datastore_name="ds-01",
                config_metadata={
                    "guest_id": "ubuntu64Guest",
                    "num_cpu": 2,
                    "memory_mb": 4096,
                    "firmware": "efi",
                    "network_adapters": [],
                },
                disk_layout=[],
            )

        self.assertEqual(removed, [{"vm_name": "demo-vm-restored", "vm_moref": "vm-restore-001"}])

        self.assertEqual(vm_folder.calls[0]["config"].firmware, "efi")

    def test_create_vm_shell_preserves_source_scsi_controller_type(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "otherGuest64",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "bios",
                "scsi_controller_type": "lsilogicsas",
                "network_adapters": [],
            },
            disk_layout=[],
        )

        scsi_device = vm_folder.calls[0]["config"].deviceChange[0].device
        self.assertIsInstance(scsi_device, vim.vm.device.VirtualLsiLogicSASController)
        self.assertEqual(result["requested_scsi_controller_type"], "lsilogicsas")

    def test_create_vm_shell_falls_back_to_paravirtual_when_controller_type_missing(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "otherGuest64",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "bios",
                "network_adapters": [],
            },
            disk_layout=[],
        )

        scsi_device = vm_folder.calls[0]["config"].deviceChange[0].device
        self.assertIsInstance(scsi_device, vim.vm.device.ParaVirtualSCSIController)
        self.assertEqual(result["requested_scsi_controller_type"], "paravirtual")

    def test_create_vm_shell_enables_secure_boot_on_efi_restore(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="efi")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "windows2019srv_64Guest",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "efi",
                "secure_boot_enabled": True,
                "network_adapters": [],
            },
            disk_layout=[],
        )

        boot_options = vm_folder.calls[0]["config"].bootOptions
        self.assertTrue(boot_options.efiSecureBootEnabled)
        self.assertTrue(result["requested_secure_boot"])

    def test_create_vm_shell_does_not_enable_secure_boot_on_bios_restore(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "otherGuest64",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "bios",
                "secure_boot_enabled": True,
                "network_adapters": [],
            },
            disk_layout=[],
        )

        self.assertIsNone(vm_folder.calls[0]["config"].bootOptions)
        self.assertTrue(result["requested_secure_boot"])

    def test_create_vm_shell_leaves_boot_options_unset_when_secure_boot_metadata_absent(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="efi")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        result = node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "otherGuest64",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "efi",
                "network_adapters": [],
            },
            disk_layout=[],
        )

        self.assertIsNone(vm_folder.calls[0]["config"].bootOptions)
        self.assertIsNone(result["requested_secure_boot"])


class VCenterNodeClusterDetectionTests(unittest.TestCase):
    def test_clustered_host_name_returns_cluster_for_host_with_cluster_parent(self) -> None:
        node = VCenterNode("esx.local", "root", "secret")
        node.si = _FakeServiceInstance([_host_in_cluster()])

        self.assertTrue(node.clustered_host_name())

    def test_clustered_host_name_returns_none_for_standalone_host(self) -> None:
        node = VCenterNode("esx.local", "root", "secret")
        node.si = _FakeServiceInstance([_host_standalone()])

        self.assertIsNone(node.clustered_host_name())

    def test_clustered_host_name_detects_cluster_among_multiple_hosts(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _FakeServiceInstance([_host_standalone("a"), _host_in_cluster("b")])

        self.assertTrue(node.clustered_host_name())

    def test_clustered_host_name_requires_connection(self) -> None:
        node = VCenterNode("esx.local", "root", "secret")

        with self.assertRaises(ValueError):
            node.clustered_host_name()


def _fake_healthy_vm(*, name: str, uuid: str) -> object:
    disk = vim.vm.device.VirtualDisk()
    disk.capacityInKB = 20 * 1024 * 1024
    disk.deviceInfo = vim.Description(label="Hard disk 1")
    disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    disk.backing.fileName = f"[datastore1] {name}/{name}.vmdk"

    nic = vim.vm.device.VirtualE1000()
    nic.deviceInfo = vim.Description(label="Network adapter 1")
    nic.macAddress = "00:11:22:33:44:55"
    nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
    nic.backing.deviceName = "VM Network"
    nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
    nic.connectable.connected = True

    return types.SimpleNamespace(
        name=name,
        summary=types.SimpleNamespace(
            config=types.SimpleNamespace(
                uuid=uuid,
                name=name,
                numCpu=2,
                memorySizeMB=4096,
                guestFullName="Ubuntu Linux (64-bit)",
            ),
            runtime=types.SimpleNamespace(powerState="poweredOn", host=None),
        ),
        config=types.SimpleNamespace(
            firmware="bios",
            hardware=types.SimpleNamespace(device=[disk, nic]),
        ),
    )


def _fake_orphaned_vm(*, name: str, uuid: str) -> object:
    # An inaccessible/orphaned VM in vSphere reports summary.config (cached data)
    # but vm.config is None, since vCenter cannot read its live configuration.
    return types.SimpleNamespace(
        name=name,
        summary=types.SimpleNamespace(
            config=types.SimpleNamespace(
                uuid=uuid,
                name=name,
                numCpu=0,
                memorySizeMB=0,
                guestFullName=None,
            ),
            runtime=types.SimpleNamespace(powerState="poweredOff", host=None),
        ),
        config=None,
    )


class VCenterNodeGetInventoryTests(unittest.TestCase):
    def test_get_inventory_skips_orphaned_vm_and_returns_healthy_vms(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")

        healthy_a = _fake_healthy_vm(name="web-01", uuid="uuid-web-01")
        orphaned = _fake_orphaned_vm(name="stale-clone", uuid="uuid-stale")
        healthy_b = _fake_healthy_vm(name="web-02", uuid="uuid-web-02")

        node.si = _FakeServiceInstance([healthy_a, orphaned, healthy_b])

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING") as logs:
            inventory = node.get_inventory()

        vm_names = [vm["name"] for vm in inventory["virtual_machines"]]
        
        self.assertEqual(vm_names, ["web-01", "web-02"])
        self.assertTrue(any("stale-clone" in message for message in logs.output))

        disk_vm_ids = {disk["vm_id"] for disk in inventory["disks"]}
        self.assertEqual(disk_vm_ids, {"uuid-web-01", "uuid-web-02"})

        nic_vm_ids = {nic["vm_id"] for nic in inventory["network_adapters"]}
        self.assertEqual(nic_vm_ids, {"uuid-web-01", "uuid-web-02"})

    def test_get_inventory_returns_all_vms_when_none_are_orphaned(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _FakeServiceInstance(
            [
                _fake_healthy_vm(name="web-01", uuid="uuid-web-01"),
                _fake_healthy_vm(name="web-02", uuid="uuid-web-02"),
            ]
        )

        inventory = node.get_inventory()

        self.assertEqual(len(inventory["virtual_machines"]), 2)
        self.assertEqual(len(inventory["disks"]), 2)
        self.assertEqual(len(inventory["network_adapters"]), 2)

    def test_get_inventory_returns_empty_when_only_vm_is_orphaned(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _FakeServiceInstance([_fake_orphaned_vm(name="stale-clone", uuid="uuid-stale")])

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            inventory = node.get_inventory()

        self.assertEqual(inventory["virtual_machines"], [])
        self.assertEqual(inventory["disks"], [])
        self.assertEqual(inventory["network_adapters"], [])

    def test_get_inventory_skips_a_vm_deleted_after_the_listing_and_names_it(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        deleted = _DeletedAfterListingVm(name="ci-clone-7", moid="vm-19831")

        node.si = _FakeServiceInstance(
            [_fake_healthy_vm(name="web-01", uuid="uuid-web-01"), deleted, _fake_healthy_vm(name="web-02", uuid="uuid-web-02")]
        )

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING") as logs:
            inventory = node.get_inventory()

        self.assertEqual([vm["name"] for vm in inventory["virtual_machines"]], ["web-01", "web-02"])
        self.assertEqual({disk["vm_id"] for disk in inventory["disks"]}, {"uuid-web-01", "uuid-web-02"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("'ci-clone-7' (vm-19831)", logs.output[0])
        self.assertIn("deleted from vCenter vc.local while the inventory rescan was reading it", logs.output[0])

    def test_get_inventory_skips_a_vm_deleted_while_its_devices_are_read(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        vanishing = _fake_healthy_vm(name="dr-test-3", uuid="uuid-dr-3")
        vanishing.config = _DeletedProperty()
        node.si = _FakeServiceInstance([vanishing, _fake_healthy_vm(name="web-01", uuid="uuid-web-01")])

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING") as logs:
            inventory = node.get_inventory()

        self.assertEqual([vm["name"] for vm in inventory["virtual_machines"]], ["web-01"])
        self.assertIn("'dr-test-3'", logs.output[0])
        self.assertIn("deleted from vCenter", logs.output[0])


class VmChangesDuringBackupTests(unittest.TestCase):
    STARTED = datetime(2026, 10, 9, 17, 33, 9, tzinfo=timezone.utc)
    AT = datetime(2026, 10, 9, 17, 33, 41, tzinfo=timezone.utc)
    ADMIN = "VSPHERE.LOCAL\\Administrator"

    def _node(self, events: list, vm: object | None) -> tuple[VCenterNode, list]:
        node = VCenterNode("vc.local", "administrator", "secret")
        asked: list = []

        def query(spec):
            asked.append(spec)
            return events

        content = types.SimpleNamespace(eventManager=types.SimpleNamespace(QueryEvents=query))
        node.si = types.SimpleNamespace(RetrieveContent=lambda: content)
        node._get_vm_by_moref = lambda moref: vm
        node._get_vm = lambda name: vm
        return node, asked

    @staticmethod
    def _vm() -> object:
        return vim.VirtualMachine("vm-1")

    def _task_event(self, description: str) -> object:
        return vim.event.TaskEvent(
            info=vim.TaskInfo(descriptionId=description),
            userName=self.ADMIN,
            createdTime=self.AT,
            vm=vim.event.VmEventArgument(vm=vim.VirtualMachine("vm-1")),
        )

    def _removed(self, moref: str = "vm-1") -> object:
        return vim.event.VmRemovedEvent(userName="ops", createdTime=self.AT, vm=vim.event.VmEventArgument(vm=vim.VirtualMachine(moref)))

    def test_a_power_off_and_a_delete_are_told_with_who_and_when(self) -> None:
        events = [
            self._task_event("VirtualMachine.createSnapshot"),
            self._task_event("VirtualMachine.reconfigure"),
            vim.event.VmPoweredOffEvent(userName=self.ADMIN, createdTime=self.AT),
            self._task_event("VirtualMachine.destroy"),
        ]
        node, asked = self._node(events, self._vm())

        changes = node.vm_changes_since("et-test-del", "vm-1", self.STARTED)

        self.assertEqual(changes, [("powered off", self.ADMIN, self.AT), ("deleted", self.ADMIN, self.AT)])
        self.assertEqual(asked[0].time.beginTime, self.STARTED)

    def test_a_vm_whose_delete_went_through_is_found_in_the_events_by_its_moref(self) -> None:
        node, _asked = self._node([self._removed("vm-1"), self._removed("vm-2")], None)

        self.assertEqual(node.vm_changes_since("et-test-del", "vm-1", self.STARTED), [("removed from vCenter", "ops", self.AT)])

    def test_a_vm_that_vanished_without_a_trace_is_still_named_as_gone(self) -> None:
        node, _asked = self._node([], None)

        self.assertEqual(
            node.vm_changes_since("et-test-del", "vm-1", self.STARTED), [("deleted or removed from the inventory", "", None)]
        )

    def test_an_unregister_is_not_called_a_delete(self) -> None:
        events = [self._task_event("VirtualMachine.unregister"), self._removed()]
        node, _asked = self._node(events, None)

        self.assertEqual(
            node.vm_changes_since("et-test-del", "vm-1", self.STARTED), [("removed from the inventory", self.ADMIN, self.AT)]
        )

    def test_a_completed_delete_is_told_once(self) -> None:
        node, _asked = self._node([self._task_event("VirtualMachine.destroy"), self._removed()], None)

        self.assertEqual(node.vm_changes_since("et-test-del", "vm-1", self.STARTED), [("deleted", self.ADMIN, self.AT)])

    def test_a_vm_the_backup_alone_touched_reports_nothing(self) -> None:
        node, _asked = self._node(
            [self._task_event("VirtualMachine.createSnapshot"), self._task_event("VirtualMachine.removeSnapshot")], self._vm()
        )

        self.assertEqual(node.vm_changes_since("et-test-del", "vm-1", self.STARTED), [])


def _managed_object_not_found(moid: str) -> vmodl.fault.ManagedObjectNotFound:
    return vmodl.fault.ManagedObjectNotFound(
        msg=f"The object 'vim.VirtualMachine:{moid}' has already been deleted or has not been completely created",
        obj=vim.VirtualMachine(moid),
    )


class _DeletedAfterListingVm:
    def __init__(self, *, name: str, moid: str) -> None:
        self._collector_name = name
        self._moId = moid

    def __getattr__(self, attribute: str) -> object:
        raise _managed_object_not_found(self._moId)


class _DeletedProperty:
    def __getattr__(self, attribute: str) -> object:
        raise _managed_object_not_found("vm-20001")


_DVS_SUMMARY = "DVSwitch: 50 16 aa bb"


def _fake_dvs_nic(label: str, portgroup_key: str) -> object:
    """A NIC on a distributed port group: its backing names only the port group
    key and switch UUID, and vCenter's summary names only the switch."""
    nic = vim.vm.device.VirtualVmxnet3()
    nic.deviceInfo = vim.Description(label=label, summary=_DVS_SUMMARY)
    nic.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
    nic.backing.port = vim.dvs.PortConnection(portgroupKey=portgroup_key, switchUuid="50 16 aa bb")
    nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, connected=True)
    return nic


def _fake_vm_networks() -> list[object]:
    return [
        types.SimpleNamespace(_moId="network-7", name="VM Network"),
        types.SimpleNamespace(_moId="dvportgroup-101", name="Prod"),
        # A port group whose key is not its MoRef id.
        types.SimpleNamespace(_moId="dvportgroup-5", key="dvportgroup-102", name="Backup"),
    ]


class VCenterNodeNetworkAdapterTests(unittest.TestCase):
    def test_restore_metadata_names_each_distributed_port_group(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(
            firmware="bios",
            devices=[
                _fake_dvs_nic("Network adapter 1", "dvportgroup-101"),
                _fake_dvs_nic("Network adapter 2", "dvportgroup-102"),
            ],
        )
        fake_vm.network = _fake_vm_networks()
        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        # Not the switch summary both NICs share: that made them one network.
        self.assertEqual([adapter["network"] for adapter in metadata["network_adapters"]], ["Prod", "Backup"])

    def test_restore_metadata_falls_back_to_the_summary_for_an_unlisted_port_group(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_restore_metadata_vm(firmware="bios", devices=[_fake_dvs_nic("Network adapter 1", "dvportgroup-999")])
        fake_vm.network = _fake_vm_networks()
        node._get_vm = lambda vm_name: fake_vm if vm_name == "demo-vm" else None  # type: ignore[method-assign]

        metadata = node.get_vm_restore_metadata("demo-vm")

        self.assertEqual(metadata["network_adapters"][0]["network"], _DVS_SUMMARY)

    def test_inventory_names_the_distributed_port_group_of_a_nic(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        fake_vm = _fake_healthy_vm(name="web-01", uuid="uuid-web-01")
        fake_vm.config.hardware.device[1] = _fake_dvs_nic("Network adapter 1", "dvportgroup-101")
        fake_vm.network = _fake_vm_networks()
        node.si = _FakeServiceInstance([fake_vm])

        inventory = node.get_inventory()

        self.assertEqual(inventory["network_adapters"][0]["network"], "Prod")

    def test_create_vm_shell_reports_which_adapters_it_attached(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]
        node._find_network = lambda datacenter, host, name: vim.Network("network-7") if name == "Prod" else None  # type: ignore[method-assign]

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            result = node.create_vm_shell(
                vm_name="demo-vm-restored",
                host_name="esx-01.local",
                datastore_name="ds-01",
                config_metadata={
                    "guest_id": "ubuntu64Guest",
                    "num_cpu": 2,
                    "memory_mb": 4096,
                    "firmware": "bios",
                    "network_adapters": [
                        {"label": "Network adapter 1", "network": "Prod", "source_network": _DVS_SUMMARY, "connected": True},
                        {"label": "Network adapter 2", "network": "Missing", "connected": True},
                    ],
                },
                disk_layout=[],
            )

        self.assertEqual(
            [(adapter["network"], adapter["attached"]) for adapter in result["network_adapters"]],
            [("Prod", True), ("Missing", False)],
        )
        self.assertEqual(result["network_adapters"][0]["source_network"], _DVS_SUMMARY)
        nics = [
            change.device
            for change in vm_folder.calls[0]["config"].deviceChange
            if isinstance(change.device, vim.vm.device.VirtualEthernetCard)
        ]
        self.assertEqual(len(nics), 1)


class _FakeSessionManager:
    def __init__(self, *, login_error: Exception | None = None) -> None:
        self.logins: list[tuple[str, str]] = []
        self._login_error = login_error

    def Login(self, user: str, password: str, locale: object = None) -> object:
        if self._login_error is not None:
            raise self._login_error
        self.logins.append((user, password))
        return object()


class _ExpiringServiceInstance:
    """Service instance whose first authenticated call fails the way vCenter's
    expired session does, and that starts working again once re-login runs."""

    def __init__(self, view: list[object], *, session_manager: _FakeSessionManager) -> None:
        self._view = view
        self._session_manager = session_manager
        self.authenticated = False
        self.content_calls = 0
        self.current_time_calls = 0

    def RetrieveContent(self) -> object:
        # RetrieveServiceContent needs no session, so it keeps answering even
        # while the session is expired -- that is what makes re-login possible.
        self.content_calls += 1
        content = _FakeContent(self._view)
        content.sessionManager = self._session_manager
        if not self.authenticated:
            content.viewManager = _UnauthenticatedViewManager(self)
        return content

    def CurrentTime(self) -> object:
        self.current_time_calls += 1
        if not self.authenticated:
            raise vim.fault.NotAuthenticated(msg="The session is not authenticated.")
        return object()


class _UnauthenticatedViewManager:
    def __init__(self, service_instance: _ExpiringServiceInstance) -> None:
        self._service_instance = service_instance

    def CreateContainerView(self, root: object, types_: object, recursive: bool) -> object:
        raise vim.fault.NotAuthenticated(msg="The session is not authenticated.")


class VCenterSessionExpiryTests(unittest.TestCase):
    def test_lookup_relogins_and_retries_when_session_expired(self) -> None:
        session_manager = _FakeSessionManager()
        node = VCenterNode("vc.local", "administrator", "secret")
        si = _ExpiringServiceInstance([_FakeVm(name="demo-vm", moid="vm-1")], session_manager=session_manager)
        node.si = si

        def _login(user: str, password: str, locale: object = None) -> object:
            si.authenticated = True
            return object()

        session_manager.Login = _login  # type: ignore[method-assign]

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            vm = node._get_vm("demo-vm")

        self.assertIsNotNone(vm)
        self.assertIs(node.si, si, "re-login must reuse the existing connection")

    def test_lookup_failure_propagates_when_relogin_does_not_help(self) -> None:
        session_manager = _FakeSessionManager()
        node = VCenterNode("vc.local", "administrator", "secret")
        # Login "succeeds" but the session stays unauthenticated, so the single
        # retry fails too -- the caller must see the fault rather than a loop.
        node.si = _ExpiringServiceInstance([], session_manager=session_manager)

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            with self.assertRaises(vim.fault.NotAuthenticated):
                node._get_vm("demo-vm")

        self.assertEqual(session_manager.logins, [("administrator", "secret")])

    def test_reauthenticate_reconnects_when_relogin_fails(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _ExpiringServiceInstance(
            [], session_manager=_FakeSessionManager(login_error=RuntimeError("connection reset"))
        )
        reconnects: list[bool] = []

        def _connect() -> None:
            reconnects.append(True)
            node.si = _FakeServiceInstance([])

        node.connect = _connect  # type: ignore[method-assign]
        node.disconnect = lambda: None  # type: ignore[method-assign]

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            node.reauthenticate()

        self.assertEqual(reconnects, [True])

    def test_touch_session_relogins_when_session_expired(self) -> None:
        session_manager = _FakeSessionManager()
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = _ExpiringServiceInstance([], session_manager=session_manager)

        node.touch_session()

        self.assertEqual(session_manager.logins, [("administrator", "secret")])

    def test_touch_session_is_a_noop_without_a_connection(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")

        node.touch_session()  # must not raise, and must not connect

        self.assertIsNone(node.si)

    def test_session_keepalive_pings_while_held_and_stops_after(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        pings: list[float] = []
        pinged = threading.Event()

        def _touch() -> None:
            pings.append(time.monotonic())
            pinged.set()

        node.touch_session = _touch  # type: ignore[method-assign]

        with node.session_keepalive(interval_seconds=0.01):
            self.assertTrue(pinged.wait(5), "keepalive thread never pinged the session")

        pings_at_exit = len(pings)
        time.sleep(0.1)
        self.assertEqual(len(pings), pings_at_exit, "keepalive kept pinging after the context closed")

    def test_session_keepalive_survives_a_failing_ping(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        attempts = threading.Semaphore(0)

        def _touch() -> None:
            attempts.release()
            raise RuntimeError("vCenter unreachable")

        node.touch_session = _touch  # type: ignore[method-assign]

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="WARNING"):
            with node.session_keepalive(interval_seconds=0.01):
                self.assertTrue(attempts.acquire(timeout=5))
                self.assertTrue(attempts.acquire(timeout=5), "a failed ping stopped the keepalive")


class DiskUuidPreservationTests(unittest.TestCase):
    @staticmethod
    def _disk(uuid_value: str | None) -> vim.vm.device.VirtualDisk:
        disk = vim.vm.device.VirtualDisk()
        disk.key = 2000
        disk.unitNumber = 0
        disk.controllerKey = 1000
        disk.capacityInKB = 2 * 1024 * 1024
        disk.deviceInfo = vim.Description(label="Hard disk 1", summary="")
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        backing.fileName = "[data1] demo-vm/demo-vm.vmdk"
        backing.thinProvisioned = True
        if uuid_value is not None:
            backing.uuid = uuid_value
        disk.backing = backing
        return disk

    def test_collect_disks_records_the_uuid_the_guest_identifies_the_disk_by(self) -> None:
        node = VCenterNode("vc.local", "user", "secret")
        vm = _fake_restore_metadata_vm(
            firmware="efi", devices=[self._disk("6000C292-d2c6-df32-2e38-2d46be8d7a29")]
        )

        disks = node._collect_disks(vm)

        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["uuid"], "6000C292-d2c6-df32-2e38-2d46be8d7a29")

    def test_collect_disks_reports_no_uuid_when_the_backing_carries_none(self) -> None:
        node = VCenterNode("vc.local", "user", "secret")
        vm = _fake_restore_metadata_vm(firmware="efi", devices=[self._disk(None)])

        disks = node._collect_disks(vm)

        self.assertIsNone(disks[0]["uuid"])


class NormalizeDiskUuidTests(unittest.TestCase):
    def test_real_uuid_is_kept(self) -> None:
        self.assertEqual(
            _normalize_disk_uuid("6000C292-d2c6-df32-2e38-2d46be8d7a29"),
            "6000C292-d2c6-df32-2e38-2d46be8d7a29",
        )

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertEqual(_normalize_disk_uuid("  6000C290-8f5e  "), "6000C290-8f5e")

    def test_placeholders_and_blanks_are_rejected(self) -> None:
        for value in (None, "", "   ", "N/A", "n/a", "None", "NULL", "Unknown"):
            with self.subTest(value=value):
                self.assertIsNone(_normalize_disk_uuid(value))


class _FakeDatastore(vim.Datastore):
    @property
    def name(self) -> str:
        return self._name


class RestoredDiskProvisioningTests(unittest.TestCase):
    @staticmethod
    def _source_disk(*, thin: bool, eagerly_scrub: bool | None) -> vim.vm.device.VirtualDisk:
        disk = vim.vm.device.VirtualDisk()
        disk.key = 2000
        disk.unitNumber = 0
        disk.controllerKey = 1000
        disk.capacityInKB = 2 * 1024 * 1024
        disk.deviceInfo = vim.Description(label="Hard disk 1", summary="")
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        backing.fileName = "[data1] demo-vm/demo-vm.vmdk"
        backing.thinProvisioned = thin
        if eagerly_scrub is not None:
            backing.eagerlyScrub = eagerly_scrub
        disk.backing = backing
        return disk

    def _created_disks(self, disk_layout: list[dict[str, object]]) -> list[object]:
        node = VCenterNode("vc.local", "administrator", "secret")
        host, datacenter, vm_folder, created_vm = _fake_vm_create_context(resulting_firmware="bios")
        datastore = _FakeDatastore("datastore-1")
        object.__setattr__(datastore, "_name", "ds-01")
        host.datastore = [datastore]
        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

        node.create_vm_shell(
            vm_name="demo-vm-restored",
            host_name="esx-01.local",
            datastore_name="ds-01",
            config_metadata={
                "guest_id": "ubuntu64Guest",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "bios",
                "network_adapters": [],
            },
            disk_layout=disk_layout,
        )
        return [
            change.device
            for change in vm_folder.calls[0]["config"].deviceChange
            if isinstance(change.device, vim.vm.device.VirtualDisk)
        ]

    def test_a_thick_source_disk_is_created_thick(self) -> None:
        disks = self._created_disks(
            [{"capacity_gb": 1, "file_name": "00-disk.vmdk", "thin_provisioned": False, "eager_zeroed": False}]
        )

        self.assertEqual(len(disks), 1)
        self.assertFalse(disks[0].backing.thinProvisioned)
        self.assertFalse(disks[0].backing.eagerlyScrub)

    def test_an_eager_zeroed_source_disk_is_created_eager_zeroed(self) -> None:
        disks = self._created_disks(
            [{"capacity_gb": 1, "file_name": "00-disk.vmdk", "thin_provisioned": False, "eager_zeroed": True}]
        )

        self.assertFalse(disks[0].backing.thinProvisioned)
        self.assertTrue(disks[0].backing.eagerlyScrub)

    def test_a_thin_source_disk_stays_thin(self) -> None:
        disks = self._created_disks(
            [{"capacity_gb": 1, "file_name": "00-disk.vmdk", "thin_provisioned": True, "eager_zeroed": False}]
        )

        self.assertTrue(disks[0].backing.thinProvisioned)

    def test_a_layout_without_a_provisioning_decision_still_creates_thin(self) -> None:
        disks = self._created_disks([{"capacity_gb": 1, "file_name": "00-disk.vmdk"}])

        self.assertTrue(disks[0].backing.thinProvisioned)

    def _attach(self, **kwargs: object) -> vim.vm.device.VirtualDisk:
        node = VCenterNode("vc.local", "administrator", "secret")
        controller = vim.vm.device.VirtualLsiLogicController()
        controller.key = 1000
        vm = types.SimpleNamespace(
            config=types.SimpleNamespace(hardware=types.SimpleNamespace(device=[controller])),
        )
        specs: list[object] = []

        def _reconfig(*, spec: object) -> object:
            specs.append(spec)
            return object()

        vm.ReconfigVM_Task = _reconfig  # type: ignore[attr-defined]
        node._get_vm = lambda vm_name: vm if vm_name == "demo-vm-restored" else None  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]

        node.attach_existing_disk("demo-vm-restored", "[ds-01] demo-vm-restored/00-disk.vmdk", **kwargs)  # type: ignore[arg-type]
        return specs[0].deviceChange[0].device

    def test_an_attached_thick_disk_is_declared_thick(self) -> None:
        device = self._attach(thin_provisioned=False, eager_zeroed=False)

        self.assertFalse(device.backing.thinProvisioned)
        self.assertFalse(device.backing.eagerlyScrub)

    def test_an_attached_eager_zeroed_disk_is_declared_eager_zeroed(self) -> None:
        device = self._attach(thin_provisioned=False, eager_zeroed=True)

        self.assertTrue(device.backing.eagerlyScrub)

    def test_an_attached_disk_with_no_decision_is_declared_thin(self) -> None:
        device = self._attach()

        self.assertTrue(device.backing.thinProvisioned)


class SourceDiskEagerZeroedTests(unittest.TestCase):
    def _disks(self, *, thin: bool, eagerly_scrub: bool | None) -> list[dict[str, object]]:
        node = VCenterNode("vc.local", "user", "secret")
        disk = RestoredDiskProvisioningTests._source_disk(thin=thin, eagerly_scrub=eagerly_scrub)
        vm = _fake_restore_metadata_vm(firmware="bios", devices=[disk])
        return node._collect_disks(vm)

    def test_a_lazy_zeroed_thick_disk_is_recorded_as_not_eager(self) -> None:
        disks = self._disks(thin=False, eagerly_scrub=False)

        self.assertEqual(disks[0]["source_disk_type_name"], "vmfs_flat")
        self.assertIs(disks[0]["source_disk_eager_zeroed"], False)

    def test_an_eager_zeroed_thick_disk_is_recorded_as_eager(self) -> None:
        disks = self._disks(thin=False, eagerly_scrub=True)

        self.assertEqual(disks[0]["source_disk_type_name"], "vmfs_flat")
        self.assertIs(disks[0]["source_disk_eager_zeroed"], True)

    def test_a_thin_disk_records_no_eager_zeroed_answer(self) -> None:
        disks = self._disks(thin=True, eagerly_scrub=None)

        self.assertEqual(disks[0]["source_disk_type_name"], "vmfs_thin")
        self.assertIsNone(disks[0]["source_disk_eager_zeroed"])


class _FakeSoapResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")


class _FakeSoapConnection:
    def __init__(self, response: _FakeSoapResponse, on_request) -> None:
        self.response = response
        self.on_request = on_request
        self.requests: list[tuple[str, str, bytes, dict]] = []
        self.closed = False

    def request(self, method: str, path: str, body: bytes, headers: dict) -> None:
        self.requests.append((method, path, body, headers))
        self.on_request()

    def getresponse(self) -> _FakeSoapResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class _FakeSoapStub:
    cookie = 'vmware_soap_session="abc"'
    versionId = '"urn:vim25/8.0.3.0"'
    path = "/sdk"

    def __init__(self, connection: _FakeSoapConnection) -> None:
        self.connection = connection
        self.returned = 0

    def GetConnection(self) -> _FakeSoapConnection:
        return self.connection

    def ReturnConnection(self, connection: _FakeSoapConnection) -> None:
        self.returned += 1


class ReleaseAccessLockTests(unittest.TestCase):
    def _node(
        self, disabled: list[str], status: int = 200, body: str = "<EnableMethodsResponse/>", api_type: str = "VirtualCenter"
    ) -> tuple:
        vm = types.SimpleNamespace(_moId="vm-42", _wsdlName="VirtualMachine", disabledMethod=list(disabled))

        def lift() -> None:
            if status == 200:
                vm.disabledMethod = [method for method in vm.disabledMethod if method != "RelocateVM_Task"]

        connection = _FakeSoapConnection(_FakeSoapResponse(status, body), lift)
        stub = _FakeSoapStub(connection)
        node = VCenterNode("vc.local", "administrator", "secret")
        node.si = types.SimpleNamespace(
            _stub=stub,
            RetrieveContent=lambda: types.SimpleNamespace(
                authorizationManager=types.SimpleNamespace(_moId="AuthorizationManager"),
                about=types.SimpleNamespace(apiType=api_type),
            ),
        )
        node._vm_holding_access_lock = lambda vm_name, vm_moref: vm
        return node, vm, connection, stub

    def test_a_vm_that_a_data_mover_left_blocked_is_re_enabled(self) -> None:
        node, vm, connection, stub = self._node(["PowerOnVM_Task", "RelocateVM_Task"])

        self.assertTrue(node.release_access_lock("web01", "vm-42"))

        self.assertEqual(vm.disabledMethod, ["PowerOnVM_Task"])
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/sdk"))
        text = body.decode("utf-8")
        self.assertIn('<_this type="AuthorizationManager">AuthorizationManager</_this>', text)
        self.assertIn('<entity type="VirtualMachine">vm-42</entity>', text)
        self.assertIn("<method>RelocateVM_Task</method>", text)
        self.assertIn("<sourceId>abr-vmdk-tool</sourceId>", text)
        self.assertEqual(headers["SOAPAction"], '"urn:vim25/8.0.3.0"')
        self.assertEqual(headers["Cookie"], 'vmware_soap_session="abc"')
        self.assertEqual(stub.returned, 1)

    def test_a_direct_esxi_connection_never_tries_to_re_enable_vcenter_only_methods(self) -> None:
        baseline = ["CloneVM_Task", "MigrateVM_Task", "RelocateVM_Task"]
        node, vm, connection, _stub = self._node(list(baseline), api_type="HostAgent")

        self.assertFalse(node.release_access_lock("esxi-src", "vm-42"))

        self.assertEqual(connection.requests, [])
        self.assertEqual(vm.disabledMethod, baseline)

    def test_a_vm_without_the_block_is_left_untouched(self) -> None:
        node, vm, connection, _stub = self._node(["PowerOnVM_Task"])

        self.assertFalse(node.release_access_lock("web01", "vm-42"))
        self.assertEqual(connection.requests, [])

    def test_a_vm_that_is_gone_has_nothing_to_release(self) -> None:
        node, _vm, connection, _stub = self._node(["RelocateVM_Task"])
        node._vm_holding_access_lock = lambda vm_name, vm_moref: None

        self.assertFalse(node.release_access_lock("web01", "vm-42"))
        self.assertEqual(connection.requests, [])

    def test_a_fault_from_vcenter_is_raised_with_its_text(self) -> None:
        node, vm, _connection, _stub = self._node(
            ["RelocateVM_Task"],
            status=500,
            body="<soapenv:Fault><faultstring>Permission to perform this operation was denied.</faultstring></soapenv:Fault>",
        )

        with self.assertRaisesRegex(RuntimeError, "Permission to perform this operation was denied"):
            node.release_access_lock("web01", "vm-42")
        self.assertIn("RelocateVM_Task", vm.disabledMethod)

    def test_a_block_that_vcenter_keeps_is_reported(self) -> None:
        node, vm, connection, _stub = self._node(["RelocateVM_Task"])
        connection.on_request = lambda: None
        clock = iter(range(0, 1000))

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep"), patch(
            "python.srv.plugins.vcenter.vcenter.time.monotonic", side_effect=lambda: next(clock)
        ), self.assertRaisesRegex(RuntimeError, "still blocks RelocateVM_Task"):
            node.release_access_lock("web01", "vm-42")

    def test_a_block_vcenter_holds_for_its_own_task_is_waited_out_without_a_warning(self) -> None:
        node, vm, connection, _stub = self._node(["PowerOnVM_Task", "RelocateVM_Task"])
        connection.on_request = lambda: None
        reads = {"count": 0}

        class _VmUnderVCenterTask:
            _moId = "vm-42"
            _wsdlName = "VirtualMachine"

            @property
            def disabledMethod(self) -> list[str]:
                reads["count"] += 1
                return ["PowerOnVM_Task", "RelocateVM_Task"] if reads["count"] <= 3 else ["PowerOnVM_Task"]

        held = _VmUnderVCenterTask()
        node._vm_holding_access_lock = lambda vm_name, vm_moref: held

        with patch("python.srv.plugins.vcenter.vcenter.time.sleep") as sleep, self.assertLogs(
            "python.srv.plugins.vcenter.vcenter", level="INFO"
        ) as logs:
            lifted = node.release_access_lock("web01", "vm-42")

        self.assertFalse(lifted)
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(any("vCenter's own task" in line for line in logs.output))

    def test_a_broken_connection_is_closed_not_returned_to_the_pool(self) -> None:
        node, _vm, connection, stub = self._node(["RelocateVM_Task"])

        def broken() -> None:
            raise OSError("connection reset")

        connection.on_request = broken

        with self.assertRaises(OSError):
            node.release_access_lock("web01", "vm-42")
        self.assertTrue(connection.closed)
        self.assertEqual(stub.returned, 0)


class _ConsolidatingVm:
    def __init__(self, needed: bool, clears_after: int = 1) -> None:
        self.runtime = types.SimpleNamespace(consolidationNeeded=needed)
        self.consolidate_calls = 0
        self._clears_after = clears_after

    def ConsolidateVMDisks_Task(self) -> str:
        self.consolidate_calls += 1
        return f"consolidate-{self.consolidate_calls}"


class VCenterNodeConsolidationTests(unittest.TestCase):
    def _node(self, vm: object, failures: list[Exception] | None = None) -> VCenterNode:
        node = VCenterNode("vc.local", "administrator", "secret")
        pending = list(failures or [])

        def wait(task: object) -> None:
            if pending:
                raise pending.pop(0)
            vm.runtime.consolidationNeeded = vm.consolidate_calls < vm._clears_after

        node._get_vm = lambda vm_name: vm  # type: ignore[method-assign]
        node._wait_for_task = wait  # type: ignore[method-assign]
        return node

    def _settle(self, seconds: int):
        return patch("python.srv.plugins.vcenter.vcenter.env_int", return_value=seconds)

    def test_a_vm_that_needs_nothing_is_left_alone(self) -> None:
        vm = _ConsolidatingVm(needed=False)

        self.assertFalse(self._node(vm).consolidate_disks("demo-vm"))
        self.assertEqual(vm.consolidate_calls, 0)

    def test_a_vm_left_on_delta_disks_is_consolidated(self) -> None:
        vm = _ConsolidatingVm(needed=True)

        with self.assertLogs("python.srv.plugins.vcenter.vcenter", level="INFO") as logs:
            self.assertTrue(self._node(vm).consolidate_disks("demo-vm"))

        self.assertEqual(vm.consolidate_calls, 1)
        self.assertFalse(vm.runtime.consolidationNeeded)
        self.assertIn("Consolidated the disks of VM 'demo-vm'", "\n".join(logs.output))

    def test_disks_still_held_open_are_retried_until_they_merge(self) -> None:
        vm = _ConsolidatingVm(needed=True, clears_after=3)
        locked = RuntimeError("vCenter task failed: Unable to access file since it is locked")

        with self._settle(60), patch("python.srv.plugins.vcenter.vcenter.time.sleep") as sleep:
            self.assertTrue(self._node(vm, [locked]).consolidate_disks("demo-vm"))

        self.assertEqual(vm.consolidate_calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_a_consolidation_that_keeps_failing_raises_once_the_wait_is_over(self) -> None:
        vm = _ConsolidatingVm(needed=True)
        locked = RuntimeError("vCenter task failed: Unable to access file since it is locked")

        with self._settle(0), patch("python.srv.plugins.vcenter.vcenter.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "locked"):
                self._node(vm, [locked, locked]).consolidate_disks("demo-vm")

        self.assertTrue(vm.runtime.consolidationNeeded)

    def test_a_missing_vm_needs_nothing(self) -> None:
        node = VCenterNode("vc.local", "administrator", "secret")
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]

        self.assertFalse(node.consolidate_disks("gone-vm"))


class _FakeTask:
    pass


class _FakePlacementVm:
    def __init__(self, nics: list[object], parent: object, *, moref: str = "vm-2728", notes: str = "", values: list | None = None) -> None:
        self._moId = moref
        self.config = types.SimpleNamespace(hardware=types.SimpleNamespace(device=nics), annotation=notes)
        self.parent = parent
        self.customValue = values or []
        self.reconfigs: list[object] = []

    def ReconfigVM_Task(self, spec: object) -> object:
        self.reconfigs.append(spec)
        if getattr(spec, "annotation", None) is not None:
            self.config.annotation = spec.annotation
        return _FakeTask()


class _FakePlacementContent:
    def __init__(self, vm: object, *, permissions: list | None = None, alarms: list | None = None, api_type: str = "VirtualCenter") -> None:
        self.about = types.SimpleNamespace(apiType=api_type)
        self.set_fields: list[tuple] = []
        self.set_permissions: list[tuple] = []
        self.created_alarms: list[tuple] = []
        self.raise_on_permissions: Exception | None = None
        content = self

        class Fields:
            field = (types.SimpleNamespace(key=101, name="Owner"),)

            def SetField(self, entity, key, value):
                content.set_fields.append((entity, key, value))

        class Authorization:
            def RetrieveEntityPermissions(self, entity, inherited):
                return permissions or []

            def SetEntityPermissions(self, entity, permission):
                if content.raise_on_permissions is not None:
                    raise content.raise_on_permissions
                content.set_permissions.append((entity, permission))

        class Alarms:
            def GetAlarm(self, entity):
                return alarms or []

            def CreateAlarm(self, entity, spec):
                content.created_alarms.append((entity, spec))

        self.customFieldsManager = Fields()
        self.authorizationManager = Authorization()
        self.alarmManager = Alarms()


def _nic(label: str, mac: str, address_type: str = "assigned") -> object:
    return vim.vm.device.VirtualVmxnet3(deviceInfo=vim.Description(label=label, summary="VM Network"), macAddress=mac, addressType=address_type)


def _alarm(vm: object, name: str) -> object:
    expression = vim.alarm.StateAlarmExpression(operator="isEqual", type=vim.VirtualMachine, statePath="runtime.powerState", red="poweredOn")
    info = types.SimpleNamespace(
        name=name, description="", enabled=True, entity=vm, expression=vim.alarm.OrAlarmExpression(expression=[expression]),
        action=None, actionFrequency=0, setting=None,
    )
    return types.SimpleNamespace(info=info)


class _FakeVapi:
    def __init__(self, attached: list[str] | None = None) -> None:
        self.attached = attached or []
        self.calls: list[tuple] = []

    @contextmanager
    def session(self):
        def call(method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            if "list-attached-tags" in path:
                return self.attached
            if path.startswith("cis/tagging/tag/"):
                return {"name": {"tag-1": "prod"}.get(path.rsplit("/", 1)[1], "other")}
            return None

        yield call


class VCenterNodePlacementTests(unittest.TestCase):
    def _node(self, vm: object, content: _FakePlacementContent, vapi: _FakeVapi | None = None) -> VCenterNode:
        node = VCenterNode("vc.local", "administrator", "secret")
        node._get_vm = lambda vm_name: vm  # type: ignore[method-assign]
        node._wait_for_task = lambda task: None  # type: ignore[method-assign]
        node.si = types.SimpleNamespace(_stub=None, RetrieveContent=lambda: content)
        node._vapi_session = (vapi or _FakeVapi()).session  # type: ignore[method-assign]
        return node

    def _original(self) -> tuple[_FakePlacementVm, _FakePlacementContent]:
        vm = _FakePlacementVm(
            [_nic("Network adapter 1", "00:50:56:b8:ba:eb"), _nic("Network adapter 2", "00:50:56:01:02:03", "manual")],
            vim.Folder("group-v42", None),
            notes="web tier, owner team-a",
            values=[types.SimpleNamespace(key=101, value="team-a")],
        )
        permission = types.SimpleNamespace(principal="VSPHERE.LOCAL\\ReadOnlyUsers", group=True, roleId=-2, propagate=False)
        content = _FakePlacementContent(vm, permissions=[permission], alarms=[])
        content.alarmManager.GetAlarm = lambda entity: [_alarm(vm, "web down")]
        return vm, content

    def test_everything_the_vm_being_replaced_has_is_read(self) -> None:
        vm, content = self._original()
        node = self._node(vm, content, _FakeVapi(["tag-1"]))

        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/Datacenter/vm/Apps"):
            placement = node.read_vm_placement("vm1")

        self.assertEqual(placement["folder"], {"moref": "group-v42", "path": "/Datacenter/vm/Apps"})
        self.assertEqual(
            placement["network_adapters"],
            [
                {"label": "Network adapter 1", "mac": "00:50:56:b8:ba:eb", "address_type": "assigned"},
                {"label": "Network adapter 2", "mac": "00:50:56:01:02:03", "address_type": "manual"},
            ],
        )
        self.assertEqual(placement["notes"], "web tier, owner team-a")
        self.assertEqual(placement["custom_attributes"], [{"key": 101, "name": "Owner", "value": "team-a"}])
        self.assertEqual(placement["permissions"], [{"principal": "VSPHERE.LOCAL\\ReadOnlyUsers", "group": True, "role_id": -2, "propagate": False}])
        self.assertEqual(placement["alarms"], ["web down"])
        self.assertEqual(placement["tags"], [{"id": "tag-1", "name": "prod"}])
        self.assertEqual(placement["unreadable"], {})
        json.dumps(placement)

    def test_a_part_that_cannot_be_read_is_named_and_the_rest_is_still_read(self) -> None:
        vm, content = self._original()
        node = self._node(vm, content)

        def refuse(*args, **kwargs):
            raise RuntimeError("vCenter API POST session answered 503")

        node._vapi_session = refuse  # type: ignore[method-assign]
        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/Datacenter/vm/Apps"):
            placement = node.read_vm_placement("vm1")

        self.assertEqual(placement["unreadable"], {"tags": "vCenter API POST session answered 503"})
        self.assertEqual(placement["alarms"], ["web down"])

    def test_a_host_without_vcenter_has_no_tags_to_read(self) -> None:
        vm, content = self._original()
        content.about.apiType = "HostAgent"
        vapi = _FakeVapi(["tag-1"])

        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/ha-datacenter/vm"):
            placement = self._node(vm, content, vapi).read_vm_placement("vm1")

        self.assertEqual(placement["tags"], [])
        self.assertEqual(vapi.calls, [])

    def test_a_host_without_alarms_or_custom_attributes_reads_none_and_warns_of_nothing(self) -> None:
        vm, content = self._original()
        content.about.apiType = "HostAgent"
        content.alarmManager = None
        content.customFieldsManager = None

        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/ha-datacenter/vm"):
            placement = self._node(vm, content).read_vm_placement("vm1")

        self.assertEqual((placement["alarms"], placement["custom_attributes"], placement["unreadable"]), ([], [], {}))

    def test_the_restored_vm_gets_everything_back(self) -> None:
        moved: list[tuple[str, list[object]]] = []

        class FakeFolder:
            def __init__(self, moref: str, stub: object) -> None:
                self.moref = moref

            def MoveIntoFolder_Task(self, entities: list[object]) -> object:
                moved.append((self.moref, entities))
                return _FakeTask()

        original, content = self._original()
        vapi = _FakeVapi(["tag-1"])
        node = self._node(original, content, vapi)
        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/Datacenter/vm/Apps"):
            placement = node.read_vm_placement("vm1")
        first, second = _nic("Network adapter 1", "00:50:56:b8:cf:5f"), _nic("Network adapter 2", "00:50:56:b8:11:11")
        restored = _FakePlacementVm([first, second], types.SimpleNamespace(_moId="group-v3"), moref="vm-2730", notes="Restored from backup on vc.local")
        node._get_vm = lambda vm_name: restored  # type: ignore[method-assign]
        content.alarmManager.GetAlarm = lambda entity: []

        with patch.object(vim, "Folder", FakeFolder):
            applied = node.apply_vm_placement("vm1", placement)

        self.assertEqual(applied["failed"], {})
        self.assertEqual((first.macAddress, first.addressType), ("00:50:56:b8:ba:eb", "assigned"))
        self.assertEqual((second.macAddress, second.addressType), ("00:50:56:01:02:03", "manual"))
        self.assertEqual(moved, [("group-v42", [restored])])
        self.assertEqual(restored.config.annotation, "web tier, owner team-a")
        self.assertEqual(content.set_fields, [(restored, 101, "team-a")])
        [(entity, permissions)] = content.set_permissions
        self.assertEqual((entity, permissions[0].principal, permissions[0].group, permissions[0].roleId), (restored, "VSPHERE.LOCAL\\ReadOnlyUsers", True, -2))
        [(entity, spec)] = content.created_alarms
        self.assertEqual((entity, spec.name), (restored, "web down"))
        self.assertIn(
            ("POST", "cis/tagging/tag-association?action=attach-multiple-tags-to-object", {"object_id": {"type": "VirtualMachine", "id": "vm-2730"}, "tag_ids": ["tag-1"]}),
            vapi.calls,
        )
        self.assertEqual(
            (applied["tags"], applied["custom_attributes"], applied["permissions"], applied["alarms"], applied["notes"]),
            (["prod"], ["Owner"], ["VSPHERE.LOCAL\\ReadOnlyUsers"], ["web down"], True),
        )

    def test_one_part_that_fails_does_not_stop_the_others(self) -> None:
        original, content = self._original()
        node = self._node(original, content)
        with patch("python.srv.plugins.vcenter.vcenter._vm_folder_path", return_value="/Datacenter/vm/Apps"):
            placement = node.read_vm_placement("vm1")
        placement["folder"] = None
        restored = _FakePlacementVm([_nic("Network adapter 1", "00:50:56:b8:00:01")], types.SimpleNamespace(_moId="group-v42"), moref="vm-2730")
        node._get_vm = lambda vm_name: restored  # type: ignore[method-assign]
        content.alarmManager.GetAlarm = lambda entity: []
        content.raise_on_permissions = RuntimeError("Permission to perform this operation was denied.")
        placement["unreadable"] = {"tags": "vCenter API POST session answered 503"}

        applied = node.apply_vm_placement("vm1", placement)

        self.assertEqual(
            applied["failed"],
            {
                "tags": "it could not be read from the VM that was replaced (vCenter API POST session answered 503)",
                "permissions": "Permission to perform this operation was denied",
            },
        )
        self.assertEqual(content.set_fields, [(restored, 101, "team-a")])
        self.assertEqual(len(content.created_alarms), 1)

    def test_alarms_whose_definitions_were_not_kept_are_reported(self) -> None:
        original, content = self._original()
        node = self._node(original, content)
        placement = {"source_moref": "vm-other", "alarms": ["web down"], "network_adapters": []}
        restored = _FakePlacementVm([], types.SimpleNamespace(_moId="group-v42"), moref="vm-2730")
        node._get_vm = lambda vm_name: restored  # type: ignore[method-assign]
        content.alarmManager.GetAlarm = lambda entity: []

        applied = node.apply_vm_placement("vm1", placement)

        self.assertEqual(applied["failed"], {"alarms": "the definitions of web down were not kept"})

    def test_adapters_without_a_matching_label_are_paired_in_order(self) -> None:
        from python.srv.plugins.vcenter.vcenter import _paired_network_adapters

        restored = [_nic("Network adapter 1", "00:50:56:b8:00:01"), _nic("Network adapter 2", "00:50:56:b8:00:02")]
        originals = [{"label": "NIC A", "mac": "00:50:56:b8:aa:01"}, {"label": "Network adapter 2", "mac": "00:50:56:b8:aa:02"}]

        pairs = _paired_network_adapters(originals, restored)

        self.assertEqual([(original["mac"], device.deviceInfo.label) for original, device in pairs],
                         [("00:50:56:b8:aa:02", "Network adapter 2"), ("00:50:56:b8:aa:01", "Network adapter 1")])

    def test_nothing_is_changed_when_the_vm_already_matches(self) -> None:
        vm = _FakePlacementVm([_nic("Network adapter 1", "00:50:56:b8:ba:eb")], types.SimpleNamespace(_moId="group-v42"), notes="same")
        content = _FakePlacementContent(vm)
        placement = {
            "folder": {"moref": "group-v42", "path": "/Datacenter/vm/Apps"},
            "network_adapters": [{"label": "Network adapter 1", "mac": "00:50:56:b8:ba:eb", "address_type": "assigned"}],
            "notes": "same",
        }

        applied = self._node(vm, content).apply_vm_placement("vm1", placement)

        self.assertEqual(vm.reconfigs, [])
        self.assertEqual(applied["failed"], {})

    def test_a_vm_that_had_more_adapters_than_the_restored_one_gives_only_what_fits(self) -> None:
        vm = _FakePlacementVm([_nic("Network adapter 1", "00:50:56:b8:00:01")], types.SimpleNamespace(_moId="group-v42"))
        placement = {
            "folder": None,
            "network_adapters": [
                {"label": "Network adapter 1", "mac": "00:50:56:b8:aa:01", "address_type": "assigned"},
                {"label": "Network adapter 2", "mac": "00:50:56:b8:aa:02", "address_type": "assigned"},
            ],
        }

        applied = self._node(vm, _FakePlacementContent(vm)).apply_vm_placement("vm1", placement)

        self.assertEqual(applied["network_adapters"], [{"label": "Network adapter 1", "mac": "00:50:56:b8:aa:01"}])
