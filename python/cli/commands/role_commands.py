# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse

from ..compact_help_formatter import CompactHelpFormatter
from .base import CommandDependencies


class RoleCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "role",
            help="Built-in roles and their permissions",
            description="Role commands. The built-in roles cannot be changed or deleted.",
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="role_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List roles.",
            description="List the roles a user can hold.\n\nExamples:\n  abr role list",
            formatter_class=CompactHelpFormatter,
        )
        list_parser.set_defaults(handler=self.list)

        info = commands.add_parser(
            "info",
            help="Show a role's permission on every function.",
            description=(
                "Show a role's permission, allowed or disabled, on every function.\n\n"
                "Examples:\n"
                "  abr role info --id backup_operator\n"
                '  abr role info --name "Restore Operator"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        selector = info.add_mutually_exclusive_group(required=True)
        selector.add_argument("--id", help="Role ID, such as backup_operator")
        selector.add_argument("--name", help="Role name, such as \"Backup Operator\"")
        info.set_defaults(handler=self.info)

        functions = commands.add_parser(
            "functions",
            help="List the product functions roles allow.",
            description="List every function a role can allow, by function ID.\n\nExamples:\n  abr role functions",
            formatter_class=CompactHelpFormatter,
        )
        functions.set_defaults(handler=self.functions)

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/roles")
        if deps.context.output == "table":
            for item in payload.get("items", []):
                item["allowed"] = len(item.get("allowed_functions") or [])
        deps.context.print_output(
            payload, [("id", "ID"), ("name", "NAME"), ("allowed", "FUNCTIONS"), ("description", "DESCRIPTION")]
        )
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        role = deps.context.api_client.get(f"api/v1/roles/{args.id or args.name}")
        if deps.context.output != "table":
            deps.context.print_output(role)
            return 0
        print(f"{role.get('name')} ({role.get('id')}): {role.get('description')}\n")
        rows = [
            {"function_id": item.get("function_id"), "permission": "allowed" if item.get("allowed") else "disabled"}
            for item in role.get("permissions") or []
        ]
        deps.context.print_output(rows, [("function_id", "FUNCTION"), ("permission", "PERMISSION")])
        return 0

    def functions(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(
            deps.context.api_client.get("api/v1/functions"),
            [("id", "FUNCTION"), ("category", "CATEGORY"), ("name", "DESCRIPTION")],
        )
        return 0
