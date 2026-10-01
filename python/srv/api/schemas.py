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
DocStrOrNull = Annotated[Any, Field(json_schema_extra={"anyOf": [{"type": "string"}, {"type": "null"}]})]


class ScheduleDTO(_LenientModel):
    type: LaxStr | None = None
    time: LaxStr | None = None
    days: DocStrArray = None
    day_of_month: LaxInt = None
    expression: LaxStr | None = None
    every: LaxStr | None = None
    interval_minutes: LaxInt = None
    enabled: LaxBool | None = None
    timezone: LaxStr | None = None


class GfsDTO(_LenientModel):
    weekly: LaxInt = None
    monthly: LaxInt = None
    yearly: LaxInt = None
    enabled: LaxBool | None = None


class GfsMarksDTO(_LenientModel):
    weekly: LaxBool | None = None
    monthly: LaxBool | None = None
    yearly: LaxBool | None = None


class JobEncryptionDTO(_LenientModel):
    enabled: LaxBool | None = None
    key_set: LaxBool | None = None
    key_id: LaxStr | None = None
    key_ids: DocStrArray = None
    key_count: LaxInt = None


class TaskParametersDTO(_LenientModel):
    compression_level: LaxInt = None
    encryption_enabled: LaxBool | None = None
    encryption_key: LaxStr | None = None
    encryption_key_id: LaxStr | None = None
    encryption_keys: list[EncryptionKeyRefDTO] | None = None
    tool_timeout_seconds: LaxInt = None
    app_aware: LaxBool | None = None
    dedup: LaxBool | None = None


class ExecutionChoiceDTO(_LenientModel):
    type: str | None = None
    reason: str | None = None


class ExecutionPolicyDTO(_LenientModel):
    first_run: ExecutionChoiceDTO | None = None
    manual_force_full: ExecutionChoiceDTO | None = None
    active_full_days: DocStrArray = None
    synthetic_full_days: DocStrArray = None
    active_full_schedule: ScheduleDTO | None = None
    synthetic_full_schedule: ScheduleDTO | None = None
    active_overrides_synthetic: LaxBool | None = None
    synthetic_requires_repository_support: LaxBool | None = None
    default: ExecutionChoiceDTO | None = None


class RepositoryImmutabilityDTO(_LenientModel):
    mode: LaxStr | None = None
    retention_days: LaxInt = None
    retention_scope: LaxStr | None = None


class S3RepositoryDTO(_LenientModel):
    bucket: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    profile: str | None = None
    path_style: bool | None = None
    verify_tls: bool | None = None
    description: str | None = None
    access_key_masked: str | None = None
    has_secret_key: bool | None = None
    immutability: RepositoryImmutabilityDTO | None = None
    location: str | None = None


class ImmutabilityEffectDTO(_LenientModel):
    previous: RepositoryImmutabilityDTO | None = None
    current: RepositoryImmutabilityDTO | None = None
    changed: bool | None = None
    existing_restore_points: int | None = None
    existing_data_protected: bool | None = None
    effect: DocStrArray = None
    limitations: DocStrArray = None
    warnings: DocStrArray = None


class ImmutabilityFailureDTO(_LenientModel):
    restore_point_id: str | None = None
    backup_name: str | None = None
    message: str | None = None


class RepositoryCapabilitiesDTO(_LenientModel):
    supports_synthetic_full: LaxBool | None = None
    supports_immutability: LaxBool | None = None


class RepositoryCatalogDTO(_LenientModel):
    backup_chains: LaxInt = None
    restore_points: LaxInt = None
    used_bytes: LaxInt = None


class StoragePermissionsDTO(_LenientModel):
    mode: LaxStr | None = None
    owner: LaxStr | None = None
    group: LaxStr | None = None
    world_writable: LaxBool | None = None
    sticky: LaxBool | None = None
    parent_world_writable: LaxBool | None = None
    readable: LaxBool | None = None
    writable: LaxBool | None = None


class SystemComponentsDTO(_LenientModel):
    api: LaxStr | None = None
    database: LaxStr | None = None
    repositories: LaxStr | None = None
    inventories: LaxStr | None = None
    scheduler: LaxStr | None = None
    task_pool: LaxStr | None = None


