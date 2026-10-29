# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Annotated, Any, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SkipValidation,
    StrictBool,
)


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
DocIntOrNull = Annotated[Any, Field(json_schema_extra={"anyOf": [{"type": "integer"}, {"type": "null"}]})]


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
    on_quiesce_failure: DocStr = None
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
    clock: DocStr = None


class SystemClockDTO(_LenientModel):
    blocked: DocBool = None
    server_time: DocStr = None
    last_known_at: DocStrOrNull = None
    detected_at: DocStrOrNull = None
    last_trusted_at: DocStrOrNull = None
    jump_seconds: DocIntOrNull = None
    source: DocStrOrNull = None
    threshold_hours: DocInt = None
    confirmed_at: DocStrOrNull = None
    confirmed_by: DocStrOrNull = None
    message: DocStrOrNull = None
    was_blocked: DocBool = None


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
    retention_min_fulls: int | None = None
    gfs: GfsDTO | DocObject = None
    active_full_days: DocStrArray = None
    synthetic_full_days: DocStrArray = None
    active_full_schedule: ScheduleDTO | DocObject = None
    synthetic_full_schedule: ScheduleDTO | DocObject = None
    active_full_next_run_at: str | None = None
    synthetic_full_next_run_at: str | None = None
    compression_level: int | None = None
    on_quiesce_failure: str | None = None
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
    vm_reassignments: DocObjectArray = None
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
    protocol: str | None = None


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
    marked_chain_broken: DocInt = None
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
    default: bool = False
    support_hyperv: bool = False
    backup_chain_count: int = 0
    restore_point_count: int = 0
    can_delete: bool = True
    delete_disabled_reason: str | None = None
    warnings: list[str] = []


class RepositoryList(_LenientModel):
    items: list[RepositoryDTO]


class DefaultRepositoryRequest(_LenientModel):
    support_hyperv: bool | None = Field(
        default=None,
        description=(
            "Turn the hidden SMB share Hyper-V hosts write through on (true) or off (false); "
            "leave it out to keep the current setting."
        ),
    )


class DefaultRepositoryResult(_LenientModel):
    created: bool
    changed: bool = False
    repository: RepositoryDTO


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
    immutability_expires_at: str | None = None
    consistency: DocStr = None
    consistency_reason: DocStr = None
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
    consistency: DocStr = None
    consistency_reason: DocStr = None
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
    role: str | None = Field(default=None, description="The user's one role, by role ID")
    enabled: bool = True
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
LaxBool = StrictBool
LaxInt = Annotated[int | None, BeforeValidator(_lax_int)]
RawInt = SkipValidation[int]


def _checked_booleans(model: type[BaseModel]) -> BeforeValidator:
    keys = tuple(name for name, field in model.model_fields.items() if LaxBool in get_args(field.annotation))

    def check(value: Any) -> Any:
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, dict):
                continue
            for key in keys:
                if item.get(key) is not None and not isinstance(item[key], bool):
                    raise ValueError(f"{key} must be true or false")
        return value

    return BeforeValidator(check)


