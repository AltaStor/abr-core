# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.common_arguments import add_name_or_id_selector
from ..parsing.secret_arguments import (
    INVENTORY_PASSWORD_ENV_VAR,
    INVENTORY_TOKEN_ENV_VAR,
    add_secret_argument,
    resolve_secret,
)
from .base import CommandDependencies


class InventoryCommands:
    def register(self, registry) -> None:
        inventory = registry.add_parser(
            "inventory",
            help="Inventory registration and discovery",
            description=(
                "Inventory commands.\n\n"
                "VM discovery runs automatically the first time you reference VM names "
                "in job create; run inventory rescan to re-discover on demand."
            ),
            formatter_class=CompactHelpFormatter,
        )
        commands = inventory.add_subparsers(dest="inventory_command", required=True)

        add = commands.add_parser(
            "add",
            help="Add virtualization inventory.",
            description=(
                "Add virtualization inventory.\n\n"
                "VM discovery is not required up front: it runs automatically the first "
                "time you reference VM names in job create.\n\n"
                "The password is read from "
                f"{INVENTORY_PASSWORD_ENV_VAR}, or prompted for when it is not set; avoid "
                "--password VALUE, which shows it to every local user.\n\n"
                "Examples:\n"
                '  abr inventory add --name "vCenter-Prod" --type vsphere --address vcenter.example.com '
                "--username administrator@vsphere.local\n"
                f'  {INVENTORY_PASSWORD_ENV_VAR}=... abr inventory add --name "HyperV-Host1" --type hyper-v '
                "--address 10.0.0.5 --port 5986 --username Administrator --insecure"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add.add_argument("--name", required=True, help="Inventory display name")
        add.add_argument("--type", required=True, choices=("vsphere", "hyper-v", "proxmox-ve", "xcp-ng"), help="Inventory type")
        add.add_argument("--address", required=True, help="Inventory endpoint address")
        add.add_argument("--port", type=int, help="Inventory connection port")
        add.add_argument("--username", help="Inventory username")
        add_secret_argument(add, "--password", env_var=INVENTORY_PASSWORD_ENV_VAR, help_text="Inventory password")
        add_secret_argument(add, "--token", env_var=INVENTORY_TOKEN_ENV_VAR, help_text="Inventory token")
        add.add_argument("--insecure", action="store_true", help="Disable SSL verification")
        add.set_defaults(handler=self.add)

        list_parser = commands.add_parser(
            "list",
            help="List registered inventories.",
            description="List registered inventories.\n\nExamples:\n  abr inventory list",
            formatter_class=CompactHelpFormatter,
        )
        list_parser.set_defaults(handler=self.list)

        list_vms = commands.add_parser(
            "list-vms",
            help="List VMs discovered in an inventory.",
            description=(
                "List VMs discovered in an inventory.\n\n"
                "Use the NAME or VM ID shown here as the value for --vm/--vms in job create.\n\n"
                "Examples:\n"
                '  abr inventory list-vms --name "vCenter-Prod"\n'
                "  abr inventory list-vms --id <inventory-id> --filter sql"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(list_vms, name_help="Inventory display name", id_help="Inventory ID")
        list_vms.add_argument("--filter", help="Only show VMs whose name contains this substring (case-insensitive)")
        list_vms.set_defaults(handler=self.list_vms)

        for name, help_text, handler in (
            ("info", "Show inventory details.", self.info),
            ("remove", "Remove an inventory registration.", self.remove),
            ("rescan", "Refresh discovered inventory objects.", self.rescan),
            ("test", "Validate inventory connectivity/authentication.", self.test),
        ):
            description = help_text
            if name == "rescan":
                description = (
                    "Refresh discovered inventory objects.\n\n"
                    "Job create discovers VMs automatically; run this to re-discover on demand."
                )
            description += f'\n\nExamples:\n  abr inventory {name} --name "vCenter-Prod"'
            parser = commands.add_parser(
                name,
                help=help_text,
                description=description,
                formatter_class=CompactHelpFormatter,
            )
            add_name_or_id_selector(parser, name_help="Inventory display name", id_help="Inventory ID")
            parser.set_defaults(handler=handler)

        edit = commands.add_parser(
            "edit",
            help="Edit inventory settings.",
            description=(
                "Edit inventory settings.\n\n"
                "Examples:\n"
                '  abr inventory edit --name "vCenter-Prod" --password\n'
                '  abr inventory edit --id <inventory-id> --new-name "vCenter-Prod-2" --secure'
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(edit, name_help="Inventory display name", id_help="Inventory ID")
        edit.add_argument("--new-name", help="Updated inventory display name")
        edit.add_argument("--address", help="Updated endpoint address")
        edit.add_argument("--port", type=int, help="Updated connection port")
        edit.add_argument("--username", help="Updated username")
        add_secret_argument(edit, "--password", env_var=INVENTORY_PASSWORD_ENV_VAR, help_text="Updated password")
        add_secret_argument(edit, "--token", env_var=INVENTORY_TOKEN_ENV_VAR, help_text="Updated token")
        edit.add_argument("--insecure", action="store_true", help="Disable SSL verification")
        edit.add_argument("--secure", action="store_true", help="Enable SSL verification")
        edit.set_defaults(handler=self.edit)

    @staticmethod
    def _resolve_id(deps: CommandDependencies, args: argparse.Namespace) -> str:
        if getattr(args, "id", None):
            if getattr(args, "name", None):
                items = deps.context.api_client.get("api/v1/inventories").get("items", [])
                matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
                if not matches or str(matches[0].get("id")) != str(args.id):
                    raise ValueError("Inventory selector mismatch: --name and --id refer to different records")
            return str(args.id)
        if not getattr(args, "name", None):
            raise ValueError("Inventory selection requires --name or --id")
        items = deps.context.api_client.get("api/v1/inventories").get("items", [])
        matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
        if not matches:
            raise ValueError(f"Inventory not found: {args.name}")
        if len(matches) > 1:
            raise ValueError(f"Multiple inventories matched name: {args.name}")
        return str(matches[0]["id"])

    def add(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        token = resolve_secret(args.token, env_var=INVENTORY_TOKEN_ENV_VAR, prompt="Inventory token")
        password = resolve_secret(
            args.password,
            env_var=INVENTORY_PASSWORD_ENV_VAR,
            prompt="Inventory password",
            needed=bool(args.username) and (args.type == "vsphere" or not token),
        )
        result = deps.context.api_client.post("api/v1/inventories", json={
            "name": args.name,
            "type": args.type,
            "address": args.address,
            "port": args.port,
            "username": args.username,
            "password": password,
            "token": token,
            "insecure": args.insecure,
        })
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("endpoint", "ENDPOINT"), ("status", "STATUS")])
        return 0

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get("api/v1/inventories"), [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("endpoint", "ENDPOINT"), ("status", "STATUS")])
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get(f"api/v1/inventories/{self._resolve_id(deps, args)}"))
        return 0

    def edit(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = {key: value for key, value in {
            "name": args.new_name,
            "address": args.address,
            "port": args.port,
            "username": args.username,
            "password": resolve_secret(args.password, env_var=INVENTORY_PASSWORD_ENV_VAR, prompt="Inventory password"),
            "token": resolve_secret(args.token, env_var=INVENTORY_TOKEN_ENV_VAR, prompt="Inventory token"),
        }.items() if value is not None}
        if args.insecure and args.secure:
            raise ValueError("Use only one of --insecure or --secure")
        if args.insecure:
            payload["insecure"] = True
        if args.secure:
            payload["insecure"] = False
        result = deps.context.api_client.patch(f"api/v1/inventories/{self._resolve_id(deps, args)}", json=payload)
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("endpoint", "ENDPOINT"), ("status", "STATUS")])
        return 0

    def remove(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.delete(f"api/v1/inventories/{self._resolve_id(deps, args)}"), [("id", "ID"), ("removed", "REMOVED")])
        return 0

    def rescan(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.post(f"api/v1/inventories/{self._resolve_id(deps, args)}/rescan"), [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("last_scan_at", "LAST SCAN")])
        return 0

    def test(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.post(f"api/v1/inventories/{self._resolve_id(deps, args)}/test"), [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("message", "MESSAGE")])
        return 0

    @staticmethod
    def _collect_vm_nodes(node: dict) -> list[dict]:
        found = []
        if node.get("kind") == "vm":
            found.append(node)

        for child in node.get("children") or []:
            found.extend(InventoryCommands._collect_vm_nodes(child))

        return found

    def list_vms(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        inventory_id = self._resolve_id(deps, args)
        tree = deps.context.api_client.get("api/v1/inventories/vms/tree").get(
            "nodes", []
        )
        inventory_node = next(
            (
                node
                for node in tree
                if str(node.get("inventory_id") or "") == inventory_id
            ),
            None,
        )
        if inventory_node is None:
            raise ValueError(f"Inventory not found: {inventory_id}")
        virtual_machines = self._collect_vm_nodes(inventory_node)

        if args.filter:
            needle = args.filter.lower()
            virtual_machines = [
                vm
                for vm in virtual_machines
                if needle in str(vm.get("label") or "").lower()
            ]

        deps.context.print_output(
            {"items": virtual_machines},
            [
                ("label", "NAME"),
                ("power_state", "POWER STATE"),
                ("guest_os", "GUEST OS"),
                ("cpu_count", "CPU"),
                ("memory_mb", "MEMORY"),
                ("vm_id", "VM ID"),
            ],
        )
        return 0