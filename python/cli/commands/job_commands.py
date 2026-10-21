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


def _session_failure_exit_code(session: Any) -> int:
    if not isinstance(session, dict):
        return INVALID_STATE
    if str(session.get("failure_kind") or "") == "validation":
        return VALIDATION_FAILED
    return INVALID_STATE


def _clock_anomalies(job: object) -> list[dict[str, Any]]:
    anomalies = job.get("clock_anomalies") if isinstance(job, dict) else None
    return [anomaly for anomaly in anomalies if isinstance(anomaly, dict)] if isinstance(anomalies, list) else []


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
                "  abr job delete --id <job-id> --yes"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(delete, name_help="Job name", id_help="Job ID")
        delete.add_argument("--yes", "--force", dest="force", action="store_true", help="Delete without confirmation")
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
                "keeps whatever their age (0-100; 0, the default, keeps none beyond --retention-days)"
            ),
        )
        if include_compression_encryption_flags:
            parser.add_argument(
                "--compression-level",
                type=parse_compression_level,
                help="Backup compression level: 0 disables compression, 1-9 select Zstd levels (higher compresses smaller but slower). Default: 3.",
            )
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
            encryption_state = parser.add_mutually_exclusive_group()
            encryption_state.add_argument(
                "--set-encryption",
                action="store_true",
                help=(
                    "Enable AES-256 encryption of backup data blocks. The key is read from "
                    f"{ENCRYPTION_KEY_ENV_VAR}, or prompted for (twice) when it is not set."
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
    def _start_columns() -> list[tuple[str, str]]:
        return [("id", "SESSION ID"), ("state", "STATE"), ("resolved_backup_type", "TYPE"), ("resolved_backup_reason", "REASON")]

    def create(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.jobs.create_backup_job_payload(args)
        result = self._decorate_job(deps.context.api_client.post("api/v1/jobs", json=payload))
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
        warnings = [str(anomaly.get("message") or anomaly.get("kind")) for anomaly in anomalies]
        if deps.context.output == "table":
            shown: dict[str, Any] = {}
            for key, value in payload.items():
                if key == "clock_anomalies" and warnings:
                    shown["clock_warning"] = " ".join(warnings)
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
        result = self._decorate_job(deps.context.api_client.patch(f"api/v1/jobs/{job_id}", json=payload))
        deps.context.print_output(result, self._columns())
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
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
            f"api/v1/jobs/{self._resolve_id(deps, args)}/retention", json={"dry_run": dry_run}
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