OMITTED_KEEPS = "null or omitted leaves it as it is"
INVENTORY_NAME = "The inventory's name, not its ID, matched whole and case-insensitively; give this or inventory_id"
INVENTORY_ID = "The inventory's ID, not its name; give this or inventory"
REPOSITORY_NAME = "The repository's name, not its ID, matched whole and case-insensitively; give this or the ID field"
REPOSITORY_ID = "The repository's ID, not its name; give this or the name field"
JOB_VMS = (
    "VMs to back up: a list, or one comma-separated text. Each item is a VM's ID (vCenter instance UUID, Hyper-V VM ID), "
    "its name matched whole and case-insensitively, or its inventory path"
)
FULL_SCHEDULE_STATE = (
    "true or false switches that full schedule on or off and keeps its settings; null or omitted keeps the schedule's own "
    "enabled (true); true or false without that schedule (its object or weekdays) in the request answers 422"
)
FULL_SCHEDULE_STATE_EDIT = (
    "true or false switches that full schedule on or off and keeps its settings; the state it already has answers 200 "
    "without moving its next run; true or false for a schedule the job does not have, and the request does not give, "
    f"answers 422; {OMITTED_KEEPS}"
)
LEGACY_FULL_DAYS = "Older form of the full schedule: weekdays (mon..sun) at the base schedule's time; prefer the *_full_schedule object"
S3_PATH_STYLE = (
    "true addresses objects as endpoint/bucket/key, false as bucket.endpoint/key; null or omitted: true when endpoint_url is "
    "given, false for AWS"
)
S3_VERIFY_TLS = "false accepts the endpoint's certificate without checking it; null or omitted: true"
CREATE_BUCKET = "true creates the bucket when it does not exist yet; false, null or omitted: the bucket must already exist"
INSECURE = "true accepts the server's certificate without checking it; false checks it"
POWER_ON = "true powers the restored VM on; false, null or omitted leaves it off"
PRESERVE_MAC = "true is refused (not supported yet); false, null or omitted gives the restored VM new MAC addresses"
PRESERVE_FIRMWARE = "true keeps the source VM's firmware (BIOS or EFI); false lets the target decide; null or omitted: the restore context's default"
CONNECT_NICS = "true, null or omitted connects the restored VM's network adapters at power-on; false leaves them disconnected"
RESTORE_POINT_ID = "ID of the restore point to restore, as GET /restore/points lists it; restore_point_id and point_id are the same field"
RESTORE_BACKUP = (
    "Backup chain ID as GET /backups lists it (backup::<job>::<vm>::<repository>); without a restore point ID the newest "
    "available point of that chain (of vm, when given) is restored; backup and backup_id are the same field"
)
RESTORE_TARGET_INVENTORY_NAME = "Inventory to restore into, by name (case-insensitive); omitted with target_inventory_id, the restore context's default"
RESTORE_TARGET_INVENTORY_ID = "Inventory to restore into, by ID; omitted with target_inventory, the restore context's default"
RESTORE_TARGET_HOST = "Host to restore onto: its ID or its name (case-insensitive); target_host and target_host_id are the same field"
RESTORE_TARGET_DATASTORE = (
    "Datastore to place the VM on: its ID or its name (case-insensitive); target_datastore and target_datastore_id are "
    "the same field"
)
ROLE = "Role ID or name in any case, with spaces or hyphens for underscores: backup_operator, Backup Operator, backup-operator; a custom role by its ID (custom-…) or name"
SCHEDULE_TYPE = "daily, weekly, monthly, periodic (interval) or cron; type, schedule_type and kind are the same field"


class ScheduleRequest(_LenientModel):
    type: LaxStr | None = Field(default=None, description=SCHEDULE_TYPE)
    schedule_type: LaxStr | None = Field(default=None, description=SCHEDULE_TYPE)
    kind: LaxStr | None = Field(default=None, description=SCHEDULE_TYPE)
    time: LaxStr | None = Field(default=None, description="HH:MM in the schedule's time zone (daily, weekly, monthly); time and start_time are the same field")
    start_time: LaxStr | None = Field(default=None, description="HH:MM in the schedule's time zone (daily, weekly, monthly); time and start_time are the same field")
    days: DocStrArray = Field(default=None, description="Weekly schedules: weekdays as mon..sun or full names; days and weekdays are the same field")
    weekdays: DocStrArray = Field(default=None, description="Weekly schedules: weekdays as mon..sun or full names; days and weekdays are the same field")
    day_of_month: LaxInt = None
    every: LaxStr | None = Field(default=None, description="Periodic schedules: 15m, 1h, 6h, 1d and the like; or interval_minutes")
    interval_minutes: LaxInt = Field(default=None, description="Periodic schedules: the interval in minutes; or every")
    expression: LaxStr | None = None
    timezone: LaxStr | None = Field(default=None, description="IANA time zone such as Europe/Kyiv or UTC; null or omitted, the server's own")
    enabled: LaxBool | None = Field(default=None, description="false keeps the schedule without running it; true, null or omitted runs it")


