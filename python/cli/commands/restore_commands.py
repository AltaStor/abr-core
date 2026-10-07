# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import INVALID_STATE
from ..parsing.secret_arguments import ENCRYPTION_KEY_ENV_VAR, add_secret_argument
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

        context = commands.add_parser(
            "context",
            help="Show what a restore point can be mapped to.",
            description=(
                "List the disks, adapters, source datastores and networks a restore point carries, with\n"
                "the target each one falls back to. These are the names --disk-datastore, --nic-network,\n"
                "--datastore-map and --network-map take.\n\n"
                "Examples:\n"
                "  abr restore context --restore-point <point-id>\n"
                "  abr restore context --restore-point <point-id> --output json"
            ),
            formatter_class=CompactHelpFormatter,
        )
        context.add_argument("--restore-point", dest="restore_point", required=True, help="Restore point ID")
        context.set_defaults(handler=self.context)

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
        start.add_argument("--datastore-map", dest="datastore_map", action="append", default=[], help="Datastore mapping source=destination for disks that must not land on --datastore. Repeat as needed.")
        start.add_argument(
            "--disk-datastore",
            dest="disk_datastore",
            action="append",
            default=[],
            help=(
                'Per-disk placement disk=destination, where disk is the disk ID or label that restore context lists '
                '(for example "Hard disk 2=fast-ssd"). Wins over --datastore-map, which wins over --datastore. Repeat as needed.'
            ),
        )
        start.add_argument(
            "--nic-network",
            dest="nic_network",
            action="append",
            default=[],
            help=(
                'Per-adapter placement adapter=destination, where adapter is the adapter ID or label that restore '
                'context lists. Sends two adapters on one source network to different targets. Repeat as needed.'
            ),
        )
        start.add_argument("--repo", help="Repository name for backup-copy restore")
        start.add_argument("--repo-id", help="Repository ID for backup-copy restore")
        start.add_argument("--power-on", action="store_true", help="Power on after restore")
        add_secret_argument(
            start,
            "--encryption-key",
            env_var=ENCRYPTION_KEY_ENV_VAR,
            help_text=(
                "Decryption key for this restore. Overrides the key stored with the job and with the restore point; "
                "needed when the job that held the key is gone"
            ),
        )
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

    @staticmethod
    def _context_columns() -> list[tuple[str, str]]:
        return [
            ("kind", "KIND"),
            ("source", "SOURCE"),
            ("target", "DEFAULT TARGET"),
        ]

    @staticmethod
    def _mappable_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        source_vm = payload.get("source_vm") if isinstance(payload.get("source_vm"), dict) else {}
        targets = payload.get("targets") if isinstance(payload.get("targets"), dict) else {}
        defaults = payload.get("defaults") if isinstance(payload.get("defaults"), dict) else {}
        datastore_names = {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in targets.get("datastores", [])
            if isinstance(item, dict)
        }
        network_names = {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in targets.get("networks", [])
            if isinstance(item, dict)
        }
        rows: list[dict[str, Any]] = []
        for mapping in defaults.get("datastore_mappings", []):
            if not isinstance(mapping, dict):
                continue
            rows.append(
                {
                    "kind": "datastore",
                    "source": str(mapping.get("source_datastore") or ""),
                    "target": datastore_names.get(str(mapping.get("target_datastore_id") or ""), ""),
                }
            )
        default_datastore = datastore_names.get(str(defaults.get("target_datastore_id") or ""), "")
        datastore_targets = {
            str(mapping.get("source_datastore") or ""): datastore_names.get(str(mapping.get("target_datastore_id") or ""), "")
            for mapping in defaults.get("datastore_mappings", [])
            if isinstance(mapping, dict)
        }
        for disk in source_vm.get("disks", []):
            if not isinstance(disk, dict):
                continue
            source_datastore = str((disk.get("datastore_path") or "").partition("]")[0]).lstrip("[").strip()
            rows.append(
                {
                    "kind": "disk",
                    "source": str(disk.get("label") or disk.get("id") or ""),
                    "target": datastore_targets.get(source_datastore) or default_datastore,
                }
            )
        nics = {str(nic.get("id") or ""): nic for nic in source_vm.get("nics", []) if isinstance(nic, dict)}
        for mapping in defaults.get("network_mappings", []):
            if not isinstance(mapping, dict):
                continue
            nic = nics.get(str(mapping.get("source_nic_id") or "")) or {}
            rows.append(
                {
                    "kind": "network",
                    "source": str(nic.get("source_network") or ""),
                    "target": network_names.get(str(mapping.get("target_network_id") or ""), ""),
                }
            )
        for mapping in defaults.get("network_mappings", []):
            if not isinstance(mapping, dict):
                continue
            nic = nics.get(str(mapping.get("source_nic_id") or "")) or {}
            if not nic:
                continue
            rows.append(
                {
                    "kind": "adapter",
                    "source": str(nic.get("label") or nic.get("id") or ""),
                    "target": network_names.get(str(mapping.get("target_network_id") or ""), ""),
                }
            )
        return rows

    def context(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(
            "api/v1/restore/context", params={"restore_point_id": args.restore_point}
        )
        if str(getattr(args, "output", "table")) == "table":
            deps.context.print_output({"items": self._mappable_rows(payload)}, self._context_columns())
        else:
            deps.context.print_output(payload)
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
