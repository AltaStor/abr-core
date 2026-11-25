# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..errors import CliStructuredError
from ..exit_codes import INVALID_STATE, INVALID_USAGE
from ..parsing.secret_arguments import ENCRYPTION_KEY_ENV_VAR, add_secret_argument, resolve_secret
from ..models.views import human_size
from ..progress_watch import session_watch_succeeded, watch_session
from .base import CommandDependencies

BACKUP_DELETE_TIMEOUT_SECONDS = 21600
RECYCLE_BIN_PATH = "api/v1/system/recycle-bin"


def _announce_session(session_id: object) -> None:
    print(f"Session: {session_id}", file=sys.stderr)

def _chain_status(chain: dict[str, Any]) -> str:
    status = str(chain.get("status") or "")
    notes = []
    corrupt = chain.get("corrupt") if isinstance(chain.get("corrupt"), dict) else None
    if corrupt:
        notes.append(f"{corrupt.get('restore_point_count')} point(s) corrupt")
    binned = chain.get("recycle_bin") if isinstance(chain.get("recycle_bin"), dict) else None
    if binned:
        notes.append(
            ("in the recycle bin" if binned.get("whole_chain") else f"{binned.get('restore_points')} point(s) in the recycle bin")
            + f" until {binned.get('purge_after')}"
        )
    pending = chain.get("pending_deletion") if isinstance(chain.get("pending_deletion"), dict) else None
    if pending:
        after = pending.get("delete_after")
        notes.append(f"{pending.get('restore_point_count')} point(s) deleting" + (f" after {after}" if after else ""))
    if not notes:
        return status
    if status == "deleting" and pending and not corrupt:
        return notes[-1]
    if status == "deleted" and binned and binned.get("whole_chain") and len(notes) == 1:
        return notes[0]
    return f"{status}, {', '.join(notes)}"


def _print_corruption_marks(marks: object) -> None:
    if not isinstance(marks, dict):
        return
    corrupt_now = [str(item) for item in marks.get("corrupt_now") or []]
    cleared = [str(item) for item in marks.get("cleared") or []]
    if corrupt_now:
        print(
            f"Marked corrupt: {len(corrupt_now)} restore point(s), those with damaged data and the ones that build on "
            "them. They are not offered for restore, and the job's next run takes an active full.",
            file=sys.stderr,
        )
    if cleared:
        print(f"No longer corrupt: {len(cleared)} restore point(s) verified intact again ({', '.join(cleared)}).", file=sys.stderr)


