# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...core.storage_tool import (
    DISK_TOOL_LAUNCHER,
    default_disk_tool_path,
    disk_tool_has_soap_api,
)

VDDK = "vddk"
SOAP = "soap"
TRANSPORTS = (VDDK, SOAP)
FROM_INVENTORY = "inventory"
TRANSPORT_NAMES = {VDDK: "VDDK", SOAP: "the vSphere SOAP API"}
VDDK_LIBRARY = Path("lib64") / "libvixDiskLib.so"


def parse_transport(value: Any, *, inventory_allowed: bool = False) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    allowed = (*TRANSPORTS, FROM_INVENTORY) if inventory_allowed else TRANSPORTS
    if text not in allowed:
        raise ValueError(f"transport must be one of: {', '.join(allowed)}")
    return None if text == FROM_INVENTORY else text


def inventory_transport(record: dict[str, Any] | None) -> str:
    metadata = (record or {}).get("metadata")
    value = str((metadata if isinstance(metadata, dict) else {}).get("vsphere_transport") or "")
    return value if value in TRANSPORTS else VDDK


def job_transport(job: dict[str, Any] | None) -> str | None:
    parameters = (job or {}).get("task_parameters")
    value = str((parameters if isinstance(parameters, dict) else {}).get("vsphere_transport") or "")
    return value if value in TRANSPORTS else None


def session_transport(job: dict[str, Any] | None, inventory: dict[str, Any] | None) -> str:
    return job_transport(job) or inventory_transport(inventory)


def vddk_library(tool_path: Path | None = None) -> Path | None:
    configured = os.environ.get("ABR_VMDK_TOOL_VDDK_DIR", "").strip()
    if configured:
        roots = [Path(configured).expanduser()]
    else:
        launcher = (tool_path or default_disk_tool_path()).expanduser()
        roots = [launcher.parent.parent / "vddk", launcher.parent.parent.parent / "vddk"]
    for root in roots:
        if (root / VDDK_LIBRARY).is_file():
            return root / VDDK_LIBRARY
    return None


def transport_problem(transport: str, tool_path: Path | None = None) -> str | None:
    launcher = (tool_path or default_disk_tool_path()).expanduser()
    if launcher.name != DISK_TOOL_LAUNCHER:
        return None
    configured = os.environ.get("ABR_VMDK_TOOL_VDDK_DIR", "").strip()
    if configured and not (Path(configured).expanduser() / VDDK_LIBRARY).is_file():
        return (
            f"ABR_VMDK_TOOL_VDDK_DIR is set to {configured}, which has no lib64/libvixDiskLib.so, and abr-vmdk-tool "
            "does not start until it points at the VMware VDDK or is unset; neither transport works until then"
        )
    if transport == SOAP and not disk_tool_has_soap_api(launcher):
        return (
            "The vSphere SOAP transport is not available: abr-vmdk-tool on this server has no --api soap. "
            "Install a newer AltaStor Backup build, or use the VDDK transport"
        )
    if transport == VDDK and vddk_library(launcher) is None:
        return (
            "The VDDK transport is not available: the VMware VDDK is not installed (no lib64/libvixDiskLib.so "
            "in ABR_VMDK_TOOL_VDDK_DIR or in the vddk folder next to abr-vmdk-tool). Install the VDDK, or use "
            "the vSphere SOAP transport"
        )
    return None


def tool_api_args(transport: str | None) -> list[str]:
    return ["--api", SOAP] if transport == SOAP else []
