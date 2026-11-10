# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import GENERAL_ERROR, INVALID_STATE, VALIDATION_FAILED
from ..parsing.common_arguments import add_name_or_id_selector
from ..parsing.secret_arguments import ENCRYPTION_KEY_ENV_VAR, add_secret_argument
from ..parsing.validators import (
    parse_compression_level,
    parse_day_of_month,
    parse_duration,
    parse_optional_weekday_csv,
    parse_strict_hhmm,
    parse_weekday_csv,
)
from ..progress_watch import session_watch_succeeded, watch_session
from .base import CommandDependencies
from .inventory_commands import INVENTORY_SCAN_TIMEOUT_SECONDS

RETENTION_TIMEOUT_SECONDS = 21600
REPLICA_DELETE_TIMEOUT_SECONDS = 1800


def _session_failure_exit_code(session: Any) -> int:
    if not isinstance(session, dict):
        return INVALID_STATE
    if str(session.get("failure_kind") or "") == "validation":
        return VALIDATION_FAILED
    return INVALID_STATE


def _clock_anomalies(job: object) -> list[dict[str, Any]]:
    anomalies = job.get("clock_anomalies") if isinstance(job, dict) else None
    return [anomaly for anomaly in anomalies if isinstance(anomaly, dict)] if isinstance(anomalies, list) else []


def _vm_reassignment_messages(job: object) -> list[str]:
    entries = job.get("vm_reassignments") if isinstance(job, dict) else None
    if not isinstance(entries, list):
        return []
    return [str(entry.get("message")) for entry in entries if isinstance(entry, dict) and entry.get("message")]


def _last_run_text(run: object) -> str:
    if not isinstance(run, dict) or not run.get("result"):
        return ""
    result = str(run["result"])
    total = int(run.get("vm_count") or 0)
    if result == "running":
        return f"Running ({total - int(run.get('running') or 0)}/{total} VMs done)"
    if result == "success":
        return "Success" if total == 1 else f"Success ({total} VMs)"
    if result == "warning":
        return f"Warning ({int(run.get('warning') or 0)} of {total} VMs with warnings)"
    return f"{result.capitalize()} ({int(run.get(result) or 0)} of {total} VMs {result})"


def _names(items: Any) -> str:
    return ", ".join(str(item.get("name") or item.get("id")) for item in items or [] if isinstance(item, dict)) or "-"


def _backup_copy_rows(view: dict[str, Any]) -> dict[str, Any]:
    retry = view.get("retry") if isinstance(view.get("retry"), dict) else {}
    run_after = view.get("run_after_job") if isinstance(view.get("run_after_job"), dict) else None
    return {
        "copy_mode": view.get("copy_mode"),
        "source_jobs": _names(view.get("source_jobs")),
        "source_repositories": _names(view.get("source_repositories")),
        "run_after_job": (run_after or {}).get("name") or "-",
        "copy_window": view.get("copy_window_summary") or "any time",
        "retry": f"{retry.get('count')} time(s), {retry.get('interval_minutes')} minute(s) apart",
    }


REPLICATION_HIDDEN_KEYS = frozenset(
    {
        "storage_layout",
        "retention_days",
        "retention_min_fulls",
        "gfs",
        "active_full_days",
        "synthetic_full_days",
        "active_full_schedule",
        "synthetic_full_schedule",
        "active_full_next_run_at",
        "synthetic_full_next_run_at",
        "compression_level",
        "on_quiesce_failure",
        "encryption",
        "guest_consistency",
        "backup_copy",
        "source_job",
    }
)


def _replication_rows(view: dict[str, Any]) -> dict[str, Any]:
    replica = view.get("replica") if isinstance(view.get("replica"), dict) else {}
    source_job = view.get("source_job") if isinstance(view.get("source_job"), dict) else {}
    source_vm = view.get("source_vm") if isinstance(view.get("source_vm"), dict) else {}
    target = view.get("target_inventory") if isinstance(view.get("target_inventory"), dict) else {}
    rows = {
        "source_job": source_job.get("name") or source_job.get("id") or "-",
        "source_vm": source_vm.get("name") or source_vm.get("id") or "-",
        "target_inventory": target.get("name") or target.get("id") or "-",
        "target_host": view.get("target_host") or "-",
        "target_datastore": view.get("target_datastore") or "-",
        "network_map": ", ".join(view.get("network_map") or []) or "-",
        "replica_name": view.get("replica_name") or "-",
        "run_after_source": "yes" if view.get("run_after_source") else "no",
        "replica_vm": replica.get("vm_name") or ("not created yet" if not replica.get("vm_id") else replica.get("vm_id")),
        "replica_point": replica.get("point_id") or "-",
        "replica_point_created_at": replica.get("point_created_at") or "-",
        "last_success_at": replica.get("last_success_at") or "-",
    }
    if view.get("start_point_id") and not replica.get("vm_id"):
        rows["start_point"] = view["start_point_id"]
    if replica.get("pending_name"):
        rows["pending_rename"] = replica["pending_name"]
    if view.get("run_pending"):
        request = view.get("pending_trigger") if isinstance(view.get("pending_trigger"), dict) else {}
        rows["run_pending"] = f"yes ({request['trigger']} since {request.get('since')})" if request.get("trigger") else "yes"
    return rows


