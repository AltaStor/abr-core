# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, SkipValidation


class _LenientModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Ref(_LenientModel):
    id: str | None = None
    name: str | None = None


class JobDTO(_LenientModel):
    id: str
    name: str
    job_type: str
    description: str | None = None
    inventory: _Ref
    vm_ids: list[str] = []
    vm_names: list[str] = []
    vm_count: int = 0
    vms: list[_Ref] = []
    repository: _Ref
    source_job: _Ref | None = None
    schedule: dict[str, Any] | None = None
    schedule_summary: str | None = None
    enabled: bool = False
    schedule_state: str | None = None
    retention_days: int | None = None
    gfs: dict[str, Any] | None = None
    active_full_days: list[Any] | None = None
    synthetic_full_days: list[Any] | None = None
    compression_level: Any = None
    encryption: dict[str, Any] | None = None
    task_parameters: dict[str, Any] = {}
    backup_options: dict[str, Any] = {}
    effective_execution_policy: Any = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_session_id: str | None = None
    active_sessions: list[Any] = []
    active_session_count: int = 0
    can_delete: bool = True
    delete_disabled_reason: str | None = None


class JobList(_LenientModel):
    items: list[JobDTO]


class InventoryDiscoveryDTO(_LenientModel):
    last_scan_at: str | None = None
    vm_count: int = 0
    host_count: int = 0
    cluster_count: int = 0


class InventoryConnectionDTO(_LenientModel):
    address: str | None = None
    endpoint: str | None = None
    port: Any = None
    username: str | None = None
    token_present: bool = False
    insecure: bool = False


class InventoryDTO(_LenientModel):
    id: str
    name: str
    type: str
    status: str
    created_at: str
    updated_at: str
    last_scan_at: str | None = None
    discovery: InventoryDiscoveryDTO
    connection: InventoryConnectionDTO
    referenced_jobs: list[Any] = []
    jobs_count: int = 0
    active_sessions: list[Any] = []
    active_session_count: int = 0
    can_delete: bool = True
    delete_disabled_reason: str | None = None


class InventoryList(_LenientModel):
    items: list[InventoryDTO]


class RepositoryStorageDTO(_LenientModel):
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    available: bool = False
    error: str | None = None
    permissions: dict[str, Any] | None = None


class RepositoryDTO(_LenientModel):
    id: str
    name: str
    type: str
    status: str
    created_at: str
    updated_at: str
    last_scan_at: str | None = None
    bucket: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    path_style: bool | None = None
    verify_tls: bool | None = None
    path: str | None = None
    location: str | None = None
    description: str | None = None
    storage: RepositoryStorageDTO
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    capabilities: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    referenced_jobs: list[Any] = []
    jobs_count: int = 0
    backup_chain_count: int = 0
    restore_point_count: int = 0
    can_delete: bool = True
    delete_disabled_reason: str | None = None
    warnings: list[str] = []


class RepositoryList(_LenientModel):
    items: list[RepositoryDTO]


class RestorePointDTO(_LenientModel):
    id: str
    created_at: str | None = None
    execution_type: str | None = None
    execution_reason: str | None = None
    gfs_marks: dict[str, Any] | None = None
    gfs_protected: bool = False
    parent_point_id: str | None = None
    status: str | None = None
    vm_id: str | None = None
    vm_name: str | None = None
    repository_id: str | None = None
    repository_name: str | None = None
    repository_type: str | None = None
    size_bytes: int | None = None
    logical_size_bytes: int | None = None
    compression_ratio: float | None = None
    compression_level: int | None = None
    encryption_enabled: bool | None = None


class BackupChainDTO(_LenientModel):
    id: str
    name: str | None = None
    job: _Ref
    vm: _Ref
    inventory: _Ref
    repository: _Ref
    status: str | None = None
    restore_point_count: int = 0
    created_at: str | None = None
    last_restore_point_at: str | None = None
    latest_execution_type: str | None = None
    latest_execution_reason: str | None = None
    gfs_protected: bool = False
    restore_points: list[RestorePointDTO] = []


class BackupList(_LenientModel):
    items: list[BackupChainDTO]


class SessionDTO(_LenientModel):
    id: str
    state: str
    type: str | None = None
    job: _Ref | None = None
    vm_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    resolved_backup_type: str | None = None
    resolved_backup_reason: str | None = None
    progress: Any = None
    processed_bytes: int | None = None
    transferred_bytes: int | None = None
    errors: Any = None
    logs: list[Any] = []