class SystemCountsDTO(_LenientModel):
    repositories: LaxInt = None
    inventories: LaxInt = None
    jobs: LaxInt = None
    sessions: LaxInt = None
    restore_points: LaxInt = None


class InventoryTreeNodeDTO(_LenientModel):
    id: LaxStr | None = None
    kind: LaxStr | None = None
    label: LaxStr | None = None
    inventory_id: LaxStr | None = None
    inventory_name: LaxStr | None = None
    datacenter_id: LaxStr | None = None
    datacenter_name: LaxStr | None = None
    disk_capacity_bytes: LaxInt = None
    cluster_id: LaxStr | None = None
    cluster_name: LaxStr | None = None
    host_id: LaxStr | None = None
    host_name: LaxStr | None = None
    vm_id: LaxStr | None = None
    guest_os: LaxStr | None = None
    cpu_count: LaxInt = None
    memory_mb: LaxInt = None
    power_state: LaxStr | None = None
    ip_address: LaxStr | None = None
    host_status: LaxStr | None = None
    cpu_model: LaxStr | None = None
    vm_count: LaxInt = None
    health: LaxStr | None = None
    children: list[InventoryTreeNodeDTO] | None = None


class BackupTreeNodeDTO(_LenientModel):
    id: LaxStr | None = None
    kind: LaxStr | None = None
    label: LaxStr | None = None
    repository_id: LaxStr | None = None
    repository_name: LaxStr | None = None
    inventory_id: LaxStr | None = None
    inventory_name: LaxStr | None = None
    job_id: LaxStr | None = None
    job_name: LaxStr | None = None
    protected_object_id: LaxStr | None = None
    protected_object_name: LaxStr | None = None
    backup_id: LaxStr | None = None
    backup_name: LaxStr | None = None
    restore_point_id: LaxStr | None = None
    timestamp: LaxStr | None = None
    last_restore_point_at: LaxStr | None = None
    backup_type: LaxStr | None = None
    health: LaxStr | None = None
    restore_points_count: LaxInt = None
    can_restore: LaxBool | None = None
    disabled_reason: LaxStr | None = None
    restore_payload: DocObject = None
    latest_restore_point: RestorePointDTO | None = None
    children: list[BackupTreeNodeDTO] | None = None


class JobRefDTO(_LenientModel):
    id: LaxStr | None = None
    name: LaxStr | None = None
    job_type: LaxStr | None = None


class SessionErrorDTO(_LenientModel):
    message: LaxStr | None = None


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
    schedule: ScheduleDTO | None = None
    schedule_summary: str | None = None
    enabled: bool = False
    schedule_state: str | None = None
    retention_days: int | None = None
    gfs: GfsDTO | None = None
    active_full_days: list[Any] | None = None
    synthetic_full_days: list[Any] | None = None
    active_full_schedule: ScheduleDTO | None = None
    synthetic_full_schedule: ScheduleDTO | None = None
    active_full_next_run_at: str | None = None
    synthetic_full_next_run_at: str | None = None
    compression_level: int | None = None
    encryption: JobEncryptionDTO | None = None
    task_parameters: TaskParametersDTO | None = {}
    backup_options: TaskParametersDTO | None = {}
    effective_execution_policy: ExecutionPolicyDTO | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    next_run_at: str | None = None
    next_incremental_run_at: str | None = None
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
    port: int | None = None
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
    last_scan_error: str | None = None
    kind: str | None = None
    description: str | None = None
    address: str | None = None
    endpoint: str | None = None
    username: str | None = None
    vm_count: int | None = None
    host_count: int | None = None
    cluster_count: int | None = None
    metadata: DocObject = None
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


class EncryptionKeyRefDTO(_LenientModel):
    key_id: str | None = None
    key: str | None = None


class RepositoryCatalogSummaryDTO(_LenientModel):
    backup_chains: int | None = None
    restore_points: int | None = None
    used_bytes: int | None = None


class RestorePointReconcileDTO(_LenientModel):
    checked: int | None = None
    marked_missing: int | None = None
    restored: int | None = None
    unreadable: int | None = None


class SystemLogEntryDTO(_LenientModel):
    id: str | None = None
    ts: str | None = None
    level: str | None = None
    source: str | None = None
    logger: str | None = None
    function: str | None = None
    line: int | None = None
    pid: int | None = None
    message: str | None = None