class GfsRequest(_LenientModel):
    enabled: LaxBool | None = Field(default=None, description="false suspends GFS protection and keeps the counts; true, null or omitted keeps restore points GFS marks")
    weekly: LaxInt = None
    monthly: LaxInt = None
    yearly: LaxInt = None


class TaskParametersRequest(_LenientModel):
    compression_level: LaxInt = None
    on_quiesce_failure: LaxStr | None = None
    encryption_enabled: LaxBool | None = Field(default=None, description="true encrypts new backups with encryption_key, which is then required; false does not. On edit, null clears the stored value (no encryption)")
    encryption_key: LaxStr | None = None
    encryption_key_id: LaxStr | None = None
    tool_timeout_seconds: LaxInt = None
    app_aware: LaxBool | None = Field(default=None, description="Stored with the job for the console; backups do not act on it")
    dedup: LaxBool | None = Field(default=None, description="Stored with the job for the console; backups do not act on it")


class S3Request(_LenientModel):
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = Field(default=None, description=S3_PATH_STYLE)
    verify_tls: LaxBool | None = Field(default=None, description=S3_VERIFY_TLS)


class ImmutabilityRequest(_LenientModel):
    mode: LaxStr | None = None
    retention_days: LaxInt = None
    retention_scope: LaxStr | None = None


class RestoreDatastoreMapping(_LenientModel):
    source_datastore_id: LaxStr | None = Field(default=None, description="Not read; give source_datastore")
    source_datastore: LaxStr | None = Field(default=None, description="Name of the source datastore whose disks move")
    target_datastore_id: LaxStr | None = Field(default=None, description="Datastore they move to: its ID or its name (case-insensitive); used when target_datastore is absent")
    target_datastore: LaxStr | None = Field(default=None, description="Datastore they move to: its ID or its name (case-insensitive)")


class CreateInventoryRequest(_LenientModel):
    name: LaxStr | None = None
    type: LaxStr | None = None
    address: LaxStr | None = None
    port: LaxInt = None
    protocol: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    token: LaxStr | None = None
    insecure: LaxBool | None = Field(default=None, description="true accepts the server's certificate without checking it; false, null or omitted checks it")
    description: LaxStr | None = None


class UpdateInventoryRequest(_LenientModel):
    name: LaxStr | None = None
    address: LaxStr | None = None
    port: LaxInt = None
    protocol: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    token: LaxStr | None = None
    insecure: LaxBool | None = Field(default=None, description=f"{INSECURE}; {OMITTED_KEEPS}")
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
    s3: Annotated[SkipValidation[S3Request] | None, _checked_booleans(S3Request)] = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = Field(default=None, description=S3_PATH_STYLE)
    verify_tls: LaxBool | None = Field(default=None, description=S3_VERIFY_TLS)
    immutability: Annotated[SkipValidation[ImmutabilityRequest] | None, _checked_booleans(ImmutabilityRequest)] = None
    create_bucket: LaxBool | None = Field(default=None, description=CREATE_BUCKET)


class UpdateRepositoryRequest(_LenientModel):
    name: LaxStr | None = None
    path: LaxStr | None = None
    address: LaxStr | None = None
    server: LaxStr | None = None
    share: LaxStr | None = None
    username: LaxStr | None = None
    password: LaxStr | None = None
    description: LaxStr | None = None
    s3: Annotated[SkipValidation[S3Request] | None, _checked_booleans(S3Request)] = None
    bucket: LaxStr | None = None
    prefix: LaxStr | None = None
    region: LaxStr | None = None
    endpoint_url: LaxStr | None = None
    access_key: LaxStr | None = None
    secret_key: LaxStr | None = None
    profile: LaxStr | None = None
    path_style: LaxBool | None = Field(default=None, description=S3_PATH_STYLE)
    verify_tls: LaxBool | None = Field(default=None, description=S3_VERIFY_TLS)
    immutability: Annotated[SkipValidation[ImmutabilityRequest] | None, _checked_booleans(ImmutabilityRequest)] = None
    create_bucket: LaxBool | None = Field(default=None, description=CREATE_BUCKET)


