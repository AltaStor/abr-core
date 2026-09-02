# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pyVmomi import vim


class VddkDiskType(IntEnum):
    MONOLITHIC_SPARSE = 1
    MONOLITHIC_FLAT = 2
    SPLIT_SPARSE = 3
    SPLIT_FLAT = 4
    VMFS_FLAT = 5
    STREAM_OPTIMIZED = 6
    VMFS_THIN = 7
    VMFS_SPARSE = 8


VDDK_DISK_TYPE_NAMES: dict[int, str] = {
    VddkDiskType.MONOLITHIC_SPARSE: "monolithic_sparse",
    VddkDiskType.MONOLITHIC_FLAT: "monolithic_flat",
    VddkDiskType.SPLIT_SPARSE: "split_sparse",
    VddkDiskType.SPLIT_FLAT: "split_flat",
    VddkDiskType.VMFS_FLAT: "vmfs_flat",
    VddkDiskType.STREAM_OPTIMIZED: "stream_optimized",
    VddkDiskType.VMFS_THIN: "vmfs_thin",
    VddkDiskType.VMFS_SPARSE: "vmfs_sparse",
}

VDDK_DISK_TYPE_ALIASES: dict[str, int] = {
    "thin": VddkDiskType.VMFS_THIN,
    "thin-provisioned": VddkDiskType.VMFS_THIN,
    "thin_provisioned": VddkDiskType.VMFS_THIN,
    "thick": VddkDiskType.VMFS_FLAT,
    "thick-provisioned": VddkDiskType.VMFS_FLAT,
    "thick_provisioned": VddkDiskType.VMFS_FLAT,
    "preallocated": VddkDiskType.VMFS_FLAT,
    "vmfs_thin": VddkDiskType.VMFS_THIN,
    "vmfs_flat": VddkDiskType.VMFS_FLAT,
}

RESTORE_DISK_TYPE_CODES = {VddkDiskType.VMFS_FLAT, VddkDiskType.VMFS_THIN}
RESTORE_DISK_TYPE_HELP = "original, thin-provisioned, or thick-provisioned"


def disk_type_name_from_code(code: int | None) -> str | None:
    if code is None:
        return None
    return VDDK_DISK_TYPE_NAMES.get(code)


def normalize_disk_type_code(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("disk type cannot be a boolean")

    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip().lower()
        if not text:
            raise ValueError("disk type cannot be empty")
        if text.isdigit():
            code = int(text)
        else:
            code = VDDK_DISK_TYPE_ALIASES.get(text, -1)

    if code not in RESTORE_DISK_TYPE_CODES:
        raise ValueError(f"disk type must be one of: {RESTORE_DISK_TYPE_HELP}")
    return int(code)


def normalize_disk_type_selection(value: Any) -> str | int:
    if isinstance(value, str) and value.strip().lower() == "original":
        return "original"
    return normalize_disk_type_code(value)


def hostagent_disk_type_from_code(code: int) -> str:
    if code == VddkDiskType.VMFS_THIN:
        return "thin"
    return "preallocated"


def classify_vmware_disk_type(backing: Any, datastore_path: str) -> tuple[int | None, str | None]:
    if isinstance(backing, vim.vm.device.VirtualDisk.SeSparseBackingInfo):
        return 8, VDDK_DISK_TYPE_NAMES[8]

    if isinstance(backing, vim.vm.device.VirtualDisk.SparseVer2BackingInfo):
        return 8, VDDK_DISK_TYPE_NAMES[8]

    thin_provisioned = getattr(backing, "thinProvisioned", None)
    if thin_provisioned is True:
        return 7, VDDK_DISK_TYPE_NAMES[7]

    if thin_provisioned is False and datastore_path.startswith("["):
        return 5, VDDK_DISK_TYPE_NAMES[5]

    return None, None
