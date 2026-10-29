# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body

from python.srv.services import abr_v41, default_repository

from ._routing import build_router, register
from .schemas import (
    CreateRepositoryRequest,
    DefaultRepositoryRequest,
    DefaultRepositoryResult,
    RemovedResult,
    RepositoryDTO,
    RepositoryImmutabilityApplyResult,
    RepositoryList,
    RepositoryScanResult,
    RepositoryVerifyResult,
    S3TestResult,
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
) -> S3TestResult:
    return abr_v41.test_s3_connection(unset_excluded(payload))


def test_s3_immutability(
    payload: CreateRepositoryRequest = Body(default_factory=CreateRepositoryRequest),
) -> S3TestResult:
    return abr_v41.test_s3_immutability(unset_excluded(payload))


def get_default_repository() -> RepositoryDTO:
    return default_repository.get_default_repository()


def create_default_repository(
    payload: DefaultRepositoryRequest = Body(default_factory=DefaultRepositoryRequest),
) -> DefaultRepositoryResult:
    repository, created = default_repository.ensure_default_repository()
    changed = False
    if payload.support_hyperv is not None:
        repository, changed = default_repository.set_hyperv_support(payload.support_hyperv)
    return {"created": created, "changed": changed, "repository": repository}


def list_repositories() -> RepositoryList:
    return {"items": abr_v41.list_repository_records()}


def get_repository(repository_id: str) -> RepositoryDTO:
    return abr_v41.resolve_repository(record_id=repository_id)


def patch_repository(
    repository_id: str, payload: UpdateRepositoryRequest = Body(default_factory=UpdateRepositoryRequest)
) -> RepositoryDTO:
    return abr_v41.edit_repository_record(repository_id, unset_excluded(payload))


def delete_repository(repository_id: str) -> RemovedResult:
    return abr_v41.remove_repository_record(repository_id)


def rescan_repository(repository_id: str) -> RepositoryScanResult:
    return abr_v41.rescan_repository(repository_id)


def verify_repository(repository_id: str) -> RepositoryVerifyResult:
    return abr_v41.verify_repository(repository_id)


def verify_repository_immutability(repository_id: str) -> RepositoryVerifyResult:
    return abr_v41.verify_repository_immutability(repository_id)


def apply_repository_immutability(repository_id: str) -> RepositoryImmutabilityApplyResult:
    return abr_v41.apply_immutability_to_existing_backups(repository_id)


register(router, [
    ("", create_repository, ["POST"], "repository.create"),
    ("/test-s3", test_s3_connection, ["POST"], "repository.create"),
    ("/test-immutability", test_s3_immutability, ["POST"], "repository.create"),
    ("", list_repositories, ["GET"], "repository.view"),
    ("/default", get_default_repository, ["GET"], "repository.view"),
    ("/default", create_default_repository, ["POST"], "repository.create"),
    ("/{repository_id}", get_repository, ["GET"], "repository.view"),
    ("/{repository_id}", patch_repository, ["PATCH"], "repository.edit"),
    ("/{repository_id}", delete_repository, ["DELETE"], "repository.delete"),
    ("/{repository_id}/rescan", rescan_repository, ["POST"], "repository.rescan"),
    ("/{repository_id}/verify", verify_repository, ["POST"], "repository.verify"),
    ("/{repository_id}/verify-immutability", verify_repository_immutability, ["POST"], "repository.verify"),
    ("/{repository_id}/apply-immutability", apply_repository_immutability, ["POST"], "repository.apply_immutability"),
])
