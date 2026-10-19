# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import os

from ..altastor_argument_parser import AltaStorArgumentParser
from ..cli_command_registry import CliCommandRegistry
from ..command_functions import COMMAND_FUNCTIONS
from ..commands import (
    AuditCommands,
    AuthCommands,
    BackupCommands,
    InventoryCommands,
    JobCommands,
    RepositoryCommands,
    RestoreCommands,
    RoleCommands,
    SessionCommands,
    SystemCommands,
    UserCommands,
)
from ..compact_help_formatter import CompactHelpFormatter

DEFAULT_BASE_URL = os.getenv("BACKUP_SERVER_URL", "https://127.0.0.1")
CLI_VERSION = "0.1.0"
PRODUCT_NAME = "AltaStor Backup & Replication"
LOG_LEVEL_CHOICES = ("debug", "info", "warn", "error")


class AltaStorParserBuilder:
    def build(self) -> argparse.ArgumentParser:
        parser = AltaStorArgumentParser(prog="abr", description="AltaStor CLI", formatter_class=CompactHelpFormatter)
        parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="AltaStor API base URL (default: BACKUP_SERVER_URL or https://127.0.0.1)")
        parser.add_argument("--output", choices=("json", "table"), default="table", help="Output format for commands that support tabular display (default: table)")
        parser.add_argument("--log-level", choices=LOG_LEVEL_CHOICES, default="info", help="Console log verbosity (default: info)")
        subparsers = parser.add_subparsers(dest="command_group", required=True)
        registry = CliCommandRegistry(subparsers)
        AuthCommands().register(registry)
        UserCommands().register(registry)
        RoleCommands().register(registry)
        AuditCommands().register(registry)
        RepositoryCommands().register(registry)
        InventoryCommands().register(registry)
        JobCommands().register(registry)
        SessionCommands().register(registry)
        BackupCommands().register(registry)
        RestoreCommands().register(registry)
        SystemCommands(product_name=PRODUCT_NAME, cli_version=CLI_VERSION, default_base_url=DEFAULT_BASE_URL).register(registry)
        _bind_command_functions(parser)
        return parser


def command_parsers(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()):
    """Each leaf command's words and parser: ("job", "start"), <parser>."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from command_parsers(sub, (*prefix, name))
            return
    if prefix:
        yield prefix, parser


def _bind_command_functions(parser: argparse.ArgumentParser) -> None:
    """Give every command the function ID it executes: in its help, and to the
    client, which names the command in the server's audit log."""
    commands = {" ".join(words): sub for words, sub in command_parsers(parser)}
    unmapped = sorted(set(commands) - set(COMMAND_FUNCTIONS))
    stale = sorted(set(COMMAND_FUNCTIONS) - set(commands))
    if unmapped or stale:
        raise RuntimeError(f"Commands without a function ID: {unmapped}; function IDs without a command: {stale}")
    for command, sub in commands.items():
        function_id = COMMAND_FUNCTIONS[command]
        sub.set_defaults(command=command, function_id=function_id)
        note = f"Permission: {function_id} (the signed-in user's role must allow it)."
        sub.epilog = f"{sub.epilog}\n\n{note}" if sub.epilog else note


def build_parser() -> argparse.ArgumentParser:
    return AltaStorParserBuilder().build()
