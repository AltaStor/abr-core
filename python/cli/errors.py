# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .exit_codes import GENERAL_ERROR, INVALID_STATE, INVALID_USAGE, NOT_FOUND


@dataclass(slots=True)
class CliError:
    code: str
    message: str
    exit_code: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), indent=2, sort_keys=True)

    def to_text(self) -> str:
        return f"Error: {self.message}"


class CliStructuredError(Exception):
    def __init__(self, *, code: str, message: str, exit_code: int, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = dict(details or {})


def cli_error_from_parse_message(message: str) -> CliError:
    details: dict[str, Any] = {}

    invalid_choice = re.search(r"argument (?P<argument>[^:]+): invalid choice: '(?P<value>[^']+)'", message)
    if invalid_choice:
        details = {
            "argument": invalid_choice.group("argument"),
            "value": invalid_choice.group("value"),
        }
        return CliError(code="invalid_argument", message=message, exit_code=INVALID_USAGE, details=details)

    unrecognized = re.search(r"unrecognized arguments: (?P<value>.+)", message)
    if unrecognized:
        details = {"argument": unrecognized.group("value")}
        return CliError(code="invalid_argument", message=message, exit_code=INVALID_USAGE, details=details)

    required = re.search(r"the following arguments are required: (?P<value>.+)", message)
    if required:
        details = {"argument": required.group("value")}
        return CliError(code="invalid_argument", message=message, exit_code=INVALID_USAGE, details=details)

    return CliError(code="invalid_argument", message=message, exit_code=INVALID_USAGE)


def cli_error_from_exception(exc: Exception) -> CliError:
    if isinstance(exc, CliStructuredError):
        return CliError(
            code=exc.code,
            message=exc.message,
            exit_code=exc.exit_code,
            details=exc.details,
        )

    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()

    if "not found" in lowered:
        return CliError(code="not_found", message=message, exit_code=NOT_FOUND)

    if any(fragment in lowered for fragment in ("does not have", "already exists", "ambiguous", "multiple ", "cancelled")):
        return CliError(code="invalid_state", message=message, exit_code=INVALID_STATE)

    if isinstance(exc, ValueError):
        return CliError(code="invalid_argument", message=message, exit_code=INVALID_USAGE)

    if isinstance(exc, TimeoutError):
        return CliError(code="timeout", message=message, exit_code=GENERAL_ERROR)

    return CliError(code="runtime_error", message=message, exit_code=GENERAL_ERROR)