def _last_run_issues(run: dict[str, Any]) -> str:
    issues = []
    for vm in run.get("vms") or []:
        if not isinstance(vm, dict) or vm.get("result") not in {"failed", "stopped", "warning"}:
            continue
        sessions = ", ".join(str(session_id) for session_id in vm.get("session_ids") or [])
        detail = vm.get("message") or ("completed with warnings" if vm.get("result") == "warning" else vm.get("result"))
        issues.append(f"{vm.get('vm_name')}: {detail} (session {sessions})")
    return "; ".join(issues)


class JobCommands:
    def register(self, registry) -> None:
        job_group = registry.add_parser("job", help="Job management", description="Job commands", formatter_class=CompactHelpFormatter)
        commands = job_group.add_subparsers(dest="job_command", required=True)

        create = commands.add_parser(
            "create",
            help="Create backup job.",
            description=(
                "Create backup job.\n\n"
                "VM names come from discovered inventory objects. Discovery runs "
                "automatically when needed, so no manual inventory rescan is required "
                "after inventory add.\n\n"
                "Examples:\n"
                '  abr job create --name "Job-SQL-Server" --inventory "vCenter-Prod" --vm "sql-01" --repository "Local-Repo-1"\n'
                '  abr job create --name "Weekly-Full" --inventory "vCenter-Prod" --vms "sql-01,sql-02" '
                '--repository "S3-Repo" --schedule weekly --days mon,thu --time 02:00\n'
                '  abr job create --name "Encrypted-Job" --inventory "vCenter-Prod" --vm "sql-01" '
                '--repository "S3-Repo" --set-encryption\n'
                '  abr job create --name "Nightly-With-Fulls" --inventory "vCenter-Prod" --vm "sql-01" '
                '--repository "Local-Repo-1" --schedule daily --time 22:00 --retention-days 30 '
                '--active-full-schedule monthly --active-full-day-of-month 1 --active-full-time 02:00 '
                '--synthetic-full-schedule weekly --synthetic-full-days sun --synthetic-full-time 03:00'
            ),
            formatter_class=CompactHelpFormatter,
        )
        self._add_common_job_create_arguments(create, for_copy=False)
        create.set_defaults(handler=self.create)

        create_copy = commands.add_parser(
            "create-copy",
            help="Clone an existing job.",
            description=(
                "Clone an existing job.\n\n"
                "Examples:\n"
                '  abr job create-copy --name "Job-SQL-Server-Copy" --source "Job-SQL-Server"\n'
                '  abr job create-copy --name "Job-SQL-Server-Copy" --source-id <job-id> --repository "S3-Repo"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        create_copy.add_argument("--name", required=True, help="New job name")
        source = create_copy.add_mutually_exclusive_group(required=True)
        source.add_argument("--source", "--source-job", dest="source_job", help="Source job name")
        source.add_argument("--source-id", "--source-job-id", dest="source_job_id", help="Source job ID")
        self._add_common_job_create_arguments(
            create_copy,
            for_copy=True,
            include_name=False,
            retention_default=None,
            gfs_default=None,
            include_policy_flags=False,
            include_compression_encryption_flags=False,
        )
        create_copy.set_defaults(handler=self.create_copy)

        create_backup_copy = commands.add_parser(
            "create-backup-copy",
            help="Create a backup copy job: copies restore points into another repository.",
            description=(
                "Create a backup copy job.\n\n"
                "It copies the restore points of its sources (backup jobs or repositories) into another repository, "
                "where it keeps a chain of its own with its own retention. It reads stored backups only, never the "
                "VMs. periodic copies on its schedule (or after another job runs) and merges what is new into one "
                "restore point; immediate copies every new restore point as soon as a backup of a source ends.\n\n"
                "Examples:\n"
                '  abr job create-backup-copy --name "SQL-offsite" --source-job "SQL-nightly" --repo "S3-offsite" '
                "--schedule daily --time 03:00 --retention-days 90 --active-full-schedule monthly "
                "--active-full-day-of-month 1 --active-full-time 04:00\n"
                '  abr job create-backup-copy --name "All-to-cloud" --mode immediate --source-repo "Local-Repo-1" '
                '--repo "S3-offsite" --copy-window "mon,tue,wed,thu,fri 22:00-06:00" --copy-window "sat,sun 00:00-24:00" '
                "--active-full-schedule weekly --active-full-days sun --active-full-time 04:00\n"
                '  abr job create-backup-copy --name "SQL-after-nightly" --source-job "SQL-nightly" --vms "sql-01" '
                '--repo "Archive-SMB" --run-after-job "SQL-nightly" --synthetic-full-schedule weekly '
                "--synthetic-full-days sat --synthetic-full-time 05:00"
            ),
            formatter_class=CompactHelpFormatter,
        )
        self._add_common_job_create_arguments(create_backup_copy, for_copy=True, include_capture_flags=False)
        self._add_backup_copy_arguments(create_backup_copy, creating=True)
        create_backup_copy.set_defaults(handler=self.create_backup_copy)

        create_replication = commands.add_parser(
            "create-replication",
            help="Create a replication job: keeps a powered-off replica VM up to date from a backup job's restore points.",
            description=(
                "Create a replication job.\n\n"
                "It creates a replica VM on the target environment from a restore point of the source backup job, "
                "and then updates that replica from each newer restore point. It reads stored backups only, never "
                "the production VM, and never powers the replica on. The source backup job must back up exactly one "
                "VM, and the target must be the same platform (VMware to VMware, Hyper-V to Hyper-V). After the "
                "replica is created, the source, target environment, host, datastore, network mapping and start "
                "restore point cannot change; every later run, scheduled or manual, brings the replica to the newest "
                "restore point of the source. When the source gets a new active full, the run rebuilds the replica's disks next to the "
                "current ones, so the datastore needs as much free space again as the replica's disks take.\n\n"
                "Examples:\n"
                '  abr job create-replication --name "SQL-DR" --source-job "SQL-nightly" --target-inventory "vCenter-DR" '
                '--target-host "esx-dr-01" --datastore "ds-dr" --network-map "VM Network=DR Network" '
                '--replica-name "sql-01-replica" --run-after-source\n'
                '  abr job create-replication --name "App-DR" --source-job "App-HV" --target-inventory "HV-DR" '
                '--target-host "HV-DR-01" --datastore "D:\\Replicas" --replica-name "app-01-replica" '
                "--schedule daily --time 03:00"
            ),
            formatter_class=CompactHelpFormatter,
        )
        create_replication.add_argument("--name", required=True, help="Job name")
        create_replication.add_argument("--source-job", dest="source_job", help="The backup job whose restore points update the replica, by name")
        create_replication.add_argument("--source-job-id", dest="source_job_id", help="The same backup job by ID")
        self._add_replication_arguments(create_replication, creating=True)
        self._add_schedule_arguments(create_replication)
        create_replication.set_defaults(handler=self.create_replication)

        list_parser = commands.add_parser(
            "list",
            help="List jobs.",
            description="List jobs.\n\nExamples:\n  abr job list",
            formatter_class=CompactHelpFormatter,
        )
        list_parser.set_defaults(handler=self.list)

        info = commands.add_parser(
            "info",
            help="Show job details.",
            description=(
                "Show job details.\n\n"
                "Examples:\n"
                '  abr job info --name "Job-SQL-Server"\n'
                "  abr job info --id <job-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(info, name_help="Job name", id_help="Job ID")
        info.set_defaults(handler=self.info)

        edit = commands.add_parser(
            "edit",
            help="Edit job settings.",
            description=(
                "Edit job settings.\n\n"
                "Examples:\n"
                '  abr job edit --name "Job-SQL-Server" --schedule daily --time 03:00\n'
                '  abr job edit --id <job-id> --new-name "Job-SQL-Server-Renamed" --retention-days 30'
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(edit, name_help="Job name", id_help="Job ID")
        edit.add_argument("--new-name", help="Updated job name")
        self._add_common_job_create_arguments(edit, for_copy=False, include_name=False, retention_default=None, gfs_default=None)
        self._add_backup_copy_arguments(edit, creating=False)
        self._add_replication_arguments(edit, creating=False)
        edit.set_defaults(handler=self.edit)

        start = commands.add_parser(
            "start",
            help="Start job now. --full forces a full backup, --synthetic merges the stored chain.",
            description=(
                "Start a job manually.\n\n"
                "--full reads the whole VM again. --synthetic builds the same full backup by merging\n"
                "the chain already in the repository, so the VM is never touched.\n\n"
                "Examples:\n"
                '  abr job start --name "Job-SQL-Server"\n'
                '  abr job start --name "Job-SQL-Server" --full\n'
                '  abr job start --name "Job-SQL-Server" --synthetic --wait\n'
                '  abr job start --name "Job-SQL-Server" --synthetic --target-repo "Archive-S3"\n'
                "  abr job start --id <job-id> --full"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(start, name_help="Job name", id_help="Job ID")
        start_kind = start.add_mutually_exclusive_group()
        start_kind.add_argument(
            "--full",
            "--force-full",
            dest="full",
            action="store_true",
            help="Force a full backup instead of an incremental backup",
        )
        start_kind.add_argument(
            "--synthetic",
            dest="synthetic",
            action="store_true",
            help=(
                "Build a full backup by merging the stored chain in the repository instead of reading "
                "the VM again. Needs a full backup followed by at least one incremental"
            ),
        )
        start.add_argument(
            "--target-repo",
            dest="target_repo",
            help="Write the synthetic full into this repository instead of the job's own (--synthetic only)",
        )
        start.add_argument(
            "--wait",
            action="store_true",
            help=(
                "Follow the started session's real progress on stderr until it finishes, then "
                "show its final state. Exits non-zero if the session does not complete "
                "successfully (failed, stopped, or canceled)."
            ),
        )
        start.set_defaults(handler=self.start)

        retention = commands.add_parser(
            "retention",
            help="Apply the job's retention policy now, or preview what it removes.",
            description=(
                "Apply the job's retention policy to every backup chain the job wrote, now.\n\n"
                "Retention also runs after each backup; this covers all chains at once. A whole\n"
                "full-plus-incrementals segment goes only when all of it has expired and no point in\n"
                "it is kept by GFS, so what stays always restores. Points still under S3 Object Lock\n"
                "are kept and reported as held. Chains with a backup running are skipped.\n\n"
                "Examples:\n"
                '  abr job retention --name "Job-SQL-Server" --dry-run\n'
                '  abr job retention --name "Job-SQL-Server"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(retention, name_help="Job name", id_help="Job ID")
        retention.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what retention would remove without removing anything",
        )
        retention.set_defaults(handler=self.retention)

        delete = commands.add_parser(
            "delete",
            help="Delete a job.",
            description=(
                "Delete a job.\n\n"
                "Examples:\n"
                '  abr job delete --name "Job-SQL-Server"\n'
                "  abr job delete --id <job-id> --yes\n"
                '  abr job delete --name "Replica-SQL" --delete-replica\n\n'
                "Deleting a replication job keeps its replica VM on the target unless --delete-replica is given."
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(delete, name_help="Job name", id_help="Job ID")
        delete.add_argument("--yes", "--force", dest="force", action="store_true", help="Delete without confirmation")
        delete.add_argument(
            "--delete-replica",
            action="store_true",
            help="Replication jobs only: delete the replica VM and its disks on the target too (refused while it is powered on)",
        )
        delete.set_defaults(handler=self.delete)

    def _add_common_job_create_arguments(
        self,
        parser: argparse.ArgumentParser,
        *,
        for_copy: bool,
        include_name: bool = True,
        retention_default: int | None = 14,
        gfs_default: int | None = 0,
        include_policy_flags: bool = True,
        include_compression_encryption_flags: bool = True,
        include_capture_flags: bool = True,
    ) -> None:
        if include_name:
            parser.add_argument("--name", required=True, help="Job name")
        if not for_copy:
            parser.add_argument("--inventory", help="Inventory name")
            parser.add_argument("--inventory-id", help="Inventory ID")
            parser.add_argument("--vm", action="append", default=[], help="VM name, ID, or path. Repeat as needed.")
            parser.add_argument("--vms", help="Protected VM names, IDs, or paths, comma-separated")
            parser.add_argument("--objects", help=argparse.SUPPRESS)
        parser.add_argument("--repository", "--repo", dest="repo", help="Repository name")
        parser.add_argument("--repository-id", "--repo-id", dest="repo_id", help="Repository ID")
        parser.add_argument(
            "--schedule",
            help=(
                "Schedule type: daily, weekly, monthly, or periodic. "
                "Legacy schedule text and cron expressions are also accepted for compatibility."
            ),
        )
        parser.add_argument("--time", type=parse_strict_hhmm, help="Schedule time in HH:MM")
        parser.add_argument("--days", type=parse_weekday_csv, help="Weekly schedule days as mon,tue,...")
        parser.add_argument("--day-of-month", dest="day_of_month", type=parse_day_of_month, help="Monthly schedule day from 1 to 31")
        parser.add_argument("--every", type=parse_duration, help="Periodic interval like 15m, 1h, 6h, or 1d")
        schedule_state = parser.add_mutually_exclusive_group()
        schedule_state.add_argument("--enabled", action="store_true", help="Enable the schedule")
        schedule_state.add_argument("--disabled", action="store_true", help="Disable the schedule")
        parser.add_argument("--description", help="Job description")
        parser.add_argument("--retention-days", type=int, default=retention_default, help="Operational retention in days")
        parser.add_argument(
            "--retention-min-fulls",
            dest="retention_min_fulls",
            type=int,
            help=(
                "How many of the newest full backups of each VM, with the incrementals on them, retention always "
                "keeps whatever their age (0-100; default 2 for a new job, the source job's for a copied one; "
                "0 keeps none beyond --retention-days)"
            ),
        )
        if include_compression_encryption_flags:
            parser.add_argument(
                "--compression-level",
                type=parse_compression_level,
                help="Backup compression level: 0 disables compression, 1-9 select Zstd levels (higher compresses smaller but slower). Default: 3.",
            )
        if include_compression_encryption_flags and include_capture_flags:
            parser.add_argument(
                "--on-quiesce-failure",
                dest="on_quiesce_failure",
                choices=("fail", "crash-consistent"),
                help=(
                    "What a backup of a VMware VM does when VMware Tools cannot quiesce the guest, for example because "
                    "a pre-freeze script or a VSS writer fails: fail (the default) ends that VM's session as failed; "
                    "crash-consistent takes the snapshot again without quiescing and completes with a warning."
                ),
            )
        if include_compression_encryption_flags:
            encryption_state = parser.add_mutually_exclusive_group()
            encryption_state.add_argument(
                "--set-encryption",
                action="store_true",
                help=(
                    "Enable AES-256 encryption of backup data blocks. The key is read from "
                    f"{ENCRYPTION_KEY_ENV_VAR}, or prompted for (twice) when it is not set. On job edit of an "
                    "encrypted job a different key becomes the job's new key for the backups that follow (key "
                    "rotation); earlier backups keep the key they were written with, and the current key again "
                    "changes nothing."
                ),
            )
            encryption_state.add_argument(
                "--reset-encryption",
                action="store_true",
                help="Disable encryption for this job.",
            )
            add_secret_argument(
                parser,
                "--encryption-key",
                env_var=ENCRYPTION_KEY_ENV_VAR,
                help_text="Encryption key/passphrase, used together with --set-encryption",
            )
        if include_compression_encryption_flags and include_capture_flags:
            parser.add_argument(
                "--guest-consistency",
                choices=("application", "crash"),
                help=(
                    "Hyper-V only: recovery checkpoint consistency. application quiesces a running guest "
                    "through VSS and falls back to crash-consistent when it cannot (default); crash never "
                    "involves the guest"
                ),
            )
        if include_policy_flags:
            parser.add_argument("--gfs-weekly", type=int, default=gfs_default, help="Weekly GFS count")
            parser.add_argument("--gfs-monthly", type=int, default=gfs_default, help="Monthly GFS count")
            parser.add_argument("--gfs-yearly", type=int, default=gfs_default, help="Yearly GFS count")
            gfs_state = parser.add_mutually_exclusive_group()
            gfs_state.add_argument(
                "--gfs-enable",
                dest="gfs_enable",
                action="store_true",
                help="Enable the GFS long-term retention policy without changing its configured counts",
            )
            gfs_state.add_argument(
                "--gfs-disable",
                dest="gfs_disable",
                action="store_true",
                help="Suspend the GFS long-term retention policy, keeping its configured counts for a later --gfs-enable",
            )
            parser.add_argument(
                "--synthetic-full-days",
                type=parse_optional_weekday_csv,
                help="Synthetic full weekdays, comma-separated Mon-Sun, for a weekly schedule only; none turns a weekly one off",
            )
            parser.add_argument(
                "--active-full-days",
                type=parse_optional_weekday_csv,
                help="Active full weekdays, comma-separated Mon-Sun, for a weekly schedule only; none turns a weekly one off",
            )
            for prefix, label in (("active-full", "Active full"), ("synthetic-full", "Synthetic full")):
                dest = prefix.replace("-", "_")
                parser.add_argument(
                    f"--{prefix}-schedule",
                    dest=f"{dest}_schedule",
                    help=f"{label} schedule type: daily, weekly, monthly, or periodic; none turns it off",
                )
                parser.add_argument(
                    f"--{prefix}-time",
                    dest=f"{dest}_time",
                    type=parse_strict_hhmm,
                    help=f"{label} schedule time in HH:MM",
                )
                parser.add_argument(
                    f"--{prefix}-day-of-month",
                    dest=f"{dest}_day_of_month",
                    type=parse_day_of_month,
                    help=f"{label} monthly schedule day from 1 to 31",
                )
                parser.add_argument(
                    f"--{prefix}-every",
                    dest=f"{dest}_every",
                    type=parse_duration,
                    help=f"{label} periodic interval like 15m, 1h, 6h, or 1d",
                )
                full_state = parser.add_mutually_exclusive_group()
                full_state.add_argument(
                    f"--{prefix}-enabled",
                    dest=f"{dest}_enabled",
                    action="store_true",
                    help=f"Enable the {label.lower()} schedule, keeping its settings",
                )
                full_state.add_argument(
                    f"--{prefix}-disabled",
                    dest=f"{dest}_disabled",
                    action="store_true",
                    help=f"Suspend the {label.lower()} schedule, keeping its settings",
                )

    @staticmethod
    def _add_backup_copy_arguments(parser: argparse.ArgumentParser, *, creating: bool) -> None:
        only = "" if creating else "Backup copy jobs only: "
        replaces = "" if creating else "; replaces the job's list, none empties it"
        if creating:
            parser.add_argument("--vm", action="append", default=[], help="Copy only this VM of the sources, by name or ID. Repeat as needed")
            parser.add_argument("--vms", help="Copy only these VMs of the sources, comma-separated; without --vm/--vms every VM the sources back up")
        else:
            parser.add_argument("--all-vms", dest="all_vms", action="store_true", help=f"{only}copy every VM the sources back up again")
        parser.add_argument(
            "--mode",
            dest="copy_mode",
            choices=("periodic", "immediate"),
            help=(
                f"{only}periodic (the default) copies on the job's schedule, or after --run-after-job, merging what is "
                "new into one restore point; immediate copies every new restore point as soon as its backup ends"
            ),
        )
        parser.add_argument("--source-job", dest="source_jobs", action="append", default=[], help=f"{only}a backup job whose restore points are copied, by name. Repeat as needed{replaces}")
        parser.add_argument("--source-job-id", dest="source_job_ids", action="append", default=[], help=f"{only}a source backup job by ID. Repeat as needed")
        parser.add_argument("--source-repo", dest="source_repositories", action="append", default=[], help=f"{only}a repository whose restore points are copied, every job's, by name. Repeat as needed{replaces}")
        parser.add_argument("--source-repo-id", dest="source_repository_ids", action="append", default=[], help=f"{only}a source repository by ID. Repeat as needed")
        parser.add_argument("--run-after-job", dest="run_after_job", help=f"{only}start the copy each time a run of this job ends (periodic only), by name; none clears it")
        parser.add_argument("--run-after-job-id", dest="run_after_job_id", help=f"{only}the same job by ID")
        parser.add_argument(
            "--copy-window",
            dest="copy_window",
            action="append",
            default=[],
            help=(
                f"{only}when copies may move data: '[days] HH:MM-HH:MM', days separated by commas in week order "
                "(mon,wed,fri; none given: every day), the end may be past midnight (22:00-06:00). Repeat for more "
                "rules; none means any time. A copy waits outside the window and stops when it closes"
            ),
        )
        parser.add_argument("--retry-count", dest="retry_count", type=int, help=f"{only}retries of a VM whose copy failed, 0-20 (default 5 for periodic, 3 for immediate)")
        parser.add_argument("--retry-interval", dest="retry_interval_minutes", type=int, help=f"{only}minutes between those retries, 0-1440 (default 0 for periodic, 10 for immediate)")

    @staticmethod
    def _add_replication_arguments(parser: argparse.ArgumentParser, *, creating: bool) -> None:
        only = "" if creating else "Replication jobs only: "
        locked = "" if creating else "; cannot change after the replica is created"
        parser.add_argument("--source-vm", dest="source_vm", help=f"{only}the VM of the source job, by name or ID; the source job must back up exactly one VM, so this only confirms it{locked}")
        parser.add_argument("--target-inventory", dest="target_inventory", help=f"{only}the environment the replica lives in, by name; same platform as the source{locked}")
        parser.add_argument("--target-inventory-id", dest="target_inventory_id", help=f"{only}the same environment by ID{locked}")
        parser.add_argument("--target-host", dest="target_host", help=f"{only}host the replica is created on, by name or ID{locked}")
        parser.add_argument("--datastore", "--target-datastore", dest="target_datastore", help=f"{only}datastore (Hyper-V: storage path) of the replica's disks, by name or ID{locked}")
        parser.add_argument(
            "--network-map",
            dest="network_map",
            action="append",
            default=[],
            help=f"{only}network mapping source=target. Repeat as needed{'' if creating else '; none clears it'}{locked}",
        )
        parser.add_argument(
            "--replica-name",
            dest="replica_name",
            help=f"{only}name of the replica VM" + (" (required)" if creating else "; an existing replica is renamed on the next run"),
        )
        after = parser.add_mutually_exclusive_group()
        after.add_argument(
            "--run-after-source",
            dest="run_after_source",
            action="store_const",
            const=True,
            default=None,
            help=f"{only}start after each successful (or warning) run of the source backup job",
        )
        after.add_argument(
            "--no-run-after-source",
            dest="run_after_source",
            action="store_const",
            const=False,
            help=f"{only}do not start after the source backup job",
        )
        parser.add_argument(
            "--restore-point",
            dest="restore_point_id",
            help=f"{only}restore point the first run creates the replica from; without it the newest one{locked}",
        )

    @staticmethod
    def _add_schedule_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--schedule", help="Schedule type: daily, weekly, monthly, or periodic; without it the job runs manually or after the source")
        parser.add_argument("--time", type=parse_strict_hhmm, help="Schedule time in HH:MM")
        parser.add_argument("--days", type=parse_weekday_csv, help="Weekly schedule days as mon,tue,...")
        parser.add_argument("--day-of-month", dest="day_of_month", type=parse_day_of_month, help="Monthly schedule day from 1 to 31")
        parser.add_argument("--every", type=parse_duration, help="Periodic interval like 15m, 1h, 6h, or 1d")
        schedule_state = parser.add_mutually_exclusive_group()
        schedule_state.add_argument("--enabled", action="store_true", help="Enable the job")
        schedule_state.add_argument("--disabled", action="store_true", help="Disable the job")
        parser.add_argument("--description", help="Job description")

    @staticmethod
    def _resolve_id(deps: CommandDependencies, args: argparse.Namespace) -> str:
        if getattr(args, "id", None):
            if getattr(args, "name", None):
                items = deps.context.api_client.get("api/v1/jobs").get("items", [])
                matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
                if not matches or str(matches[0].get("id")) != str(args.id):
                    raise ValueError("Job selector mismatch: --name and --id refer to different records")
            return str(args.id)
        if not getattr(args, "name", None):
            raise ValueError("Job selection requires --name or --id")
        items = deps.context.api_client.get("api/v1/jobs").get("items", [])
        matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
        if not matches:
            raise ValueError(f"Job not found: {args.name}")
        if len(matches) > 1:
            raise ValueError(f"Multiple jobs matched name: {args.name}")
        return str(matches[0]["id"])

    @staticmethod
    def _decorate_job(item: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(item)
        schedule = decorated.get("schedule")
        vm_ids = decorated.get("vm_ids") if isinstance(decorated.get("vm_ids"), list) else []
        vm_names = decorated.get("vm_names") if isinstance(decorated.get("vm_names"), list) else []
        decorated.setdefault("vm_count", len(vm_ids) or len(vm_names))
        if decorated.get("job_type") == "backup-copy" and not decorated["vm_count"]:
            decorated["vm_count"] = "all"
        decorated.setdefault("schedule_state", "enabled" if isinstance(schedule, dict) and bool(schedule.get("enabled", True)) else "disabled" if isinstance(schedule, dict) else "manual")
        if not decorated.get("schedule_summary"):
            decorated["schedule_summary"] = "Manual only"
        decorated["last_result"] = _last_run_text(decorated.get("last_run"))
        return decorated

    @classmethod
    def _columns(cls) -> list[tuple[str, str]]:
        return [
            ("id", "ID"),
            ("name", "NAME"),
            ("job_type", "TYPE"),
            ("inventory.name", "INVENTORY"),
            ("repository.name", "REPOSITORY"),
            ("vm_count", "VMS"),
            ("compression_level", "COMPRESSION"),
            ("encryption.enabled", "ENCRYPTED"),
            ("schedule_summary", "SCHEDULE"),
            ("schedule_state", "STATE"),
            ("last_run_at", "LAST RUN"),
            ("last_result", "LAST RESULT"),
            ("next_run_at", "NEXT RUN"),
        ]

    @staticmethod
    def _delete_columns() -> list[tuple[str, str]]:
        return [("id", "ID"), ("deleted", "DELETED")]

    @staticmethod
    def _delete_replica_columns() -> list[tuple[str, str]]:
        return [("id", "ID"), ("deleted", "DELETED"), ("replica_deleted", "REPLICA DELETED"), ("message", "MESSAGE")]

    @staticmethod
    def _start_columns() -> list[tuple[str, str]]:
        return [("id", "SESSION ID"), ("state", "STATE"), ("resolved_backup_type", "TYPE"), ("resolved_backup_reason", "REASON")]

    def create(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.jobs.create_backup_job_payload(args)
        result = self._decorate_job(
            deps.context.api_client.post("api/v1/jobs", json=payload, timeout=INVENTORY_SCAN_TIMEOUT_SECONDS)
        )
        deps.context.print_output(result, self._columns())
        return 0

    def create_backup_copy(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.jobs.create_backup_copy_job_payload(args)
        result = self._decorate_job(deps.context.api_client.post("api/v1/jobs/backup-copy", json=payload))
        deps.context.print_output(result, self._columns())
        return 0

    def create_replication(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.jobs.create_replication_job_payload(args)
        result = self._decorate_job(deps.context.api_client.post("api/v1/jobs/replication", json=payload))
        deps.context.print_output(result, self._columns())
        return 0

    def create_copy(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.jobs.create_copy_job_payload(args)
        result = self._decorate_job(deps.context.api_client.post("api/v1/jobs/copy", json=payload))
        deps.context.print_output(result, self._columns())
        return 0

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/jobs")
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            items = [self._decorate_job(item) for item in items]
            shown = items
            if deps.context.output == "table":
                shown = [
                    {**item, "last_run_at": f"{item.get('last_run_at')} (!)"} if _clock_anomalies(item) else item
                    for item in items
                ]
            payload = {**payload, "items": shown}
        deps.context.print_output(payload, self._columns())
        for item in items if isinstance(items, list) else []:
            if _clock_anomalies(item):
                print(
                    f"Warning: job {item.get('name')!r} last ran at {item.get('last_run_at')}, ahead of this "
                    f"server's clock (!); see abr job info --name {item.get('name')!r}",
                    file=sys.stderr,
                )
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = self._decorate_job(deps.context.api_client.get(f"api/v1/jobs/{self._resolve_id(deps, args)}"))
        anomalies = _clock_anomalies(payload)
        clock_warnings = [str(anomaly.get("message") or anomaly.get("kind")) for anomaly in anomalies]
        reassignments = _vm_reassignment_messages(payload)
        warnings = [*clock_warnings, *reassignments]
        if deps.context.output == "table":
            shown: dict[str, Any] = {}
            is_replication = payload.get("job_type") == "replication"
            for key, value in payload.items():
                if is_replication and key in REPLICATION_HIDDEN_KEYS:
                    continue
                if key == "replication":
                    if isinstance(value, dict):
                        shown.update(_replication_rows(value))
                    continue
                if key == "clock_anomalies" and clock_warnings:
                    shown["clock_warning"] = " ".join(clock_warnings)
                elif key == "vm_reassignments":
                    if reassignments:
                        shown["vm_reassignment_warning"] = " ".join(reassignments)
                elif key == "backup_copy":
                    if isinstance(value, dict):
                        shown.update(_backup_copy_rows(value))
                elif key == "source_job" and payload.get("job_type") == "backup-copy":
                    continue
                elif key == "last_run" and isinstance(value, dict):
                    shown["last_run_id"] = value.get("id")
                    issues = _last_run_issues(value)
                    if issues:
                        shown["last_run_issues"] = issues
                else:
                    shown[key] = value
            payload = shown
        deps.context.print_output(payload)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0

    def edit(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        job_id = self._resolve_id(deps, args)
        current = deps.context.api_client.get(f"api/v1/jobs/{job_id}")
        payload = deps.jobs.build_edit_payload(current, args)
        result = self._decorate_job(
            deps.context.api_client.patch(f"api/v1/jobs/{job_id}", json=payload, timeout=INVENTORY_SCAN_TIMEOUT_SECONDS)
        )
        deps.context.print_output(result, self._columns())
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        if getattr(args, "delete_replica", False):
            payload = deps.context.api_client.delete(
                f"api/v1/jobs/{self._resolve_id(deps, args)}",
                params={"delete_replica": "true"},
                timeout=REPLICA_DELETE_TIMEOUT_SECONDS,
            )
            deps.context.print_output(payload, self._delete_replica_columns())
            return 0
        payload = deps.context.api_client.delete(f"api/v1/jobs/{self._resolve_id(deps, args)}")
        deps.context.print_output(payload, self._delete_columns())
        return 0

    @staticmethod
    def _resolve_repository_id(deps: CommandDependencies, name: str) -> str:
        items = deps.context.api_client.get("api/v1/repositories").get("items", [])
        matches = [item for item in items if str(item.get("name") or "").lower() == name.lower()]
        if not matches:
            raise ValueError(f"Repository not found: {name}")
        if len(matches) > 1:
            raise ValueError(f"Multiple repositories matched name: {name}")
        return str(matches[0]["id"])

    @staticmethod
    def _retention_columns(dry_run: bool) -> list[tuple[str, str]]:
        return [
            ("vm", "VM"),
            ("repository", "REPOSITORY"),
            ("points", "POINTS"),
            ("removed", "WOULD REMOVE" if dry_run else "REMOVED"),
            ("held", "HELD"),
            ("failed", "FAILED"),
            ("note", "NOTE"),
        ]

    @staticmethod
    def _retention_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for chain in result.get("chains") or []:
            held = chain.get("held") or []
            notes = []
            if chain.get("skipped_reason") and chain.get("skipped_reason") == chain.get("clock_blocked"):
                notes.append("skipped: server clock jumped forward")
            elif chain.get("skipped_reason"):
                notes.append(f"skipped: {chain['skipped_reason']}")
            if held:
                last = max(str(item.get("until") or "") for item in held)
                notes.append(f"Object Lock until {last}")
            if chain.get("kept_min_fulls"):
                notes.append(f"{len(chain['kept_min_fulls'])} expired kept as the newest full(s)")
            if chain.get("failed"):
                notes.append("could not remove " + ", ".join(chain["failed"]))
            rows.append(
                {
                    "vm": chain.get("vm_name") or chain.get("vm_id"),
                    "repository": chain.get("repository_name") or chain.get("repository_id") or "-",
                    "points": chain.get("points"),
                    "removed": len(chain.get("removed") or []),
                    "held": len(held) + len(chain.get("kept_for_chain") or []),
                    "failed": len(chain.get("failed") or []),
                    "note": "; ".join(notes) or "-",
                }
            )
        return rows

    def retention(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        dry_run = bool(getattr(args, "dry_run", False))
        result = deps.context.api_client.post(
            f"api/v1/jobs/{self._resolve_id(deps, args)}/retention",
            json={"dry_run": dry_run},
            timeout=RETENTION_TIMEOUT_SECONDS,
        )
        summary = result.get("summary") if isinstance(result, dict) else {}
        summary = summary if isinstance(summary, dict) else {}
        if deps.context.output == "table":
            gfs = result.get("gfs") if isinstance(result.get("gfs"), dict) else {}
            gfs_text = ""
            if gfs.get("enabled") and any(int(gfs.get(key) or 0) for key in ("weekly", "monthly", "yearly")):
                gfs_text = (
                    f", GFS {int(gfs.get('weekly') or 0)} weekly / {int(gfs.get('monthly') or 0)} monthly / "
                    f"{int(gfs.get('yearly') or 0)} yearly"
                )
            min_fulls = int(result.get("retention_min_fulls") or 0)
            floor_text = f", at least the newest {min_fulls} full backup(s)" if min_fulls else ""
            verb = "would remove" if dry_run else "removed"
            print(
                f"Retention of job {result.get('job_name')}: keep {result.get('retention_days')} day(s){floor_text}{gfs_text}. "
                f"{int(summary.get('removed') or 0)} of {int(summary.get('points') or 0)} restore point(s) {verb}"
                + (", nothing changed (dry run)" if dry_run else "")
                + ".",
                file=sys.stderr,
            )
            deps.context.print_output({"items": self._retention_rows(result)}, self._retention_columns(dry_run))
        else:
            deps.context.print_output(result)
        clock_blocked = result.get("clock_blocked") if isinstance(result, dict) else None
        if clock_blocked:
            print(
                f"Warning: {clock_blocked}" if dry_run else f"Error: {clock_blocked}",
                file=sys.stderr,
            )
            if not dry_run:
                return INVALID_STATE
        unavailable = [
            str(chain.get("repository_unavailable"))
            for chain in (result.get("chains") if isinstance(result, dict) else None) or []
            if isinstance(chain, dict) and chain.get("repository_unavailable")
        ]
        if unavailable:
            print(
                ("Warning: " if dry_run else "Error: ")
                + f"{len(unavailable)} chain(s) were not touched: {unavailable[0]}",
                file=sys.stderr,
            )
            if not dry_run:
                return INVALID_STATE
        if int(summary.get("failed") or 0):
            print(
                f"Error: {int(summary.get('failed'))} expired restore point(s) could not be removed and are kept "
                "for the next pass; see the server log for the storage error.",
                file=sys.stderr,
            )
            return GENERAL_ERROR
        return 0

    def start(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        synthetic = getattr(args, "synthetic", False)
        target_repo = getattr(args, "target_repo", None)
        if target_repo and not synthetic:
            raise ValueError("--target-repo is only used with --synthetic")
        payload: dict[str, Any] = {"force_full": args.full, "force_synthetic": synthetic}
        if target_repo:
            payload["target_repository_id"] = self._resolve_repository_id(deps, str(target_repo))
        result = deps.context.api_client.post(
            f"api/v1/jobs/{self._resolve_id(deps, args)}/start", json=payload
        )
        sessions = result.get("items") if isinstance(result, dict) else None
        if getattr(args, "wait", False) and isinstance(sessions, list) and sessions:
            if len(sessions) == 1:
                watched = watch_session(deps.context.api_client, str(sessions[0]["id"]))
                deps.context.print_output(watched)
                if not session_watch_succeeded(watched):
                    return _session_failure_exit_code(watched)
            elif self._one_vm(sessions):
                return self._watch_in_order(deps, result, sessions)
            else:
                print(
                    "Job started " + str(len(sessions)) + " sessions; "
                    "--wait only follows one at a time. Not watching -- use "
                    "`abr session info --id <id> --watch` for each of: "
                    + ", ".join(str(session.get("id")) for session in sessions),
                    file=sys.stderr,
                )
                deps.context.print_output(result, self._start_columns())
        else:
            deps.context.print_output(result, self._start_columns())
        return 0

    @staticmethod
    def _one_vm(sessions: list[dict[str, Any]]) -> bool:
        vm_ids = {str(session.get("vm_id") or "") for session in sessions}
        return len(vm_ids) == 1 and "" not in vm_ids

    def _watch_in_order(self, deps: CommandDependencies, result: dict[str, Any], sessions: list[dict[str, Any]]) -> int:
        watched: list[dict[str, Any]] = []
        for session in sessions:
            current = watch_session(deps.context.api_client, str(session["id"]))
            watched.append(current)
            if not session_watch_succeeded(current):
                deps.context.print_output({**result, "items": watched}, self._start_columns())
                return _session_failure_exit_code(current)
        deps.context.print_output({**result, "items": watched}, self._start_columns())
        return 0
