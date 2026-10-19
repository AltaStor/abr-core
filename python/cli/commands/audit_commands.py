# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse

from ..compact_help_formatter import CompactHelpFormatter
from .base import CommandDependencies


class AuditCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "audit",
            help="The audit log",
            description="Audit log commands",
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="audit_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List audit records, newest first.",
            description=(
                "List audit records, newest first: who ran which function on what, with the role "
                "they held then, the result (allowed, denied or failed) and the session it started. "
                "Times are UTC.\n\n"
                "Examples:\n"
                "  abr audit list\n"
                "  abr audit list --result denied\n"
                "  abr audit list --user jdoe --since 2026-10-01\n"
                "  abr audit list --function job. --limit 50"
            ),
            formatter_class=CompactHelpFormatter,
        )
        list_parser.add_argument("--user", help="Only records of this user name")
        list_parser.add_argument(
            "--function", help="A function ID (job.start), or an area with a trailing dot (job.) for all of it"
        )
        list_parser.add_argument("--result", choices=("allowed", "denied", "failed"), help="Only this result")
        list_parser.add_argument("--since", help="Only records at or after this ISO timestamp")
        list_parser.add_argument("--until", help="Only records at or before this ISO timestamp")
        list_parser.add_argument("--limit", type=int, help="Return at most this many records (default 100, max 1000)")
        list_parser.add_argument("--offset", type=int, help="Skip this many records first; page with --limit")
        list_parser.set_defaults(handler=self.list)

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        params = {
            key: value
            for key, value in (
                ("user", args.user),
                ("function", args.function),
                ("result", args.result),
                ("since", args.since),
                ("until", args.until),
                ("limit", args.limit),
                ("offset", args.offset),
            )
            if value is not None
        }
        payload = deps.context.api_client.get("api/v1/audit", params=params or None)
        if deps.context.output == "table":
            for item in payload.get("items", []):
                target = item.get("target") or {}
                item["target_text"] = f"{target.get('type')}:{target.get('name') or target.get('id')}" if target else ""
        deps.context.print_output(
            payload,
            [
                ("timestamp", "TIME (UTC)"),
                ("user", "USER"),
                ("role", "ROLE"),
                ("function_id", "FUNCTION"),
                ("target_text", "TARGET"),
                ("result", "RESULT"),
                ("session_id", "SESSION"),
                ("interface", "VIA"),
            ],
        )
        return 0
