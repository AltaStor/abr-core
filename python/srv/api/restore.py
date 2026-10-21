# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Query

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import (
    RestoreContextDTO,
    RestorePointList,
    RestoreStartResult,
    RestoreVmList,
    StartRestoreRequest,
    unset_excluded,
)

router = build_router("/restore", tag="restore")


def list_restore_vms(
    inventory: str | None = Query(
        default=None,
        description="Only VMs backed up from the inventory with this name: the inventory's name, not its ID, matched whole and case-insensitively",
    ),
) -> RestoreVmList:
    return {"items": abr_v41.restore_vm_list(inventory_name=inventory)}


def list_restore_point_catalog(
    vm: str | None = Query(
        default=None,
        description="Only restore points of this VM: its ID, or its name matched whole and case-insensitively",
    ),
    backup: str | None = Query(
        default=None,
        description="Only restore points of this backup chain: the chain ID as GET /backups lists it (backup::<job>::<vm>::<repository>)",
    ),
    inventory: str | None = Query(
        default=None,
        description="Only restore points from this inventory: its ID, or its name matched whole and case-insensitively",
    ),
) -> RestorePointList:
    return {"items": abr_v41.list_restore_points(vm=vm, backup_id=backup, inventory=inventory)}


def list_restore_points(vm_id: str) -> RestorePointList:
    return {"items": abr_v41.restore_point_list(vm_id)}


def get_restore_context(
    restore_point_id: str = Query(..., description="ID of the restore point to restore, as GET /restore/points lists it"),
) -> RestoreContextDTO:
    return abr_v41.get_restore_context(restore_point_id)


def start_restore(payload: StartRestoreRequest = Body(default_factory=StartRestoreRequest)) -> RestoreStartResult:
    return abr_v41.restore_start(unset_excluded(payload))


register(router, [
    ("/vms", list_restore_vms, ["GET"], "restore.view"),
    ("/points", list_restore_point_catalog, ["GET"], "restore.view"),
    ("/vms/{vm_id}/points", list_restore_points, ["GET"], "restore.view"),
    ("/context", get_restore_context, ["GET"], "restore.view"),
    ("/start", start_restore, ["POST"], "restore.start"),
])
