# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body, Query

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import BackupChainDTO, BackupList, ImportBackupRequest, unset_excluded

router = build_router("/backups", tag="backups")


def list_backups(repo: str | None = Query(default=None)) -> BackupList:
    return {"items": abr_v41.list_backup_chains(repo_name=repo)}


def list_backup_tree() -> dict[str, Any]:
    return {"nodes": abr_v41.list_backup_tree()}


def get_backup(backup_id: str) -> BackupChainDTO:
    return abr_v41.get_backup_chain(backup_id)


def delete_backup(
    backup_id: str,
    segment: str | None = Query(default=None),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    return abr_v41.delete_backup_chain(backup_id, segment_id=segment, force=force)


def verify_backup(backup_id: str, wait: bool = Query(default=True)) -> dict[str, Any]:
    return abr_v41.verify_backup_chain_request(backup_id, wait=wait)


def import_backup(payload: ImportBackupRequest = Body(default_factory=ImportBackupRequest)) -> BackupChainDTO:
    body = unset_excluded(payload)
    return abr_v41.import_backup_chain(
        path_value=str(body.get("path") or ""),
        repo_name=body.get("repo"),
        repo_id=body.get("repo_id"),
    )


register(router, [
    ("", list_backups, ["GET"]),
    ("/tree", list_backup_tree, ["GET"]),
    ("/{backup_id}", get_backup, ["GET"]),
    ("/{backup_id}", delete_backup, ["DELETE"]),
    ("/{backup_id}/verify", verify_backup, ["POST"]),
    ("/import", import_backup, ["POST"]),
])