class CreateJobRequest(_LenientModel):
    name: LaxStr | None = None
    vms: list[LaxStr] | LaxStr | None = Field(default=None, description=JOB_VMS)
    inventory: LaxStr | None = Field(default=None, description="The inventory's name, not its ID, matched whole and case-insensitively; omitted with inventory_id, the one inventory that holds every VM in vms")
    inventory_id: LaxStr | None = Field(default=None, description="The inventory's ID, not its name; omitted with inventory, the one inventory that holds every VM in vms")
    repo: LaxStr | None = Field(default=None, description=REPOSITORY_NAME)
    repo_id: LaxStr | None = Field(default=None, description=REPOSITORY_ID)
    schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    enabled: LaxBool | None = Field(default=None, description="Used only when no schedule is given; with a schedule its own enabled decides. null or omitted: true")
    retention_days: RawInt | None = None
    retention_min_fulls: RawInt | None = Field(default=None, description="How many of the newest full backups of each VM, with their incrementals, retention keeps whatever their age; 0 to 100, null or omitted: 0")
    gfs: Annotated[SkipValidation[GfsRequest] | None, _checked_booleans(GfsRequest)] = None
    active_full_days: list[Any] | LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    active_full_days_csv: LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    synthetic_full_days: list[Any] | LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    synthetic_full_days_csv: LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    active_full_schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    synthetic_full_schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    active_full_enabled: LaxBool | None = Field(default=None, description=FULL_SCHEDULE_STATE)
    synthetic_full_enabled: LaxBool | None = Field(default=None, description=FULL_SCHEDULE_STATE)
    description: LaxStr | None = None
    task_parameters: Annotated[SkipValidation[TaskParametersRequest] | None, _checked_booleans(TaskParametersRequest)] = None
    backup_options: Annotated[SkipValidation[TaskParametersRequest] | None, _checked_booleans(TaskParametersRequest)] = Field(default=None, description="Older spelling of task_parameters, read only when task_parameters is absent")


class CreateCopyJobRequest(_LenientModel):
    name: LaxStr | None = None
    source: LaxStr | None = Field(default=None, description="The job to copy, by name (case-insensitive); source and source_job are the same field; or source_id")
    source_job: LaxStr | None = Field(default=None, description="The job to copy, by name (case-insensitive); source and source_job are the same field; or source_id")
    source_id: LaxStr | None = Field(default=None, description="The job to copy, by ID; source_id and source_job_id are the same field; or source")
    source_job_id: LaxStr | None = Field(default=None, description="The job to copy, by ID; source_id and source_job_id are the same field; or source")
    repo: LaxStr | None = Field(default=None, description="Repository for the copy, by name (case-insensitive); repo and repository are the same field; omitted with the ID, the source job's")
    repository: LaxStr | None = Field(default=None, description="Repository for the copy, by name (case-insensitive); repo and repository are the same field; omitted with the ID, the source job's")
    repo_id: LaxStr | None = Field(default=None, description="Repository for the copy, by ID; repo_id and repository_id are the same field; omitted with the name, the source job's")
    repository_id: LaxStr | None = Field(default=None, description="Repository for the copy, by ID; repo_id and repository_id are the same field; omitted with the name, the source job's")
    schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    enabled: LaxBool | None = Field(default=None, description="true or false switches the copy's schedule on or off; null or omitted keeps the copied schedule's state")
    retention_days: RawInt | None = Field(default=None, description="null or omitted: the source job's")
    retention_min_fulls: RawInt | None = Field(default=None, description="0 to 100; null or omitted: the source job's")
    description: LaxStr | None = None