class SessionList(_LenientModel):
    items: list[SessionDTO]
    # Pagination envelope. `total` is the match count before limit/offset, so a
    # client can page until it has them all; `limit` is null when the caller
    # asked for everything.
    total: int = 0
    limit: int | None = None
    offset: int = 0


class PublicUserDTO(_LenientModel):
    id: str
    name: str
    fullName: str = ""
    created_at: str | None = None
    passwordPolicyExempt: bool = False


class UserList(_LenientModel):
    items: list[PublicUserDTO]


def unset_excluded(payload: BaseModel | None) -> dict[str, Any]:
    return (
        payload.model_dump(exclude_unset=True, warnings=False)
        if payload is not None
        else {}
    )


def _lax_int(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    
    if isinstance(value, float):
        return int(value)
    
    if isinstance(value, str):
        text = value.strip()

        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return value

    return value


LaxStr = SkipValidation[str]
LaxBool = SkipValidation[bool]
LaxInt = Annotated[int | None, BeforeValidator(_lax_int)]


class CreateInventoryRequest(_LenientModel):
    name: LaxStr | None = None
    type: LaxStr | None = None
    address: LaxStr | None = None
    port: LaxInt = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    token: LaxStr | None = None
    insecure: LaxBool | None = None
    description: LaxStr | None = None


class UpdateInventoryRequest(_LenientModel):
    name: LaxStr | None = None
    address: LaxStr | None = None
    port: LaxInt = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    token: LaxStr | None = None
    insecure: LaxBool | None = None
    description: LaxStr | None = None


class CreateRepositoryRequest(_LenientModel):
    name: LaxStr | None = None
    type: LaxStr | None = None
    path: LaxStr | None = None
    address: LaxStr | None = None
    server: LaxStr | None = None
    share: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    description: LaxStr | None = None
    s3: dict[str, Any] | None = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = None
    verify_tls: LaxBool | None = None


class UpdateRepositoryRequest(_LenientModel):
    name: LaxStr | None = None
    path: LaxStr | None = None
    address: LaxStr | None = None
    server: LaxStr | None = None
    share: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    description: LaxStr | None = None
    s3: dict[str, Any] | None = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = None
    verify_tls: LaxBool | None = None


class CreateJobRequest(_LenientModel):
    name: LaxStr | None = None
    vms: list[LaxStr] | LaxStr | None = None
    inventory: LaxStr | None = None
    inventory_id: LaxStr | None = None
    repo: LaxStr | None = None
    repo_id: LaxStr | None = None
    schedule: dict[str, Any] | LaxStr | None = None
    retention_days: LaxInt = None
    gfs: dict[str, Any] | None = None
    active_full_days: list[Any] | LaxStr | None = None
    active_full_days_csv: LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days_csv: LaxStr | None = None
    description: LaxStr | None = None
    task_parameters: dict[str, Any] | None = None
    backup_options: dict[str, Any] | None = None


class CreateCopyJobRequest(_LenientModel):
    name: LaxStr | None = None
    source: LaxStr | None = None
    source_job: LaxStr | None = None
    source_id: LaxStr | None = None
    source_job_id: LaxStr | None = None
    repo: LaxStr | None = None
    repository: LaxStr | None = None
    repo_id: LaxStr | None = None
    repository_id: LaxStr | None = None
    schedule: dict[str, Any] | LaxStr | None = None
    enabled: LaxBool | None = None
    retention_days: LaxInt = None
    description: LaxStr | None = None


class UpdateJobRequest(_LenientModel):
    name: LaxStr | None = None
    schedule: dict[str, Any] | LaxStr | None = None
    retention_days: LaxInt = None
    gfs_weekly: LaxInt = None
    gfs_monthly: LaxInt = None
    gfs_yearly: LaxInt = None
    gfs_enabled: LaxBool | None = None
    active_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    description: LaxStr | None = None
    task_parameters: dict[str, Any] | None = None
    repo: LaxStr | None = None
    repo_id: LaxStr | None = None
    inventory: LaxStr | None = None
    inventory_id: LaxStr | None = None
    vms: list[LaxStr] | LaxStr | None = None
    source_job: LaxStr | None = None
    source_job_id: LaxStr | None = None


class StartJobRequest(_LenientModel):
    force_full: LaxBool | None = None


class StopSessionRequest(_LenientModel):
    force: LaxBool | None = None


class ImportBackupRequest(_LenientModel):
    path: LaxStr | None = None
    repo: LaxStr | None = None
    repo_id: LaxStr | None = None


class CreateUserRequest(_LenientModel):
    name: LaxStr | None = None
    password: LaxStr | None = None
    fullName: LaxStr | None = None
