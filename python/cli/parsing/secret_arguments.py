# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import getpass
import os
import sys

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


_LAST_READ = {"without_terminal": False}


def _read_without_terminal(_prompt: str, _stream: object = None) -> str:
    _LAST_READ["without_terminal"] = True
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def read_secret(prompt: str) -> str:
    _LAST_READ["without_terminal"] = False
    fallback = getpass.fallback_getpass
    getpass.fallback_getpass = _read_without_terminal
    try:
        return getpass.getpass(prompt)
    finally:
        getpass.fallback_getpass = fallback


def confirms(prompt: str, secret: str) -> bool:
    return _LAST_READ["without_terminal"] or read_secret(prompt) == secret


MIN_PASSWORD_LENGTH = 8
STDIN_HINT = "run the command in a terminal, or give the password on its stdin (one line)"


def read_password(prompt: str, *, missing: str) -> str:
    try:
        return read_secret(prompt)
    except EOFError as exc:
        raise CliStructuredError(code="invalid_argument", message=missing, exit_code=INVALID_USAGE) from exc


def read_new_password(prompt: str, confirm_prompt: str, *, missing: str) -> str:
    while True:
        password = read_password(prompt, missing=missing)
        without_terminal = _LAST_READ["without_terminal"]
        if len(password) < MIN_PASSWORD_LENGTH:
            if without_terminal:
                raise CliStructuredError(
                    code="invalid_argument",
                    message=f"The password must be at least {MIN_PASSWORD_LENGTH} characters",
                    exit_code=INVALID_USAGE,
                )
            print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
            continue
        try:
            confirmed = confirms(confirm_prompt, password)
        except EOFError as exc:
            raise CliStructuredError(code="invalid_argument", message=missing, exit_code=INVALID_USAGE) from exc
        if not confirmed:
            print("Passwords didn't match", file=sys.stderr)
            continue
        return password


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
        secret = read_secret(f"{prompt}: ")
        if secret and confirm and not confirms(f"Confirm {prompt[:1].lower()}{prompt[1:]}: ", secret):
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