RESTORE_POINT_COLUMNS = [
    ("id", "ID"),
    ("created_at", "CREATED"),
    ("execution_type", "TYPE"),
    ("status", "STATUS"),
    ("size", "SIZE"),
    ("compression_level", "COMPRESSION"),
    ("encryption_enabled", "ENCRYPTED"),
    ("gfs", "GFS"),
    ("immutability_expires_at", "IMMUTABLE UNTIL"),
    ("consistency", "CONSISTENCY"),
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
            ("undelete", "Bring a deleted backup chain back from the recycle bin.", self.undelete),
            ("verify", "Verify backup integrity.", self.verify),
        ):
            description = f"{help_text}\n\nExamples:\n  abr backup {name} --id <backup-id>"
            if name == "delete":
                description = (
                    f"{help_text} Asks before deleting, unless --yes; without a terminal it needs --yes. When the "
                    "recycle bin keeps deleted backups (abr settings recycle-bin), the chain moves there: its data "
                    "stays in the repository until the server deletes it for good, abr backup undelete brings it "
                    "back, and deleting a chain that is all in the recycle bin deletes it for good at once."
                    f"\n\nExamples:\n  abr backup {name} --id <backup-id>"
                    "\n  abr backup delete --id <backup-id> --yes"
                    "\n  abr backup delete --id <backup-id> --force --yes"
                )
            if name == "undelete":
                description = (
                    f"{help_text} Every restore point of the chain in the recycle bin comes back with the status it "
                    "had: it can be restored again and the job's retention counts it again."
                    f"\n\nExamples:\n  abr backup {name} --id <backup-id>"
                )
            if name == "verify":
                description += (
                    "\n\nVerify follows the check to the end, drawing the same live progress "
                    "bar as `job start --wait`."
                )
            parser = commands.add_parser(name, help=help_text, description=description, formatter_class=CompactHelpFormatter)
            parser.add_argument("--id", required=True, help="Backup chain ID")
            if name == "verify":
                add_secret_argument(
                    parser,
                    "--encryption-key",
                    env_var=ENCRYPTION_KEY_ENV_VAR,
                    help_text=(
                        "Decryption key to check against the chain. Overrides the key stored with the job and with "
                        "the restore points; the data checksums are verified either way"
                    ),
                )
            if name == "delete":
                parser.add_argument(
                    "--force",
                    action="store_true",
                    help="Also delete GFS-protected restore points. Deliberately discards data a GFS policy marked for retention.",
                )
                parser.add_argument("--yes", action="store_true", help="Delete without asking first")
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
        payload = deps.context.api_client.get("api/v1/backups", params={"repo": args.repo} if args.repo else None)
        if deps.context.output == "table":
            chains = payload.get("items") if isinstance(payload, dict) else payload
            payload = [{**chain, "status": _chain_status(chain)} for chain in chains or [] if isinstance(chain, dict)]
        deps.context.print_output(payload, [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("restore_point_count", "POINTS"), ("latest_execution_type", "LATEST TYPE"), ("last_restore_point_at", "LATEST POINT")])
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"api/v1/backups/{args.id}")
        if deps.context.output != "table":
            deps.context.print_output(payload)
            return 0

        chain = dict(payload)
        chain["status"] = _chain_status(chain)
        restore_points = chain.pop("restore_points", None) or []
        deps.context.print_output(chain)
        if restore_points:
            print()
            rows = [
                {
                    **point,
                    "size": human_size(point.get("size_bytes")),
                    "gfs": ",".join(
                        mark for mark, held in (point.get("gfs_marks") or {}).items() if held
                    ),
                }
                for point in restore_points
            ]
            columns = RESTORE_POINT_COLUMNS
            if any(point.get("purge_after") for point in restore_points):
                columns = [*RESTORE_POINT_COLUMNS, ("purge_after", "IN RECYCLE BIN UNTIL")]
            deps.context.print_output(rows, columns)
        return 0

    @staticmethod
    def _confirm_delete(client, backup_id: str) -> None:
        chain = client.get(f"api/v1/backups/{backup_id}") or {}
        points = chain.get("restore_points") or []
        vm = (chain.get("vm") or {}).get("name") or (chain.get("vm") or {}).get("id") or "?"
        in_use = [point for point in points if point.get("status") != "deleted"]
        keep_days = int((client.get(RECYCLE_BIN_PATH) or {}).get("keep_days") or 0)
        if in_use and keep_days:
            question = (
                f"Delete chain {backup_id} of {vm} ({len(in_use)} restore point(s))? It goes to the recycle bin and is "
                f"deleted for good after {keep_days} day(s)."
            )
        else:
            question = f"Delete chain {backup_id} of {vm} ({len(points)} restore point(s)) for good?"
        if not sys.stdin.isatty():
            raise CliStructuredError(
                code="confirmation_required",
                message=f"{question} There is no terminal to ask in: add --yes to delete without asking",
                exit_code=INVALID_USAGE,
            )
        try:
            answer = input(f"{question} [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise CliStructuredError(code="cancelled", message="Nothing was deleted", exit_code=INVALID_USAGE)

    def delete(self, args: argparse.Namespace) -> int:
        if not str(args.id or "").strip():
            raise ValueError("Backup selection requires a non-empty --id")
        deps = CommandDependencies(args)
        if not getattr(args, "yes", False):
            self._confirm_delete(deps.context.api_client, str(args.id))
        params: dict[str, Any] = {"wait": "false"}
        if getattr(args, "force", False):
            params["force"] = True
        started = deps.context.api_client.delete(
            f"api/v1/backups/{args.id}", params=params, timeout=BACKUP_DELETE_TIMEOUT_SECONDS
        )
        session_id = started.get("session_id") if isinstance(started, dict) else None
        session: dict[str, Any] | None = None
        if session_id:
            _announce_session(session_id)
            session = watch_session(deps.context.api_client, str(session_id))
            payload = session.get("delete") if isinstance(session.get("delete"), dict) else session
        else:
            payload = started
        deps.context.print_output(
            payload,
            [
                ("id", "ID"),
                ("deleted", "DELETED"),
                ("restore_point_count", "POINTS"),
                ("artifacts_removed", "ARTIFACTS_REMOVED"),
                ("gfs_protection_overridden", "GFS_OVERRIDDEN"),
                ("message", "MESSAGE"),
            ],
        )
        if isinstance(payload, dict) and payload.get("deleted") is False:
            return INVALID_STATE
        if session is not None and not session_watch_succeeded(session):
            return INVALID_STATE
        return 0

    def undelete(self, args: argparse.Namespace) -> int:
        if not str(args.id or "").strip():
            raise ValueError("Backup selection requires a non-empty --id")
        deps = CommandDependencies(args)
        payload = deps.context.api_client.post(f"api/v1/backups/{args.id}/undelete")
        if deps.context.output == "table" and isinstance(payload, dict):
            deps.context.print_output(
                payload, [("id", "ID"), ("undeleted_restore_points", "POINTS"), ("message", "MESSAGE")]
            )
            return 0
        deps.context.print_output(payload)
        return 0

    def verify(self, args: argparse.Namespace) -> int:
        if not str(args.id or "").strip():
            raise ValueError("Backup selection requires a non-empty --id")
        deps = CommandDependencies(args)
        encryption_key = str(
            resolve_secret(getattr(args, "encryption_key", None), env_var=ENCRYPTION_KEY_ENV_VAR, prompt="Encryption key")
            or ""
        ).strip()
        started = deps.context.api_client.post(
            f"api/v1/backups/{args.id}/verify",
            json={"encryption_key": encryption_key} if encryption_key else None,
            params={"wait": "false"},
            timeout=21600,
        )
        session_id = started.get("session_id") if isinstance(started, dict) else None
        session: dict[str, Any] | None = None
        if session_id:
            _announce_session(session_id)
            session = watch_session(deps.context.api_client, str(session_id))
            result = session.get("verify")
        else:
            result = started

        if not isinstance(result, dict):
            deps.context.print_output(session if session is not None else started)
            return INVALID_STATE

        deps.context.print_output(result, [("id", "ID"), ("result", "RESULT"), ("checked_restore_points", "POINTS"), ("checked_artifacts", "ARTIFACTS"), ("warnings", "WARNINGS"), ("errors", "ERRORS")])
        _print_corruption_marks(result.get("corrupt"))
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