class VerifyCheckDTO(_LenientModel):
    name: str | None = None
    status: str | None = None
    message: str | None = None


class LastVerifyDTO(_LenientModel):
    checked_at: str | None = None
    status: str | None = None
    message: str | None = None


class RepositoryStorageDTO(_LenientModel):
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    available: bool = False
    error: str | None = None
    permissions: StoragePermissionsDTO | None = None


class RepositoryDTO(_LenientModel):
    id: str
    name: str
    type: str
    status: str
    created_at: str
    updated_at: str
    last_scan_at: str | None = None
    last_verify_at: str | None = None
    last_verify: LastVerifyDTO | None = None
    s3: S3RepositoryDTO | None = None
    address: str | None = None
    metadata: DocObject = None
    bucket: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    path_style: bool | None = None
    verify_tls: bool | None = None
    immutability: RepositoryImmutabilityDTO | None = None
    path: str | None = None
    location: str | None = None
    description: str | None = None
    storage: RepositoryStorageDTO
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    capabilities: RepositoryCapabilitiesDTO | None = {}
    catalog: RepositoryCatalogDTO | None = {}
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
    gfs_marks: GfsMarksDTO | None = None
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
    restore_point_id: str | None = None
    backup_id: str | None = None
    point_type: str | None = None
    available: bool | None = None
    consistency_state: str | None = None
    job_id: str | None = None
    job_name: str | None = None
    inventory_id: str | None = None
    inventory_name: str | None = None
    inventory: _Ref | None = None
    repository: _Ref | None = None


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


class SessionVerifyDTO(_LenientModel):
    id: str | None = None
    result: str | None = None
    warnings: list[Any] = []
    errors: list[Any] = []
    checked_restore_points: int | None = None
    checked_artifacts: int | None = None


class CatalogCountsDTO(_LenientModel):
    hypervisors: int | None = None
    repositories: int | None = None
    jobs: int | None = None
    backups: int | None = None
    tasks: int | None = None
    users: int | None = None


class CatalogManifestDTO(_LenientModel):
    format_version: int | None = None
    product: str | None = None
    product_version: str | None = None
    exported_at: str | None = None
    counts: CatalogCountsDTO | None = None
    configuration_keys: DocStrArray = None


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
    progress: int | None = None
    processed_bytes: int | None = None
    transferred_bytes: int | None = None
    errors: list[SessionErrorDTO] | None = None
    warnings: list[Any] = []
    message: str | None = None
    triggered_by: str | None = None
    vm_name: str | None = None
    source_vm_name: str | None = None
    backup_id: str | None = None
    restore_point_id: str | None = None
    failure_kind: str | None = None
    verify: SessionVerifyDTO | None = None
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


class ScheduleRequest(_LenientModel):
    type: LaxStr | None = None
    schedule_type: LaxStr | None = None
    kind: LaxStr | None = None
    time: LaxStr | None = None
    start_time: LaxStr | None = None
    days: DocStrArray = None
    weekdays: DocStrArray = None
    day_of_month: LaxInt = None
    every: LaxStr | None = None
    interval_minutes: LaxInt = None
    expression: LaxStr | None = None
    timezone: LaxStr | None = None
    enabled: LaxBool | None = None


class GfsRequest(_LenientModel):
    enabled: LaxBool | None = None
    weekly: LaxInt = None
    monthly: LaxInt = None
    yearly: LaxInt = None


class TaskParametersRequest(_LenientModel):
    compression_level: LaxInt = None
    encryption_enabled: LaxBool | None = None
    encryption_key: LaxStr | None = None
    encryption_key_id: LaxStr | None = None
    tool_timeout_seconds: LaxInt = None
    app_aware: LaxBool | None = None
    dedup: LaxBool | None = None


class S3Request(_LenientModel):
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = None
    verify_tls: LaxBool | None = None


class ImmutabilityRequest(_LenientModel):
    mode: LaxStr | None = None
    retention_days: LaxInt = None
    retention_scope: LaxStr | None = None


class RestoreDatastoreMapping(_LenientModel):
    source_datastore_id: LaxStr | None = None
    source_datastore: LaxStr | None = None
    target_datastore_id: LaxStr | None = None
    target_datastore: LaxStr | None = None


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
    s3: SkipValidation[S3Request] | None = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = None
    verify_tls: LaxBool | None = None
    immutability: SkipValidation[ImmutabilityRequest] | None = None
    create_bucket: LaxBool | None = None