class UpdateJobRequest(_LenientModel):
    name: LaxStr | None = None
    schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    enabled: LaxBool | None = Field(default=None, description=f"true or false switches the whole job and its base schedule on or off; {OMITTED_KEEPS}")
    retention_days: RawInt | None = None
    retention_min_fulls: RawInt | None = Field(default=None, description=f"0 to 100; 0 switches it off; {OMITTED_KEEPS}")
    gfs_weekly: RawInt | None = None
    gfs_monthly: RawInt | None = None
    gfs_yearly: RawInt | None = None
    gfs_enabled: LaxBool | None = Field(default=None, description=f"false suspends GFS protection and keeps the counts, true resumes it; {OMITTED_KEEPS}")
    active_full_days: list[Any] | LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    synthetic_full_days: list[Any] | LaxStr | None = Field(default=None, description=LEGACY_FULL_DAYS)
    active_full_schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    synthetic_full_schedule: Annotated[SkipValidation[ScheduleRequest] | LaxStr | None, _checked_booleans(ScheduleRequest)] = None
    active_full_enabled: LaxBool | None = Field(default=None, description=FULL_SCHEDULE_STATE_EDIT)
    synthetic_full_enabled: LaxBool | None = Field(default=None, description=FULL_SCHEDULE_STATE_EDIT)
    description: LaxStr | None = None
    task_parameters: Annotated[SkipValidation[TaskParametersRequest] | None, _checked_booleans(TaskParametersRequest)] = Field(default=None, description="Merged into the stored ones: a key set to null goes back to its default, keys left out are kept")
    repo: LaxStr | None = Field(default=None, description=REPOSITORY_NAME)
    repo_id: LaxStr | None = Field(default=None, description=REPOSITORY_ID)
    inventory: LaxStr | None = Field(default=None, description=INVENTORY_NAME)
    inventory_id: LaxStr | None = Field(default=None, description=INVENTORY_ID)
    vms: list[LaxStr] | LaxStr | None = Field(default=None, description=f"{JOB_VMS}; replaces the job's VM list")
    source_job: LaxStr | None = Field(default=None, description="Backup copy jobs only: the source job by name (case-insensitive); or source_job_id")
    source_job_id: LaxStr | None = Field(default=None, description="Backup copy jobs only: the source job by ID; or source_job")


class StartJobRequest(_LenientModel):
    force_full: LaxBool | None = Field(default=None, description="true takes an active full backup now; false, null or omitted lets the job's schedules decide. Not together with force_synthetic")
    force_synthetic: LaxBool | None = Field(default=None, description="true builds a synthetic full from the stored chain (it needs a full and at least one incremental); false, null or omitted lets the schedules decide")
    target_repository_id: LaxStr | None = Field(default=None, description="With force_synthetic only: ID, not name, of the repository to write the synthetic full to; omitted, the job's own")


class RunRetentionRequest(_LenientModel):
    dry_run: LaxBool | None = Field(default=None, description="true only reports what retention would remove; false, null or omitted runs it and deletes the expired restore points")


class StopSessionRequest(_LenientModel):
    force: LaxBool | None = Field(default=None, description="Only worded differently in the session log; a stop works the same with true, false, null or omitted")


class ImportBackupRequest(_LenientModel):
    path: LaxStr | None = None
    repo: LaxStr | None = Field(default=None, description=REPOSITORY_NAME)
    repo_id: LaxStr | None = Field(default=None, description=REPOSITORY_ID)


class CreateUserRequest(_LenientModel):
    name: LaxStr | None = None
    password: LaxStr | None = None
    fullName: LaxStr | None = None
    role: LaxStr | None = Field(default=None, description=ROLE)


