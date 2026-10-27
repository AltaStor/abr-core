# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from urllib.parse import quote

from ..compact_help_formatter import CompactHelpFormatter
from .base import CommandDependencies

FUNCTIONS_HELP = (
    "Function IDs, comma-separated or repeated; area.* stands for all of an area's functions "
    "(abr role functions lists them)"
)


def _add_role_selector(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", help="Role ID, such as backup_operator or custom-1a2b3c4d")
    selector.add_argument("--name", help="Role name, such as \"Backup Operator\"")


def _role_path(args: argparse.Namespace) -> str:
    return f"api/v1/roles/{quote(str(args.id or args.name), safe='')}"


class RoleCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "role",
            help="Roles and their permissions",
            description=(
                "Role commands. The built-in roles cannot be changed or deleted; clone one to make a "
                "custom role. Every role allows the self-service functions (signing in and out, "
                "changing one's own password)."
            ),
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="role_command", required=True)

        list_parser = commands.add_parser(
            "list",
            help="List roles.",
            description="List the roles a user can hold, built-in and custom.\n\nExamples:\n  abr role list",
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
        _add_role_selector(info)
        info.set_defaults(handler=self.info)

        functions = commands.add_parser(
            "functions",
            help="List the product functions roles allow.",
            description="List every function a role can allow, by function ID.\n\nExamples:\n  abr role functions",
            formatter_class=CompactHelpFormatter,
        )
        functions.set_defaults(handler=self.functions)

        create = commands.add_parser(
            "create",
            help="Create a custom role.",
            description=(
                "Create a custom role that allows exactly the given functions; every other one is "
                "disabled. A role can only allow functions your own role allows.\n\n"
                "Examples:\n"
                '  abr role create --name "Night Operator" --allow "job.view,job.start,session.*"\n'
                '  abr role create --name "Job Reader" --allow job.view --allow session.view'
            ),
            formatter_class=CompactHelpFormatter,
        )
        create.add_argument("--name", required=True, help="Role name, unique among all roles")
        create.add_argument("--description", help="What the role is for")
        create.add_argument("--allow", action="append", required=True, metavar="FUNCTIONS", help=FUNCTIONS_HELP)
        create.set_defaults(handler=self.create)

        clone = commands.add_parser(
            "clone",
            help="Create a custom role from a copy of another role.",
            description=(
                "Create a custom role with the functions and description of a built-in or custom role, "
                "then change it with abr role edit.\n\n"
                "Examples:\n"
                '  abr role clone --id backup_operator --new-name "Night Operator"\n'
                '  abr role clone --name "Night Operator"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        _add_role_selector(clone)
        clone.add_argument("--new-name", help='Name of the new role (default: "<role> (copy)")')
        clone.add_argument("--description", help="Description of the new role (default: the source role's)")
        clone.set_defaults(handler=self.clone)

        edit = commands.add_parser(
            "edit",
            help="Change a custom role's name, description or permissions.",
            description=(
                "Change a custom role. Its users get the change on their next request. Your own role, "
                "a built-in role and a role allowing functions your role does not cannot be changed.\n\n"
                "Examples:\n"
                '  abr role edit --name "Night Operator" --allow backup.verify --deny job.start\n'
                '  abr role edit --name "Night Operator" --set "job.view,session.*"\n'
                '  abr role edit --id custom-1a2b3c4d --new-name "Night Shift"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        _add_role_selector(edit)
        edit.add_argument("--new-name", help="New role name")
        edit.add_argument("--description", help="New description (an empty text clears it)")
        edit.add_argument("--allow", action="append", metavar="FUNCTIONS", help=f"Also allow these. {FUNCTIONS_HELP}")
        edit.add_argument("--deny", action="append", metavar="FUNCTIONS", help=f"Disable these. {FUNCTIONS_HELP}")
        edit.add_argument(
            "--set", action="append", metavar="FUNCTIONS", help=f"Allow exactly these instead. {FUNCTIONS_HELP}"
        )
        edit.set_defaults(handler=self.edit)

        delete = commands.add_parser(
            "delete",
            help="Delete a custom role.",
            description=(
                "Delete a custom role. A role users hold cannot be deleted: give them another role "
                "first (abr user edit --role).\n\n"
                'Examples:\n  abr role delete --name "Night Operator"'
            ),
            formatter_class=CompactHelpFormatter,
        )
        _add_role_selector(delete)
        delete.set_defaults(handler=self.delete)

    @staticmethod
    def _expand(deps: CommandDependencies, values: list[str] | None) -> list[str]:
        """Function IDs from comma-separated or repeated values, with area.*
        standing for every function of that area."""
        wanted = [item.strip() for value in values or [] for item in value.split(",") if item.strip()]
        if not any(item.endswith(".*") for item in wanted):
            return list(dict.fromkeys(wanted))
        known = [str(item.get("id")) for item in deps.context.api_client.get("api/v1/functions").get("items", [])]
        expanded: list[str] = []
        for item in wanted:
            if item.endswith(".*"):
                matches = [function_id for function_id in known if function_id.startswith(item[:-1])]
                if not matches:
                    raise ValueError(f"No functions match {item}")
                expanded.extend(matches)
            else:
                expanded.append(item)
        return list(dict.fromkeys(expanded))

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/roles")
        if deps.context.output == "table":
            for item in payload.get("items", []):
                item["allowed"] = len(item.get("allowed_functions") or [])
                item["kind"] = "built-in" if item.get("built_in") else "custom"
        deps.context.print_output(
            payload,
            [
                ("id", "ID"),
                ("name", "NAME"),
                ("kind", "KIND"),
                ("allowed", "FUNCTIONS"),
                ("user_count", "USERS"),
                ("description", "DESCRIPTION"),
            ],
        )
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        role = deps.context.api_client.get(_role_path(args))
        if deps.context.output != "table":
            deps.context.print_output(role)
            return 0
        kind = "built-in" if role.get("built_in") else "custom"
        print(f"{role.get('name')} ({role.get('id')}, {kind}): {role.get('description') or '-'}")
        source = role.get("cloned_from") or {}
        if source:
            print(f"Cloned from {source.get('name')} ({source.get('id')})")
        print(f"Users holding it: {role.get('user_count', 0)}\n")
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

    def create(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = {"name": args.name, "allowed_functions": self._expand(deps, args.allow)}
        if args.description is not None:
            payload["description"] = args.description
        deps.context.print_output(deps.context.api_client.post("api/v1/roles", json=payload))
        return 0

    def clone(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = {"clone_from": args.id or args.name}
        if args.new_name is not None:
            payload["name"] = args.new_name
        if args.description is not None:
            payload["description"] = args.description
        deps.context.print_output(deps.context.api_client.post("api/v1/roles", json=payload))
        return 0

    def edit(self, args: argparse.Namespace) -> int:
        if args.set and (args.allow or args.deny):
            raise ValueError("--set replaces the whole list: give it alone, or use --allow and --deny")
        payload: dict = {
            key: value
            for key, value in (("name", args.new_name), ("description", args.description))
            if value is not None
        }
        if not payload and not (args.allow or args.deny or args.set):
            raise ValueError("Nothing to change: pass --new-name, --description, --allow, --deny or --set")
        deps = CommandDependencies(args)
        if args.set:
            payload["allowed_functions"] = self._expand(deps, args.set)
        elif args.allow or args.deny:
            current = deps.context.api_client.get(_role_path(args))
            allowed = [str(item) for item in current.get("allowed_functions") or []]
            denied = set(self._expand(deps, args.deny))
            added = [item for item in self._expand(deps, args.allow) if item not in allowed]
            payload["allowed_functions"] = [item for item in [*allowed, *added] if item not in denied]
        deps.context.print_output(deps.context.api_client.patch(_role_path(args), json=payload))
        return 0

    def delete(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.delete(_role_path(args)))
        return 0
