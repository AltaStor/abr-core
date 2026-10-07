# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import INVALID_STATE
from ..progress_watch import session_watch_succeeded, watch_session
from .base import CommandDependencies


class SessionCommands:
    def register(self, registry) -> None:
        group = registry.add_parser("session", help="Execution sessions", description="Session commands", formatter_class=CompactHelpFormatter)
        commands = group.add_subparsers(dest="session_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List sessions.",
            description=(
                "List sessions, newest first.\n\n"
                "Examples:\n"
                "  abr session list\n"
                '  abr session list --job "Job-SQL-Server"\n'
                "  abr session list --limit 50\n"
                "  abr session list --limit 50 --offset 50\n"
                "  abr session list --since 2026-01-01\n"
                "  abr session list --run <run-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        list_parser.add_argument("--job", help="Filter by job name")
        list_parser.add_argument("--run", help="Only the sessions of one job run, by the run id job info shows as last_run_id")
        list_parser.add_argument("--limit", type=int, help="Return at most this many sessions (server caps at 500)")
        list_parser.add_argument("--offset", type=int, help="Skip this many sessions first; page with --limit")
        # Narrows what is being paged, rather than paging through everything to
        # reach it -- the cheap way to ask for "this week's runs".
        list_parser.add_argument("--since", help="Only sessions started at or after this ISO timestamp (e.g. 2026-01-01)")
        list_parser.set_defaults(handler=self.list)

        for name, help_text, handler in (
            ("info", "Show session details.", self.info),
            ("log", "Show session log.", self.log),
        ):
            description = f"{help_text}\n\nExamples:\n  abr session {name} --id <session-id>"
            if name == "info":
                description += "\n  abr session info --id <session-id> --watch"
            parser = commands.add_parser(name, help=help_text, description=description, formatter_class=CompactHelpFormatter)
            parser.add_argument("--id", required=True, help="Session ID")
            if name == "info":
                parser.add_argument(
                    "--watch",
                    action="store_true",
                    help=(
                        "Follow the session's real progress on stderr until it finishes, then "
                        "show its final state. Exits non-zero if the session does not complete "
                        "successfully (failed, stopped, or canceled)."
                    ),
                )
            parser.set_defaults(handler=handler)

        stop = commands.add_parser(
            "stop",
            help="Stop a session.",
            description=(
                "Stop a running or queued session. A stopped backup discards the data it "
                "had written; a stopped restore removes the VM or disk it had created.\n\n"
                "Examples:\n"
                "  abr session stop --id <session-id>\n"
                "  abr session stop --id <session-id> --force"
            ),
            formatter_class=CompactHelpFormatter,
        )
        stop.add_argument("--id", required=True, help="Session ID")
        stop.add_argument("--force", action="store_true", help="Request forced stop")
        stop.set_defaults(handler=self.stop)

    @staticmethod
    def _columns() -> list[tuple[str, str]]:
        return [("id", "SESSION ID"), ("state", "STATE"), ("type", "TYPE"), ("vm_name", "VM"), ("job.id", "JOB ID"), ("started_at", "STARTED"), ("finished_at", "FINISHED"), ("resolved_backup_type", "BACKUP TYPE"), ("progress", "PROGRESS")]

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        supplied = (
            ("job", args.job),
            ("run_id", getattr(args, "run", None)),
            ("limit", getattr(args, "limit", None)),
            ("offset", getattr(args, "offset", None)),
            ("since", getattr(args, "since", None)),
        )
        params = {name: value for name, value in supplied if value is not None} or None
        deps.context.print_output(deps.context.api_client.get("api/v1/sessions", params=params), self._columns())
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        watching = getattr(args, "watch", False)
        if watching:
            session = watch_session(deps.context.api_client, args.id)
        else:
            session = deps.context.api_client.get(f"api/v1/sessions/{args.id}")
        deps.context.print_output(session)
        if watching and not session_watch_succeeded(session):
            return INVALID_STATE
        return 0

    def log(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"api/v1/sessions/{args.id}/log")
        if str(getattr(args, "output", "table")) == "table":
            logs = payload.get("logs") if isinstance(payload, dict) and isinstance(payload.get("logs"), list) else []
            deps.context.print_output(
                {"items": [entry for entry in logs if isinstance(entry, dict)]},
                [("timestamp", "TIME"), ("level", "LEVEL"), ("message", "MESSAGE")],
            )
        else:
            deps.context.print_output(payload)
        return 0

    def stop(self, args: argparse.Namespace) -> int:
        if not str(args.id or "").strip():
            raise ValueError("Session selection requires a non-empty --id")
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.post(f"api/v1/sessions/{args.id}/stop", json={"force": args.force}), [("id", "SESSION ID"), ("state", "STATE"), ("message", "MESSAGE")])
        return 0
