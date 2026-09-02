# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any, Dict, List

from .hypervisor import Hypervisor, ProgressCallback
from ..services.mock_backup_emulator import MockBackupEmulator


class MockHypervisor(Hypervisor):
    def __init__(self, emulator: MockBackupEmulator | None = None) -> None:
        self._emulator = emulator or MockBackupEmulator()

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get_thumbprint(self) -> str:
        return ""

    def get_service_type(self) -> str:
        return "mock"

    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> Dict[str, str]:
        raise NotImplementedError("MockHypervisor does not provide VMware backup parameters")

    def create_backup_snapshot(self, vm_name: str) -> str:
        raise NotImplementedError("MockHypervisor does not support snapshots")

    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        raise NotImplementedError("MockHypervisor does not support CBT")

    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        raise NotImplementedError("MockHypervisor does not support CBT")

    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: int,
        change_id: str,
    ) -> List[Dict[str, int]]:
        raise NotImplementedError("MockHypervisor does not support CBT")

    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        raise NotImplementedError("MockHypervisor does not expose VM disks")

    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        raise NotImplementedError("MockHypervisor does not expose restore metadata")

    def list_hosts(self) -> List[Dict[str, Any]]:
        return []

    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: Dict[str, Any],
        disk_layout: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("MockHypervisor does not create VM shells")

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        raise NotImplementedError("MockHypervisor does not support snapshots")

    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("MockHypervisor does not attach disks")

    def get_all_vms(self) -> List[Dict[str, Any]]:
        return []

    def get_inventory(self) -> Dict[str, List[Dict[str, Any]]]:
        return {}

    def backup(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        return self._emulator.backup(task, progress_callback=progress_callback)

    def restore(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        return self._emulator.restore(task, progress_callback=progress_callback)
