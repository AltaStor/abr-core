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
    type: DocStr = None
    time: DocStr = None
    days: DocStrArray = None
    day_of_month: DocInt = None
    expression: DocStr = None
    every: DocStr = None
    interval_minutes: DocInt = None
    enabled: DocBool = None
    timezone: DocStr = None


class GfsDTO(_LenientModel):
    weekly: DocInt = None
    monthly: DocInt = None
    yearly: DocInt = None
    enabled: DocBool = None


class GfsMarksDTO(_LenientModel):
    weekly: DocBool = None
    monthly: DocBool = None
    yearly: DocBool = None


class JobEncryptionDTO(_LenientModel):
    enabled: DocBool = None
    key_set: DocBool = None
    key_id: DocStr = None
    key_ids: DocStrArray = None
    key_count: DocInt = None


class TaskParametersDTO(_LenientModel):
    compression_level: DocInt = None
    encryption_enabled: DocBool = None
    encryption_key: DocStr = None
    encryption_key_id: DocStr = None
    encryption_keys: list[EncryptionKeyRefDTO] | DocArray = None
    tool_timeout_seconds: DocInt = None
    app_aware: DocBool = None
    dedup: DocBool = None
    hyperv_consistency: DocStr = None


class ExecutionChoiceDTO(_LenientModel):
    type: DocStr = None
    reason: DocStr = None


class ExecutionPolicyDTO(_LenientModel):
    first_run: ExecutionChoiceDTO | DocObject = None
    manual_force_full: ExecutionChoiceDTO | DocObject = None
    active_full_days: DocStrArray = None
    synthetic_full_days: DocStrArray = None
    active_full_schedule: ScheduleDTO | DocObject = None
    synthetic_full_schedule: ScheduleDTO | DocObject = None
    active_overrides_synthetic: DocBool = None
    synthetic_requires_repository_support: DocBool = None
    default: ExecutionChoiceDTO | DocObject = None


class RepositoryImmutabilityDTO(_LenientModel):
    mode: DocStr = None
    retention_days: DocInt = None
    retention_scope: DocStr = None


class S3RepositoryDTO(_LenientModel):
    bucket: DocStr = None
    prefix: DocStr = None
    region: DocStr = None
    endpoint_url: DocStr = None
    profile: DocStr = None
    path_style: DocBool = None
    verify_tls: DocBool = None
    description: DocStr = None
    access_key_masked: DocStr = None
    has_secret_key: DocBool = None
    immutability: RepositoryImmutabilityDTO | DocObject = None
    location: DocStr = None


class ImmutabilityEffectDTO(_LenientModel):
    previous: RepositoryImmutabilityDTO | DocObject = None
    current: RepositoryImmutabilityDTO | DocObject = None
    changed: DocBool = None
    existing_restore_points: DocInt = None
    existing_data_protected: DocBool = None
    effect: DocStrArray = None
    limitations: DocStrArray = None
    warnings: DocStrArray = None


class ImmutabilityFailureDTO(_LenientModel):
    restore_point_id: DocStr = None
    backup_name: DocStr = None
    message: DocStr = None


class RepositoryCapabilitiesDTO(_LenientModel):
    supports_synthetic_full: DocBool = None
    supports_immutability: DocBool = None


class RepositoryCatalogDTO(_LenientModel):
    backup_chains: DocInt = None
    restore_points: DocInt = None
    used_bytes: DocInt = None


class StoragePermissionsDTO(_LenientModel):
    mode: DocStr = None
    owner: DocStr = None
    group: DocStr = None
    world_writable: DocBool = None
    sticky: DocBool = None
    parent_world_writable: DocBool = None
    readable: DocBool = None
    writable: DocBool = None


class SystemComponentsDTO(_LenientModel):
    api: DocStr = None
    database: DocStr = None
    repositories: DocStr = None
    inventories: DocStr = None
    scheduler: DocStr = None
    task_pool: DocStr = None


class SystemCountsDTO(_LenientModel):
    repositories: DocInt = None
    inventories: DocInt = None
    jobs: DocInt = None
    sessions: DocInt = None
    restore_points: DocInt = None


class HyperVDiskDTO(_LenientModel):
    name: DocStr = None
    controller: DocStr = None
    path: DocStr = None
    format: DocStr = None
    type: DocStr = None
    capacity_bytes: DocInt = None
    file_bytes: DocInt = None
    has_parent: bool | None = None


class HyperVNetworkAdapterDTO(_LenientModel):
    name: DocStr = None
    switch: DocStr = None
    mac: DocStr = None
    connected: bool | None = None


