# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import os

from ..altastor_argument_parser import AltaStorArgumentParser
from ..cli_command_registry import CliCommandRegistry
from ..commands import (
    AuthCommands,
    BackupCommands,
    InventoryCommands,
    JobCommands,
    RepositoryCommands,
    RestoreCommands,
    SessionCommands,
    SystemCommands,
    UserCommands,
)
from ..compact_help_formatter import CompactHelpFormatter

DEFAULT_BASE_URL = os.getenv("BACKUP_SERVER_URL", "https://127.0.0.1")
CLI_VERSION = "0.1.0"
PRODUCT_NAME = "AltaStor Backup & Replication"


class AltaStorParserBuilder:
    def build(self) -> argparse.ArgumentParser:
        parser = AltaStorArgumentParser(prog="abr", description="AltaStor CLI", formatter_class=CompactHelpFormatter)
        parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="AltaStor API base URL (default: BACKUP_SERVER_URL or https://127.0.0.1)")
        parser.add_argument("--output", choices=("json", "table"), default="table", help="Output format for commands that support tabular display (default: table)")
        parser.add_argument("--log-level", choices=("debug", "info", "warn", "error"), default="info", help="Console log verbosity (default: info)")
        subparsers = parser.add_subparsers(dest="command_group", required=True)
        registry = CliCommandRegistry(subparsers)
        AuthCommands().register(registry)
        UserCommands().register(registry)
        RepositoryCommands().register(registry)
        InventoryCommands().register(registry)
        JobCommands().register(registry)
        SessionCommands().register(registry)
        BackupCommands().register(registry)
        RestoreCommands().register(registry)
        SystemCommands(product_name=PRODUCT_NAME, cli_version=CLI_VERSION, default_base_url=DEFAULT_BASE_URL).register(registry)
        return parser


def build_parser() -> argparse.ArgumentParser:
    return AltaStorParserBuilder().build()
