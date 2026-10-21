# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import getpass
import os
import sys

from .. import token_store
from ..cli_context import TOKEN_ENV_VAR
from ..compact_help_formatter import CompactHelpFormatter
from ..errors import CliStructuredError
from ..exit_codes import INVALID_USAGE
from .base import CommandDependencies

USERNAME_ENV_VAR = "ABR_USERNAME"
PASSWORD_ENV_VAR = "ABR_PASSWORD"


def _prompt(read, prompt: str, *, missing: str) -> str:
    try:
        return read(prompt)
    except EOFError as exc:
        raise CliStructuredError(code="invalid_argument", message=missing, exit_code=INVALID_USAGE) from exc


class AuthCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "auth",
            help="Authenticate against the AltaStor API",
            description="Authentication commands",
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="auth_command", required=True)

        login = commands.add_parser(
            "login",
            help="Log in and cache a session token for this server.",
            description=(
                "Log in and cache a session token locally, scoped to --base-url.\n\n"
                "The password is never accepted as a command-line flag; set "
                f"{PASSWORD_ENV_VAR} to script this non-interactively, or omit it to be "
                f"prompted. --name falls back to {USERNAME_ENV_VAR} if omitted.\n\n"
                "Examples:\n"
                "  abr auth login --name admin\n"
                f"  {PASSWORD_ENV_VAR}=secret abr auth login --name admin --print-token"
            ),
            formatter_class=CompactHelpFormatter,
        )
        login.add_argument("--name", help=f"User name (default: {USERNAME_ENV_VAR})")
        login.add_argument(
            "--print-token",
            action="store_true",
            help=f"Print only the raw token to stdout (for `export {TOKEN_ENV_VAR}=$(...)`), no other output.",
        )
        login.set_defaults(handler=self.login)

        logout = commands.add_parser(
            "logout",
            help="Invalidate the current session and clear the cached token.",
            description=(
                "Invalidate the current session server-side and clear the locally cached token.\n\n"
                "Examples:\n"
                "  abr auth logout"
            ),
            formatter_class=CompactHelpFormatter,
        )
        logout.set_defaults(handler=self.logout)

        change_password = commands.add_parser(
            "change-password",
            help="Change your own account password.",
            description=(
                "Change your own account password. Prompts for the current password and the new "
                f"one (with confirmation); the current password also falls back to {PASSWORD_ENV_VAR}. "
                "Invalidates every other active session for this account; the one making this "
                "call stays valid.\n\n"
                "Examples:\n"
                "  abr auth change-password"
            ),
            formatter_class=CompactHelpFormatter,
        )
        change_password.set_defaults(handler=self.change_password)

        whoami = commands.add_parser(
            "whoami",
            help="Show the signed-in user, their role and what it allows.",
            description=(
                "Show the signed-in user, their role, and the function IDs that role allows.\n\n"
                "Examples:\n"
                "  abr auth whoami\n"
                "  abr auth whoami --output json"
            ),
            formatter_class=CompactHelpFormatter,
        )
        whoami.set_defaults(handler=self.whoami)

    def login(self, args) -> int:
        deps = CommandDependencies(args)
        name = args.name or os.environ.get(USERNAME_ENV_VAR)
        if not name:
            name = _prompt(
                input,
                "User name: ",
                missing=f"No user name provided; pass --name, set {USERNAME_ENV_VAR} or run interactively",
            )
        password = os.environ.get(PASSWORD_ENV_VAR) or _prompt(
            getpass.getpass,
            "Password: ",
            missing=f"No password provided; set {PASSWORD_ENV_VAR} or run interactively",
        )

        result = deps.context.api_client.post("api/v1/auth/login", json={"name": name, "password": password})
        token_store.save_token(deps.context.base_url, result["token"], result["expires_at"])

        role = result["user"].get("role")
        confirmation = (
            f"Logged in as {result['user']['name']}{f' ({role})' if role else ''} "
            f"(session expires {result['expires_at']})."
        )
        if getattr(args, "print_token", False):
            print(confirmation, file=sys.stderr)
            print(result["token"])
        else:
            print(confirmation)

        if result["user"].get("passwordPolicyExempt"):
            print(
                "WARNING: this is the default/bootstrap password and does not meet the minimum "
                "security policy. Change it now: abr auth change-password",
                file=sys.stderr,
            )
        return 0

    def logout(self, args) -> int:
        deps = CommandDependencies(args)
        try:
            if deps.context.api_client.token:
                deps.context.api_client.post("api/v1/auth/logout")
        finally:
            token_store.clear_token(deps.context.base_url)
        print("Logged out.")
        return 0

    def whoami(self, args) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/auth/me")
        if deps.context.output != "table":
            deps.context.print_output(payload)
            return 0
        user = payload.get("user") or {}
        role = payload.get("role") or {}
        full_name = f" ({user['fullName']})" if user.get("fullName") else ""
        print(f"User:  {user.get('name')}{full_name}")
        print(f"Role:  {role.get('name') or user.get('role') or '-'}")
        print("Allowed functions:")
        for function_id in payload.get("permissions") or []:
            print(f"  {function_id}")
        return 0

    def change_password(self, args) -> int:
        deps = CommandDependencies(args)
        current_password = os.environ.get(PASSWORD_ENV_VAR) or getpass.getpass("Current password: ")
        new_password = self._prompt_new_password()

        deps.context.api_client.post(
            "api/v1/auth/change-password",
            json={"current_password": current_password, "new_password": new_password},
        )
        print("Password changed. Every other active session for this account was logged out.")
        return 0

    @staticmethod
    def _prompt_new_password() -> str:
        while True:
            password = getpass.getpass("New password: ")
            if len(password) < 8:
                print("Password must be at least 8 characters", file=sys.stderr)
                continue

            confirm = getpass.getpass("Confirm new password: ")
            if password != confirm:
                print("Passwords didn't match", file=sys.stderr)
                continue

            return password
