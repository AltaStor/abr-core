# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import (
    CreateInventoryRequest,
    InventoryDTO,
    InventoryList,
    InventoryScanResult,
    InventoryTestResult,
    RemovedResult,
    InventoryTreeResponse,
    UpdateInventoryRequest,
    unset_excluded,
)

router = build_router("/inventories", tag="inventories")


def create_inventory(payload: CreateInventoryRequest = Body(default_factory=CreateInventoryRequest)) -> InventoryDTO:
    return abr_v41.create_inventory_record(unset_excluded(payload))


def list_inventories() -> InventoryList:
    return {"items": abr_v41.list_inventory_records()}


def list_inventory_vm_tree() -> InventoryTreeResponse:
    return {"nodes": abr_v41.list_inventory_vm_tree()}


def get_inventory(inventory_id: str) -> InventoryDTO:
    return abr_v41.resolve_inventory(record_id=inventory_id)


def patch_inventory(
    inventory_id: str, payload: UpdateInventoryRequest = Body(default_factory=UpdateInventoryRequest)
) -> InventoryDTO:
    return abr_v41.edit_inventory_record(inventory_id, unset_excluded(payload))


def delete_inventory(inventory_id: str) -> RemovedResult:
    return abr_v41.remove_inventory_record(inventory_id)


def rescan_inventory(inventory_id: str) -> InventoryScanResult:
    return abr_v41.rescan_inventory(inventory_id)


def test_inventory(inventory_id: str) -> InventoryTestResult:
    return abr_v41.test_inventory(inventory_id)


register(router, [
    ("", create_inventory, ["POST"]),
    ("", list_inventories, ["GET"]),
    ("/vms/tree", list_inventory_vm_tree, ["GET"]),
    ("/{inventory_id}", get_inventory, ["GET"]),
    ("/{inventory_id}", patch_inventory, ["PATCH"]),
    ("/{inventory_id}", delete_inventory, ["DELETE"]),
    ("/{inventory_id}/rescan", rescan_inventory, ["POST"]),
    ("/{inventory_id}/test", test_inventory, ["POST"]),
])