class UpdateRepositoryRequest(_LenientModel):
    name: LaxStr | None = None
    path: LaxStr | None = None
    address: LaxStr | None = None
    server: LaxStr | None = None
    share: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    description: LaxStr | None = None
    s3: SkipValidation[S3Request] | None = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = None
    verify_tls: LaxBool | None = None
    immutability: SkipValidation[ImmutabilityRequest] | None = None
    create_bucket: LaxBool | None = None


class CreateJobRequest(_LenientModel):
    name: LaxStr | None = None
    vms: list[LaxStr] | LaxStr | None = None
    inventory: LaxStr | None = None
    inventory_id: LaxStr | None = None
    repo: LaxStr | None = None
    repo_id: LaxStr | None = None
    schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    enabled: LaxBool | None = None
    retention_days: LaxInt = None
    gfs: SkipValidation[GfsRequest] | None = None
    active_full_days: list[Any] | LaxStr | None = None
    active_full_days_csv: LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days_csv: LaxStr | None = None
    active_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    synthetic_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    description: LaxStr | None = None
    task_parameters: SkipValidation[TaskParametersRequest] | None = None
    backup_options: SkipValidation[TaskParametersRequest] | None = None


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
    schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    enabled: LaxBool | None = None
    retention_days: LaxInt = None
    description: LaxStr | None = None


class UpdateJobRequest(_LenientModel):
    name: LaxStr | None = None
    schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    enabled: LaxBool | None = None
    retention_days: LaxInt = None
    gfs_weekly: LaxInt = None
    gfs_monthly: LaxInt = None
    gfs_yearly: LaxInt = None
    gfs_enabled: LaxBool | None = None
    active_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    active_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    synthetic_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    description: LaxStr | None = None
    task_parameters: SkipValidation[TaskParametersRequest] | None = None
    repo: LaxStr | None = None
    repo_id: LaxStr | None = None
    inventory: LaxStr | None = None
    inventory_id: LaxStr | None = None
    vms: list[LaxStr] | LaxStr | None = None
    source_job: LaxStr | None = None
    source_job_id: LaxStr | None = None


class StartJobRequest(_LenientModel):
    force_full: LaxBool | None = None
    force_synthetic: LaxBool | None = None
    target_repository_id: LaxStr | None = None


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


class VerifyBackupRequest(_LenientModel):
    encryption_key: LaxStr | None = None


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
    datastore_map: DocStrArray = None
    datastore_mappings: SkipValidation[list[RestoreDatastoreMapping]] | None = None
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
    encryption_key: LaxStr | None = None
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




class RestoreVmDTO(_LenientModel):
    vm_id: str | None = None
    vm_name: str | None = None
    inventory: _Ref | None = None
    restore_available: bool | None = None
    latest_restore_point: RestorePointDTO | None = None


class RestoreVmList(_LenientModel):
    items: list[RestoreVmDTO] | None = None


class RestorePointList(_LenientModel):
    items: list[RestorePointDTO] | None = None






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




class TlsCertificateInstallRequest(_LenientModel):
    certificate: LaxStr | None = Field(
        default=None,
        description="PEM certificate: the server certificate first, then any intermediates in order.",
    )
    private_key: LaxStr | None = Field(
        default=None, description="PEM private key (RSA 2048+ or EC P-256/P-384/P-521)."
    )
    passphrase: LaxStr | None = Field(
        default=None, description="Passphrase of an encrypted private key; not stored."
    )


TLS_WARNING_CODES = [
    "expired",
    "expires_soon",
    "no_subject_alternative_names",
    "appliance_names_not_covered",
]

DocTlsWarningArray = Annotated[
    Any,
    Field(
        json_schema_extra={
            "type": "array",
            "items": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "enum": TLS_WARNING_CODES},
                    "message": {"type": "string"},
                    "days_remaining": {
                        "type": "integer",
                        "description": 'Only on "expires_soon": days left before the certificate expires.',
                    },
                    "appliance_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            'Only on "appliance_names_not_covered": this appliance\'s own host '
                            "names and addresses, none of which the certificate lists."
                        ),
                    },
                },
            },
        }
    ),
]


