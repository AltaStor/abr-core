# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List


ProgressCallback = Callable[[dict[str, Any]], None] | None


class QuiesceFailedError(RuntimeError):
    def __init__(self, vm_name: str, reason: str | None) -> None:
        self.vm_name = vm_name
        self.reason = reason
        super().__init__(
            f"VMware Tools could not quiesce VM '{vm_name}' for the backup snapshot"
            f"{': ' + reason.rstrip(' .') if reason else ''}. "
            "The usual cause is a pre-freeze script under /etc/vmware-tools/backupScripts.d/ that exits with an error "
            "(Linux) or a failing VSS writer, listed by 'vssadmin list writers' (Windows). Fix it in the guest, "
            "or let the job take a crash-consistent backup when quiescing fails."
        )


class Hypervisor(ABC):
    """Abstract class for all hypervisor plugins"""

    def __enter__(self) -> "Hypervisor":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()

    @abstractmethod
    def connect(self) -> None:
        """Connecting with hypervisor."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnecting with hypervisor."""
        pass
    @abstractmethod
    def get_thumbprint(self) -> str:
        """Get print of hypervisor certificate(if available)"""
        pass
    
    @abstractmethod
    def get_service_type(self) -> str:
        """Get the specific type of the hypervisor API service."""
        pass

    @abstractmethod
    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> Dict[str, str]:
        """Collect specific parameters for the backup utility."""
        pass

    @abstractmethod
    def create_backup_snapshot(self, vm_name: str, *, quiesce: bool = True) -> str:
        """Creating backup snapshot and return its id."""
        pass

    @abstractmethod
    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        """Enable changed block tracking for a VM if it is currently disabled."""
        pass

    @abstractmethod
    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        """Collect per-disk CBT change IDs from the given VM snapshot."""
        pass

    @abstractmethod
    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: int,
        change_id: str,
    ) -> List[Dict[str, int]]:
        """Collect CBT changed disk extents for a snapshot disk since the given change ID."""
        pass

    @abstractmethod
    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        """Get list of virtual machine disks."""
        pass

    @abstractmethod
    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        """Collect essential VM configuration metadata required for restoration."""
        pass

    @abstractmethod
    def list_hosts(self) -> List[Dict[str, Any]]:
        """Get a list of available physical hosts and their accessible datastores."""
        pass

    @abstractmethod
    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: Dict[str, Any],
        disk_layout: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Create a new virtual machine with specified hardware and empty disks."""
        pass

    @abstractmethod
    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        """Delete snapshot from virtual machine."""
        pass

    def consolidate_disks(self, vm_name: str) -> bool:
        return False

    @abstractmethod
    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
    ) -> dict[str, Any]:
        """Attach an existing virtual disk file from a datastore to the specified virtual machine."""
        pass

    @abstractmethod
    def get_all_vms(self) -> List[Dict[str, Any]]:
        """Get base list of all virtual machines."""
        pass

    @abstractmethod
    def get_inventory(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get full inventories(vms, disks, adapters)."""
        pass

    @abstractmethod
    def backup(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        """Run a hypervisor-specific backup task."""
        pass

    @abstractmethod
    def restore(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        """Run a hypervisor-specific restore task."""
        pass
