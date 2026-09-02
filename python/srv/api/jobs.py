# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import (
    CreateCopyJobRequest,
    CreateJobRequest,
    JobDTO,
    JobList,
    StartJobRequest,
    UpdateJobRequest,
    unset_excluded,
)

router = build_router("/jobs", tag="jobs")


def create_job(payload: CreateJobRequest = Body(default_factory=CreateJobRequest)) -> JobDTO:
    return abr_v41.create_backup_job(unset_excluded(payload))


def create_copy_job(payload: CreateCopyJobRequest = Body(default_factory=CreateCopyJobRequest)) -> JobDTO:
    return abr_v41.create_copy_job(unset_excluded(payload))


def list_jobs() -> JobList:
    return {"items": abr_v41.list_jobs()}


def get_job(job_id: str) -> JobDTO:
    return abr_v41.job_to_dto(abr_v41.resolve_job(record_id=job_id))


def patch_job(job_id: str, payload: UpdateJobRequest = Body(default_factory=UpdateJobRequest)) -> JobDTO:
    return abr_v41.edit_job_record(job_id, unset_excluded(payload))


def delete_job(job_id: str) -> dict[str, Any]:
    return abr_v41.delete_job_record(job_id)


def start_job(job_id: str, payload: StartJobRequest = Body(default_factory=StartJobRequest)) -> dict[str, Any]:
    return abr_v41.start_job_record(job_id, force_full=bool(unset_excluded(payload).get("force_full", False)))


register(router, [
    ("", create_job, ["POST"]),
    ("/copy", create_copy_job, ["POST"]),
    ("", list_jobs, ["GET"]),
    ("/{job_id}", get_job, ["GET"]),
    ("/{job_id}", patch_job, ["PATCH"]),
    ("/{job_id}", delete_job, ["DELETE"]),
    ("/{job_id}/start", start_job, ["POST"]),
])
