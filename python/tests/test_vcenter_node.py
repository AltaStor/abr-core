# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import threading
import time
import types
import unittest

try:
    from python.srv.plugins.vcenter.vcenter import VCenterNode, _normalize_disk_uuid
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.plugins.vcenter.vcenter import VCenterNode, _normalize_disk_uuid

from pyVmomi import vim, vmodl


class _FakeContainer:
    def __init__(self, view: list[object]) -> None:
        self.view = view
        self.destroyed = False

    def Destroy(self) -> None:
        self.destroyed = True


class _FakeViewManager:
    def __init__(self, view: list[object]) -> None:
        self._view = view

    def CreateContainerView(self, root: object, types: object, recursive: bool) -> _FakeContainer:
        return _FakeContainer(self._view)


class _FakeContent:
    def __init__(self, view: list[object]) -> None:
        self.rootFolder = object()
        self.viewManager = _FakeViewManager(view)


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
        self.power_off_calls = 0
        self.power_on_calls = 0
        self.rename_calls: list[str] = []

    def Destroy_Task(self) -> object:
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

        node._get_host = lambda host_name: host if host_name == "esx-01.local" else None  # type: ignore[method-assign]
        node._get_vm = lambda vm_name: None  # type: ignore[method-assign]
        node._get_datacenter = lambda obj: datacenter  # type: ignore[method-assign]
        node._wait_for_task = lambda task: created_vm  # type: ignore[method-assign]

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
