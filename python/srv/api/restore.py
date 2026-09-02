# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body, Query

from python.srv.services import abr_v41

from ._routing import body_dict, build_router, register

router = build_router("/restore", tag="restore")


def list_restore_vms(inventory: str | None = Query(default=None)) -> dict[str, Any]:
    return {"items": abr_v41.restore_vm_list(inventory_name=inventory)}


def list_restore_point_catalog(
    vm: str | None = Query(default=None),
    backup: str | None = Query(default=None),
    inventory: str | None = Query(default=None),
) -> dict[str, Any]:
    return {"items": abr_v41.list_restore_points(vm=vm, backup_id=backup, inventory=inventory)}


def list_restore_points(vm_id: str) -> dict[str, Any]:
    return {"items": abr_v41.restore_point_list(vm_id)}


def get_restore_context(restore_point_id: str = Query(...)) -> dict[str, Any]:
    return abr_v41.get_restore_context(restore_point_id)


def start_restore(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return abr_v41.restore_start(body_dict(payload))


register(router, [
    ("/vms", list_restore_vms, ["GET"]),
    ("/points", list_restore_point_catalog, ["GET"]),
    ("/vms/{vm_id}/points", list_restore_points, ["GET"]),
    ("/context", get_restore_context, ["GET"]),
    ("/start", start_restore, ["POST"]),
])
