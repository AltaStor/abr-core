# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import sys

from ..compact_help_formatter import CompactHelpFormatter
from ..errors import CliStructuredError
from ..exit_codes import INVALID_USAGE
from .base import CommandDependencies

VSPHERE_PATH = "api/v1/system/vsphere"
RECYCLE_BIN_PATH = "api/v1/system/recycle-bin"
LIB64_NOTE = (
    "A VMware VDDK library must stay in the lib64 folder of its bundle: its transport plugins are found only "
    "through that folder."
)


class SettingsCommands:
    """Server-wide settings: abr settings vsphere|recycle-bin show|set."""

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
                "path to a readable file that exports VixDiskLib_InitEx. Options left out keep their value; at "
                "least one is needed. A new server starts on vddk with the LibreVDDK library when it is installed. "
                + LIB64_NOTE + "\n\n"
                "Examples:\n"
                "  abr settings vsphere set --vddk-lib librevddk\n"
                "  abr settings vsphere set --vddk-lib /opt/vmware-vix-disklib/lib64/libvixDiskLib.so.9.1.0.0\n"
                "  abr settings vsphere set --vddk-lib default\n"
                "  abr settings vsphere set --api soap --update-existing-hypervisors --yes"
            ),
            formatter_class=CompactHelpFormatter,
        )
        set_parser.add_argument("--api", choices=("vddk", "soap"), help="vddk (the VMware VDDK) or soap (the vSphere SOAP API)")
        set_parser.add_argument(
            "--vddk-lib",
            metavar="PATH|librevddk|default",
            help="The full path of the VDDK library file; librevddk takes the LibreVDDK library installed on the backup "
            "server; default also takes LibreVDDK, and follows the VDDK directory only while LibreVDDK is not installed",
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

        recycle_bin = commands.add_parser(
            "recycle-bin",
            help="Show or change how long deleted backups stay in the recycle bin.",
            description=(
                "abr backup delete moves a chain to the recycle bin for this many days: its data stays in the "
                "repository, abr backup undelete brings it back, and then the server deletes it for good. 0 deletes "
                "at once."
            ),
            formatter_class=CompactHelpFormatter,
        )
        recycle_bin_commands = recycle_bin.add_subparsers(dest="recycle_bin_command", required=True)
        recycle_show = recycle_bin_commands.add_parser(
            "show",
            help="Show the days deleted backups stay in the recycle bin.",
            description="Show the days deleted backups stay in the recycle bin.\n\nExamples:\n  abr settings recycle-bin show",
            formatter_class=CompactHelpFormatter,
        )
        recycle_show.set_defaults(handler=self.recycle_bin_show)
        recycle_set = recycle_bin_commands.add_parser(
            "set",
            help="Set the days deleted backups stay in the recycle bin.",
            description=(
                "Set the days deleted backups stay in the recycle bin, 0 to 365; 0 deletes at once. Chains already "
                "in the recycle bin keep the date they got.\n\n"
                "Examples:\n  abr settings recycle-bin set --keep-days 14\n  abr settings recycle-bin set --keep-days 0"
            ),
            formatter_class=CompactHelpFormatter,
        )
        recycle_set.add_argument("--keep-days", required=True, type=int, metavar="DAYS", help="0 to 365; 0 turns the recycle bin off")
        recycle_set.set_defaults(handler=self.recycle_bin_set)

    def recycle_bin_show(self, args) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get(RECYCLE_BIN_PATH))
        return 0

    def recycle_bin_set(self, args) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.put(RECYCLE_BIN_PATH, json={"keep_days": args.keep_days}))
        return 0

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
            changes["vddk_library"] = self._library_value(client, args.vddk_lib)
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
    def _library_value(client, text: str) -> str | None:
        value = text.strip()
        if value.lower() == "default":
            return None
        if value.lower() == "librevddk":
            found = [item for item in (client.get(VSPHERE_PATH) or {}).get("detected_vddk_libraries") or [] if item.get("kind") == "librevddk"]
            if not found:
                raise CliStructuredError(code="invalid_argument", message="LibreVDDK is not installed on the backup server", exit_code=INVALID_USAGE)
            return str(found[0]["path"])
        return value

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
