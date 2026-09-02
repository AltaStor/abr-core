# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import getpass
import sys

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.common_arguments import add_name_or_id_selector
from .base import CommandDependencies


class UserCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "user", help="Manage API users", description="User management commands", formatter_class=CompactHelpFormatter
        )
        commands = group.add_subparsers(dest="user_command", required=True)

        add = commands.add_parser(
            "add",
            help="Add a new user.",
            description='Add a new user.\n\nExamples:\n  abr user add --name jdoe --full-name "Jane Doe"',
            formatter_class=CompactHelpFormatter,
        )
        add.add_argument("--name", required=True, help="Login name")
        add.add_argument("--full-name", default="", help="Display/full name")
        add.set_defaults(handler=self.add)

        delete = commands.add_parser(
            "delete",
            help="Delete a user.",
            description="Delete a user.\n\nExamples:\n  abr user delete --name jdoe",
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(delete, name_help="Login name", id_help="User ID")
        delete.set_defaults(handler=self.delete)

        list_parser = commands.add_parser(
            "list",
            help="List users.",
            description="List users.\n\nExamples:\n  abr user list",
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
            "password": self._prompt_new_password(),
        }
        deps.context.print_output(deps.context.api_client.post("api/v1/users", json=payload))
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        user_id = self._resolve_id(deps, args)
        deps.context.print_output(deps.context.api_client.delete(f"api/v1/users/{user_id}"))
        return 0

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(
            deps.context.api_client.get("api/v1/users"),
            [("id", "ID"), ("name", "NAME"), ("fullName", "FULL NAME"), ("created_at", "CREATED")],
        )
        return 0
