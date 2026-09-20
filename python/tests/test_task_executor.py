# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import json
import subprocess
import unittest
from contextlib import contextmanager
from unittest.mock import patch

try:
    from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, RESOURCES_BY_HV, TASKS
    from python.srv.core.storage_tool import StorageToolError
    from python.srv.core.subprocess_util import ProcessCancelled
    from python.srv.core.task_executor import TaskStopRequested, run_task
    from python.srv.plugins.vcenter.vcenter_disk_types import VddkDiskType
    from python.srv.plugins.vcenter.vcenter_backup import (
        _accumulate_tool_sizes,
        _compression_ratio,
        _disk_timeout_seconds,
        _parse_tool_result,
    )
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, RESOURCES_BY_HV, TASKS
    from python.srv.core.storage_tool import StorageToolError
    from python.srv.core.subprocess_util import ProcessCancelled
    from python.srv.core.task_executor import TaskStopRequested, run_task
    from python.srv.plugins.vcenter.vcenter_disk_types import VddkDiskType
    from python.srv.plugins.vcenter.vcenter_backup import (
        _accumulate_tool_sizes,
        _compression_ratio,
        _disk_timeout_seconds,
        _parse_tool_result,
    )

# Progress the mover prints before its --json result document.
TOOL_PROGRESS = "2%\n50%\n100%\n"


class FakeLocalizableMessage:
    def __init__(self, key: str, message: str = "") -> None:
        self.key = key
        self.message = message


class FakeVimFault(Exception):
    """Stand-in for a pyVmomi vim.fault.* SOAP fault, which carries its
    machine-readable identity in faultMessage[].key rather than its Python
    exception type."""

    def __init__(self, msg: str, fault_message_key: str = "") -> None:
        super().__init__(msg)
        self.msg = msg
        self.faultMessage = [FakeLocalizableMessage(fault_message_key, msg)] if fault_message_key else []


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeVCenterNode:
    snapshots_created: list[str] = []
    snapshots_removed: list[str] = []
    attachments: list[dict[str, object]] = []
    created_vms: list[dict[str, object]] = []
    created_managed_disks: list[dict[str, object]] = []
    detached_disks: list[dict[str, object]] = []
    deleted_managed_disks: list[str] = []
    destroyed_vms: list[str] = []
    destroyed_vm_requests: list[dict[str, object]] = []
    powered_off_vms: list[str] = []
    powered_on_vms: list[str] = []
    renamed_vms: list[dict[str, object]] = []
    raise_on_rename_vm: Exception | None = None
    raise_on_read_identity: Exception | None = None
    raise_on_set_identity: Exception | None = None
    original_identity: dict = {"uuid": "42200000-1111-2222-3333-444455556666", "instance_uuid": "50200000-1111-2222-3333-444455556666"}
    identity_reads: list = []
    applied_identities: list = []
    changed_area_queries: list[dict[str, object]] = []
    service_type: str = "VirtualCenter"
    vm_firmware: str | None = "bios"
    cbt_enable_calls: list[str] = []
    cbt_enabled_result: bool = True
    raise_on_query_changed_disk_areas: Exception | None = None
    created_vm_firmware: str | None = None
    host_connected: bool = True
    keepalive_windows: int = 0
    snapshot_change_ids: list[dict[str, object]] = [
        {
            "disk_label": "Hard disk 1",
            "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
            "change_id": "change-001",
            "uuid": "disk-uuid-1",
            "controller_key": 1000,
            "unit_number": 0,
            "key": 2000,
        }
    ]

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 443,
        verify_ssl: bool = False,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl

    def __enter__(self) -> "FakeVCenterNode":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    @contextmanager
    def session_keepalive(self, interval_seconds: float | None = None):
        # The real node pings vCenter on a background thread so the session
        # survives a long transfer; nothing to keep alive against a fake.
        self.keepalive_windows += 1
        yield

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def backup(self, task: dict[str, object], progress_callback=None) -> dict[str, object]:
        from python.srv.plugins.vcenter.vcenter_backup import run_vcenter_backup

        return run_vcenter_backup(task, progress_callback=progress_callback)

    def restore(self, task: dict[str, object], progress_callback=None) -> dict[str, object]:
        from python.srv.plugins.vcenter.vcenter_restore import run_vcenter_restore

        return run_vcenter_restore(task, progress_callback=progress_callback)

    def create_backup_snapshot(self, vm_name: str) -> str:
        name = f"{vm_name}-snapshot"
        self.snapshots_created.append(name)
        return name

    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> dict[str, str]:
        return {"vm_moref": "vm-123", "snapshot_moref": "snapshot-456"}

    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        self.cbt_enable_calls.append(vm_name)
        return self.cbt_enabled_result

    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> list[dict[str, object]]:
        return list(self.snapshot_change_ids)

    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: int,
        change_id: str,
    ) -> list[dict[str, int]]:
        self.changed_area_queries.append(
            {
                "vm_name": vm_name,
                "snapshot_name": snapshot_name,
                "disk_key": disk_key,
                "change_id": change_id,
            }
        )
        if FakeVCenterNode.raise_on_query_changed_disk_areas is not None:
            raise FakeVCenterNode.raise_on_query_changed_disk_areas
        return [{"offset": 0, "length": 4096}, {"offset": 8192, "length": 4096}]

    def get_thumbprint(self) -> str:
        return "AA:BB:CC"

    def get_service_type(self) -> str:
        return self.service_type

    def clustered_host_name(self) -> str | None:
        # Standalone ESXi host that is not part of any cluster.
        return None

    def ensure_vm_host_connected(self, vm_name: str) -> None:
        if not FakeVCenterNode.host_connected:
            raise RuntimeError(
                f"Cannot start backup for VM '{vm_name}': its ESXi host "
                "'esx-01.local' is disconnected in vCenter, not connected. "
                "Reconnect the host in vCenter (or wait for it to reconnect) and retry the backup."
            )

    def get_vm_disks(self, vm_name: str) -> list[dict[str, object]]:
        return [
            {
                "disk_label": "Hard disk 1",
                "capacity_gb": 40,
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "uuid": "disk-uuid-1",
                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                "source_disk_type_name": "vmfs_thin",
            }
        ]

    def get_vm_restore_metadata(self, vm_name: str) -> dict[str, object]:
        metadata: dict[str, object] = {
            "name": vm_name,
            "guest_id": "ubuntu64Guest",
            "guest_os": "Ubuntu Linux (64-bit)",
            "num_cpu": 2,
            "memory_mb": 4096,
            "power_state": "poweredOff",
            "source_host": "esx-src.local",
            "network_adapters": [
                {
                    "label": "Network adapter 1",
                    "network": "VM Network",
                    "connected": True,
                }
            ],
        }
        if self.vm_firmware is not None:
            metadata["firmware"] = self.vm_firmware
        return metadata

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        self.snapshots_removed.append(snapshot_name)

    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
        disk_uuid: str | None = None,
    ) -> dict[str, object]:
        payload = {
            "vm_name": vm_name,
            "datastore_path": datastore_path,
            "disk_label": disk_label,
            "disk_uuid": disk_uuid,
            "controller_key": 1000,
            "unit_number": 1,
            "label": disk_label or "Restored disk",
        }
        self.attachments.append(payload)
        return payload

    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: dict[str, object],
        disk_layout: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        disk_paths = [
            f"[{datastore_name}] {vm_name}/{disk['file_name']}"
            for disk in (disk_layout or [])
        ]
        requested_firmware = str(config_metadata.get("firmware") or "bios")
        restored_firmware = self.created_vm_firmware or requested_firmware
        payload = {
            "vm_name": vm_name,
            "host_name": host_name,
            "datastore_name": datastore_name,
            "config_metadata": config_metadata,
            "vm_moref": "vm-restore-001",
            "disk_layout": disk_layout or [],
            "disk_paths": disk_paths,
            "requested_firmware": requested_firmware,
            "restored_firmware": restored_firmware,
        }
        self.created_vms.append(payload)
        return payload

    def create_managed_disk(
        self,
        *,
        datastore_name: str,
        disk_path: str,
        capacity_gb: float,
        disk_type_code: int = VddkDiskType.VMFS_THIN,
    ) -> dict[str, object]:
        payload = {
            "datastore_name": datastore_name,
            "disk_path": disk_path,
            "capacity_gb": capacity_gb,
            "disk_type_code": disk_type_code,
        }
        self.created_managed_disks.append(payload)
        return payload

    def detach_disk(self, vm_name: str, datastore_path: str) -> bool:
        self.detached_disks.append({"vm_name": vm_name, "datastore_path": datastore_path})
        return True

    def delete_managed_disk(self, disk_path: str) -> None:
        self.deleted_managed_disks.append(disk_path)

    def destroy_vm(self, vm_name: str, vm_moref: str | None = None) -> None:
        self.destroyed_vm_requests.append({"vm_name": vm_name, "vm_moref": vm_moref})
        self.destroyed_vms.append(vm_name)

    def power_off_vm(self, vm_name: str, vm_moref: str | None = None) -> dict[str, object]:
        self.powered_off_vms.append(vm_name)
        return {"vm_name": vm_name, "vm_moref": vm_moref or "vm-restore-001", "power_state": "poweredOff", "changed": True}

    def power_on_vm(self, vm_name: str, vm_moref: str | None = None) -> dict[str, object]:
        self.powered_on_vms.append(vm_name)
        return {"vm_name": vm_name, "vm_moref": vm_moref or "vm-restore-001", "power_state": "poweredOn", "changed": True}

    def rename_vm(self, vm_name: str, new_name: str, vm_moref: str | None = None) -> dict[str, object]:
        if FakeVCenterNode.raise_on_rename_vm is not None:
            raise FakeVCenterNode.raise_on_rename_vm
        self.renamed_vms.append({"from": vm_name, "to": new_name, "vm_moref": vm_moref})
        return {"vm_name": new_name, "vm_moref": vm_moref or "vm-restore-001", "previous_name": vm_name, "changed": True}

    def read_vm_identity(self, vm_name: str, vm_moref: str | None = None) -> dict[str, str]:
        if FakeVCenterNode.raise_on_read_identity is not None:
            raise FakeVCenterNode.raise_on_read_identity
        FakeVCenterNode.identity_reads.append(vm_name)
        return dict(FakeVCenterNode.original_identity)

    def set_vm_identity(
        self,
        vm_name: str,
        *,
        uuid: str = "",
        instance_uuid: str = "",
        vm_moref: str | None = None,
    ) -> dict[str, object]:
        if FakeVCenterNode.raise_on_set_identity is not None:
            raise FakeVCenterNode.raise_on_set_identity
        FakeVCenterNode.applied_identities.append(
            {"vm_name": vm_name, "uuid": uuid, "instance_uuid": instance_uuid}
        )
        return {"changed": True}

    def _get_vm(self, vm_name: str):
        return types.SimpleNamespace(_moId="vm-123")


# abr-vmdk-tool is built in the separate abr-vmdk-tools repository and is never
# present in this checkout, so tests point tool_path at a file that always is:
# run_capped is mocked, and only the existence check looks at the path.
EXISTING_TOOL_PATH = str(pathlib.Path(__file__).resolve())


def abr_artifact_path(command: list[str]) -> pathlib.Path:
    """Where file-mode abr-vmdk-tool would write the artifact for this command.

    Read out of the command rather than assumed at a fixed index: the options
    are order-independent, and a stale index silently drops the stand-in file
    somewhere else instead of failing.
    """
    folder = command[command.index("--folder") + 1]
    name = command[command.index("--name") + 1]
    return pathlib.Path(folder) / name


class TaskExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        # The backup-completion manifest write mirrors itself into the abr
        # metadata store via `meta_save` (best-effort). Stub it out so
        # tests exercise the mock, not a real abr-vmdk-tool subprocess.
        run_disk_tool_patcher = patch(
            "python.srv.plugins.vcenter.vcenter_backup.run_disk_tool",
            return_value=None,
        )
        self.run_disk_tool_mock = run_disk_tool_patcher.start()
        self.addCleanup(run_disk_tool_patcher.stop)

        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        BACKUPS.clear()
        FakeVCenterNode.snapshots_created.clear()
        FakeVCenterNode.snapshots_removed.clear()
        FakeVCenterNode.attachments.clear()
        FakeVCenterNode.created_vms.clear()
        FakeVCenterNode.created_managed_disks.clear()
        FakeVCenterNode.detached_disks.clear()
        FakeVCenterNode.deleted_managed_disks.clear()
        FakeVCenterNode.destroyed_vms.clear()
        FakeVCenterNode.destroyed_vm_requests.clear()
        FakeVCenterNode.powered_off_vms.clear()
        FakeVCenterNode.powered_on_vms.clear()
        FakeVCenterNode.renamed_vms.clear()
        FakeVCenterNode.raise_on_rename_vm = None
        FakeVCenterNode.raise_on_read_identity = None
        FakeVCenterNode.raise_on_set_identity = None
        FakeVCenterNode.identity_reads.clear()
        FakeVCenterNode.applied_identities.clear()
        FakeVCenterNode.original_identity = {
            "uuid": "42200000-1111-2222-3333-444455556666",
            "instance_uuid": "50200000-1111-2222-3333-444455556666",
        }
        FakeVCenterNode.changed_area_queries.clear()
        FakeVCenterNode.service_type = "VirtualCenter"
        FakeVCenterNode.vm_firmware = "bios"
        FakeVCenterNode.cbt_enable_calls.clear()
        FakeVCenterNode.cbt_enabled_result = True
        FakeVCenterNode.raise_on_query_changed_disk_areas = None
        FakeVCenterNode.created_vm_firmware = None
        FakeVCenterNode.host_connected = True
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        BACKUPS.clear()

    def _assert_abr_mode_file(self, command: list[str]) -> None:
        """Assert the command carries the consecutive --mode file pair."""
        self.assertIn("--mode", command)
        mode_index = command.index("--mode")
        self.assertEqual(command[mode_index + 1], "file")

    def _register_backup_point(
        self,
        point_id: str,
        manifest_path: "pathlib.Path | str",
        *,
        parent_point_id: str | None = None,
    ) -> dict[str, object]:
        """Register an in-memory restore point used by the .abr restore path."""
        point = {
            "id": point_id,
            "parent_point_id": parent_point_id,
            "artifact": {"manifest_path": str(manifest_path)},
        }
        BACKUPS[point_id] = point
        return point

    def test_mock_backup_tasks_still_use_mock_worker(self) -> None:
        HYPERVISORS["hv-mock"] = {"id": "hv-mock", "kind": "mock"}
        task = {
            "id": "task-mock",
            "status": "queued",
            "task_type": "backup",
            "hypervisor_id": "hv-mock",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-1",
            "parameters": {"duration_seconds": 0.01},
        }

        result = run_task(task, simulated_seconds=0.01)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["worker_mode"], "mock-backup-emulator")
        self.assertEqual(result["result"]["operation"], "backup")

    def test_vcenter_backup_task_uses_abr_vmdk_tool_worker(self) -> None:
        FakeVCenterNode.vm_firmware = "efi"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        progress_updates: list[dict[str, object]] = []
        recorded_commands: list[list[str]] = []
        manifest_payload: dict[str, object] | None = None
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task, progress_callback=progress_updates.append)
            manifest_payload = json.loads(
                pathlib.Path(result["result"]["manifest_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["worker_mode"], "vcenter-abr-vmdk-tool")
        self.assertEqual(payload["operation"], "backup")
        self.assertEqual(payload["vm_name"], "demo-vm")
        self.assertEqual(payload["disk_count"], 1)
        self.assertTrue(payload["manifest_path"].endswith("manifest.json"))
        self.assertEqual(payload["vm_config"]["guest_id"], "ubuntu64Guest")
        self.assertEqual(payload["vm_config"]["firmware"], "efi")
        self.assertEqual(payload["disks"][0]["disk_label"], "Hard disk 1")
        self.assertEqual(payload["disks"][0]["source_disk_type_code"], int(VddkDiskType.VMFS_THIN))
        self.assertEqual(payload["disks"][0]["source_disk_type_name"], "vmfs_thin")
        self.assertIsNotNone(manifest_payload)
        self.assertEqual(manifest_payload["vm_config"]["firmware"], "efi")
        self.assertEqual(FakeVCenterNode.snapshots_created, ["demo-vm-snapshot"])
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])
        # CBT must be ensured for a FULL backup too (not just incrementals), so the
        # full anchors the chain on a valid CBT baseline. Otherwise later
        # incrementals fail with "Nonmatching change UUID".
        self.assertEqual(FakeVCenterNode.cbt_enable_calls, ["demo-vm"])
        self.assertTrue(any(update.get("stage") == "cbt" for update in progress_updates))
        self.assertTrue(any(update.get("stage") == "transfer" for update in progress_updates))
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[1], "backup")
        backup_name = command[command.index("--name") + 1]
        self.assertTrue(backup_name.endswith("-demo-vm.vmdk"))
        self._assert_abr_mode_file(command)
        self.assertIn("--vm", command)
        self.assertIn("moref=vm-123", command)
        self.assertIn("--snapshot", command)
        self.assertIn("snapshot-456", command)
        self.assertEqual(command[command.index("--vmdk") + 1], "[datastore1] demo-vm/demo-vm.vmdk")
        # The launcher script selects the VDDK, so no bundle path is passed;
        # --libdir does not exist any more and must not come back.
        self.assertNotIn("--libdir", command)
        self.assertEqual(payload["disks"][0]["destination_path"], "")
        self.assertEqual(payload["disks"][0]["abr_backup_name"], backup_name)
        self.assertTrue(payload["disks"][0]["abr_path"])
        self.assertEqual(payload["disks"][0]["storage_mode"], "local")

        self.run_disk_tool_mock.assert_called_once()
        save_meta_command = self.run_disk_tool_mock.call_args.args[0]
        self.assertEqual(save_meta_command[0], "meta_save")
        self.assertEqual(save_meta_command[save_meta_command.index("--name") + 1], "manifest")
        self._assert_abr_mode_file(save_meta_command)
        self.assertEqual(save_meta_command[-1], result["result"]["manifest_path"])
        # Default compression level (matches the abr-vmdk-tool --compression default).
        self.assertIn("--compression", command)
        self.assertEqual(command[command.index("--compression") + 1], "3")
        self.assertEqual(payload["compression_level"], 3)
        self.assertEqual(manifest_payload["compression_level"], 3)

    def test_vcenter_backup_records_real_size_and_compression_ratio_from_the_tool(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        recorded_commands: list[list[str]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-sizes",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {"destination_dir": temp_dir, "tool_path": EXISTING_TOOL_PATH},
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(
                    returncode=0,
                    stdout=TOOL_PROGRESS
                    + json.dumps(
                        {
                            "command": "backup",
                            "status": "ok",
                            "raw_data_size": 8_471_179_264,
                            "stored_data_size": 3_016_784_896,
                            "compression_ratio": 0.356124,
                        }
                    ),
                )

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertIn("--json", recorded_commands[0])
        self.assertEqual(payload["logical_bytes"], 8_471_179_264)
        # transferred_bytes is what the mover says it wrote, not the directory
        # scan's guess -- the scan can't see an S3 repository at all.
        self.assertEqual(payload["transferred_bytes"], 3_016_784_896)
        self.assertAlmostEqual(payload["compression_ratio"], 0.356124, places=5)
        disk = payload["disks"][0]
        self.assertEqual(disk["logical_bytes"], 8_471_179_264)
        self.assertEqual(disk["stored_bytes"], 3_016_784_896)
        self.assertAlmostEqual(disk["compression_ratio"], 0.356124, places=5)

    def test_vcenter_backup_keeps_its_own_estimates_when_the_tool_reports_no_sizes(self) -> None:
        # An older mover build prints no result document. The backup must still
        # complete, just without the exact figures.
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-no-sizes",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {"destination_dir": temp_dir, "tool_path": EXISTING_TOOL_PATH},
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_backup.run_capped",
                    side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="Done"),
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertIsNone(payload["logical_bytes"])
        self.assertIsNone(payload["compression_ratio"])
        self.assertIsNone(payload["disks"][0]["logical_bytes"])

    def test_vcenter_backup_task_completes_even_if_the_manifest_metadata_mirror_fails(self) -> None:
        self.run_disk_tool_mock.side_effect = StorageToolError("bucket unreachable")
        FakeVCenterNode.vm_firmware = "efi"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task, progress_callback=lambda *_: None)
            manifest_written = pathlib.Path(result["result"]["manifest_path"]).is_file()

        self.assertEqual(result["status"], "completed")
        self.assertTrue(manifest_written)
        self.run_disk_tool_mock.assert_called_once()

    def test_vcenter_backup_task_fails_cleanly_when_vm_host_is_disconnected(self) -> None:
        FakeVCenterNode.host_connected = False
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-disconnected-host",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {"destination_dir": temp_dir, "tool_path": EXISTING_TOOL_PATH},
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        message = result["result"]["message"]
        self.assertIn("esx-01.local", message)
        self.assertIn("Reconnect the host", message)
        # No raw vmodl fault dump ("(vmodl.fault...) { dynamicType = ...") should
        # ever reach the task's error message.
        self.assertNotIn("dynamicType", message)
        # The preflight check must reject the job before any snapshot/CBT work
        # is attempted against the unreachable host.
        self.assertEqual(FakeVCenterNode.snapshots_created, [])
        self.assertEqual(FakeVCenterNode.cbt_enable_calls, [])

    def test_vcenter_backup_task_surfaces_real_tool_progress(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        progress_updates: list[dict[str, object]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-progress",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], on_progress=None, **kwargs) -> FakeCompletedProcess:
                if on_progress is not None:
                    on_progress(25, 1)
                    on_progress(75, 2)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task, progress_callback=progress_updates.append)

        self.assertEqual(result["status"], "completed")

        tool_updates = [u for u in progress_updates if "tool_block_index" in u]
        self.assertEqual(len(tool_updates), 2)
        self.assertEqual(tool_updates[0]["tool_block_index"], 1)
        self.assertEqual(tool_updates[1]["tool_block_index"], 2)
        self.assertIn("25%", tool_updates[0]["message"])
        self.assertIn("75%", tool_updates[1]["message"])
        self.assertFalse(tool_updates[0]["log_entry"])
        self.assertFalse(tool_updates[1]["log_entry"])
        disk_progress_values = [int(u["progress"]) for u in tool_updates]
        self.assertTrue(all(25 <= p <= 85 for p in disk_progress_values))
        self.assertLess(disk_progress_values[0], disk_progress_values[1])

    def test_vcenter_backup_task_logs_snapshot_removal_when_stopped_mid_backup(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        class _StoppingProgress:
            def __init__(self, stop_after: int) -> None:
                self.calls: list[dict[str, object]] = []
                self.stop_after = stop_after

            def __call__(self, update: dict[str, object]) -> None:
                self.calls.append(update)
                if len(self.calls) >= self.stop_after:
                    raise TaskStopRequested("Session stop requested")

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        progress = _StoppingProgress(stop_after=4)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-stop",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task, progress_callback=progress)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])
        messages = [str(update.get("message") or "") for update in progress.calls]
        self.assertTrue(
            any("Removing snapshot" in message for message in messages),
            f"expected a 'Removing snapshot' log entry, got: {messages}",
        )

    def test_vcenter_backup_task_honors_custom_compression_level(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        recorded_commands: list[list[str]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-compression",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "compression_level": 9,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["compression_level"], 9)
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertIn("--compression", command)
        self.assertEqual(command[command.index("--compression") + 1], "9")

    def test_vcenter_backup_task_rejects_out_of_range_compression_level(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-bad-compression",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "compression_level": 10,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "VCenterBackupError")
        self.assertIn("compression_level", result["result"]["message"])

    def test_vcenter_incremental_backup_honors_compression_level(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm-parent.vmdk",
                                "uuid": "N/A",
                                "change_id": "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-compression",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "compression_level": 1,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["compression_level"], 1)
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[command.index("--type") + 1], "incremental")
        self.assertIn("--compression", command)
        self.assertEqual(command[command.index("--compression") + 1], "1")

    def test_vcenter_backup_task_omits_encryption_flags_by_default(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        recorded_commands: list[list[str]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-no-encryption",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["result"]["encryption_enabled"])
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertNotIn("--key", command)

    def test_vcenter_backup_task_honors_encryption_settings(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        recorded_commands: list[list[str]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-encryption",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "encryption_enabled": True,
                    "encryption_key": "hunter2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertTrue(payload["encryption_enabled"])
        self.assertEqual(payload["encryption_algorithm"], 1)
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertIn("--key", command)
        self.assertEqual(command[command.index("--key") + 1], "hunter2")

    def test_vcenter_backup_task_rejects_encryption_enabled_without_key(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-bad-encryption",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "encryption_enabled": True,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "VCenterBackupError")
        self.assertIn("encryption_key", result["result"]["message"])

    def test_vcenter_incremental_backup_honors_encryption_settings(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm-parent.vmdk",
                                "uuid": "N/A",
                                "change_id": "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-encryption",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "encryption_enabled": True,
                    "encryption_key": "hunter2",
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[command.index("--type") + 1], "incremental")
        self.assertIn("--key", command)
        self.assertEqual(command[command.index("--key") + 1], "hunter2")

    def test_vcenter_disk_backup_task_uses_selected_disk_only(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                },
            },
        }

        recorded_commands: list[list[str]] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-disk",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "disk",
                "resource_external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                output_path = abr_artifact_path(command)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("fake-backup", encoding="utf-8")
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["disk_count"], 1)
        self.assertEqual(len(recorded_commands), 1)
        self.assertIn("[datastore1] demo-vm/demo-vm.vmdk", recorded_commands[0])

    def test_vcenter_incremental_backup_stores_changed_blocks_using_parent_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm-parent.vmdk",
                                "uuid": "N/A",
                                "change_id": "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertTrue(payload["incremental"])
        self.assertEqual(payload["backup_mode"], "incremental")
        self.assertTrue(payload["cbt_enabled"])
        self.assertEqual(payload["disk_count"], 1)
        self.assertEqual(payload["disks"][0]["change_id"], "change-001")
        self.assertEqual(
            payload["disks"][0]["previous_change_id"],
            "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
        )
        self.assertEqual(
            payload["disks"][0]["utility_change_id"],
            "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
        )
        self.assertEqual(payload["disks"][0]["utility_change_id_format"], "auto")
        self.assertEqual(payload["disks"][0]["changed_area_count"], 2)
        self.assertEqual(payload["disks"][0]["changed_bytes"], 8192)
        # .abr incrementals chain by backup name + changeId, so there is no
        # parent .vmdk file nor single destination file recorded.
        self.assertEqual(payload["disks"][0]["parent_path"], "")
        self.assertEqual(payload["disks"][0]["destination_path"], "")
        self.assertEqual(payload["disks"][0]["abr_backup_name"], "01-demo-vm.vmdk")
        self.assertEqual(payload["disks"][0]["storage_mode"], "local")
        self.assertEqual(
            FakeVCenterNode.changed_area_queries,
            [
                {
                    "vm_name": "demo-vm",
                    "snapshot_name": "demo-vm-snapshot",
                    "disk_key": 2000,
                    "change_id": "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
                }
            ],
        )
        self.assertEqual(FakeVCenterNode.cbt_enable_calls, ["demo-vm"])
        self.assertEqual(FakeVCenterNode.snapshots_created, ["demo-vm-snapshot"])
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[command.index("--type") + 1], "incremental")
        self.assertNotIn("-parent", command)
        self._assert_abr_mode_file(command)
        self.assertIn("--change-id", command)
        self.assertIn("52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37", command)
        self.assertIn("--transport", command)
        self.assertIn("nbdssl", command)

    def test_vcenter_incremental_backup_can_omit_transport_mode(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "parent-change-001",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-auto-transport",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "transport_mode": "auto",
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[command.index("--type") + 1], "incremental")
        self._assert_abr_mode_file(command)
        self.assertIn("--vm", command)
        self.assertIn("--snapshot", command)
        self.assertNotIn("--transport", command)
        self.assertNotIn("nbdssl", command)
        self.assertEqual(command[command.index("--vmdk") + 1], "[datastore1] demo-vm/demo-vm.vmdk")

    def test_vcenter_differential_backup_uses_latest_full_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            vm_root = pathlib.Path(temp_dir) / "demo-vm"
            full_task_root = vm_root / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            (full_task_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "full-change-001",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            incremental_task_root = vm_root / "task-incremental"
            incremental_task_root.mkdir(parents=True, exist_ok=True)
            (incremental_task_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "backup_mode": "incremental",
                        "incremental": True,
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-21T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "incremental-change-002",
                                "destination_path": "",
                                "abr_path": str(incremental_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            task = {
                "id": "task-vcenter-differential",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "differential": True,
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertTrue(payload["differential"])
        self.assertFalse(payload["incremental"])
        self.assertEqual(payload["backup_mode"], "differential")
        self.assertEqual(payload["disks"][0]["previous_change_id"], "full-change-001")
        # The differential is taken against the latest FULL manifest, so the
        # resolved parent manifest must be the full one, not the incremental.
        self.assertEqual(
            payload["disks"][0]["parent_manifest_path"],
            str((full_task_root / "manifest.json").resolve()),
        )
        command = recorded_commands[0]
        self.assertEqual(command[command.index("--type") + 1], "incremental")
        self._assert_abr_mode_file(command)
        self.assertIn("--change-id", command)
        self.assertIn("full-change-001", command)
        self.assertNotIn("incremental-change-002", command)

    def test_incremental_and_differential_cannot_both_be_enabled(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-invalid-changed-block-mode",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "differential": True,
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertIn("cannot both be true", result["result"]["message"])

    def test_hostagent_incremental_backup_uses_raw_change_id_by_default(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm-000001.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]
        HYPERVISORS["hv-esxi"] = {
            "id": "hv-esxi",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-esxi"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True)
            parent_vmdk = previous_task_root / "01-demo-vm.vmdk"
            parent_vmdk.write_text("fake-parent", encoding="utf-8")
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
                                "destination_path": str(parent_vmdk),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-hostagent-incremental",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-esxi",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                output_path = abr_artifact_path(command)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("fake-incremental", encoding="utf-8")
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["service_type"], "HostAgent")
        self.assertEqual(
            payload["disks"][0]["utility_change_id"],
            "52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37",
        )
        self.assertEqual(
            payload["disks"][0]["utility_datastore_path"],
            "[datastore1] demo-vm/demo-vm-000001.vmdk",
        )
        self.assertEqual(payload["disks"][0]["disk_key"], 2000)
        self.assertEqual(payload["disks"][0]["changed_area_count"], 2)
        self.assertIn("52 03 41 89 5f 66 99 4b-98 d6 70 56 4c f7 b5 72/37", recorded_commands[0])
        self.assertIn("[datastore1] demo-vm/demo-vm-000001.vmdk", recorded_commands[0])

    def test_incremental_backup_can_fallback_to_full_store_when_utility_incremental_fails(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        HYPERVISORS["hv-esxi"] = {
            "id": "hv-esxi",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-esxi"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "parent-change-001",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-incremental-full-fallback",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-esxi",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "incremental_fallback_full": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                if "--type" in command:
                    return FakeCompletedProcess(returncode=1, stderr="query changed areas failed")
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["backup_mode"], "incremental-full-fallback")
        self.assertTrue(payload["incremental_full_fallback_used"])
        self.assertEqual(payload["disks"][0]["store_mode"], "full-fallback")
        self.assertTrue(payload["disks"][0]["diagnostic_path"].endswith(".diagnostics.json"))
        self.assertEqual(len(recorded_commands), 2)
        self.assertEqual(recorded_commands[0][recorded_commands[0].index("--type") + 1], "incremental")
        self.assertEqual(recorded_commands[1][1], "backup")
        self._assert_abr_mode_file(recorded_commands[1])
        # A full legacy copy was actually written -- the catalog must record
        # this as a full backup, not the scheduler's original "Incremental".
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "IncrementalFallbackToFull")

    def test_vcenter_incremental_backup_without_parent_manifest_falls_back_to_full(self) -> None:
        HYPERVISORS["hv-esxi"] = {
            "id": "hv-esxi",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-esxi"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "capacity_gb": 40,
                },
            },
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-first-incremental-fallback",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-esxi",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "incremental_fallback_full": True,
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["backup_mode"], "incremental-full-fallback")
        self.assertTrue(payload["incremental_full_fallback_used"])
        # The full-fallback path emits an .abr backup, so the disk artifact is
        # recorded via abr_backup_name rather than a destination .vmdk file.
        self.assertEqual(payload["disks"][0]["destination_path"], "")
        self.assertTrue(str(payload["disks"][0]["abr_backup_name"]).endswith(".vmdk"))
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "IncrementalFallbackToFull")
        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertEqual(command[1], "backup")
        self.assertNotIn("--type", command)
        self._assert_abr_mode_file(command)

    def test_vcenter_incremental_backup_can_query_changed_areas_without_utility_copy(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "parent-change-001",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-query-only",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "query_changed_areas_only": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": "/missing/abr-vmdk-tool",
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="unexpected")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertTrue(payload["incremental"])
        self.assertTrue(payload["query_changed_areas_only"])
        self.assertEqual(payload["disks"][0]["previous_change_id"], "parent-change-001")
        self.assertEqual(payload["disks"][0]["changed_area_count"], 2)
        self.assertEqual(payload["disks"][0]["changed_areas"][0], {"offset": 0, "length": 4096})
        self.assertEqual(payload["disks"][0]["destination_path"], "")
        self.assertEqual(recorded_commands, [])

    def test_vcenter_backup_task_times_out_cleanly(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-timeout",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_timeout_seconds": 1,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                exc = subprocess.TimeoutExpired(
                    cmd=command,
                    timeout=kwargs.get("timeout", 0),
                    output="copy started",
                )
                exc.stderr = "still waiting"
                raise exc

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "VCenterBackupError")
        # tool_timeout_seconds is a FLOOR, not the effective budget: the per-disk
        # timeout scales with the disk's capacity (40GB here), so the reported
        # figure is the scaled one, not the configured 1s.
        expected_timeout = _disk_timeout_seconds(1, 40)
        self.assertGreater(expected_timeout, 1)
        self.assertIn(f"timed out after {expected_timeout} seconds", result["result"]["message"])
        self.assertEqual(FakeVCenterNode.snapshots_created, ["demo-vm-snapshot"])
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])

    def test_progress_reporter_emits_live_byte_counters(self) -> None:
        from python.srv.plugins.vcenter.vcenter_backup import _disk_tool_progress_reporter

        GB = 1024**3
        events: list[dict] = []

        # Backup shape: reads are interpolated from the tool's percent, the
        # written side is a real measurement of the repository directory.
        report = _disk_tool_progress_reporter(
            lambda payload: events.append(payload),
            base_progress=25,
            progress_span=60,
            message_prefix="Backing up disk 1/2",
            base_read_bytes=10 * GB,
            disk_total_bytes=40 * GB,
            measure_transferred=lambda: 7 * GB,
        )
        report(50, 0)
        self.assertEqual(events[-1]["processed_bytes"], 30 * GB)  # 10 + half of 40
        self.assertEqual(events[-1]["transferred_bytes"], 7 * GB)  # measured, not derived
        self.assertEqual(events[-1]["progress"], 55)  # 25 + half of 60

        # Restore shape: nothing local to stat, so BOTH sides interpolate.
        events.clear()
        report = _disk_tool_progress_reporter(
            lambda payload: events.append(payload),
            base_progress=20,
            progress_span=55,
            message_prefix="Restoring disk 1/1",
            base_read_bytes=0,
            disk_total_bytes=8 * GB,
            base_sent_bytes=0,
            disk_sent_bytes=40 * GB,
        )
        for pct in (0, 25, 100):
            report(pct, 0)
        self.assertEqual([e["processed_bytes"] for e in events], [0, 2 * GB, 8 * GB])
        self.assertEqual([e["transferred_bytes"] for e in events], [0, 10 * GB, 40 * GB])
        # Monotonic and strictly increasing — a counter that went backwards
        # would make the console hold its last rate instead of showing a new one.
        sent = [e["transferred_bytes"] for e in events]
        self.assertEqual(sent, sorted(sent))

    def test_disk_timeout_scales_with_capacity_and_floors_at_configured_value(self) -> None:
        # A 94GB disk is the case that regressed: at the shipped 900s flat
        # timeout it was killed mid-transfer despite moving data throughout.
        self.assertGreater(_disk_timeout_seconds(900, 94), 900)
        # Bigger disk => strictly bigger budget.
        self.assertGreater(_disk_timeout_seconds(900, 500), _disk_timeout_seconds(900, 94))
        # A small disk keeps the operator's configured floor rather than
        # shrinking below it.
        self.assertEqual(_disk_timeout_seconds(3600, 1), 3600)
        # Unknown/unusable capacity falls back to the configured value untouched.
        for unknown in (None, "", "not-a-number", 0, -5):
            self.assertEqual(_disk_timeout_seconds(900, unknown), 900)

    def test_tool_result_is_read_from_the_json_document_after_the_progress_lines(self) -> None:
        stdout = TOOL_PROGRESS + json.dumps(
            {
                "command": "backup",
                "status": "ok",
                "raw_data_size": 8_471_179_264,
                "stored_data_size": 3_016_784_896,
                "compression_ratio": 0.356124,
            }
        )

        result = _parse_tool_result(stdout)

        self.assertEqual(result["raw_data_size"], 8_471_179_264)
        self.assertEqual(result["stored_data_size"], 3_016_784_896)

    def test_tool_result_ignores_anything_that_is_not_a_successful_document(self) -> None:
        # A failed attempt's document must not contribute sizes: the same
        # variable carries the stdout of every retry, and only the run that
        # actually stored data reports what it wrote.
        failed = json.dumps({"command": "backup", "status": "error", "error": "disk open failed"})
        for stdout in ("", None, "42%\n100%\n", "not json at all", "{oops", failed):
            self.assertEqual(_parse_tool_result(stdout), {})

    def test_accumulated_tool_sizes_sum_per_disk_and_flag_a_gap(self) -> None:
        totals = {"logical": 0, "stored": 0, "complete": True}
        for raw, stored in ((1_000, 400), (3_000, 600)):
            _accumulate_tool_sizes(
                totals,
                json.dumps({"status": "ok", "raw_data_size": raw, "stored_data_size": stored}),
            )

        self.assertEqual((totals["logical"], totals["stored"], totals["complete"]), (4_000, 1_000, True))

        # One silent disk invalidates the run total rather than undercounting it.
        _accumulate_tool_sizes(totals, "no document here")
        self.assertFalse(totals["complete"])

    def test_compression_ratio_is_stored_over_logical_and_none_when_unknowable(self) -> None:
        self.assertAlmostEqual(_compression_ratio(1_000, 250), 0.25)
        # An incompressible backup can store as much as it read; that is a real
        # ratio of 1.0, not a reason to report nothing.
        self.assertAlmostEqual(_compression_ratio(1_000, 1_000), 1.0)
        for logical, stored in ((0, 100), (None, 100), (1_000, None), (-1, 10), (1_000, -1)):
            self.assertIsNone(_compression_ratio(logical, stored))

    def test_vcenter_vm_restore_carries_the_source_disk_uuid_onto_the_new_disks(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {"firmware": "efi", "guest_id": "windows2022srvNext_64Guest"},
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "6000C292-d2c6-df32-2e38-2d46be8d7a29",
                                "capacity_gb": 40,
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            },
                            {
                                "index": 2,
                                "disk_label": "Hard disk 2",
                                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                                "uuid": "N/A",
                                "capacity_gb": 2,
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "02-demo-vm_1.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-vm-uuid", manifest_path)

            task = {
                "id": "task-vcenter-restore-disk-uuid",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-vm-uuid",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        disk_layout = FakeVCenterNode.created_vms[0]["disk_layout"]
        self.assertEqual(len(disk_layout), 2)
        self.assertEqual(disk_layout[0]["disk_uuid"], "6000C292-d2c6-df32-2e38-2d46be8d7a29")
        self.assertEqual(disk_layout[1]["disk_uuid"], "N/A")

    def test_vcenter_vm_restore_task_creates_new_vm_and_attaches_restored_disks(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {
                    "num_cpu": 2,
                    "memory_mb": 4096,
                    "guest_os": "Ubuntu Linux (64-bit)",
                },
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "firmware": "efi",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [
                                {
                                    "label": "Network adapter 1",
                                    "network": "VM Network",
                                    "connected": True,
                                }
                            ],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-vm-1", manifest_path)

            task = {
                "id": "task-vcenter-restore-vm",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-vm-1",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["status"] if "status" in payload else "completed", "completed")
        self.assertEqual(payload["restore_mode"], "new-vm")
        self.assertEqual(payload["target_vm_name"], "demo-vm-restored")
        self.assertIsNone(payload["disk_type"])
        self.assertEqual(payload["disks"][0]["requested_disk_type"], "original")
        self.assertEqual(payload["disks"][0]["resolved_disk_type_code"], 5)
        self.assertEqual(payload["source_firmware"], "efi")
        self.assertEqual(payload["requested_firmware"], "efi")
        self.assertEqual(payload["restored_firmware"], "efi")
        self.assertEqual(FakeVCenterNode.created_vms[0]["config_metadata"]["firmware"], "efi")
        self.assertEqual(FakeVCenterNode.created_vms[0]["host_name"], "esx-dst.local")
        self.assertEqual(len(FakeVCenterNode.created_vms[0]["disk_layout"]), 1)
        self.assertEqual(len(FakeVCenterNode.created_managed_disks), 0)
        self.assertEqual(len(FakeVCenterNode.attachments), 0)
        command = recorded_commands[0]
        self.assertEqual(command[1], "restore")
        self.assertEqual(command[command.index("--name") + 1], "01-demo-vm.vmdk")
        self._assert_abr_mode_file(command)
        self.assertIn("demo-vm-restored", command[command.index("--vmdk") + 1])

    @staticmethod
    def _disk_c_added_after_incremental_entry(
        index: int, task_root: "pathlib.Path", label: str, filename: str
    ) -> dict[str, object]:
        return {
            "index": index,
            "disk_label": label,
            "datastore_path": f"[datastore1] demo-vm/{filename}",
            "uuid": f"disk-uuid-{index}",
            "destination_path": "",
            "abr_path": str(task_root),
            "abr_backup_name": filename,
            "storage_mode": "local",
            "store_mode": "full",
            "source_disk_type_code": 5,
            "source_disk_type_name": "vmfs_flat",
        }

    def _write_disk_c_added_after_incremental_scenario(
        self, temp_dir: "pathlib.Path"
    ) -> tuple["pathlib.Path", "pathlib.Path", "pathlib.Path"]:
        def _vm_config() -> dict[str, object]:
            return {
                "guest_id": "ubuntu64Guest",
                "guest_os": "Ubuntu Linux (64-bit)",
                "num_cpu": 2,
                "memory_mb": 4096,
                "network_adapters": [],
            }

        entry = self._disk_c_added_after_incremental_entry

        full_v1_root = pathlib.Path(temp_dir) / "task-full-v1"
        full_v1_root.mkdir(parents=True, exist_ok=True)
        full_v1_manifest_path = full_v1_root / "manifest.json"
        full_v1_manifest_path.write_text(
            json.dumps(
                {
                    "backup_mode": "full",
                    "created_at": "2026-08-26T19:38:00+00:00",
                    "vm_name": "demo-vm",
                    "vm_config": _vm_config(),
                    "disks": [
                        entry(1, full_v1_root, "Hard disk 1", "01-demo-vm.vmdk"),
                        entry(2, full_v1_root, "Hard disk 2", "02-demo-vm.vmdk"),
                    ],
                }
            ),
            encoding="utf-8",
        )

        inc_v1_root = pathlib.Path(temp_dir) / "task-inc-v1"
        inc_v1_root.mkdir(parents=True, exist_ok=True)
        inc_v1_manifest_path = inc_v1_root / "manifest.json"
        inc_v1_disks = [
            entry(1, inc_v1_root, "Hard disk 1", "01-demo-vm.vmdk"),
            entry(2, inc_v1_root, "Hard disk 2", "02-demo-vm.vmdk"),
        ]
        for disk in inc_v1_disks:
            disk["store_mode"] = "incremental"
        inc_v1_manifest_path.write_text(
            json.dumps(
                {
                    "backup_mode": "incremental",
                    "created_at": "2026-08-26T19:38:30+00:00",
                    "vm_name": "demo-vm",
                    "vm_config": _vm_config(),
                    "disks": inc_v1_disks,
                }
            ),
            encoding="utf-8",
        )

        full_v2_root = pathlib.Path(temp_dir) / "task-full-v2"
        full_v2_root.mkdir(parents=True, exist_ok=True)
        full_v2_manifest_path = full_v2_root / "manifest.json"
        full_v2_manifest_path.write_text(
            json.dumps(
                {
                    "backup_mode": "full",
                    "created_at": "2026-08-26T19:40:00+00:00",
                    "vm_name": "demo-vm",
                    "vm_config": _vm_config(),
                    "disks": [
                        entry(1, full_v2_root, "Hard disk 1", "01-demo-vm.vmdk"),
                        entry(2, full_v2_root, "Hard disk 2", "02-demo-vm.vmdk"),
                        entry(3, full_v2_root, "Hard disk 3", "03-demo-vm.vmdk"),
                    ],
                }
            ),
            encoding="utf-8",
        )

        self._register_backup_point("point-full-v1", full_v1_manifest_path)
        self._register_backup_point(
            "point-inc-v1", inc_v1_manifest_path, parent_point_id="point-full-v1"
        )
        self._register_backup_point(
            "point-full-v2", full_v2_manifest_path, parent_point_id="point-inc-v1"
        )
        return full_v1_manifest_path, inc_v1_manifest_path, full_v2_manifest_path

    def test_vcenter_vm_restore_from_incremental_does_not_attach_disk_added_afterward(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            _, inc_v1_manifest_path, _ = self._write_disk_c_added_after_incremental_scenario(temp_dir)

            task = {
                "id": "task-vcenter-restore-inc-v1",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(inc_v1_manifest_path),
                    "point_id": "point-inc-v1",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(len(payload["disks"]), 2)

        disk_layout = FakeVCenterNode.created_vms[0]["disk_layout"]
        self.assertEqual(len(disk_layout), 2)
        restored_file_names = {entry.get("file_name") for entry in disk_layout}
        self.assertFalse(any("03-demo-vm" in name for name in restored_file_names))

        for command in recorded_commands:
            self.assertNotIn("03-demo-vm.vmdk", command)

        self.assertEqual(len(recorded_commands), 4)
        force_count = sum(1 for command in recorded_commands if "--force" in command)
        self.assertEqual(force_count, 2)

    def test_hostagent_vm_restore_from_incremental_does_not_attach_disk_added_afterward(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            _, inc_v1_manifest_path, _ = self._write_disk_c_added_after_incremental_scenario(temp_dir)

            task = {
                "id": "task-hostagent-restore-inc-v1",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(inc_v1_manifest_path),
                    "point_id": "point-inc-v1",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(len(payload["disks"]), 2)
        self.assertEqual(len(FakeVCenterNode.created_managed_disks), 2)
        for managed_disk in FakeVCenterNode.created_managed_disks:
            self.assertNotIn("03-demo-vm", str(managed_disk.get("disk_path") or ""))
        for command in recorded_commands:
            self.assertNotIn("03-demo-vm.vmdk", command)

    def test_vcenter_vm_restore_in_place_from_incremental_does_not_end_up_with_disk_added_afterward(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            _, inc_v1_manifest_path, _ = self._write_disk_c_added_after_incremental_scenario(temp_dir)

            task = {
                "id": "task-in-place-restore-inc-v1",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(inc_v1_manifest_path),
                    "point_id": "point-inc-v1",
                    "restore_mode": "in_place",
                    "target_vm_name": "demo-vm",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(len(payload["disks"]), 2)
        self.assertEqual(len(FakeVCenterNode.created_vms), 1)
        disk_layout = FakeVCenterNode.created_vms[0]["disk_layout"]
        self.assertEqual(len(disk_layout), 2)
        restored_file_names = {entry.get("file_name") for entry in disk_layout}
        self.assertFalse(any("03-demo-vm" in name for name in restored_file_names))
        self.assertEqual(FakeVCenterNode.destroyed_vms, ["demo-vm"])

    def _in_place_restore_task(
        self,
        temp_dir: "pathlib.Path",
        *,
        point_id: str,
        task_id: str,
        power_on: bool = False,
        vm_name: str = "demo-vm",
    ) -> dict[str, object]:
        manifest_path = pathlib.Path(temp_dir) / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "vm_name": vm_name,
                    "vm_config": {
                        "guest_id": "ubuntu64Guest",
                        "guest_os": "Ubuntu Linux (64-bit)",
                        "num_cpu": 2,
                        "memory_mb": 4096,
                        "network_adapters": [],
                    },
                    "disks": [
                        {
                            "index": 1,
                            "disk_label": "Hard disk 1",
                            "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                            "destination_path": "",
                            "abr_path": str(pathlib.Path(temp_dir)),
                            "abr_backup_name": "01-demo-vm.vmdk",
                            "storage_mode": "local",
                            "store_mode": "full",
                            "source_disk_type_code": 5,
                            "source_disk_type_name": "vmfs_flat",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._register_backup_point(point_id, manifest_path)
        return {
            "id": task_id,
            "status": "queued",
            "task_type": "restore",
            "hypervisor_id": "hv-vc",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-uuid-1",
            "parameters": {
                "manifest_path": str(manifest_path),
                "point_id": point_id,
                "restore_mode": "in_place",
                # In-place restore always targets the source VM's own name —
                # abr_v41.restore_start forces this server-side; here it's
                # set directly since the test drives run_task without going
                # through restore_start.
                "target_vm_name": vm_name,
                "target_host_name": "esx-dst.local",
                "target_datastore_name": "datastore2",
                "power_on": power_on,
                "tool_path": EXISTING_TOOL_PATH,
            },
        }

    def test_a_restored_disk_keeps_its_plain_name_when_there_is_room(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        self.assertEqual(vcenter_restore._build_disk_file_name(2, "demo-vm.vmdk"), "02-demo-vm.vmdk")

    def test_repeated_restores_cannot_grow_a_disk_name_past_the_limit(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        name = "demo-vm.vmdk"
        for _ in range(200):
            name = vcenter_restore._build_disk_file_name(1, name)

        self.assertLessEqual(len(name), vcenter_restore.RESTORED_DISK_FILE_NAME_LIMIT)
        self.assertTrue(name.startswith("01-"))
        self.assertTrue(name.endswith(".vmdk"))

    def test_a_restored_disk_still_leaves_room_for_a_snapshot_of_its_own(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        name = "d" * 104 + ".vmdk"
        for _ in range(10):
            name = vcenter_restore._build_disk_file_name(1, name)
            delta = f"{name[:-5]}-000001-sesparse.vmdk"
            self.assertLessEqual(
                len(delta),
                vcenter_restore.VSPHERE_DISK_FILE_NAME_LIMIT,
                "a restored disk that cannot be snapshotted can never be backed up again",
            )

    def test_disks_of_one_vm_stay_distinct_even_when_their_names_are_trimmed(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        source = f"{'q' * 400}.vmdk"
        names = {vcenter_restore._build_disk_file_name(index, source) for index in (1, 2, 3)}

        self.assertEqual(len(names), 3)
        for name in names:
            self.assertLessEqual(len(name), vcenter_restore.RESTORED_DISK_FILE_NAME_LIMIT)

    def test_a_staging_name_never_exceeds_what_vsphere_accepts(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        for length in (8, 60, 61, 62, 80, 125):
            staging = vcenter_restore._build_staging_vm_name("x" * length, "abcdef1234")
            self.assertLessEqual(len(staging), vcenter_restore.VSPHERE_VM_NAME_LIMIT, length)
            self.assertTrue(staging.endswith("-restoring-abcdef12"), length)

    def test_a_short_name_keeps_its_whole_staging_name(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        self.assertEqual(
            vcenter_restore._build_staging_vm_name("demo-vm", "abcdef1234"),
            "demo-vm-restoring-abcdef12",
        )

    def test_a_restored_disk_file_name_stays_within_the_datastore_limit(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        path = vcenter_restore._build_restore_target_path(
            f"[datastore1] vm/{'y' * 300}.vmdk", "abcdef1234"
        )
        file_name = path.rsplit("/", 1)[-1]

        self.assertLessEqual(len(file_name), vcenter_restore.RESTORED_DISK_FILE_NAME_LIMIT)
        self.assertTrue(file_name.endswith("-restored-abcdef12.vmdk"))

    def test_an_in_place_restore_works_for_a_name_that_leaves_no_room_for_the_suffix(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        long_name = "prod-eu-west-db-cluster-01-node-03-" + "x" * 27
        self.assertEqual(len(long_name), 62)
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": long_name,
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = self._in_place_restore_task(
                temp_dir,
                point_id="point-ip-long",
                task_id="task-in-place-long-name",
                vm_name=long_name,
            )

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        staging_name = FakeVCenterNode.created_vms[0]["vm_name"]
        self.assertLessEqual(len(staging_name), vcenter_restore.VSPHERE_VM_NAME_LIMIT)
        self.assertNotEqual(staging_name, long_name)
        self.assertEqual(
            FakeVCenterNode.renamed_vms,
            [{"from": staging_name, "to": long_name, "vm_moref": "vm-restore-001"}],
        )

    def test_vcenter_vm_restore_in_place_creates_staging_vm_then_swaps(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-1", task_id="task-in-place-swap")

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        payload = result["result"]
        self.assertEqual(payload["restore_mode"], "in-place")
        self.assertEqual(payload["target_vm_name"], "demo-vm")

        # The shell and every disk write happened under a staging name, never
        # touching the still-live "demo-vm" until the swap.
        self.assertEqual(len(FakeVCenterNode.created_vms), 1)
        staging_name = FakeVCenterNode.created_vms[0]["vm_name"]
        self.assertTrue(staging_name.startswith("demo-vm-restoring-"))
        self.assertNotEqual(staging_name, "demo-vm")

        self.assertEqual(FakeVCenterNode.powered_off_vms, ["demo-vm"])
        self.assertEqual(FakeVCenterNode.destroyed_vms, ["demo-vm"])
        self.assertEqual(
            FakeVCenterNode.renamed_vms,
            [{"from": staging_name, "to": "demo-vm", "vm_moref": "vm-restore-001"}],
        )
        self.assertEqual(FakeVCenterNode.powered_on_vms, [])

    def test_an_in_place_restore_gives_the_replacement_the_original_identity(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-id", task_id="task-in-place-identity")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped",
                    side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="ok"),
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["result"]["identity_preserved"])
        self.assertEqual(FakeVCenterNode.identity_reads, ["demo-vm"])
        self.assertEqual(
            FakeVCenterNode.applied_identities,
            [
                {
                    "vm_name": "demo-vm",
                    "uuid": "42200000-1111-2222-3333-444455556666",
                    "instance_uuid": "50200000-1111-2222-3333-444455556666",
                }
            ],
        )

    def test_the_identity_is_read_before_the_original_is_destroyed(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        order: list[str] = []
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-order", task_id="task-in-place-order")

            original_read = FakeVCenterNode.read_vm_identity
            original_destroy = FakeVCenterNode.destroy_vm

            def tracking_read(self, vm_name, vm_moref=None):
                order.append("read")
                return original_read(self, vm_name, vm_moref)

            def tracking_destroy(self, vm_name, vm_moref=None):
                order.append("destroy")
                return original_destroy(self, vm_name, vm_moref)

            with patch.object(FakeVCenterNode, "read_vm_identity", tracking_read):
                with patch.object(FakeVCenterNode, "destroy_vm", tracking_destroy):
                    with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                        with patch(
                            "python.srv.plugins.vcenter.vcenter_restore.run_capped",
                            side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="ok"),
                        ):
                            run_task(task)

        self.assertEqual(order, ["read", "destroy"])

    def test_an_in_place_restore_survives_an_identity_that_cannot_be_applied(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        FakeVCenterNode.raise_on_set_identity = RuntimeError("vCenter refused the identifiers")
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-fail", task_id="task-in-place-id-fail")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped",
                    side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="ok"),
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["result"]["identity_preserved"])
        self.assertEqual(FakeVCenterNode.renamed_vms[0]["to"], "demo-vm")

    def test_an_in_place_restore_survives_an_identity_that_cannot_be_read(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        FakeVCenterNode.raise_on_read_identity = RuntimeError("VM is orphaned; config unavailable")
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-read", task_id="task-in-place-id-read")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped",
                    side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="ok"),
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["result"]["identity_preserved"])
        self.assertEqual(FakeVCenterNode.applied_identities, [])
        self.assertEqual(FakeVCenterNode.renamed_vms[0]["to"], "demo-vm")

    def test_a_restore_to_a_new_name_leaves_identity_alone(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-new-name", task_id="task-new-name")
            task["parameters"]["restore_mode"] = "new_vm"
            task["parameters"]["target_vm_name"] = "demo-vm-copy"

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped",
                    side_effect=lambda command, **kwargs: FakeCompletedProcess(returncode=0, stdout="ok"),
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(FakeVCenterNode.identity_reads, [])
        self.assertEqual(FakeVCenterNode.applied_identities, [])
        self.assertFalse(result["result"]["identity_preserved"])

    def test_vcenter_vm_restore_in_place_powers_on_after_rename(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-2", task_id="task-in-place-power-on", power_on=True)

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        # Powered on under the FINAL name, after the rename — not the staging name.
        self.assertEqual(FakeVCenterNode.powered_on_vms, ["demo-vm"])
        self.assertEqual(FakeVCenterNode.renamed_vms[0]["to"], "demo-vm")

    def test_vcenter_vm_restore_in_place_failure_before_swap_leaves_original_untouched(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-3", task_id="task-in-place-fail-before-swap")

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=1, stdout="", stderr="disk write failed")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        staging_name = FakeVCenterNode.created_vms[0]["vm_name"]
        self.assertTrue(staging_name.startswith("demo-vm-restoring-"))

        # Only the staging VM is cleaned up — the original is never powered
        # off, destroyed, or renamed when a disk write fails.
        self.assertEqual(FakeVCenterNode.destroyed_vms, [staging_name])
        self.assertEqual(FakeVCenterNode.powered_off_vms, [])
        self.assertEqual(FakeVCenterNode.renamed_vms, [])
        self.assertNotIn("demo-vm", FakeVCenterNode.destroyed_vms)

    def test_vcenter_vm_restore_in_place_failure_after_original_destroyed_raises_critical_and_leaves_staging_vm(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.raise_on_rename_vm = RuntimeError("vCenter task failed: host disconnected mid-rename")
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            (pathlib.Path(temp_dir) / "01-demo-vm.vmdk").write_text("fake-vmdk", encoding="utf-8")
            task = self._in_place_restore_task(temp_dir, point_id="point-ip-4", task_id="task-in-place-fail-after-destroy")

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        staging_name = FakeVCenterNode.created_vms[0]["vm_name"]
        self.assertIn("host disconnected mid-rename", result["result"]["message"])

        # The original truly is gone — this is the disclosed residual-risk
        # window between destroy and rename — but the fully-written staging
        # VM survives untouched: the cleanup path must NOT run here, since
        # destroying it now would throw away the only surviving copy of the
        # restored data.
        self.assertEqual(FakeVCenterNode.destroyed_vms, ["demo-vm"])
        self.assertEqual(FakeVCenterNode.powered_off_vms, ["demo-vm"])
        self.assertNotIn(staging_name, FakeVCenterNode.destroyed_vms)

    def test_vcenter_vm_restore_accepts_differential_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            full_task_root = pathlib.Path(temp_dir) / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            full_manifest_path = full_task_root / "manifest.json"
            full_manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            diff_task_root = pathlib.Path(temp_dir) / "task-diff"
            diff_task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = diff_task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "differential",
                        "differential": True,
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(diff_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-full", full_manifest_path)
            self._register_backup_point(
                "point-diff", manifest_path, parent_point_id="point-full"
            )

            task = {
                "id": "task-vcenter-restore-differential-vm",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-diff",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["restore_mode"], "new-vm")
        self.assertEqual(payload["disks"][0]["backup_mode"], "differential")
        # The differential chain replays full first, then the differential layer.
        self.assertEqual(len(recorded_commands), 2)
        for command in recorded_commands:
            self.assertEqual(command[1], "restore")
            self._assert_abr_mode_file(command)

    def test_vcenter_vm_restore_progress_does_not_regress_across_abr_layers(self) -> None:
        # Regression test: a disk with a multi-layer .abr chain (full +
        # incremental) restores as two sequential subprocess runs, each
        # reporting its own independent 0-100%. Before the fix, both layers
        # were mapped onto the *same* progress band, so the reported overall
        # progress snapped back down every time one layer finished and the
        # next began — visible on the Activity page as the bar rolling
        # backward mid-restore.
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            full_task_root = pathlib.Path(temp_dir) / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            full_manifest_path = full_task_root / "manifest.json"
            full_manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "vm_config": {"guest_id": "ubuntu64Guest", "num_cpu": 2, "memory_mb": 4096, "network_adapters": []},
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            diff_task_root = pathlib.Path(temp_dir) / "task-diff"
            diff_task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = diff_task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "differential",
                        "differential": True,
                        "vm_name": "demo-vm",
                        "vm_config": {"guest_id": "ubuntu64Guest", "num_cpu": 2, "memory_mb": 4096, "network_adapters": []},
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(diff_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-full-prog", full_manifest_path)
            self._register_backup_point("point-diff-prog", manifest_path, parent_point_id="point-full-prog")

            task = {
                "id": "task-vcenter-restore-progress",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-diff-prog",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            progress_events: list[int] = []

            def capture(update: dict[str, object]) -> None:
                if update.get("stage") == "transfer" and isinstance(update.get("progress"), int):
                    progress_events.append(int(update["progress"]))

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                # Each layer's subprocess reports its own independent 0-100%
                # run, exactly like the real abr-vmdk-tool does per invocation.
                on_progress = kwargs.get("on_progress")
                if on_progress:
                    on_progress(0, 0)
                    on_progress(50, 1)
                    on_progress(100, 2)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task, progress_callback=capture)

        self.assertEqual(result["status"], "completed")
        # Two layers x (start-of-disk sample + 3 on_progress samples each).
        self.assertGreaterEqual(len(progress_events), 6)
        for earlier, later in zip(progress_events, progress_events[1:]):
            self.assertLessEqual(earlier, later, f"progress regressed somewhere in {progress_events}")

    def test_vcenter_vm_restore_defaults_host_and_datastore_from_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "source_host": "esx-src.local",
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[ds-01] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-defaults", manifest_path)

            task = {
                "id": "task-vcenter-restore-vm-defaults",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-defaults",
                    "target_vm_name": "demo-vm-restored",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["target_host_name"], "esx-src.local")
        self.assertEqual(payload["target_datastore_name"], "ds-01")
        self.assertEqual(FakeVCenterNode.created_vms[0]["host_name"], "esx-src.local")
        self.assertEqual(FakeVCenterNode.created_vms[0]["datastore_name"], "ds-01")
        self.assertEqual(recorded_commands[0][1], "restore")
        self.assertIn("[ds-01] demo-vm-restored/01-demo-vm.vmdk", recorded_commands[0][recorded_commands[0].index("--vmdk") + 1])

    def test_vcenter_vm_restore_falls_back_to_bios_when_manifest_firmware_is_missing(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[ds-01] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-firmware", manifest_path)

            task = {
                "id": "task-vcenter-restore-vm-missing-firmware",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-firmware",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "ds-01",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    with self.assertLogs("python.srv.plugins.vcenter.vcenter_restore", level="WARNING") as logs:
                        result = run_task(task)

        payload = result["result"]
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(payload["source_firmware"])
        self.assertEqual(payload["requested_firmware"], "bios")
        self.assertEqual(payload["restored_firmware"], "bios")
        self.assertEqual(FakeVCenterNode.created_vms[0]["config_metadata"]["firmware"], "bios")
        self.assertTrue(
            any(
                "Source firmware is missing from restore point metadata; falling back to default firmware 'bios'."
                in message
                for message in logs.output
            )
        )
        self.assertIn("[ds-01] demo-vm-restored/01-demo-vm.vmdk", recorded_commands[0][recorded_commands[0].index("--vmdk") + 1])

    def test_restore_disk_can_resolve_backup_path_from_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                },
            },
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            full_task_root = pathlib.Path(temp_dir) / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            full_manifest_path = full_task_root / "manifest.json"
            full_manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            inc_task_root = pathlib.Path(temp_dir) / "task-inc"
            inc_task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = inc_task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "incremental",
                        "incremental": True,
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(inc_task_root),
                                "abr_backup_name": "01-demo-vm-inc.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-disk-full", full_manifest_path)
            self._register_backup_point(
                "point-disk-inc", manifest_path, parent_point_id="point-disk-full"
            )

            task = {
                "id": "task-vcenter-restore-disk-from-manifest",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "disk",
                "resource_external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-disk-inc",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["manifest_path"], str(manifest_path.resolve()))
        # The selected restore point is the tip of the chain (the incremental
        # layer), so source_backup_path points at its .abr artifact.
        self.assertEqual(
            payload["source_backup_path"],
            f"{inc_task_root}/01-demo-vm-inc.vmdk",
        )
        # The chain replays full first, then the incremental layer.
        self.assertEqual(len(recorded_commands), 2)
        for command in recorded_commands:
            self.assertEqual(command[1], "restore")
            self._assert_abr_mode_file(command)
        self.assertEqual(recorded_commands[0][recorded_commands[0].index("--name") + 1], "01-demo-vm.vmdk")
        self.assertEqual(recorded_commands[1][recorded_commands[1].index("--name") + 1], "01-demo-vm-inc.vmdk")

    def test_restore_disk_forwards_encryption_key_to_abr_restore_command(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                },
            },
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            full_task_root = pathlib.Path(temp_dir) / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = full_task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-disk-encrypted", manifest_path)

            task = {
                "id": "task-vcenter-restore-disk-encrypted",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "disk",
                "resource_external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-disk-encrypted",
                    "encryption_key": "hunter2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    run_task(task)

        self.assertEqual(len(recorded_commands), 1)
        command = recorded_commands[0]
        self.assertIn("--key", command)
        self.assertEqual(command[command.index("--key") + 1], "hunter2")

    def test_restore_disk_replays_full_then_incremental_abr_layers(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                },
            },
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            full_task_root = pathlib.Path(temp_dir) / "task-full"
            full_task_root.mkdir(parents=True, exist_ok=True)
            full_manifest_path = full_task_root / "manifest.json"
            full_manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(full_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            inc_task_root = pathlib.Path(temp_dir) / "task-inc"
            inc_task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = inc_task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "incremental",
                        "incremental": True,
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(inc_task_root),
                                "abr_backup_name": "01-demo-vm-inc.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-strip-full", full_manifest_path)
            self._register_backup_point(
                "point-strip-inc", manifest_path, parent_point_id="point-strip-full"
            )

            task = {
                "id": "task-vcenter-restore-disk-chain",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "disk",
                "resource_external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-strip-inc",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")
        # The incremental chain replays the full layer first, then the
        # incremental layer, each via a single restore command onto the
        # same restore target.
        self.assertEqual(len(recorded_commands), 2)
        for command in recorded_commands:
            self.assertEqual(command[1], "restore")
            self._assert_abr_mode_file(command)
        self.assertEqual(
            recorded_commands[0][recorded_commands[0].index("--name") + 1],
            "01-demo-vm.vmdk",
        )
        self.assertEqual(
            recorded_commands[1][recorded_commands[1].index("--name") + 1],
            "01-demo-vm-inc.vmdk",
        )
        self.assertEqual(recorded_commands[0][recorded_commands[0].index("--vmdk") + 1], recorded_commands[1][recorded_commands[1].index("--vmdk") + 1])

    def test_vm_restore_rejects_query_only_backup_manifest(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "incremental",
                        "incremental": True,
                        "query_changed_areas_only": True,
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "storage_mode": "local",
                                "query_changed_areas_only": True,
                                "store_mode": "query-only",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-query-only", manifest_path)

            task = {
                "id": "task-vcenter-restore-query-only",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-query-only",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        # A query-only / metadata-only restore point carries no replayable .abr
        # data and must be rejected by the chain resolver.
        self.assertIn("only contains metadata", result["result"]["message"])
        self.assertEqual(FakeVCenterNode.created_vms, [])

    def test_vcenter_vm_restore_supports_per_disk_type_overrides(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path1 = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path2 = pathlib.Path(temp_dir) / "02-demo-vm.vmdk"
            backup_path1.write_text("fake-vmdk-1", encoding="utf-8")
            backup_path2.write_text("fake-vmdk-2", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/disk-1.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            },
                            {
                                "index": 2,
                                "disk_label": "Hard disk 2",
                                "datastore_path": "[datastore1] demo-vm/disk-2.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "02-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-overrides", manifest_path)

            task = {
                "id": "task-vcenter-restore-vm-overrides",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-overrides",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "disk_type": "original",
                    "disk_types": {"2": "thin-provisioned"},
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["disk_count"], 2)
        self.assertEqual(payload["disks"][0]["requested_disk_type"], "original")
        self.assertEqual(payload["disks"][0]["resolved_disk_type_code"], int(VddkDiskType.VMFS_THIN))
        self.assertEqual(payload["disks"][1]["requested_disk_type"], int(VddkDiskType.VMFS_THIN))
        self.assertEqual(payload["disks"][1]["resolved_disk_type_code"], int(VddkDiskType.VMFS_THIN))
        # One .abr restore command per disk, replaying its full layer.
        self.assertEqual(len(recorded_commands), 2)
        self.assertEqual(recorded_commands[0][recorded_commands[0].index("--name") + 1], "01-demo-vm.vmdk")
        self.assertEqual(recorded_commands[1][recorded_commands[1].index("--name") + 1], "02-demo-vm.vmdk")
        for command in recorded_commands:
            self._assert_abr_mode_file(command)

    def test_hostagent_vm_restore_uses_precreated_vm_disks(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "debian12_64Guest",
                            "num_cpu": 2,
                            "memory_mb": 2048,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "capacity_gb": 20,
                                "datastore_path": "[ds-01] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-hostagent", manifest_path)

            task = {
                "id": "task-hostagent-restore-vm",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-hostagent",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-01.local",
                    "target_datastore_name": "ds-01",
                    "disk_type": "thick-provisioned",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["service_type"], "HostAgent")
        self.assertEqual(payload["restore_mode"], "new-vm")
        self.assertEqual(payload["disk_type"], "thick-provisioned")
        self.assertEqual(FakeVCenterNode.created_vms[0]["disk_layout"], [])
        self.assertEqual(len(FakeVCenterNode.created_managed_disks), 1)
        self.assertEqual(len(FakeVCenterNode.attachments), 1)
        self.assertEqual(FakeVCenterNode.created_managed_disks[0]["disk_type_code"], 5)
        self.assertEqual(
            FakeVCenterNode.created_managed_disks[0]["disk_path"],
            "[ds-01] demo-vm-restored/01-demo-vm.vmdk",
        )
        self.assertEqual(recorded_commands[0][1], "restore")
        self.assertIn("[ds-01] demo-vm-restored/01-demo-vm.vmdk", recorded_commands[0])

    def test_vm_restore_rejects_unsupported_disk_type_before_creating_vm(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "debian12_64Guest",
                            "num_cpu": 2,
                            "memory_mb": 2048,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "capacity_gb": 20,
                                "datastore_path": "[ds-01] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-sesparse", manifest_path)

            task = {
                "id": "task-hostagent-restore-vm-sesparse",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-sesparse",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-01.local",
                    "target_datastore_name": "ds-01",
                    "disk_type": 8,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "disk type must be one of: original, thin-provisioned, or thick-provisioned",
            result["result"]["message"],
        )
        self.assertEqual(FakeVCenterNode.created_vms, [])

    def test_vm_restore_rolls_back_created_vm_when_restore_fails(self) -> None:
        FakeVCenterNode.service_type = "HostAgent"
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "esxi.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "root", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "debian12_64Guest",
                            "num_cpu": 2,
                            "memory_mb": 2048,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "capacity_gb": 20,
                                "datastore_path": "[ds-01] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-rollback", manifest_path)

            task = {
                "id": "task-hostagent-restore-vm-fail",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-rollback",
                    "target_vm_name": "demo-vm-restored-fail",
                    "target_host_name": "esx-01.local",
                    "target_datastore_name": "ds-01",
                    "disk_type": 5,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                raise AssertionError("clone should not run after managed disk creation failure")

            def fake_create_managed_disk(**kwargs):
                raise RuntimeError("spec.diskType invalid")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch.object(FakeVCenterNode, "create_managed_disk", side_effect=fake_create_managed_disk):
                    with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                        result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertIn("spec.diskType invalid", result["result"]["message"])
        self.assertIn("demo-vm-restored-fail", FakeVCenterNode.destroyed_vms)
        self.assertEqual(
            FakeVCenterNode.destroyed_vm_requests[-1],
            {"vm_name": "demo-vm-restored-fail", "vm_moref": "vm-restore-001"},
        )

    def test_vcenter_vm_restore_does_not_swap_disks_when_uuid_is_placeholder(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "LNX-EFI-M",
                "payload": {},
            }
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path1 = pathlib.Path(temp_dir) / "01-LNX-EFI-M.vmdk"
            backup_path2 = pathlib.Path(temp_dir) / "02-LNX-EFI-M_1.vmdk"
            backup_path1.write_text("fake-vmdk-32gb", encoding="utf-8")
            backup_path2.write_text("fake-vmdk-64gb", encoding="utf-8")

            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "LNX-EFI-M",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[data1] LNX-EFI-M/LNX-EFI-M.vmdk",
                                "destination_path": "",
                                "capacity_gb": 32,
                                "uuid": "N/A",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-LNX-EFI-M.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            },
                            {
                                "index": 2,
                                "disk_label": "Hard disk 2",
                                "datastore_path": "[data1] LNX-EFI-M/LNX-EFI-M_1.vmdk",
                                "destination_path": "",
                                "capacity_gb": 64,
                                "uuid": "N/A",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "02-LNX-EFI-M_1.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-placeholder-uuid", manifest_path)

            task = {
                "id": "task-vcenter-restore-vm-placeholder-uuid",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-placeholder-uuid",
                    "target_vm_name": "LNX-EFI-M-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "data2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        payload = result["result"]
        self.assertEqual(payload["disk_count"], 2)

        disk_layout = FakeVCenterNode.created_vms[0]["disk_layout"]

        self.assertEqual(disk_layout[0]["file_name"], "01-LNX-EFI-M.vmdk")
        self.assertEqual(disk_layout[0]["capacity_gb"], 32.0)
        self.assertEqual(disk_layout[1]["file_name"], "02-LNX-EFI-M_1.vmdk")
        self.assertEqual(disk_layout[1]["capacity_gb"], 64.0)

        self.assertEqual(len(recorded_commands), 2)

        first_source = recorded_commands[0][recorded_commands[0].index("--name") + 1]
        first_target = recorded_commands[0][recorded_commands[0].index("--vmdk") + 1]

        second_source = recorded_commands[1][recorded_commands[1].index("--name") + 1]
        second_target = recorded_commands[1][recorded_commands[1].index("--vmdk") + 1]

        self.assertEqual(first_source, "01-LNX-EFI-M.vmdk")
        self.assertIn("01-LNX-EFI-M.vmdk", first_target)
        self.assertEqual(second_source, "02-LNX-EFI-M_1.vmdk")
        self.assertIn("02-LNX-EFI-M_1.vmdk", second_target)
        self.assertNotEqual(second_source, first_source)


    def test_vcenter_incremental_backup_without_cbt_change_id_falls_back_to_full(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-no-cbt",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]

        self.assertEqual(payload["backup_mode"], "incremental-full-fallback")
        self.assertTrue(payload["incremental_full_fallback_used"])
        self.assertEqual(payload["disks"][0]["store_mode"], "full-no-cbt-baseline")
        self.assertEqual(payload["disks"][0]["changed_area_count"], 0)
        self.assertEqual(len(recorded_commands), 1)
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "IncrementalFallbackToFull")

        command = recorded_commands[0]

        self.assertEqual(command[1], "backup")
        self.assertNotIn("--type", command)
        self.assertEqual(FakeVCenterNode.changed_area_queries, [])
        self._assert_abr_mode_file(command)

    def test_vcenter_incremental_backup_falls_back_to_full_when_disk_capacity_changed(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-capacity-changed",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]

        self.assertEqual(payload["backup_mode"], "full")
        self.assertFalse(payload["incremental_full_fallback_used"])
        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertNotIn("store_mode", payload["disks"][0])
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")

        self.assertEqual(FakeVCenterNode.changed_area_queries, [])

        command = recorded_commands[0]
        # A full backup is the absence of --type incremental.
        self.assertNotIn("--type", command)
        self.assertNotIn("--change-id", command)
        self._assert_abr_mode_file(command)

    def test_vcenter_backup_records_the_live_disk_uuid_when_the_catalog_has_none(self) -> None:
        """A manifest must carry the UUID the guest identifies its disk by.

        Catalogs written before the collector read the right backing field hold
        no UUID at all, and a restore without one hands the guest a disk it does
        not recognise -- Windows then re-signatures the GPT and fails to boot.
        The backup takes the value live rather than waiting for a rescan.
        """
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-N/A"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-N/A",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": None,
                    "capacity_gb": 40,
                },
            },
        }

        def live_disk(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 40,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "6000C292-d2c6-df32-2e38-2d46be8d7a29",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                }
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-backup-live-disk-uuid",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {"destination_dir": temp_dir, "tool_path": EXISTING_TOOL_PATH},
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", live_disk):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["result"]["disks"][0]["uuid"], "6000C292-d2c6-df32-2e38-2d46be8d7a29"
        )
        # The catalog entry itself stays as the last scan left it.
        catalog_disk = RESOURCES_BY_HV["hv-vc"][("disk", "vm-uuid-1-disk-1-N/A")]
        self.assertIsNone(catalog_disk["payload"]["uuid"])

    def test_vcenter_incremental_backup_detects_resize_not_yet_seen_by_inventory_rescan(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "capacity_gb": 16,
                },
            },
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

        def resized_disk(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 20,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                }
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 16,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-resize-before-rescan",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", resized_disk):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]

        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")
        self.assertEqual(payload["disks"][0]["capacity_gb"], 20)

        command = recorded_commands[0]
        self.assertNotIn("--type", command)
        self.assertNotIn("--change-id", command)

        catalog_disk = RESOURCES_BY_HV["hv-vc"][("disk", "vm-uuid-1-disk-1-disk-uuid-1")]
        self.assertEqual(catalog_disk["payload"]["capacity_gb"], 16)

    def test_vcenter_incremental_backup_escalates_whole_vm_to_full_when_one_disk_capacity_changed(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            },
            {
                "disk_label": "Hard disk 2",
                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                "change_id": "change-101",
                "uuid": "disk-uuid-2",
                "controller_key": 1000,
                "unit_number": 1,
                "key": 2001,
            },
        ]

        def two_disks(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 40,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
                {
                    "disk_label": "Hard disk 2",
                    "capacity_gb": 10,
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                            {
                                "index": 2,
                                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                                "uuid": "disk-uuid-2",
                                "change_id": "change-100",
                                "capacity_gb": 10,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "02-demo-vm_1.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-multidisk-partial-resize",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", two_disks):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]
        disks_by_index = {disk["index"]: disk for disk in payload["disks"]}

        self.assertNotIn("store_mode", disks_by_index[1])
        self.assertNotIn("store_mode", disks_by_index[2])

        self.assertFalse(payload["incremental_full_fallback_used"])
        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")

        inc_commands = [c for c in recorded_commands if "incremental" in c]
        full_commands = [c for c in recorded_commands if c not in inc_commands]
        self.assertEqual(len(full_commands), 2)
        self.assertEqual(len(inc_commands), 0)

    def test_vcenter_incremental_backup_auto_fulls_whole_vm_when_a_disk_is_added_since_the_parent_backup(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            },
            {
                "disk_label": "Hard disk 2",
                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                "change_id": "change-101",
                "uuid": "disk-uuid-2",
                "controller_key": 1000,
                "unit_number": 1,
                "key": 2001,
            },
        ]

        def two_disks(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 30,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
                {
                    "disk_label": "Hard disk 2",
                    "capacity_gb": 20,
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []
        progress_updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-new-disk",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", two_disks):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task, progress_callback=progress_updates.append)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]
        disks_by_index = {disk["index"]: disk for disk in payload["disks"]}

        self.assertNotIn("store_mode", disks_by_index[1])
        self.assertNotIn("store_mode", disks_by_index[2])
        self.assertEqual(disks_by_index[2]["change_id"], "change-101")
        self.assertEqual(disks_by_index[2]["uuid"], "disk-uuid-2")

        self.assertFalse(payload["incremental_full_fallback_used"])
        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertEqual(payload["backup_mode"], "full")
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")

        inc_commands = [c for c in recorded_commands if "incremental" in c]
        full_commands = [c for c in recorded_commands if c not in inc_commands]
        self.assertEqual(len(full_commands), 2)
        self.assertEqual(len(inc_commands), 0)

        messages = " ".join(str(update.get("message") or "") for update in progress_updates)
        self.assertIn("VM configuration changed", messages)

    def test_vcenter_incremental_backup_reconciles_a_disk_added_since_the_last_inventory_rescan(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "capacity_gb": 30,
                },
            },
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            },
            {
                "disk_label": "Hard disk 2",
                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                "change_id": "change-101",
                "uuid": "disk-uuid-2",
                "controller_key": 1000,
                "unit_number": 1,
                "key": 2001,
            },
        ]

        def two_disks(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 30,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
                {
                    "disk_label": "Hard disk 2",
                    "capacity_gb": 20,
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []
        progress_updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-stale-discovery-cache",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", two_disks):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task, progress_callback=progress_updates.append)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]
        disks_by_index = {disk["index"]: disk for disk in payload["disks"]}
        self.assertEqual(set(disks_by_index), {1, 2})
        self.assertNotIn("store_mode", disks_by_index[1])
        self.assertNotIn("store_mode", disks_by_index[2])
        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")

        messages = " ".join(str(update.get("message") or "") for update in progress_updates)
        self.assertIn("VM configuration change detected", messages)
        self.assertIn("performing a full backup of the entire VM", messages)

    def test_vcenter_incremental_backup_excludes_a_disk_removed_since_the_last_inventory_rescan(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "capacity_gb": 30,
                },
            },
            ("disk", "vm-uuid-1-disk-2"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-2",
                "name": "Hard disk 2",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "capacity_gb": 20,
                },
            },
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            },
        ]

        def one_disk(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 30,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []
        progress_updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                            {
                                "index": 2,
                                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                                "uuid": "disk-uuid-2",
                                "change_id": "change-100",
                                "capacity_gb": 20,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "02-demo-vm_1.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-removed-disk",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", one_disk):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task, progress_callback=progress_updates.append)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]
        disks_by_uuid = {disk["uuid"]: disk for disk in payload["disks"]}
        self.assertEqual(set(disks_by_uuid), {"disk-uuid-1"})
        self.assertFalse(payload["vm_config_changed_full_required"])
        self.assertEqual(payload["resolved_backup_type"], "Incremental")

        messages = " ".join(str(update.get("message") or "") for update in progress_updates)
        self.assertIn("VM configuration change detected", messages)
        self.assertIn("1 disk(s) removed since the last inventory scan", messages)

    def test_vcenter_force_full_backup_excludes_a_disk_removed_since_the_last_inventory_rescan(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "capacity_gb": 30,
                },
            },
            ("disk", "vm-uuid-1-disk-2"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-2",
                "name": "Hard disk 2",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "capacity_gb": 20,
                },
            },
        }

        def one_disk(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 30,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []
        progress_updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-force-full-removed-disk",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.object(FakeVCenterNode, "get_vm_disks", one_disk):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                        result = run_task(task, progress_callback=progress_updates.append)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]
        self.assertEqual(payload["disk_count"], 1)
        disks_by_uuid = {disk["uuid"]: disk for disk in payload["disks"]}
        self.assertEqual(set(disks_by_uuid), {"disk-uuid-1"})

        messages = " ".join(str(update.get("message") or "") for update in progress_updates)
        self.assertIn("VM configuration change detected", messages)
        self.assertIn("1 disk(s) removed since the last inventory scan", messages)

    def test_vcenter_differential_backup_falls_back_to_full_when_disk_capacity_changed(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "backup_mode": "full",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                                "capacity_gb": 30,
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-differential-capacity-changed",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "differential": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]

        self.assertFalse(payload["incremental_full_fallback_used"])
        self.assertTrue(payload["vm_config_changed_full_required"])
        self.assertNotIn("store_mode", payload["disks"][0])
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "VmConfigurationChanged")
        self.assertEqual(FakeVCenterNode.changed_area_queries, [])

        command = recorded_commands[0]
        # A full backup is the absence of --type incremental.
        self.assertNotIn("--type", command)
        self.assertNotIn("--change-id", command)

    def test_vcenter_query_changed_areas_only_still_fails_without_cbt_change_id(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            }
        ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)
            
            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-query-only-no-cbt",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "query_changed_areas_only": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": "/missing/abr-vmdk-tool",
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "VCenterBackupError")
        self.assertIn("did not return a CBT change ID", result["result"]["message"])

    def test_vcenter_query_changed_areas_only_still_fails_for_a_disk_added_since_the_parent_backup(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 1",
                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                "change_id": "change-001",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 0,
                "key": 2000,
            },
            {
                "disk_label": "Hard disk 2",
                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                "change_id": "change-101",
                "uuid": "disk-uuid-2",
                "controller_key": 1000,
                "unit_number": 1,
                "key": 2001,
            },
        ]

        def two_disks_second_is_new(self, vm_name: str) -> list[dict[str, object]]:
            return [
                {
                    "disk_label": "Hard disk 1",
                    "capacity_gb": 30,
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
                {
                    "disk_label": "Hard disk 2",
                    "capacity_gb": 20,
                    "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                    "uuid": "disk-uuid-2",
                    "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                    "source_disk_type_name": "vmfs_thin",
                },
            ]

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-000",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-query-only-new-disk",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "query_changed_areas_only": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": "/missing/abr-vmdk-tool",
                },
            }

            with patch.object(FakeVCenterNode, "get_vm_disks", two_disks_second_is_new):
                with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                    result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "VCenterBackupError")
        self.assertIn("was added to the VM after the parent backup", result["result"]["message"])

    def test_vcenter_incremental_backup_falls_back_to_full_when_cbt_cannot_get_changes(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.snapshot_change_ids = [
            {
                "disk_label": "Hard disk 2",
                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                "change_id": "change-002",
                "uuid": "disk-uuid-1",
                "controller_key": 1000,
                "unit_number": 1,
                "key": 2001,
            }
        ]
        FakeVCenterNode.raise_on_query_changed_disk_areas = FakeVimFault(
            "Error caused by file /vmfs/volumes/uuid/demo-vm/demo-vm_1.vmdk",
            fault_message_key="vim.hostd.vmsvc.cbt.cannotGetChanges",
        )

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm_1.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-001",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-cbt-cannot-get-changes",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="Done")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed")

        payload = result["result"]

        self.assertEqual(payload["backup_mode"], "incremental-full-fallback")
        self.assertTrue(payload["incremental_full_fallback_used"])
        self.assertEqual(payload["disks"][0]["store_mode"], "full-no-cbt-baseline")
        self.assertEqual(payload["disks"][0]["changed_area_count"], 0)
        self.assertEqual(len(recorded_commands), 1)
        self.assertEqual(payload["resolved_backup_type"], "ActiveFull")
        self.assertEqual(payload["resolved_backup_reason"], "IncrementalFallbackToFull")

        command = recorded_commands[0]

        self.assertEqual(command[1], "backup")
        self.assertNotIn("--type", command)
        self.assertEqual(len(FakeVCenterNode.changed_area_queries), 1)
        self._assert_abr_mode_file(command)
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])

    def test_vcenter_incremental_backup_still_fails_for_unrelated_query_changed_areas_error(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.raise_on_query_changed_disk_areas = FakeVimFault(
            "Permission to perform this operation was denied.",
            fault_message_key="vim.fault.NoPermission",
        )

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-001",
                                "destination_path": "",
                                "abr_path": str(previous_task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "incremental",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-incremental-unrelated-error",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "FakeVimFault")
        self.assertIn("Permission to perform this operation was denied", result["result"]["message"])
        self.assertEqual(FakeVCenterNode.snapshots_removed, ["demo-vm-snapshot"])

    def test_vcenter_query_changed_areas_only_still_fails_when_cbt_cannot_get_changes(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            }
        }
        FakeVCenterNode.raise_on_query_changed_disk_areas = FakeVimFault(
            "Error caused by file /vmfs/volumes/uuid/demo-vm/demo-vm.vmdk",
            fault_message_key="vim.hostd.vmsvc.cbt.cannotGetChanges",
        )

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_task_root = pathlib.Path(temp_dir) / "demo-vm" / "task-prev"
            previous_task_root.mkdir(parents=True, exist_ok=True)

            parent_manifest = previous_task_root / "manifest.json"
            parent_manifest.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation": "vm-backup",
                        "vm_name": "demo-vm",
                        "vm_external_id": "vm-uuid-1",
                        "created_at": "2026-04-20T09:00:00+00:00",
                        "disks": [
                            {
                                "index": 1,
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "change_id": "change-001",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "task-vcenter-query-only-cbt-cannot-get-changes",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "incremental": True,
                    "query_changed_areas_only": True,
                    "destination_dir": temp_dir,
                    "parent_manifest_path": str(parent_manifest),
                    "tool_path": "/missing/abr-vmdk-tool",
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "FakeVimFault")
        self.assertIn(
            "Error caused by file",
            result["result"]["message"],
        )

    def test_vcenter_vm_restore_succeeds_for_imported_point_with_no_discovered_source(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }

        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = pathlib.Path(temp_dir) / "01-demo-vm.vmdk"
            backup_path.write_text("fake-vmdk", encoding="utf-8")

            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "firmware": "efi",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "network_adapters": [
                                {
                                    "label": "Network adapter 1",
                                    "network": "VM Network",
                                    "connected": True,
                                }
                            ],
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-imported-1", manifest_path)

            task = {
                "id": "task-vcenter-restore-imported",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "imported-11111111-1111-1111-1111-111111111111",
                "source_resource_available": False,
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-imported-1",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                recorded_commands.append(command)
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                    result = run_task(task)

        self.assertEqual(result["status"], "completed", result.get("result"))
        payload = result["result"]

        self.assertEqual(payload["restore_mode"], "new-vm")
        self.assertEqual(payload["target_vm_name"], "demo-vm-restored")
        self.assertEqual(FakeVCenterNode.created_vms[0]["host_name"], "esx-dst.local")

        command = recorded_commands[0]

        self.assertEqual(command[1], "restore")

    def test_vcenter_vm_restore_does_not_need_the_source_to_still_be_discovered(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        task = {
            "id": "task-vcenter-restore-missing-resource",
            "status": "queued",
            "task_type": "restore",
            "hypervisor_id": "hv-vc",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-uuid-missing",
            "parameters": {
                "manifest_path": "/nonexistent/manifest.json",
                "point_id": "point-missing",
                "target_vm_name": "demo-vm-restored",
            },
        }

        result = run_task(task)

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("no longer in the inventory", result["result"]["message"])
        self.assertIn("manifest", result["result"]["message"].lower())


    def _write_chain_manifest(
        self, root: "pathlib.Path", backup_name: str, store_mode: str
    ) -> "pathlib.Path":
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "backup_mode": "full" if store_mode == "full" else "incremental",
                    "vm_name": "demo-vm",
                    "vm_config": {"guest_id": "ubuntu64Guest", "firmware": "efi"},
                    "disks": [
                        {
                            "index": 1,
                            "disk_label": "Hard disk 1",
                            "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                            "destination_path": "",
                            "abr_path": str(root),
                            "abr_backup_name": backup_name,
                            "storage_mode": "local",
                            "store_mode": store_mode,
                            "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                            "source_disk_type_name": "vmfs_thin",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return manifest_path

    def _register_synthetic_chain(self, temp_dir: str) -> dict[str, "pathlib.Path"]:
        base = pathlib.Path(temp_dir)
        manifests = {
            "full": self._write_chain_manifest(base / "task-full", "01-full.vmdk", "full"),
            "inc": self._write_chain_manifest(base / "task-inc", "01-inc.vmdk", "incremental"),
            "synthetic": self._write_chain_manifest(
                base / "task-synthetic", "01-synthetic.vmdk", "full"
            ),
            "inc2": self._write_chain_manifest(base / "task-inc2", "01-inc2.vmdk", "incremental"),
        }
        self._register_backup_point("point-full", manifests["full"])
        self._register_backup_point("point-inc", manifests["inc"], parent_point_id="point-full")
        # A synthetic full opens a new chain, so it records no parent — the same
        # shape register_backup_from_completed_task produces for any full.
        self._register_backup_point("point-synthetic", manifests["synthetic"])
        self._register_backup_point(
            "point-inc2", manifests["inc2"], parent_point_id="point-synthetic"
        )
        return manifests

    def _run_chain_restore(self, point_id: str, manifest_path: "pathlib.Path") -> list[list[str]]:
        self._register_vcenter_stand()
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        recorded: list[list[str]] = []

        task = {
            "id": f"task-restore-{point_id}",
            "status": "queued",
            "task_type": "restore",
            "hypervisor_id": "hv-vc",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-uuid-1",
            "parameters": {
                "manifest_path": str(manifest_path),
                "point_id": point_id,
                "target_vm_name": "demo-vm-restored",
                "target_host_name": "esx-dst.local",
                "target_datastore_name": "datastore2",
                "tool_path": EXISTING_TOOL_PATH,
            },
        }

        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            recorded.append(command)
            return FakeCompletedProcess(returncode=0, stdout="ok")

        with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
            with patch("python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run):
                result = run_task(task)

        self.assertEqual(result["status"], "completed")
        return recorded

    def test_a_restore_from_a_synthetic_full_replays_only_that_one_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = self._register_synthetic_chain(temp_dir)
            recorded = self._run_chain_restore("point-synthetic", manifests["synthetic"])

        names = [command[command.index("--name") + 1] for command in recorded]
        self.assertEqual(names, ["01-synthetic.vmdk"])

    def test_a_restore_from_an_increment_over_a_synthetic_stops_at_the_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = self._register_synthetic_chain(temp_dir)
            recorded = self._run_chain_restore("point-inc2", manifests["inc2"])

        names = [command[command.index("--name") + 1] for command in recorded]
        self.assertEqual(names, ["01-synthetic.vmdk", "01-inc2.vmdk"])
        self.assertNotIn("01-full.vmdk", names, "the pre-synthetic chain must not be replayed")
        self.assertNotIn("01-inc.vmdk", names)

    def test_every_layer_of_a_synthetic_chain_restores_onto_the_same_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = self._register_synthetic_chain(temp_dir)
            recorded = self._run_chain_restore("point-inc2", manifests["inc2"])

        targets = {command[command.index("--vmdk") + 1] for command in recorded}
        self.assertEqual(len(targets), 1)

    def test_a_synthetic_base_layer_is_restored_like_any_other_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = self._register_synthetic_chain(temp_dir)
            synthetic = self._run_chain_restore("point-synthetic", manifests["synthetic"])
            FakeVCenterNode.created_vms.clear()
            plain_full = self._run_chain_restore("point-full", manifests["full"])

        self.assertNotIn("--force", synthetic[0])
        self.assertEqual("--force" in synthetic[0], "--force" in plain_full[0])

    def test_increments_over_a_synthetic_are_still_applied_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifests = self._register_synthetic_chain(temp_dir)
            recorded = self._run_chain_restore("point-inc2", manifests["inc2"])

        self.assertNotIn("--force", recorded[0])
        self.assertIn("--force", recorded[1])

    def _register_vcenter_stand(self) -> None:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {"num_cpu": 2, "memory_mb": 4096},
            }
        }

    @staticmethod
    def _write_abr_artifacts(command: list[str]) -> None:
        artifact = abr_artifact_path(command)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        (artifact.parent / f"{artifact.name}.abrd").write_bytes(b"x" * 4096)
        (artifact.parent / f"{artifact.name}.abri").write_bytes(b"i" * 128)

    @staticmethod
    def _files_under(root: "pathlib.Path") -> list[str]:
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def _run_stopped_backup(self, fake_run, progress_callback=None) -> tuple[dict, str]:
        self._register_vcenter_stand()
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-stop",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "destination_dir": temp_dir,
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }
            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}), patch(
                "python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run
            ), patch(
                "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup"
            ) as remove_mock, patch(
                "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
                side_effect=StorageToolError("backup not found"),
            ):
                result = run_task(task, progress_callback=progress_callback)
            leftovers = self._files_under(pathlib.Path(temp_dir))

        self.removed_backup_names = [call.args[2] for call in remove_mock.call_args_list]
        return result, leftovers

    def test_a_backup_stopped_while_a_disk_is_copying_leaves_no_data_behind(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            raise ProcessCancelled("cancellation requested")

        result, leftovers = self._run_stopped_backup(fake_run)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(leftovers, [])
        self.assertIn("01-demo-vm.vmdk", self.removed_backup_names)

    def test_a_backup_stopped_after_a_disk_finished_leaves_no_data_behind(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            return FakeCompletedProcess(returncode=0, stdout="ok")

        def stop_once_the_disk_is_stored(update: dict) -> None:
            if str(update.get("message") or "").startswith("Stored disk"):
                raise TaskStopRequested("Session stop requested")

        result, leftovers = self._run_stopped_backup(fake_run, stop_once_the_disk_is_stored)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(leftovers, [])
        self.assertIn("01-demo-vm.vmdk", self.removed_backup_names)

    def test_a_backup_that_fails_after_a_disk_finished_leaves_no_data_behind(self) -> None:
        calls: list[int] = []

        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            calls.append(1)
            self._write_abr_artifacts(command)
            return FakeCompletedProcess(returncode=7, stderr="disk copy failed")

        result, leftovers = self._run_stopped_backup(fake_run)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(leftovers, [])

    def test_a_single_disk_restore_stopped_mid_disk_settles_stopped(self) -> None:
        self._register_vcenter_stand()
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {
                            "guest_id": "ubuntu64Guest",
                            "firmware": "efi",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                        },
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-vm-1", manifest_path)

            task = {
                "id": "task-vcenter-restore-stop",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-vm-1",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                raise ProcessCancelled("cancellation requested")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run
                ):
                    result = run_task(task)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["result"]["message"], "Session stop requested")
        self.assertEqual(len(FakeVCenterNode.created_vms), 1)
        self.assertEqual(
            FakeVCenterNode.destroyed_vms, [FakeVCenterNode.created_vms[0]["vm_name"]]
        )

    def test_a_restore_asks_the_data_mover_to_stop_when_the_session_is_cancelled(self) -> None:
        self._register_vcenter_stand()
        TASKS["task-vcenter-restore-cancel"] = {
            "id": "task-vcenter-restore-cancel",
            "cancel_requested": True,
        }
        self.addCleanup(TASKS.clear)
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)
        observed: list[bool] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "demo-vm",
                        "vm_config": {"guest_id": "ubuntu64Guest", "firmware": "efi"},
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "destination_path": "",
                                "abr_path": str(pathlib.Path(temp_dir)),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": 5,
                                "source_disk_type_name": "vmfs_flat",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-vm-cancel", manifest_path)

            task = {
                "id": "task-vcenter-restore-cancel",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-vm-cancel",
                    "target_vm_name": "demo-vm-restored",
                    "target_host_name": "esx-dst.local",
                    "target_datastore_name": "datastore2",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
                should_cancel = kwargs.get("should_cancel")
                observed.append(should_cancel is not None and should_cancel())
                return FakeCompletedProcess(returncode=0, stdout="ok")

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run
                ):
                    run_task(task)

        self.assertEqual(observed, [True])


    def _run_stopped_disk_restore(self, fake_run) -> dict:
        HYPERVISORS["hv-vc"] = {
            "id": "hv-vc",
            "kind": "vcenter",
            "metadata": {
                "host": "vc.local",
                "port": 443,
                "verify_ssl": False,
                "credentials": {"username": "administrator", "password": "secret"},
            },
        }
        RESOURCES_BY_HV["hv-vc"] = {
            ("virtual_machine", "vm-uuid-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-1",
                "name": "demo-vm",
                "payload": {},
            },
            ("disk", "vm-uuid-1-disk-1-disk-uuid-1"): {
                "resource_type": "disk",
                "external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "name": "Hard disk 1",
                "payload": {
                    "vm_id": "vm-uuid-1",
                    "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                    "uuid": "disk-uuid-1",
                },
            },
        }
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = pathlib.Path(temp_dir) / "task-full"
            task_root.mkdir(parents=True, exist_ok=True)
            manifest_path = task_root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "backup_mode": "full",
                        "vm_name": "demo-vm",
                        "disks": [
                            {
                                "index": 1,
                                "disk_label": "Hard disk 1",
                                "datastore_path": "[datastore1] demo-vm/demo-vm.vmdk",
                                "uuid": "disk-uuid-1",
                                "destination_path": "",
                                "abr_path": str(task_root),
                                "abr_backup_name": "01-demo-vm.vmdk",
                                "storage_mode": "local",
                                "store_mode": "full",
                                "source_disk_type_code": int(VddkDiskType.VMFS_THIN),
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self._register_backup_point("point-disk-stop", manifest_path)

            task = {
                "id": "task-vcenter-restore-disk-stop",
                "status": "queued",
                "task_type": "restore",
                "hypervisor_id": "hv-vc",
                "resource_type": "disk",
                "resource_external_id": "vm-uuid-1-disk-1-disk-uuid-1",
                "parameters": {
                    "manifest_path": str(manifest_path),
                    "point_id": "point-disk-stop",
                    "tool_path": EXISTING_TOOL_PATH,
                },
            }

            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}):
                with patch(
                    "python.srv.plugins.vcenter.vcenter_restore.run_capped", side_effect=fake_run
                ):
                    return run_task(task)

    def test_a_stopped_disk_restore_removes_the_disk_it_created(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            raise ProcessCancelled("cancellation requested")

        result = self._run_stopped_disk_restore(fake_run)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(len(FakeVCenterNode.created_managed_disks), 1)
        created_path = str(FakeVCenterNode.created_managed_disks[0]["disk_path"])
        self.assertEqual(FakeVCenterNode.deleted_managed_disks, [created_path])
        self.assertEqual(
            [entry["datastore_path"] for entry in FakeVCenterNode.detached_disks], [created_path]
        )

    def test_a_failed_disk_restore_removes_the_disk_it_created(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            return FakeCompletedProcess(returncode=9, stderr="restore failed")

        result = self._run_stopped_disk_restore(fake_run)

        self.assertEqual(result["status"], "failed")
        created_path = str(FakeVCenterNode.created_managed_disks[0]["disk_path"])
        self.assertEqual(FakeVCenterNode.deleted_managed_disks, [created_path])


    def test_a_stop_arriving_after_the_data_is_complete_keeps_the_backup(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            return FakeCompletedProcess(returncode=0, stdout="ok")

        def stop_once_the_snapshot_is_being_removed(update: dict) -> None:
            if str(update.get("message") or "").startswith("Removing snapshot"):
                raise TaskStopRequested("Session stop requested")

        result, leftovers = self._run_stopped_backup(fake_run, stop_once_the_snapshot_is_being_removed)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["result"]["stop_requested_after_completion"])
        self.assertTrue(leftovers, "the completed backup must survive the late stop")
        self.assertEqual(self.removed_backup_names, [])

    def test_a_backup_kept_after_a_late_stop_says_so_on_the_session(self) -> None:
        from python.srv.services.backup_catalog import register_backup_from_completed_task

        task = {
            "id": "task-late-stop",
            "task_type": "backup",
            "status": "completed",
            "result": {"stop_requested_after_completion": True},
        }
        register_backup_from_completed_task(task)

        self.assertTrue(
            any("was kept" in str(w.get("message")) for w in task.get("warnings") or []),
            f"expected a session warning, got: {task.get('warnings')}",
        )


    def test_a_stop_landing_after_the_last_progress_report_is_still_recorded(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            return FakeCompletedProcess(returncode=0, stdout="ok")

        TASKS["task-vcenter-stop"] = {"id": "task-vcenter-stop", "cancel_requested": True}
        self.addCleanup(TASKS.clear)

        result, leftovers = self._run_stopped_backup(fake_run)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(
            result["result"]["stop_requested_after_completion"],
            "a stop nobody reported through progress must still reach the session",
        )
        self.assertTrue(leftovers)


    def test_a_stop_that_object_lock_blocks_names_what_it_left_behind(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            raise ProcessCancelled("cancellation requested")

        self._register_vcenter_stand()
        fake_module = types.SimpleNamespace(VCenterNode=FakeVCenterNode)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = {
                "id": "task-vcenter-stop-locked",
                "status": "queued",
                "task_type": "backup",
                "hypervisor_id": "hv-vc",
                "resource_type": "virtual_machine",
                "resource_external_id": "vm-uuid-1",
                "parameters": {"destination_dir": temp_dir, "tool_path": EXISTING_TOOL_PATH},
            }
            with patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": fake_module}), patch(
                "python.srv.plugins.vcenter.vcenter_backup.run_capped", side_effect=fake_run
            ), patch(
                "python.srv.plugins.vcenter.vcenter_backup.remove_disk_tool_backup",
                side_effect=StorageToolError("object is locked"),
            ), patch(
                "python.srv.plugins.vcenter.vcenter_backup.read_storage_backup_info",
                return_value={"name": "01-demo-vm.vmdk"},
            ):
                result = run_task(task)

        self.assertEqual(result["status"], "stopped")
        message = result["result"]["message"]
        self.assertIn("Object Lock", message)
        self.assertIn("01-demo-vm.vmdk", message)

    def test_an_ordinary_stop_still_reports_only_the_stop(self) -> None:
        def fake_run(command: list[str], **kwargs) -> FakeCompletedProcess:
            self._write_abr_artifacts(command)
            raise ProcessCancelled("cancellation requested")

        result, _ = self._run_stopped_backup(fake_run)

        self.assertEqual(result["result"]["message"], "Session stop requested")


class TaskPoolProgressReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        TASKS.clear()

    def tearDown(self) -> None:
        TASKS.clear()

    def test_message_is_logged_even_when_a_stop_was_already_requested(self) -> None:
        from python.srv.core.task_pool import TaskProcessPool

        TASKS["task-1"] = {"id": "task-1", "cancel_requested": True, "logs": []}
        pool = TaskProcessPool(worker_count=1)

        with self.assertRaises(TaskStopRequested):
            pool._report_progress("task-1", {"stage": "cleanup", "message": "Removing snapshot demo-vm-snapshot"})

        logs = TASKS["task-1"]["logs"]
        self.assertTrue(
            any("Removing snapshot" in str(entry.get("message")) for entry in logs),
            f"expected the progress message to be logged before raising, got: {logs}",
        )

    def test_status_is_not_reset_to_running_once_a_stop_was_requested(self) -> None:
        from python.srv.core.task_pool import TaskProcessPool

        TASKS["task-1"] = {"id": "task-1", "cancel_requested": True, "status": "stopping", "logs": []}
        pool = TaskProcessPool(worker_count=1)

        with self.assertRaises(TaskStopRequested):
            pool._report_progress("task-1", {"stage": "cleanup", "message": "Removing snapshot demo-vm-snapshot"})

        self.assertEqual(TASKS["task-1"]["status"], "stopping")


if __name__ == "__main__":
    unittest.main()
