# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import sys

import requests

from python.cli.core.logger_conf import setup_logging

from .command_not_implemented_error import CommandNotImplementedError
from .errors import CliError, cli_error_from_exception
from .exit_codes import GENERAL_ERROR, INVALID_USAGE, UNAUTHORIZED
from .parsing.parser_builder import AltaStorParserBuilder

logger = logging.getLogger(__name__)


def _scan_log_level(argv: list[str] | None) -> str:
    tokens = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(tokens):
        if token == "--log-level" and index + 1 < len(tokens):
            return tokens[index + 1]
    return "info"


def _looks_like_commandless_invocation(argv: list[str] | None) -> bool:
    tokens = list(sys.argv[1:] if argv is None else argv)
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {"--base-url", "--output", "--log-level"}:
            skip_next = True
            continue
        if token == "--help":
            return False
        if not token.startswith("-"):
            return False
    return len(tokens) == 0 or True


def main(argv: list[str] | None = None) -> int:
    setup_logging(
        component="cli", console_stream="stderr", console_level=_scan_log_level(argv)
    )
    logger.debug("CLI session started with arguments: %s", argv)
    parser = AltaStorParserBuilder().build()
    if _looks_like_commandless_invocation(argv):
        if parser._requested_output(argv) == "json":
            parser.emit_cli_error(
                CliError(
                    code="invalid_argument",
                    message="A command is required.",
                    exit_code=INVALID_USAGE,
                    details={"argument": "command"},
                ),
                argv,
            )
        parser.print_help(sys.stderr)
        return INVALID_USAGE
    args = parser.parse_args(argv)
    setup_logging(
        component="cli", console_stream="stderr", console_level=args.log_level
    )
    try:
        handler = getattr(args, "handler", None)
        if handler is None:
            parser.emit_cli_error(
                CliError(
                    code="invalid_argument",
                    message="No command handler was selected",
                    exit_code=INVALID_USAGE,
                ),
                argv,
            )
        return handler(args)
    except CommandNotImplementedError as exc:
        logger.error("Command not implemented: %s", exc)
        parser.emit_cli_error(
            CliError(code="not_implemented", message=str(exc), exit_code=GENERAL_ERROR),
            argv,
        )
    except requests.RequestException as exc:
        message = str(exc)
        cli_error: CliError | None = None
        if exc.response is not None:
            try:
                body = exc.response.json()
            except ValueError:
                body = exc.response.text
            error = body.get("error") if isinstance(body, dict) else None
            detail = (
                error
                if isinstance(error, dict)
                else body.get("detail")
                if isinstance(body, dict)
                else None
            )
            if isinstance(detail, dict):
                cli_error = CliError(
                    code=str(detail.get("code") or "runtime_error"),
                    message=str(detail.get("message") or message),
                    exit_code=4
                    if exc.response.status_code in {409, 503}
                    else 3
                    if exc.response.status_code == 404
                    else INVALID_USAGE
                    if exc.response.status_code in {400, 422}
                    else GENERAL_ERROR,
                    details=detail.get("details")
                    if isinstance(detail.get("details"), dict)
                    else {
                        key: value
                        for key, value in detail.items()
                        if key not in {"code", "message"}
                    },
                )
                message = cli_error.message
            else:
                message = f"{message}: {body}"
        logger.error("API Request failed: %s", message)
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 401 and (cli_error is None or cli_error.code == "unauthorized"):
            parser.emit_cli_error(
                CliError(
                    code="unauthorized",
                    message="Not authenticated (or session expired). Run `abr auth login`.",
                    exit_code=UNAUTHORIZED,
                ),
                argv,
            )
        if cli_error is not None:
            parser.emit_cli_error(cli_error, argv)
        if status_code == 404:
            parser.emit_cli_error(
                CliError(
                    code="not_found", message=f"Request failed: {message}", exit_code=3
                ),
                argv,
            )
        if status_code == 409:
            parser.emit_cli_error(
                CliError(
                    code="invalid_state",
                    message=f"Request failed: {message}",
                    exit_code=4,
                ),
                argv,
            )
        parser.emit_cli_error(
            CliError(
                code="runtime_error",
                message=f"Request failed: {message}",
                exit_code=GENERAL_ERROR,
            ),
            argv,
        )
    except Exception as exc:
        cli_error = cli_error_from_exception(exc)
        if cli_error.code == "runtime_error":
            logger.exception("CLI command failed")
        else:
            logger.error("CLI command failed: %s", cli_error.message)
        parser.emit_cli_error(cli_error, argv)
    return 0
