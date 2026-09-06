# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import sys

from .errors import CliError, cli_error_from_parse_message
from .exit_codes import INVALID_USAGE


GLOBAL_HELP_SECTION = """Global options:
  --base-url BASE_URL            AltaStor API base URL (default: BACKUP_SERVER_URL or https://127.0.0.1)
  --output {json,table}          Output format (default: table)
  --log-level {debug,info,warn,error}
                                 Console log verbosity (default: info)
"""


def normalize_global_cli_options(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        argv = sys.argv[1:]

    tokens = list(argv)
    leading: list[str] = []
    trailing: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--output", "--base-url", "--log-level"}:
            trailing.append(token)
            if index + 1 < len(tokens):
                trailing.append(tokens[index + 1])
                index += 2
                continue
        leading.append(token)
        index += 1
    return [*trailing, *leading]


class AltaStorArgumentParser(argparse.ArgumentParser):
    def _raw_args(self, args: list[str] | None = None) -> list[str]:
        return list(sys.argv[1:] if args is None else args)

    def _requested_output(self, args: list[str] | None = None) -> str:
        tokens = self._raw_args(args)
        for index, token in enumerate(tokens):
            if token == "--output" and index + 1 < len(tokens):
                return tokens[index + 1]
        return "table"

    def emit_cli_error(self, cli_error: CliError, args: list[str] | None = None) -> None:
        if self._requested_output(args) == "json":
            self._print_message(f"{cli_error.to_json()}\n", sys.stderr)
        else:
            self._print_message(f"{cli_error.to_text()}\n", sys.stderr)
        raise SystemExit(cli_error.exit_code)

    def parse_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        return super().parse_args(normalize_global_cli_options(args), namespace)

    def parse_known_args(
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        return super().parse_known_args(normalize_global_cli_options(args), namespace)

    def error(self, message: str) -> None:
        self.emit_cli_error(cli_error_from_parse_message(message))

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            super().exit(status=status, message=message)
            return
        if message is None:
            raise SystemExit(status)
        self.emit_cli_error(CliError(code="invalid_argument", message=message.strip(), exit_code=INVALID_USAGE))

    def format_help(self) -> str:
        help_text = super().format_help()
        if "--log-level" in help_text and "--output" in help_text:
            return help_text
        return f"{help_text.rstrip()}\n\n{GLOBAL_HELP_SECTION}"
