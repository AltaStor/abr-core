# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import sys

from ..compact_help_formatter import CompactHelpFormatter
from ..errors import CliStructuredError
from ..exit_codes import INVALID_USAGE
from .base import CommandDependencies

VSPHERE_PATH = "api/v1/system/vsphere"


class SettingsCommands:
    """Server-wide settings: abr settings vsphere show|set."""

    def register(self, registry) -> None:
        group = registry.add_parser(
            "settings",
            help="Server-wide settings",
            description="Server-wide settings commands",
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="settings_command", required=True)
        vsphere = commands.add_parser(
            "vsphere",
            help="Show or change the server-wide vSphere API and VDDK library.",
            description=(
                "Every vSphere hypervisor (inventory) without its own API or VDDK library uses these; inventory "
                "add/edit --api and --vddk-lib set its own. Sessions keep what they started with."
            ),
            formatter_class=CompactHelpFormatter,
        )
        vsphere_commands = vsphere.add_subparsers(dest="vsphere_command", required=True)
        show = vsphere_commands.add_parser(
            "show",
            help="Show the server-wide vSphere API and VDDK library.",
            description=(
                "Show the server-wide API and VDDK library, the VDDK directory a library left at its default "
                "follows, the file the library resolves to, whether this server can use each API, and which "
                "vSphere hypervisors have their own values. abr inventory info shows a hypervisor's own values, "
                "the values in effect and where each comes from.\n\n"
                "Examples:\n  abr settings vsphere show"
            ),
            formatter_class=CompactHelpFormatter,
        )
        show.set_defaults(handler=self.vsphere_show)
        set_parser = vsphere_commands.add_parser(
            "set",
            help="Change the server-wide vSphere API and VDDK library.",
            description=(
                "Change the server-wide API and VDDK library. The library is checked on the backup server: a full "
                "path to a readable file that exports VixDiskLib_InitEx. Options left out keep their value.\n\n"
                "Examples:\n"
                "  abr settings vsphere set --vddk-lib /opt/vmware-vix-disklib/lib64/libvixDiskLib.so.9.1.0.0\n"
                "  abr settings vsphere set --vddk-lib default\n"
                "  abr settings vsphere set --api soap --update-existing-hypervisors --yes"
            ),
            formatter_class=CompactHelpFormatter,
        )
        set_parser.add_argument("--api", choices=("vddk", "soap"), help="vddk (the VMware VDDK) or soap (the vSphere SOAP API)")
        set_parser.add_argument(
            "--vddk-lib",
            metavar="PATH|default",
            help="The full path of the VDDK library file; default makes it follow the VDDK directory again "
            "(<directory>/lib64/libvixDiskLib.so)",
        )
        set_parser.add_argument(
            "--update-existing-hypervisors",
            action="store_true",
            help="Remove the API and VDDK library of every vSphere hypervisor that has its own, so all of them use "
            "the server-wide values from now on; asks first, unless --yes",
        )
        set_parser.add_argument(
            "--yes", action="store_true", help="Do not ask before --update-existing-hypervisors changes hypervisors"
        )
        set_parser.set_defaults(handler=self.vsphere_set)

    def vsphere_show(self, args) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get(VSPHERE_PATH))
        return 0

    def vsphere_set(self, args) -> int:
        deps = CommandDependencies(args)
        client = deps.context.api_client
        changes: dict = {}
        if args.api:
            changes["api"] = args.api
        if args.vddk_lib is not None:
            changes["vddk_library"] = None if args.vddk_lib.strip().lower() == "default" else args.vddk_lib
        if args.update_existing_hypervisors:
            changes["apply_to_existing_hypervisors"] = True
            own = [
                str(item.get("name") or item.get("id"))
                for item in (client.get(VSPHERE_PATH) or {}).get("hypervisors") or []
                if item.get("has_own_settings")
            ]
            if own and not args.yes:
                self._confirm(own)
        if not changes:
            raise CliStructuredError(
                code="invalid_argument",
                message="Give --api, --vddk-lib or --update-existing-hypervisors",
                exit_code=INVALID_USAGE,
            )
        payload = client.put(VSPHERE_PATH, json=changes)
        if deps.context.output == "table" and isinstance(payload, dict):
            deps.context.print_output({key: value for key, value in payload.items() if key != "updated_hypervisors"})
            updated = [str(item.get("name") or item.get("id")) for item in payload.get("updated_hypervisors") or []]
            if args.update_existing_hypervisors:
                print(
                    f"Hypervisors whose own API and VDDK library were removed: {', '.join(updated)}"
                    if updated
                    else "No hypervisor had its own API or VDDK library."
                )
            return 0
        deps.context.print_output(payload)
        return 0

    @staticmethod
    def _confirm(names: list[str]) -> None:
        question = (
            f"{len(names)} hypervisor(s) have their own API or VDDK library: {', '.join(names)}. "
            "Remove them, so they use the server-wide values? [y/N] "
        )
        if not sys.stdin.isatty():
            raise CliStructuredError(
                code="confirmation_required",
                message=f"{question.split('. Remove')[0]}. Add --yes to remove them without asking",
                exit_code=INVALID_USAGE,
            )
        try:
            answer = input(question)
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise CliStructuredError(code="cancelled", message="Nothing was changed", exit_code=INVALID_USAGE)
