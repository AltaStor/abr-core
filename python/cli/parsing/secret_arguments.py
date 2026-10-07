# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import getpass
import os

from ..errors import CliStructuredError
from ..exit_codes import INVALID_USAGE

ASK = "\0ask"

INVENTORY_PASSWORD_ENV_VAR = "ABR_INVENTORY_PASSWORD"
INVENTORY_TOKEN_ENV_VAR = "ABR_INVENTORY_TOKEN"
REPO_PASSWORD_ENV_VAR = "ABR_REPO_PASSWORD"
REPO_SECRET_KEY_ENV_VAR = "ABR_REPO_SECRET_KEY"
ENCRYPTION_KEY_ENV_VAR = "ABR_ENCRYPTION_KEY"


def add_secret_argument(
    parser: argparse.ArgumentParser,
    flag: str,
    *,
    env_var: str,
    help_text: str,
    dest: str | None = None,
) -> None:
    parser.add_argument(
        flag,
        dest=dest or flag.lstrip("-").replace("-", "_"),
        nargs="?",
        const=ASK,
        metavar="VALUE",
        help=(
            f"{help_text}. Give the flag without a value to read it from {env_var} or be prompted. "
            "A value typed after the flag is visible to every local user (ps, /proc) while the "
            f"command runs and stays in the shell history; use {env_var} or the prompt instead."
        ),
    )


def resolve_secret(
    value: str | None,
    *,
    env_var: str,
    prompt: str,
    needed: bool = False,
    confirm: bool = False,
) -> str | None:
    if value is not None and value != ASK:
        return value
    if value is None and not needed:
        return None
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    try:
        secret = getpass.getpass(f"{prompt}: ")
        if secret and confirm and getpass.getpass(f"Confirm {prompt[:1].lower()}{prompt[1:]}: ") != secret:
            raise CliStructuredError(
                code="invalid_argument",
                message=f"The two {prompt.lower()} entries did not match",
                exit_code=INVALID_USAGE,
            )
    except EOFError as exc:
        raise CliStructuredError(
            code="invalid_argument",
            message=f"No {prompt.lower()} provided; set {env_var} or run interactively",
            exit_code=INVALID_USAGE,
        ) from exc
    if not secret:
        raise CliStructuredError(
            code="invalid_argument",
            message=f"No {prompt.lower()} entered; set {env_var} or type it at the prompt",
            exit_code=INVALID_USAGE,
        )
    return secret
