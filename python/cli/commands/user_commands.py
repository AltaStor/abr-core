# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import getpass
import sys

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.common_arguments import add_name_or_id_selector
from .base import CommandDependencies

ROLE_HELP = (
    "Role: administrator, backup_administrator, backup_operator, restore_operator or auditor "
    "(abr role list describes them)"
)
USER_COLUMNS = [
    ("id", "ID"),
    ("name", "NAME"),
    ("fullName", "FULL NAME"),
    ("role", "ROLE"),
    ("enabled", "ENABLED"),
    ("created_at", "CREATED"),
]


class UserCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "user", help="Manage API users", description="User management commands", formatter_class=CompactHelpFormatter
        )
        commands = group.add_subparsers(dest="user_command", required=True)

        add = commands.add_parser(
            "add",
            help="Add a new user.",
            description=(
                "Add a new user with one role.\n\n"
                "Examples:\n"
                '  abr user add --name jdoe --full-name "Jane Doe" --role backup_operator'
            ),
            formatter_class=CompactHelpFormatter,
        )
        add.add_argument("--name", required=True, help="Login name")
        add.add_argument("--full-name", default="", help="Display/full name")
        add.add_argument("--role", required=True, help=ROLE_HELP)
        add.set_defaults(handler=self.add)

        edit = commands.add_parser(
            "edit",
            help="Change a user's role, full name, or enabled state.",
            description=(
                "Change a user's role, full name, or enabled state. Disabling a user ends their "
                "sessions. The last enabled Administrator cannot be demoted or disabled.\n\n"
                "Examples:\n"
                "  abr user edit --name jdoe --role restore_operator\n"
                "  abr user edit --name jdoe --disable"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(edit, name_help="Login name", id_help="User ID")
        edit.add_argument("--full-name", help="New display/full name")
        edit.add_argument("--role", help=ROLE_HELP)
        state = edit.add_mutually_exclusive_group()
        state.add_argument("--enable", dest="enabled", action="store_const", const=True, help="Let the user sign in")
        state.add_argument(
            "--disable", dest="enabled", action="store_const", const=False, help="Stop the user signing in"
        )
        edit.set_defaults(handler=self.edit)

        info = commands.add_parser(
            "info",
            help="Show a user.",
            description="Show a user.\n\nExamples:\n  abr user info --name jdoe",
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(info, name_help="Login name", id_help="User ID")
        info.set_defaults(handler=self.info)

        delete = commands.add_parser(
            "delete",
            help="Delete a user.",
            description=(
                "Delete a user. The last enabled Administrator cannot be deleted.\n\n"
                "Examples:\n  abr user delete --name jdoe"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(delete, name_help="Login name", id_help="User ID")
        delete.set_defaults(handler=self.delete)

        list_parser = commands.add_parser(
            "list",
            help="List users.",
            description="List users with their roles.\n\nExamples:\n  abr user list",
            formatter_class=CompactHelpFormatter,
        )
        list_parser.set_defaults(handler=self.list)

    @staticmethod
    def _resolve_id(deps: CommandDependencies, args: argparse.Namespace) -> str:
        if getattr(args, "id", None):
            if getattr(args, "name", None):
                items = deps.context.api_client.get("api/v1/users").get("items", [])
                matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]

                if not matches or str(matches[0].get("id")) != str(args.id):
                    raise ValueError("User selector mismatch: --name and --id refer to different records")
            return str(args.id)
        if not getattr(args, "name", None):
            raise ValueError("User selection requires --name or --id")

        items = deps.context.api_client.get("api/v1/users").get("items", [])
        matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
        if not matches:
            raise ValueError(f"User not found: {args.name}")

        if len(matches) > 1:
            raise ValueError(f"Multiple users matched name: {args.name}")
        return str(matches[0]["id"])

    @staticmethod
    def _prompt_new_password() -> str:
        while True:
            password = getpass.getpass("Set password for new user: ")
            if len(password) < 8:
                print("Password must be at least 8 characters", file=sys.stderr)
                continue

            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Passwords didn't match", file=sys.stderr)
                continue

            return password

    def add(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = {
            "name": args.name,
            "fullName": args.full_name,
            "role": args.role,
            "password": self._prompt_new_password(),
        }
        deps.context.print_output(deps.context.api_client.post("api/v1/users", json=payload))
        return 0

    def edit(self, args: argparse.Namespace) -> int:
        payload = {
            key: value
            for key, value in (("fullName", args.full_name), ("role", args.role), ("enabled", args.enabled))
            if value is not None
        }
        if not payload:
            raise ValueError("Nothing to change: pass --role, --full-name, --enable or --disable")
        deps = CommandDependencies(args)
        user_id = self._resolve_id(deps, args)
        deps.context.print_output(deps.context.api_client.patch(f"api/v1/users/{user_id}", json=payload))
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get(f"api/v1/users/{self._resolve_id(deps, args)}"))
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        user_id = self._resolve_id(deps, args)
        deps.context.print_output(deps.context.api_client.delete(f"api/v1/users/{user_id}"))
        return 0

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get("api/v1/users"), USER_COLUMNS)
        return 0