class CreateRoleRequest(_LenientModel):
    name: LaxStr | None = Field(default=None, description="Unique among all roles, in any case; a clone without one is named \"<source> (copy)\"")
    description: LaxStr | None = None
    allowed_functions: SkipValidation[list[str]] | None = Field(default=None, description="The function IDs the role allows (GET /functions); every other one is disabled. Required unless clone_from is given")
    clone_from: LaxStr | None = Field(default=None, description="A built-in or custom role, by ID or name, whose functions and description the new role starts from")


class UpdateRoleRequest(_LenientModel):
    name: LaxStr | None = Field(default=None, description=f"A new unique name; {OMITTED_KEEPS}")
    description: LaxStr | None = Field(default=None, description=f"An empty text clears it; {OMITTED_KEEPS}")
    allowed_functions: SkipValidation[list[str]] | None = Field(default=None, description=f"The whole new list of allowed function IDs; {OMITTED_KEEPS}")


class UpdateUserRequest(_LenientModel):
    name: LaxStr | None = Field(default=None, description=f"The login name; the password stays as it was unless a new one is given; {OMITTED_KEEPS}")
    password: LaxStr | None = Field(default=None, description=f"A new password, which signs the user out everywhere; not for the caller's own, which needs the current one (POST /auth/change-password); {OMITTED_KEEPS}")
    fullName: LaxStr | None = None
    role: LaxStr | None = Field(default=None, description=f"{ROLE}; {OMITTED_KEEPS}")
    enabled: SkipValidation[bool] | None = Field(default=None, description=f"false stops the user signing in, true lets them again; {OMITTED_KEEPS}")




