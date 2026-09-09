# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SkipValidation


class _LenientModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Ref(_LenientModel):
    id: str | None = None
    name: str | None = None


DocStr = Annotated[Any, Field(json_schema_extra={"type": "string"})]
DocInt = Annotated[Any, Field(json_schema_extra={"type": "integer"})]
DocNum = Annotated[Any, Field(json_schema_extra={"type": "number"})]
DocBool = Annotated[Any, Field(json_schema_extra={"type": "boolean"})]
DocObject = Annotated[Any, Field(json_schema_extra={"type": "object"})]
DocArray = Annotated[Any, Field(json_schema_extra={"type": "array"})]
DocStrArray = Annotated[Any, Field(json_schema_extra={"type": "array", "items": {"type": "string"}})]
DocObjectArray = Annotated[Any, Field(json_schema_extra={"type": "array", "items": {"type": "object"}})]


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
    immutability: dict[str, Any] | None = None
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
    # client knows the size of what it asked for without fetching it; `limit` is
    # null when the caller asked for everything.
    total: int = 0
    limit: int | None = None
    offset: int = 0
    # Page-shaped view of the same slice, for clients that think in pages rather
    # than offsets. `next`/`prev` are ready-to-follow URLs, null at either end
    # (and whenever no `limit` was given, since then there is only one page).
    page: int = 1
    per_page: int | None = None
    pages: int = 1
    has_next: bool = False
    has_prev: bool = False
    next: str | None = None
    prev: str | None = None


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
    immutability: dict[str, Any] | None = None


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
    immutability: dict[str, Any] | None = None


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




class UserResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    fullName: DocStr = None
    created_at: DocStr = None
    passwordPolicyExempt: DocBool = None





class StartRestoreTarget(_LenientModel):
    inventory_id: LaxStr | None = None
    inventory: LaxStr | None = None
    host_id: LaxStr | None = None
    datastore_id: LaxStr | None = None
    vm_name: LaxStr | None = None
    folder_id: LaxStr | None = None
    resource_pool_id: LaxStr | None = None


class StartRestoreOptions(_LenientModel):
    power_on_after_restore: LaxBool | None = None
    preserve_mac_addresses: LaxBool | None = None
    preserve_firmware: LaxBool | None = None
    connect_nics: LaxBool | None = None
    disk_provisioning: LaxStr | None = None


class StartRestoreNetworkMapping(_LenientModel):
    source_nic_id: LaxStr | None = None
    target_network_id: LaxStr | None = None


class StartRestoreRequest(_LenientModel):
    restore_point_id: LaxStr | None = None
    point_id: LaxStr | None = None
    backup_id: LaxStr | None = None
    backup: LaxStr | None = None
    inventory_id: LaxStr | None = None
    repository_id: LaxStr | None = None
    repository: LaxStr | None = None
    repo_id: LaxStr | None = None
    repo: LaxStr | None = None
    restore_mode: LaxStr | None = None
    target: SkipValidation[StartRestoreTarget] | None = None
    options: SkipValidation[StartRestoreOptions] | None = None
    network_mappings: SkipValidation[list[StartRestoreNetworkMapping]] | None = None
    network_map: DocStrArray = None
    target_inventory: LaxStr | None = None
    target_inventory_id: LaxStr | None = None
    target_host: LaxStr | None = None
    target_host_id: LaxStr | None = None
    target_datastore: LaxStr | None = None
    target_datastore_id: LaxStr | None = None
    target_vm_name: LaxStr | None = None
    name: LaxStr | None = None
    power_on: LaxBool | None = None
    power_on_after_restore: LaxBool | None = None
    preserve_mac_addresses: LaxBool | None = None
    preserve_firmware: LaxBool | None = None
    connect_nics: LaxBool | None = None
    disk_provisioning: LaxStr | None = None
    disk_type: LaxStr | None = None
    force: LaxBool | None = None


class RestoreStartResult(_LenientModel):
    id: DocStr = None
    restore_session_id: DocStr = None
    session_state: DocStr = None
    state: DocStr = None
    status: DocStr = None
    restore_mode: DocStr = None
    restore_point_id: DocStr = None
    backup_id: DocStr = None
    source_inventory_id: DocStr = None
    source_inventory_name: DocStr = None
    source_vm_id: DocStr = None
    source_vm_name: DocStr = None
    target_inventory_id: DocStr = None
    target_inventory_name: DocStr = None
    target_host_id: DocStr = None
    target_host_name: DocStr = None
    target_datastore_id: DocStr = None
    datastore_name: DocStr = None
    new_name: DocStr = None




