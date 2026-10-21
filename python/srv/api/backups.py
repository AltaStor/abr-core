# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Query

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import (
    BackupChainDTO,
    BackupDeleteResult,
    BackupList,
    BackupVerifyResult,
    ImportBackupRequest,
    BackupTreeResponse,
    VerifyBackupRequest,
    unset_excluded,
)

router = build_router("/backups", tag="backups")


def list_backups(
    repo: str | None = Query(
        default=None,
        description="Only chains in the repository with this name: the repository's name, not its ID, matched whole and case-insensitively",
    ),
) -> BackupList:
    return {"items": abr_v41.list_backup_chains(repo_name=repo)}


def list_backup_tree() -> BackupTreeResponse:
    return {"nodes": abr_v41.list_backup_tree()}


def get_backup(backup_id: str) -> BackupChainDTO:
    return abr_v41.get_backup_chain(backup_id)


def delete_backup(
    backup_id: str,
    segment: str | None = Query(
        default=None,
        description=(
            "Delete only one segment of the chain, a full backup and the incrementals on it: the restore point ID of "
            "that segment's full backup. Omitted, the whole chain is deleted"
        ),
    ),
    force: bool = Query(
        default=False,
        description="true deletes restore points GFS keeps; without it such a delete is refused with 409 gfs_protected_backup",
    ),
    wait: bool = Query(
        default=True,
        description="true answers when the delete has finished; false starts it as a session and answers at once with the session ID",
    ),
) -> BackupDeleteResult:
    return abr_v41.delete_backup_chain_request(backup_id, segment_id=segment, force=force, wait=wait)


def verify_backup(
    backup_id: str,
    wait: bool = Query(
        default=True,
        description="true answers with the verdict when the check has finished; false starts it as a session and answers at once with the session ID",
    ),
    payload: VerifyBackupRequest = Body(default_factory=VerifyBackupRequest),
) -> BackupVerifyResult:
    body = unset_excluded(payload)
    return abr_v41.verify_backup_chain_request(
        backup_id,
        wait=wait,
        encryption_key=str(body.get("encryption_key") or "").strip() or None,
    )


def import_backup(payload: ImportBackupRequest = Body(default_factory=ImportBackupRequest)) -> BackupChainDTO:
    body = unset_excluded(payload)
    return abr_v41.import_backup_chain(
        path_value=str(body.get("path") or ""),
        repo_name=body.get("repo"),
        repo_id=body.get("repo_id"),
    )


register(router, [
    ("", list_backups, ["GET"], "backup.view"),
    ("/tree", list_backup_tree, ["GET"], "backup.view"),
    ("/{backup_id}", get_backup, ["GET"], "backup.view"),
    ("/{backup_id}", delete_backup, ["DELETE"], "backup.delete"),
    ("/{backup_id}/verify", verify_backup, ["POST"], "backup.verify"),
    ("/import", import_backup, ["POST"], "backup.import"),
])