class TlsCertificateDTO(_LenientModel):
    source: DocStr
    auto_renew: DocBool
    subject: DocStr
    common_name: DocStrOrNull = Field(
        description="Subject common name, or null when the certificate carries none."
    )
    issuer: DocStr
    issuer_common_name: DocStrOrNull = Field(
        description="Issuer common name, or null when the issuer carries none."
    )
    self_signed: DocBool
    dns_names: DocStrArray
    ip_addresses: DocStrArray
    serial_number: DocStr
    fingerprint_sha256: DocStr
    key_type: DocStr
    not_before: DocStr
    not_after: DocStr
    days_remaining: DocInt
    expired: DocBool
    chain_length: DocInt
    installed_at: DocStrOrNull = Field(
        description=(
            "When an administrator installed this certificate, ISO 8601. Null for a "
            'generated certificate, which nobody installed (source "generated", and for '
            "an administrator certificate copied over the files by hand)."
        )
    )
    installed_by: DocStrOrNull = Field(
        description=(
            "Who installed this certificate. Null for a generated certificate, for one "
            "copied over the files by hand, and when the request carried no user."
        )
    )
    live_reload: DocBool
    warnings: DocTlsWarningArray


class TlsCertificateChangeDTO(TlsCertificateDTO):
    reloaded: DocBool


class SystemHealthDTO(_LenientModel):
    status: DocStr = None
    components: SystemComponentsDTO | None = None
    counts: SystemCountsDTO | None = None


class DeletedResult(_LenientModel):
    id: DocStr = None
    deleted: DocBool = None


class RemovedResult(_LenientModel):
    id: DocStr = None
    removed: DocBool = None



class JobStartResult(_LenientModel):
    job: JobRefDTO | None = None
    items: list[SessionDTO] | None = None



class SessionLogEntryDTO(_LenientModel):
    timestamp: str | None = None
    level: str | None = None
    component: str | None = None
    message: str | None = None


class SessionLogDTO(_LenientModel):
    id: DocStr = None
    logs: list[SessionLogEntryDTO] | None = None


class StopSessionResult(_LenientModel):
    id: DocStr = None
    state: DocStr = None
    message: DocStr = None



class InventoryTreeResponse(_LenientModel):
    nodes: list[InventoryTreeNodeDTO] | None = None


class BackupTreeResponse(_LenientModel):
    nodes: list[BackupTreeNodeDTO] | None = None


class RepositoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    catalog: RepositoryCatalogSummaryDTO | None = None
    restore_points: RestorePointReconcileDTO | None = None


class S3TestResult(_LenientModel):
    status: DocStr = None
    message: DocStr = None
    checks: list[VerifyCheckDTO] | None = None


class RepositoryVerifyResult(RepositoryDTO):
    message: DocStr = None
    checks: list[VerifyCheckDTO] | None = None
    immutability_effect: ImmutabilityEffectDTO | None = None
    error: DocStr = None


class RepositoryImmutabilityApplyResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    message: DocStr = None
    immutability: RepositoryImmutabilityDTO | None = None
    actual_retention_days: list[int] | None = None
    restore_point_count: DocInt = None
    updated_restore_points: DocInt = None
    failed_restore_points: DocInt = None
    failures: list[ImmutabilityFailureDTO] | None = None


class InventoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    discovery: InventoryDiscoveryDTO | None = None


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
    entries: list[SystemLogEntryDTO] | None = None
    total: DocInt = None
    limit: DocInt = None
    level: DocStrOrNull = Field(
        default=None,
        description="The minimum level the entries were filtered to, or null when none was asked for.",
    )
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
    manifest: CatalogManifestDTO | None = None
    archive_base64: DocStr = None


class CatalogRestoreResult(_LenientModel):
    manifest: CatalogManifestDTO | None = None
    restored: DocObject = None
    configuration: DocObject = None



class SupportBundleResult(_LenientModel):
    manifest: CatalogManifestDTO | None = None
    archive_base64: DocStr = None


class HealthDTO(_LenientModel):
    status: DocStr = None


class LoginResult(_LenientModel):
    token: DocStr = None
    expires_at: DocStr = None
    user: PublicUserDTO | None = None


class LogoutResult(_LenientModel):
    status: DocStr = None


class ExtendSessionResult(_LenientModel):
    expires_at: DocStr = None