class HyperVVmDetailsDTO(_LenientModel):
    generation: DocInt = None
    configuration_version: DocStr = None
    firmware: DocStr = None
    checkpoint_count: DocInt = None
    clustered: bool | None = None
    owner_node: DocStr = None
    config_path: DocStr = None
    disks: list[HyperVDiskDTO] | DocArray = None
    network_adapters: list[HyperVNetworkAdapterDTO] | DocArray = None


class InventoryTreeNodeDTO(_LenientModel):
    id: DocStr = None
    kind: DocStr = None
    label: DocStr = None
    inventory_id: DocStr = None
    inventory_name: DocStr = None
    datacenter_id: DocStr = None
    datacenter_name: DocStr = None
    disk_capacity_bytes: DocInt = None
    cluster_id: DocStr = None
    cluster_name: DocStr = None
    host_id: DocStr = None
    host_name: DocStr = None
    vm_id: DocStr = None
    guest_os: DocStr = None
    cpu_count: DocInt = None
    memory_mb: DocInt = None
    power_state: DocStr = None
    ip_address: DocStr = None
    host_status: DocStr = None
    cpu_model: DocStr = None
    vm_count: DocInt = None
    health: DocStr = None
    hyperv: HyperVVmDetailsDTO | DocObject = None
    children: list[InventoryTreeNodeDTO] | DocArray = None


class BackupTreeNodeDTO(_LenientModel):
    id: DocStr = None
    kind: DocStr = None
    label: DocStr = None
    repository_id: DocStr = None
    repository_name: DocStr = None
    inventory_id: DocStr = None
    inventory_name: DocStr = None
    job_id: DocStr = None
    job_name: DocStr = None
    protected_object_id: DocStr = None
    protected_object_name: DocStr = None
    backup_id: DocStr = None
    backup_name: DocStr = None
    restore_point_id: DocStr = None
    timestamp: DocStr = None
    last_restore_point_at: DocStr = None
    backup_type: DocStr = None
    health: DocStr = None
    restore_points_count: DocInt = None
    can_restore: DocBool = None
    disabled_reason: DocStr = None
    restore_payload: DocObject = None
    latest_restore_point: RestorePointDTO | DocObject = None
    children: list[BackupTreeNodeDTO] | DocArray = None


