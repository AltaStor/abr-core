# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import INVALID_STATE
from ..models.views import human_size
from ..progress_watch import session_watch_succeeded, watch_session
from .base import CommandDependencies

RESTORE_POINT_COLUMNS = [
    ("id", "ID"),
    ("created_at", "CREATED"),
    ("execution_type", "TYPE"),
    ("status", "STATUS"),
    ("size", "SIZE"),
    ("compression_level", "COMPRESSION"),
    ("encryption_enabled", "ENCRYPTED"),
]


class BackupCommands:
    def register(self, registry) -> None:
        group = registry.add_parser("backup", help="Backup chains and restore points", description="Backup commands", formatter_class=CompactHelpFormatter)
        commands = group.add_subparsers(dest="backup_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List backups.",
            description=(
                "List backup chains.\n\n"
                "Examples:\n"
                "  abr backup list\n"
                '  abr backup list --repo "Local-Repo-1"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        list_parser.add_argument("--repo", help="Repository name")
        list_parser.set_defaults(handler=self.list)

        for name, help_text, handler in (
            ("info", "Show backup details.", self.info),
            ("delete", "Delete a backup chain after safety checks.", self.delete),
            ("verify", "Verify backup integrity.", self.verify),
        ):
            description = f"{help_text}\n\nExamples:\n  abr backup {name} --id <backup-id>"
            if name == "delete":
                description += "\n  abr backup delete --id <backup-id> --force"
            if name == "verify":
                description += (
                    "\n\nVerify follows the check to the end, drawing the same live progress "
                    "bar as `job start --wait`."
                )
            parser = commands.add_parser(name, help=help_text, description=description, formatter_class=CompactHelpFormatter)
            parser.add_argument("--id", required=True, help="Backup chain ID")
            if name == "delete":
                parser.add_argument(
                    "--force",
                    action="store_true",
                    help="Also delete GFS-protected restore points. Deliberately discards data a GFS policy marked for retention.",
                )
            parser.set_defaults(handler=handler)

        import_parser = commands.add_parser(
            "import",
            help="Import a backup chain.",
            description=(
                "Import a backup chain from an external path into a repository.\n\n"
                "Examples:\n"
                '  abr backup import --path /mnt/external/backup-manifest --repo "Local-Repo-1"\n'
                "  abr backup import --path /mnt/external/backup-manifest --repo-id <repo-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        import_parser.add_argument("--path", required=True, help="External manifest path or directory")
        repo = import_parser.add_mutually_exclusive_group(required=True)
        repo.add_argument("--repo", help="Target repository name")
        repo.add_argument("--repo-id", help="Target repository ID")
        import_parser.set_defaults(handler=self.import_backup)

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get("api/v1/backups", params={"repo": args.repo} if args.repo else None), [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("restore_point_count", "POINTS"), ("latest_execution_type", "LATEST TYPE"), ("last_restore_point_at", "LATEST POINT")])
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"api/v1/backups/{args.id}")
        if deps.context.output != "table":
            deps.context.print_output(payload)
            return 0

        chain = dict(payload)
        restore_points = chain.pop("restore_points", None) or []
        deps.context.print_output(chain)
        if restore_points:
            print()
            rows = [{**point, "size": human_size(point.get("size_bytes"))} for point in restore_points]
            deps.context.print_output(rows, RESTORE_POINT_COLUMNS)
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        params = {"force": True} if getattr(args, "force", False) else None
        payload = deps.context.api_client.delete(f"api/v1/backups/{args.id}", params=params)
        deps.context.print_output(
            payload,
            [
                ("id", "ID"),
                ("deleted", "DELETED"),
                ("restore_point_count", "POINTS"),
                ("artifacts_removed", "ARTIFACTS_REMOVED"),
                ("gfs_protection_overridden", "GFS_OVERRIDDEN"),
            ],
        )
        return 0

    def verify(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        started = deps.context.api_client.post(
            f"api/v1/backups/{args.id}/verify", params={"wait": "false"}, timeout=21600
        )
        session_id = started.get("session_id") if isinstance(started, dict) else None
        session: dict[str, Any] | None = None
        if session_id:
            session = watch_session(deps.context.api_client, str(session_id))
            result = session.get("verify")
        else:
            result = started

        if not isinstance(result, dict):
            deps.context.print_output(session if session is not None else started)
            return INVALID_STATE

        deps.context.print_output(result, [("id", "ID"), ("result", "RESULT"), ("checked_restore_points", "POINTS"), ("checked_artifacts", "ARTIFACTS"), ("warnings", "WARNINGS"), ("errors", "ERRORS")])
        if result.get("result") != "valid":
            return INVALID_STATE
        if session is not None and not session_watch_succeeded(session):
            return INVALID_STATE
        return 0

    def import_backup(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        result = deps.context.api_client.post("api/v1/backups/import", json={"path": args.path, "repo": args.repo, "repo_id": args.repo_id})
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("restore_point_count", "POINTS")])
        return 0
