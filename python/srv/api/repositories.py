# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import (
    CreateRepositoryRequest,
    RepositoryDTO,
    RepositoryList,
    UpdateRepositoryRequest,
    unset_excluded,
)

router = build_router("/repositories", tag="repositories")


def create_repository(
    payload: CreateRepositoryRequest = Body(default_factory=CreateRepositoryRequest),
) -> RepositoryDTO:
    return abr_v41.create_repository_record(unset_excluded(payload))


def test_s3_connection(
    payload: CreateRepositoryRequest = Body(default_factory=CreateRepositoryRequest),
) -> dict[str, Any]:
    return abr_v41.test_s3_connection(unset_excluded(payload))


def test_s3_immutability(
    payload: CreateRepositoryRequest = Body(default_factory=CreateRepositoryRequest),
) -> dict[str, Any]:
    return abr_v41.test_s3_immutability(unset_excluded(payload))


def list_repositories() -> RepositoryList:
    return {"items": abr_v41.list_repository_records()}


def get_repository(repository_id: str) -> RepositoryDTO:
    return abr_v41.resolve_repository(record_id=repository_id)


def patch_repository(
    repository_id: str, payload: UpdateRepositoryRequest = Body(default_factory=UpdateRepositoryRequest)
) -> RepositoryDTO:
    return abr_v41.edit_repository_record(repository_id, unset_excluded(payload))


def delete_repository(repository_id: str) -> dict[str, Any]:
    return abr_v41.remove_repository_record(repository_id)


def rescan_repository(repository_id: str) -> dict[str, Any]:
    return abr_v41.rescan_repository(repository_id)


def verify_repository(repository_id: str) -> dict[str, Any]:
    return abr_v41.verify_repository(repository_id)


def verify_repository_immutability(repository_id: str) -> dict[str, Any]:
    return abr_v41.verify_repository_immutability(repository_id)


def apply_repository_immutability(repository_id: str) -> dict[str, Any]:
    return abr_v41.apply_immutability_to_existing_backups(repository_id)


register(router, [
    ("", create_repository, ["POST"]),
    ("/test-s3", test_s3_connection, ["POST"]),
    ("/test-immutability", test_s3_immutability, ["POST"]),
    ("", list_repositories, ["GET"]),
    ("/{repository_id}", get_repository, ["GET"]),
    ("/{repository_id}", patch_repository, ["PATCH"]),
    ("/{repository_id}", delete_repository, ["DELETE"]),
    ("/{repository_id}/rescan", rescan_repository, ["POST"]),
    ("/{repository_id}/verify", verify_repository, ["POST"]),
    ("/{repository_id}/verify-immutability", verify_repository_immutability, ["POST"]),
    ("/{repository_id}/apply-immutability", apply_repository_immutability, ["POST"]),
])