class UserResult(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    fullName: DocStr = None
    role: DocStr = None
    enabled: DocBool = None
    created_at: DocStr = None
    passwordPolicyExempt: DocBool = None





class StartRestoreTarget(_LenientModel):
    inventory_id: LaxStr | None = Field(default=None, description=RESTORE_TARGET_INVENTORY_ID)
    inventory: LaxStr | None = Field(default=None, description=RESTORE_TARGET_INVENTORY_NAME)
    host_id: LaxStr | None = Field(default=None, description="Host to restore onto: its ID or its name (case-insensitive)")
    datastore_id: LaxStr | None = Field(default=None, description="Datastore to place the VM on: its ID or its name (case-insensitive)")
    vm_name: LaxStr | None = Field(default=None, description="Name of the restored VM; null or omitted, the restore context's default")
    folder_id: LaxStr | None = Field(default=None, description="Not supported yet; a value is refused")
    resource_pool_id: LaxStr | None = Field(default=None, description="Not supported yet; a value is refused")


class StartRestoreOptions(_LenientModel):
    power_on_after_restore: LaxBool | None = Field(default=None, description=POWER_ON)
    preserve_mac_addresses: LaxBool | None = Field(default=None, description=PRESERVE_MAC)
    preserve_firmware: LaxBool | None = Field(default=None, description=PRESERVE_FIRMWARE)
    connect_nics: LaxBool | None = Field(default=None, description=CONNECT_NICS)
    disk_provisioning: LaxStr | None = None


class StartRestoreNetworkMapping(_LenientModel):
    source_nic_id: LaxStr | None = Field(default=None, description="A source network adapter's ID as GET /restore/context lists it under source_vm.nics")
    target_network_id: LaxStr | None = Field(default=None, description="Network to connect it to, as GET /restore/context lists it under networks")


class VerifyBackupRequest(_LenientModel):
    encryption_key: LaxStr | None = None


class StartRestoreRequest(_LenientModel):
    restore_point_id: LaxStr | None = Field(default=None, description=RESTORE_POINT_ID)
    point_id: LaxStr | None = Field(default=None, description=RESTORE_POINT_ID)
    backup_id: LaxStr | None = Field(default=None, description=RESTORE_BACKUP)
    backup: LaxStr | None = Field(default=None, description=RESTORE_BACKUP)
    inventory_id: LaxStr | None = Field(default=None, description="ID of the inventory the restore point was backed up from; refused with 409 when it does not match")
    repository_id: LaxStr | None = Field(default=None, description="Repository holding the restore point, by ID (repository_id and repo_id are the same field); refused when it does not hold it")
    repository: LaxStr | None = Field(default=None, description="Repository holding the restore point, by name (case-insensitive); repository and repo are the same field")
    repo_id: LaxStr | None = Field(default=None, description="Repository holding the restore point, by ID (repository_id and repo_id are the same field); refused when it does not hold it")
    repo: LaxStr | None = Field(default=None, description="Repository holding the restore point, by name (case-insensitive); repository and repo are the same field")
    restore_mode: LaxStr | None = Field(default=None, description="new_vm (also new) or in_place (also overwrite, needs force); null or omitted: new_vm, or in_place when only force is given")
    target: Annotated[SkipValidation[StartRestoreTarget] | None, _checked_booleans(StartRestoreTarget)] = None
    options: Annotated[SkipValidation[StartRestoreOptions] | None, _checked_booleans(StartRestoreOptions)] = None
    network_mappings: Annotated[SkipValidation[list[StartRestoreNetworkMapping]] | None, _checked_booleans(StartRestoreNetworkMapping)] = None
    network_map: DocStrArray = None
    datastore_map: DocStrArray = None
    disk_datastore_map: DocStrArray = None
    nic_network_map: DocStrArray = None
    datastore_mappings: Annotated[SkipValidation[list[RestoreDatastoreMapping]] | None, _checked_booleans(RestoreDatastoreMapping)] = None
    target_inventory: LaxStr | None = Field(default=None, description=RESTORE_TARGET_INVENTORY_NAME)
    target_inventory_id: LaxStr | None = Field(default=None, description=RESTORE_TARGET_INVENTORY_ID)
    target_host: LaxStr | None = Field(default=None, description=RESTORE_TARGET_HOST)
    target_host_id: LaxStr | None = Field(default=None, description=RESTORE_TARGET_HOST)
    target_datastore: LaxStr | None = Field(default=None, description=RESTORE_TARGET_DATASTORE)
    target_datastore_id: LaxStr | None = Field(default=None, description=RESTORE_TARGET_DATASTORE)
    target_vm_name: LaxStr | None = Field(default=None, description="Name of the restored VM; target_vm_name and name are the same field; null or omitted, the restore context's default")
    name: LaxStr | None = Field(default=None, description="Name of the restored VM; target_vm_name and name are the same field; null or omitted, the restore context's default")
    power_on: LaxBool | None = Field(default=None, description=f"{POWER_ON}; power_on and power_on_after_restore are the same field")
    power_on_after_restore: LaxBool | None = Field(default=None, description=f"{POWER_ON}; power_on and power_on_after_restore are the same field")
    preserve_mac_addresses: LaxBool | None = Field(default=None, description=PRESERVE_MAC)
    preserve_firmware: LaxBool | None = Field(default=None, description=PRESERVE_FIRMWARE)
    connect_nics: LaxBool | None = Field(default=None, description=CONNECT_NICS)
    disk_provisioning: LaxStr | None = Field(default=None, description="thin, thick or eager-zeroed; disk_provisioning and disk_type are the same field; null or omitted keeps the source's")
    disk_type: LaxStr | None = Field(default=None, description="thin, thick or eager-zeroed; disk_provisioning and disk_type are the same field; null or omitted keeps the source's")
    encryption_key: LaxStr | None = None
    force: LaxBool | None = Field(default=None, description="true authorizes an in-place restore that overwrites the source VM; refused for a restore to a new VM")


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
    "chain_incomplete",
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
                    "issuer": {
                        "type": "string",
                        "description": 'Only on "chain_incomplete": the issuer of the server certificate, which is not installed.',
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
    kept_min_fulls: DocStrArray = None
    skipped_reason: DocStrOrNull = None
    clock_blocked: DocStrOrNull = None


class RetentionSummaryDTO(_LenientModel):
    chains: DocInt = None
    points: DocInt = None
    removed: DocInt = None
    held: DocInt = None
    kept_min_fulls: DocInt = None
    failed: DocInt = None
    skipped: DocInt = None


class RetentionRunResult(_LenientModel):
    job_id: DocStr = None
    job_name: DocStr = None
    dry_run: DocBool = None
    clock_blocked: DocStrOrNull = None
    retention_days: DocInt = None
    retention_min_fulls: DocInt = None
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


SESSION_EXPIRES_AT = (
    "When the session ends if it stays idle; for a web session every request the user causes moves it to now plus "
    "its inactivity timeout, a background refresh (X-ABR-Refresh) does not"
)
SESSION_ABSOLUTE_EXPIRES_AT = (
    "When a web session ends whatever the activity (ABR_WEB_SESSION_LIFETIME_SECONDS after sign-in); null when "
    "there is no such limit, and always for CLI sessions"
)


class LoginResult(_LenientModel):
    token: DocStr = None
    expires_at: DocStr = Field(default=None, description=SESSION_EXPIRES_AT)
    absolute_expires_at: DocStrOrNull = Field(default=None, description=SESSION_ABSOLUTE_EXPIRES_AT)
    user: PublicUserDTO | DocObject = None
    permissions: DocStrArray = None


class RoleSummaryDTO(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    description: DocStr = None
    built_in: DocBool = None


class WhoAmIResult(_LenientModel):
    user: PublicUserDTO | DocObject = None
    role: RoleSummaryDTO | None = None
    permissions: DocStrArray = None


class RolePermissionDTO(_LenientModel):
    function_id: DocStr = None
    allowed: DocBool = None


class RoleSourceDTO(_LenientModel):
    id: DocStr = None
    name: DocStr = None


class RoleDTO(RoleSummaryDTO):
    permissions: list[RolePermissionDTO] | DocArray = None
    allowed_functions: DocStrArray = None
    user_count: DocInt = Field(default=None, description="How many users hold the role")
    created_at: DocStr = None
    updated_at: DocStr = None
    cloned_from: RoleSourceDTO | None = Field(default=None, description="The role a custom role was cloned from")


class RoleList(_LenientModel):
    items: list[RoleDTO]


class FunctionDTO(_LenientModel):
    id: DocStr = None
    name: DocStr = None
    category: DocStr = None
    starts_session: DocBool = None
    always_allowed: DocBool = Field(default=None, description="Every role allows it, a custom one too")


class FunctionList(_LenientModel):
    items: list[FunctionDTO]


class AuditTargetDTO(_LenientModel):
    type: DocStr = None
    id: DocStr = None
    name: DocStr = None


class AuditRecordDTO(_LenientModel):
    id: DocStr = None
    timestamp: DocStr = Field(default=None, description="UTC")
    user: DocStr = None
    user_id: DocStr = None
    role: DocStr = Field(default=None, description="The role the user held at the time")
    function_id: DocStr = None
    target: AuditTargetDTO | None = None
    result: DocStr = Field(default=None, description="allowed, denied or failed")
    session_id: DocStr = Field(default=None, description="The session the command started, if any")
    auth_session_id: DocStr = Field(default=None, description="A reference to the sign-in session")
    interface: DocStr = Field(default=None, description="web, cli or api")
    command: DocStr = Field(default=None, description="The CLI command, when it came from the CLI")
    client: DocStr = None
    status: DocInt = None
    error: DocObject = None
    parameters: DocObject = Field(default=None, description="The request, with credentials masked")
    details: DocObject = None


class AuditList(_LenientModel):
    items: list[AuditRecordDTO]
    total: DocInt = None
    limit: DocInt = None
    offset: DocInt = None


class LogoutResult(_LenientModel):
    status: DocStr = None


class ExtendSessionResult(_LenientModel):
    expires_at: DocStr = Field(default=None, description=SESSION_EXPIRES_AT)
    absolute_expires_at: DocStrOrNull = Field(default=None, description=SESSION_ABSOLUTE_EXPIRES_AT)