class JobRefDTO(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    job_type: DocStr = None


class SessionErrorDTO(_LenientModel):
    message: DocStr = None


class JobRunVmDTO(_LenientModel):
    vm_id: DocStr = None
    vm_name: DocStr = None
    result: DocStr = None
    session_ids: DocStrArray = None
    message: DocStr = None


class JobRunDTO(_LenientModel):
    id: DocStr = None
    trigger: DocStr = None
    started_at: DocStr = None
    finished_at: DocStr = None
    result: DocStr = None
    vm_count: DocInt = None
    succeeded: DocInt = None
    warning: DocInt = None
    failed: DocInt = None
    stopped: DocInt = None
    running: DocInt = None
    vms: list[JobRunVmDTO] | None = None


class JobDTO(_LenientModel):
    id: str
    name: str
    job_type: str
    description: str | None = None
    inventory: _Ref
    vm_ids: list[str] = []
    vm_names: list[str] = []
    vm_count: int = 0
    vms: list[_Ref] | DocArray = []
    repository: _Ref
    source_job: _Ref | DocObject = None
    schedule: ScheduleDTO | DocObject = None
    schedule_summary: str | None = None
    enabled: bool = False
    schedule_state: str | None = None
    retention_days: int | None = None
    gfs: GfsDTO | DocObject = None
    active_full_days: DocStrArray = None
    synthetic_full_days: DocStrArray = None
    active_full_schedule: ScheduleDTO | DocObject = None
    synthetic_full_schedule: ScheduleDTO | DocObject = None
    active_full_next_run_at: str | None = None
    synthetic_full_next_run_at: str | None = None
    compression_level: int | None = None
    encryption: JobEncryptionDTO | DocObject = None
    task_parameters: TaskParametersDTO | DocObject = {}
    backup_options: TaskParametersDTO | DocObject = {}
    effective_execution_policy: ExecutionPolicyDTO | DocObject = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    next_run_at: str | None = None
    next_incremental_run_at: str | None = None
    last_run_at: str | None = None
    clock_anomalies: DocObjectArray = None
    last_session_id: str | None = None
    last_run: JobRunDTO | None = None
    active_sessions: DocObjectArray = None
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
    last_scan_error: DocStr = None
    status_checked_at: DocStr = None
    status_error: DocStr = None
    kind: DocStr = None
    description: DocStr = None
    address: DocStr = None
    endpoint: DocStr = None
    username: DocStr = None
    vm_count: DocInt = None
    host_count: DocInt = None
    cluster_count: DocInt = None
    metadata: DocObject = None
    discovery: InventoryDiscoveryDTO
    connection: InventoryConnectionDTO
    referenced_jobs: DocObjectArray = None
    jobs_count: int = 0
    active_sessions: DocObjectArray = None
    active_session_count: int = 0
    can_delete: bool = True
    delete_disabled_reason: str | None = None


class InventoryList(_LenientModel):
    items: list[InventoryDTO]


class EncryptionKeyRefDTO(_LenientModel):
    key_id: DocStr = None
    key: DocStr = None


class RepositoryCatalogSummaryDTO(_LenientModel):
    backup_chains: DocInt = None
    restore_points: DocInt = None
    used_bytes: DocInt = None


class RestorePointReconcileDTO(_LenientModel):
    checked: DocInt = None
    marked_missing: DocInt = None
    restored: DocInt = None
    unreadable: DocInt = None


class SystemLogEntryDTO(_LenientModel):
    id: DocStr = None
    ts: DocStr = None
    level: DocStr = None
    source: DocStr = None
    logger: DocStr = None
    function: DocStr = None
    line: DocInt = None
    pid: DocInt = None
    message: DocStr = None


class VerifyCheckDTO(_LenientModel):
    name: DocStr = None
    status: DocStr = None
    message: DocStr = None


class LastVerifyDTO(_LenientModel):
    checked_at: DocStr = None
    status: DocStr = None
    message: DocStr = None


class RepositoryStorageDTO(_LenientModel):
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    available: bool = False
    error: str | None = None
    permissions: StoragePermissionsDTO | DocObject = None


class RepositoryDTO(_LenientModel):
    id: str
    name: str
    type: str
    status: str
    created_at: str
    updated_at: str
    last_scan_at: str | None = None
    last_verify_at: DocStr = None
    last_verify: LastVerifyDTO | DocObject = None
    s3: S3RepositoryDTO | DocObject = None
    address: DocStr = None
    metadata: DocObject = None
    bucket: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    path_style: bool | None = None
    verify_tls: bool | None = None
    immutability: RepositoryImmutabilityDTO | DocObject = None
    path: str | None = None
    location: str | None = None
    description: str | None = None
    storage: RepositoryStorageDTO
    capacity_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    capabilities: RepositoryCapabilitiesDTO | DocObject = {}
    catalog: RepositoryCatalogDTO | DocObject = {}
    referenced_jobs: DocObjectArray = None
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
    gfs_marks: GfsMarksDTO | DocObject = None
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
    restore_point_id: DocStr = None
    backup_id: DocStr = None
    point_type: DocStr = None
    available: DocBool = None
    consistency_state: DocStr = None
    job_id: DocStr = None
    job_name: DocStr = None
    inventory_id: DocStr = None
    inventory_name: DocStr = None
    inventory: _Ref | DocObject = None
    repository: _Ref | DocObject = None


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
    restore_points: list[RestorePointDTO] | DocArray = []


class BackupList(_LenientModel):
    items: list[BackupChainDTO]


class SessionVerifyDTO(_LenientModel):
    id: DocStr = None
    result: DocStr = None
    warnings: DocObjectArray = None
    errors: list[Any] = []
    checked_restore_points: DocInt = None
    checked_artifacts: DocInt = None


class CatalogCountsDTO(_LenientModel):
    hypervisors: DocInt = None
    repositories: DocInt = None
    jobs: DocInt = None
    backups: DocInt = None
    tasks: DocInt = None
    users: DocInt = None


class CatalogManifestDTO(_LenientModel):
    format_version: DocInt = None
    product: DocStr = None
    product_version: DocStr = None
    exported_at: DocStr = None
    counts: CatalogCountsDTO | DocObject = None
    configuration_keys: DocStrArray = None


class SessionDTO(_LenientModel):
    id: str
    state: str
    type: str | None = None
    job: _Ref | DocObject = None
    vm_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    resolved_backup_type: str | None = None
    resolved_backup_reason: str | None = None
    progress: int | None = None
    processed_bytes: int | None = None
    transferred_bytes: int | None = None
    errors: list[SessionErrorDTO] | DocArray = None
    warnings: DocObjectArray = None
    message: DocStr = None
    triggered_by: DocStr = None
    run_id: DocStr = None
    vm_name: DocStr = None
    source_vm_name: DocStr = None
    backup_id: DocStr = None
    restore_point_id: DocStr = None
    failure_kind: DocStr = None
    verify: SessionVerifyDTO | DocObject = None
    logs: DocObjectArray = None


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
RawInt = SkipValidation[int]


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
    retention_days: RawInt | None = None
    gfs: SkipValidation[GfsRequest] | None = None
    active_full_days: list[Any] | LaxStr | None = None
    active_full_days_csv: LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days_csv: LaxStr | None = None
    active_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    synthetic_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    active_full_enabled: LaxBool | None = None
    synthetic_full_enabled: LaxBool | None = None
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
    retention_days: RawInt | None = None
    description: LaxStr | None = None


class UpdateJobRequest(_LenientModel):
    name: LaxStr | None = None
    schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    enabled: LaxBool | None = None
    retention_days: RawInt | None = None
    gfs_weekly: RawInt | None = None
    gfs_monthly: RawInt | None = None
    gfs_yearly: RawInt | None = None
    gfs_enabled: LaxBool | None = None
    active_full_days: list[Any] | LaxStr | None = None
    synthetic_full_days: list[Any] | LaxStr | None = None
    active_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    synthetic_full_schedule: SkipValidation[ScheduleRequest] | LaxStr | None = None
    active_full_enabled: LaxBool | None = None
    synthetic_full_enabled: LaxBool | None = None
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


class RunRetentionRequest(_LenientModel):
    dry_run: LaxBool | None = None


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
    disk_datastore_map: DocStrArray = None
    nic_network_map: DocStrArray = None
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


class RestoreStartResult(SessionDTO):
    restore_session_id: DocStr = None
    session_state: DocStr = None
    status: DocStr = None
    restore_mode: DocStr = None
    source_inventory_id: DocStr = None
    source_inventory_name: DocStr = None
    source_vm_id: DocStr = None
    target_inventory_id: DocStr = None
    target_inventory_name: DocStr = None
    target_host_id: DocStr = None
    target_host_name: DocStr = None
    target_datastore_id: DocStr = None
    datastore_name: DocStr = None
    new_name: DocStr = None





class RestoreVmDTO(_LenientModel):
    vm_id: DocStr = None
    vm_name: DocStr = None
    inventory: _Ref | DocObject = None
    restore_available: DocBool = None
    latest_restore_point: RestorePointDTO | DocObject = None


class RestoreVmList(_LenientModel):
    items: list[RestoreVmDTO] | DocArray = None


class RestorePointList(_LenientModel):
    items: list[RestorePointDTO] | DocArray = None






class RestoreContextPointDTO(_LenientModel):
    id: DocStr = None
    type: DocStr = None
    status: DocStr = None
    health: DocStr = None
    available: DocBool = None
    timestamp: DocStr = None
    backup_id: DocStr = None
    job_id: DocStr = None
    job_name: DocStr = None
    inventory_id: DocStr = None
    inventory: _Ref | DocObject = None
    repository_id: DocStr = None
    repository_type: DocStr = None
    repository: _Ref | DocObject = None


class RestoreSourceVmDTO(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    instance_uuid: DocStr = None
    guest_id: DocStr = None
    guest_os: DocStr = None
    firmware: DocStr = None
    cpu_count: DocInt = None
    memory_mb: DocInt = None
    disks: DocObjectArray = None
    nics: DocObjectArray = None
    folder: DocStr = None
    original_host: DocStr = None
    original_datastore: DocStr = None
    resource_pool: DocStr = None


class RestoreTargetsDTO(_LenientModel):
    inventories: DocObjectArray = None
    hosts: DocObjectArray = None
    datastores: DocObjectArray = None
    networks: DocObjectArray = None
    folders: DocObjectArray = None
    resource_pools: DocObjectArray = None
    existing_vms: DocObjectArray = None


class RestoreDefaultsDTO(_LenientModel):
    restore_mode: DocStr = None
    target_inventory_id: DocStr = None
    target_host_id: DocStr = None
    target_datastore_id: DocStr = None
    target_vm_name: DocStr = None
    disk_provisioning: DocStr = None
    power_on_after_restore: DocBool = None
    preserve_mac_addresses: DocBool = None
    preserve_firmware: DocBool = None
    connect_nics: DocBool = None
    network_mappings: DocObjectArray = None
    datastore_mappings: DocObjectArray = None


class RestoreCapabilitiesDTO(_LenientModel):
    supports_in_place_restore: DocBool = None
    supports_network_mapping: DocBool = None
    supports_datastore_mapping: DocBool = None
    supports_disk_provisioning: DocBool = None
    supports_folder_selection: DocBool = None
    supports_resource_pool_selection: DocBool = None
    supports_preserve_mac: DocBool = None
    supports_power_on_after_restore: DocBool = None
    supports_connect_nics: DocBool = None


class RestoreContextDTO(_LenientModel):
    restore_point: RestoreContextPointDTO | DocObject = None
    source_vm: RestoreSourceVmDTO | DocObject = None
    targets: RestoreTargetsDTO | DocObject = None
    defaults: RestoreDefaultsDTO | DocObject = None
    capabilities: RestoreCapabilitiesDTO | DocObject = None


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
    components: SystemComponentsDTO | DocObject = None
    counts: SystemCountsDTO | DocObject = None


class DeletedResult(_LenientModel):
    id: DocStr = None
    deleted: DocBool = None


class RemovedResult(_LenientModel):
    id: DocStr = None
    removed: DocBool = None



class JobStartResult(_LenientModel):
    job: JobRefDTO | DocObject = None
    items: list[SessionDTO] | DocArray = None


class RetentionHoldDTO(_LenientModel):
    point_id: DocStr = None
    until: DocStr = None


class RetentionChainDTO(_LenientModel):
    job_id: DocStr = None
    vm_id: DocStr = None
    vm_name: DocStrOrNull = None
    repository_id: DocStrOrNull = None
    repository_name: DocStrOrNull = None
    points: DocInt = None
    kept: DocInt = None
    removed: DocStrArray = None
    held: list[RetentionHoldDTO] | DocObjectArray = None
    failed: DocStrArray = None
    kept_for_chain: DocStrArray = None
    skipped_reason: DocStrOrNull = None


class RetentionSummaryDTO(_LenientModel):
    chains: DocInt = None
    points: DocInt = None
    removed: DocInt = None
    held: DocInt = None
    failed: DocInt = None
    skipped: DocInt = None


class RetentionRunResult(_LenientModel):
    job_id: DocStr = None
    job_name: DocStr = None
    dry_run: DocBool = None
    retention_days: DocInt = None
    gfs: GfsDTO | DocObject = None
    summary: RetentionSummaryDTO | DocObject = None
    chains: list[RetentionChainDTO] | DocArray = None
    ran_at: DocStr = None



class SessionLogEntryDTO(_LenientModel):
    timestamp: DocStr = None
    level: DocStr = None
    component: DocStr = None
    message: DocStr = None


class SessionLogDTO(_LenientModel):
    id: DocStr = None
    logs: list[SessionLogEntryDTO] | DocArray = None


class StopSessionResult(_LenientModel):
    id: DocStr = None
    state: DocStr = None
    message: DocStr = None



class InventoryTreeResponse(_LenientModel):
    nodes: list[InventoryTreeNodeDTO] | DocArray = None


class BackupTreeResponse(_LenientModel):
    nodes: list[BackupTreeNodeDTO] | DocArray = None


class RepositoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    catalog: RepositoryCatalogSummaryDTO | DocObject = None
    restore_points: RestorePointReconcileDTO | DocObject = None


class S3TestResult(_LenientModel):
    status: DocStr = None
    message: DocStr = None
    checks: list[VerifyCheckDTO] | DocArray = None


class RepositoryVerifyResult(RepositoryDTO):
    message: DocStr = None
    checks: list[VerifyCheckDTO] | DocArray = None
    immutability_effect: ImmutabilityEffectDTO | DocObject = None
    error: DocStr = None


class RepositoryImmutabilityApplyResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    message: DocStr = None
    immutability: RepositoryImmutabilityDTO | DocObject = None
    actual_retention_days: list[int] | None = None
    restore_point_count: DocInt = None
    updated_restore_points: DocInt = None
    failed_restore_points: DocInt = None
    failures: list[ImmutabilityFailureDTO] | DocArray = None


class InventoryScanResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    status: DocStr = None
    last_scan_at: DocStr = None
    discovery: InventoryDiscoveryDTO | DocObject = None


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
    entries: list[SystemLogEntryDTO] | DocArray = None
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
    manifest: CatalogManifestDTO | DocObject = None
    archive_base64: DocStr = None


class CatalogRestoreResult(_LenientModel):
    manifest: CatalogManifestDTO | DocObject = None
    restored: DocObject = None
    replaced_users: DocStrArray = None
    configuration: DocObject = None



class SupportBundleResult(_LenientModel):
    manifest: CatalogManifestDTO | DocObject = None
    archive_base64: DocStr = None


class HealthDTO(_LenientModel):
    status: DocStr = None


class LoginResult(_LenientModel):
    token: DocStr = None
    expires_at: DocStr = None
    user: PublicUserDTO | DocObject = None


class LogoutResult(_LenientModel):
    status: DocStr = None


class ExtendSessionResult(_LenientModel):
    expires_at: DocStr = None