class RestoreVmList(_LenientModel):
    items: DocObjectArray = None


class RestorePointList(_LenientModel):
    items: DocObjectArray = None






class RestoreContextDTO(_LenientModel):
    restore_point: DocObject = None
    source_vm: DocObject = None
    targets: DocObject = None
    defaults: DocObject = None
    capabilities: DocObject = None


class SystemVersionDTO(_LenientModel):
    product: DocStr = None
    version: DocStr = None
    cli_binary: DocStr = None


class SystemInfoDTO(_LenientModel):
    product: DocStr = None
    version: DocStr = None
    hostname: DocStr = None
    os: DocStr = None
    kernel: DocStr = None
    uptime_sec: DocNum = None
    cpu_cores: DocInt = None
    memory_gb: DocNum = None




class SystemHealthDTO(_LenientModel):
    status: DocStr = None
    components: DocObject = None
    counts: DocObject = None


class DeletedResult(_LenientModel):
    id: DocStr = None
    deleted: DocBool = None


class RemovedResult(_LenientModel):
    id: DocStr = None
    removed: DocBool = None



class JobStartResult(_LenientModel):
    job: DocObject = None
    items: DocObjectArray = None



class SessionLogDTO(_LenientModel):
    id: DocStr = None
    logs: DocObjectArray = None


class StopSessionResult(_LenientModel):
    id: DocStr = None
    state: DocStr = None
    message: DocStr = None



class TreeResponse(_LenientModel):
    nodes: DocObjectArray = None


class RepositoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    catalog: DocObject = None


class S3TestResult(_LenientModel):
    status: DocStr = None
    message: DocStr = None
    checks: DocObjectArray = None


class RepositoryVerifyResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    type: DocStr = None
    status: DocStr = None
    message: DocStr = None
    checks: DocObjectArray = None
    immutability: DocObject = None
    immutability_effect: DocObject = None


class RepositoryImmutabilityApplyResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    message: DocStr = None
    immutability: DocObject = None
    actual_retention_days: DocArray = None
    restore_point_count: DocInt = None
    updated_restore_points: DocInt = None
    failed_restore_points: DocInt = None
    failures: DocObjectArray = None


class InventoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    discovery: DocObject = None


class InventoryTestResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    type: DocStr = None
    status: DocStr = None
    code: DocStr = None
    message: DocStr = None
    cluster: DocStr = None
    error: DocStr = None


class BackupDeleteResult(_LenientModel):
    id: DocStr = None
    session_id: DocStr = None
    state: DocStr = None
    deleted: DocBool = None
    restore_point_count: DocInt = None
    artifacts_removed: DocBool = None


class BackupVerifyResult(_LenientModel):
    id: DocStr = None
    session_id: DocStr = None
    state: DocStr = None
    result: DocStr = None
    checked_restore_points: DocInt = None
    checked_artifacts: DocInt = None
    warnings: DocArray = None
    errors: DocArray = None



class SystemLogsDTO(_LenientModel):
    entries: DocObjectArray = None
    total: DocInt = None
    limit: DocInt = None
    level: DocStr = None
    has_more: DocBool = None
    files: DocStrArray = None
    searched: DocStrArray = None
    warnings: DocStrArray = None


class CatalogSnapshotRequest(_LenientModel):
    password: LaxStr | None = None


class CatalogRestoreRequest(_LenientModel):
    password: LaxStr | None = None
    archive_base64: LaxStr | None = None



class CatalogSnapshotResult(_LenientModel):
    manifest: DocObject = None
    archive_base64: DocStr = None


class CatalogRestoreResult(_LenientModel):
    manifest: DocObject = None
    restored: DocObject = None
    configuration: DocObject = None



class SupportBundleResult(_LenientModel):
    manifest: DocObject = None
    archive_base64: DocStr = None


class HealthDTO(_LenientModel):
    status: DocStr = None


class LoginResult(_LenientModel):
    token: DocStr = None
    expires_at: DocStr = None
    user: DocObject = None


class LogoutResult(_LenientModel):
    status: DocStr = None


class ExtendSessionResult(_LenientModel):
    expires_at: DocStr = None
