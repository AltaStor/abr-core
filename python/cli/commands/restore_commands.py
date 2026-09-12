# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import INVALID_STATE
from ..progress_watch import session_watch_succeeded, watch_session
from .base import CommandDependencies


class RestoreCommands:
    def register(self, registry) -> None:
        group = registry.add_parser("restore", help="Restore operations", description="Restore commands", formatter_class=CompactHelpFormatter)
        commands = group.add_subparsers(dest="restore_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List restore points.",
            description=(
                "List restore points available for VM restore.\n\n"
                "Examples:\n"
                "  abr restore list\n"
                '  abr restore list --vm "sql-01" --inventory "vCenter-Prod"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        list_parser.add_argument("--vm", help="Filter by VM name or ID")
        list_parser.add_argument("--backup", help="Filter by backup chain ID")
        list_parser.add_argument("--inventory", help="Filter by inventory name or ID")
        list_parser.set_defaults(handler=self.list)

        point = commands.add_parser("point", help="Restore point operations.", description="Restore point commands.", formatter_class=CompactHelpFormatter)
        point_commands = point.add_subparsers(dest="restore_point_command", required=True)
        point_list = point_commands.add_parser(
            "list",
            help="List restore points for a VM.",
            description=(
                "List restore points for a VM by unique VM ID.\n\n"
                "Examples:\n"
                "  abr restore point list --vm-id <vm-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        point_list.add_argument("--vm-id", required=True, help="Unique VM ID")
        point_list.set_defaults(handler=self.point_list)

        start = commands.add_parser(
            "start",
            help="Start restore.",
            description=(
                "Start a full VM restore from a restore point.\n\n"
                "Examples:\n"
                '  abr restore start --restore-point <point-id> --new-name "sql-01-restored"\n'
                '  abr restore start --backup <backup-id> --vm "sql-01" --target-inventory "vCenter-Prod" '
                '--datastore "datastore1" --power-on\n'
                "  abr restore start --restore-point <point-id> --in-place --force"
            ),
            formatter_class=CompactHelpFormatter,
        )
        start.add_argument("--restore-point", help="Restore point ID")
        start.add_argument("--point-id", help="Restore point ID (legacy alias)")
        start.add_argument("--backup", help="Backup chain ID to resolve the latest restore point from")
        start.add_argument("--vm", help="Source VM name or ID when resolving from --backup")
        start.add_argument("--new-name", help="Name for the restored VM")
        start.add_argument("--name", help="New VM name for out-of-place restore (legacy alias)")
        start.add_argument("--target-inventory", help="Target inventory name or ID")
        start.add_argument("--target-inventory-id", help="Target inventory ID")
        start.add_argument("--target-host", help="Target host name or ID")
        start.add_argument("--datastore", help="Target datastore name or ID")
        start.add_argument("--target-datastore", help="Target datastore name (legacy alias)")
        start.add_argument("--network-map", action="append", default=[], help="Network mapping source=destination. Repeat as needed.")
        start.add_argument("--repo", help="Repository name for backup-copy restore")
        start.add_argument("--repo-id", help="Repository ID for backup-copy restore")
        start.add_argument("--power-on", action="store_true", help="Power on after restore")
        start.add_argument(
            "--in-place",
            dest="in_place",
            action="store_true",
            help=(
                "Restore in place: replace the running source VM at its original host, datastore, "
                "and name. Requires --force. Cannot be combined with --target-inventory/--target-host/"
                "--datastore/--new-name -- in-place restore always uses the source VM's own placement."
            ),
        )
        start.add_argument("--force", action="store_true", help="Authorize the overwrite for --in-place restore. Has no effect without --in-place.")
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

    @staticmethod
    def _point_columns() -> list[tuple[str, str]]:
        return [
            ("id", "POINT ID"),
            ("backup_id", "BACKUP ID"),
            ("vm_name", "VM NAME"),
            ("inventory_name", "INVENTORY"),
            ("repository_name", "REPOSITORY"),
            ("created_at", "CREATED"),
            ("point_type", "TYPE"),
            ("status", "STATUS"),
        ]

    @staticmethod
    def _start_columns() -> list[tuple[str, str]]:
        return [
            ("restore_session_id", "SESSION ID"),
            ("status", "STATUS"),
            ("restore_point_id", "POINT ID"),
            ("source_vm_name", "SOURCE VM"),
            ("target_inventory_name", "TARGET INVENTORY"),
            ("target_host_name", "TARGET HOST"),
            ("datastore_name", "DATASTORE"),
            ("new_name", "NEW NAME"),
        ]

    @staticmethod
    def _decorate_restore_point(item: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(item)
        decorated.setdefault("backup_id", item.get("chain_id"))
        inventory = decorated.get("inventory") if isinstance(decorated.get("inventory"), dict) else {}
        repository = decorated.get("repository") if isinstance(decorated.get("repository"), dict) else {}
        decorated.setdefault("inventory_id", inventory.get("id"))
        decorated.setdefault("inventory_name", inventory.get("name"))
        decorated.setdefault("repository_id", repository.get("id"))
        decorated.setdefault("repository_name", repository.get("name"))
        return decorated

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/restore/points", params=deps.restore.build_restore_list_params(args))
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            payload = {**payload, "items": [self._decorate_restore_point(item) for item in items]}
        deps.context.print_output(payload, self._point_columns())
        return 0

    def point_list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"api/v1/restore/vms/{args.vm_id}/points")
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            payload = {**payload, "items": [self._decorate_restore_point(item) for item in items]}
        deps.context.print_output(payload, self._point_columns())
        return 0

    def start(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.restore.build_start_payload(args)
        result = deps.context.api_client.post("api/v1/restore/start", json=payload)
        session_id = result.get("restore_session_id") if isinstance(result, dict) else None
        if getattr(args, "wait", False) and session_id:
            result = watch_session(deps.context.api_client, str(session_id))
            deps.context.print_output(result)
            if not session_watch_succeeded(result):
                return INVALID_STATE
        else:
            deps.context.print_output(result, self._start_columns())
        return 0
