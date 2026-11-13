# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import getpass
import os
import sys

from ..compact_help_formatter import CompactHelpFormatter
from ..errors import CliStructuredError
from ..exit_codes import INVALID_STATE, INVALID_USAGE
from ..models.views import human_size
from .base import CommandDependencies

PASSWORD_ENV_VAR = "ABR_EXPORT_PASSWORD"
CATALOG_TABLE_LABELS = {
    "hypervisors": "inventories",
}


def _read_password(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError as exc:
        raise CliStructuredError(
            code="invalid_argument",
            message=f"No password provided; set {PASSWORD_ENV_VAR} or run interactively",
            exit_code=INVALID_USAGE,
        ) from exc


def _label_counts(counts: dict) -> str:
    return ", ".join(
        f"{CATALOG_TABLE_LABELS.get(name, name)}={count}"
        for name, count in counts.items()
    )


class SystemCommands:
    def __init__(
        self, *, product_name: str, cli_version: str, default_base_url: str
    ) -> None:
        self.product_name = product_name
        self.cli_version = cli_version
        self.default_base_url = default_base_url

    def register(self, registry) -> None:
        group = registry.add_parser(
            "system", help="System information", description="System commands", formatter_class=CompactHelpFormatter
        )
        commands = group.add_subparsers(dest="system_command", required=True)
        version = commands.add_parser(
            "version",
            help="Show installed product version.",
            description="Show installed product version.\n\nExamples:\n  abr system version",
            formatter_class=CompactHelpFormatter,
        )
        version.set_defaults(handler=self.version)
        health = commands.add_parser(
            "health",
            help="Show component health.",
            description="Show component health status.\n\nExamples:\n  abr system health",
            formatter_class=CompactHelpFormatter,
        )
        health.set_defaults(handler=self.health)

        clock = commands.add_parser(
            "clock",
            help="Show or confirm the server clock that retention measures from.",
            description=(
                "Retention deletes nothing after the server clock jumps forward by more than "
                "BACKUPSERVER_CLOCK_JUMP_HOURS, while running or between two runs, until an "
                "Administrator confirms the time is right."
            ),
            formatter_class=CompactHelpFormatter,
        )
        clock_commands = clock.add_subparsers(dest="clock_command", required=True)
        clock_show = clock_commands.add_parser(
            "show",
            help="Show the server time and whether retention is blocked by a clock jump.",
            description=(
                "Show the server time, the last time it was known to be right, and whether retention is "
                "blocked by a forward clock jump. Exits with 4 while it is blocked.\n\n"
                "Examples:\n  abr system clock show"
            ),
            formatter_class=CompactHelpFormatter,
        )
        clock_show.set_defaults(handler=self.clock_show)
        clock_confirm = clock_commands.add_parser(
            "confirm",
            help="Confirm the server time is right so retention deletes expired restore points again.",
            description=(
                "Confirm the current server time is right. Retention takes the restore points' age from it "
                "again, so expired points go on its next pass. Check the time first: a point removed on a "
                "wrong clock cannot be brought back.\n\n"
                "Examples:\n  abr system clock confirm"
            ),
            formatter_class=CompactHelpFormatter,
        )
        clock_confirm.set_defaults(handler=self.clock_confirm)

        vsphere = commands.add_parser(
            "vsphere",
            help="Show or change the server-wide vSphere transport and VDDK library.",
            description=(
                "Every vSphere inventory without its own transport or VDDK library uses these (inventory "
                "add/edit --transport and --vddk-library set its own). Sessions keep what they started with."
            ),
            formatter_class=CompactHelpFormatter,
        )
        vsphere_commands = vsphere.add_subparsers(dest="vsphere_command", required=True)
        vsphere_show = vsphere_commands.add_parser(
            "show",
            help="Show the server-wide vSphere transport and VDDK library.",
            description=(
                "Show the server-wide transport and VDDK library, the VDDK directory a library left at its "
                "default follows, the file the library resolves to, whether this server can use each transport, "
                "and which vSphere inventories have their own values.\n\n"
                "Examples:\n  abr system vsphere show"
            ),
            formatter_class=CompactHelpFormatter,
        )
        vsphere_show.set_defaults(handler=self.vsphere_show)
        vsphere_set = vsphere_commands.add_parser(
            "set",
            help="Change the server-wide vSphere transport and VDDK library.",
            description=(
                "Change the server-wide transport and VDDK library. The library is checked on the backup server: "
                "a full path to a readable file that exports VixDiskLib_InitEx. Options left out keep their value.\n\n"
                "Examples:\n"
                "  abr system vsphere set --vddk-library /opt/vmware-vix-disklib/lib64/libvixDiskLib.so.9.1.0.0\n"
                "  abr system vsphere set --vddk-library default\n"
                "  abr system vsphere set --transport soap --update-all-inventories"
            ),
            formatter_class=CompactHelpFormatter,
        )
        vsphere_set.add_argument(
            "--transport",
            choices=("vddk", "soap"),
            help="vddk (the VMware VDDK) or soap (the vSphere SOAP API)",
        )
        vsphere_set.add_argument(
            "--vddk-library",
            metavar="PATH|default",
            help="The full path of the VDDK library file; default makes it follow the VDDK directory again "
            "(<directory>/lib64/libvixDiskLib.so)",
        )
        vsphere_set.add_argument(
            "--update-all-inventories",
            action="store_true",
            help="Remove the transport and VDDK library of every vSphere inventory that has its own, so all of them "
            "use the server-wide values from now on",
        )
        vsphere_set.set_defaults(handler=self.vsphere_set)

        export = commands.add_parser(
            "export",
            help="Export DB catalog + configuration to an encrypted archive",
            description=(
                "Export the server's state (backups, repositories, inventories, jobs, settings) to a single AES-256-encrypted file. The server "
                f"performs the encryption; set {PASSWORD_ENV_VAR} to script this "
                "non-interactively, or omit it to be prompted\n\n"
                "Examples:\n"
                "  abr system export --file /var/backups/altastor-export.enc"
            ),
            formatter_class=CompactHelpFormatter,
        )
        export.add_argument(
            "--file",
            required=True,
            help="Path to write the encrypted export archive to",
        )
        export.set_defaults(handler=self.export)

        import_cmd = commands.add_parser(
            "import",
            help="Import an encrypted archive created by 'abr system export'",
            description=(
                "Restore DB catalog + configuration onto this (normally freshly installed) server\n\n"
                "Examples:\n"
                "  abr system import --file /var/backups/altastor-export.enc"
            ),
            formatter_class=CompactHelpFormatter,
        )
        import_cmd.add_argument(
            "--file", required=True, help="Path to the encrypted export archive"
        )
        import_cmd.set_defaults(handler=self.import_)

        support_bundle = commands.add_parser(
            "get-support-bundle",
            help="Collect log files, configuration, and system info into a ZIP archive.",
            description=(
                "Collect application log files, sanitized configuration, and system "
                "information into a local ZIP support bundle for troubleshooting.\n\n"
                "Examples:\n"
                "  abr system get-support-bundle\n"
                "  abr system get-support-bundle --file /tmp/support-bundle.zip"
            ),
            formatter_class=CompactHelpFormatter,
        )
        support_bundle.add_argument(
            "--file",
            help=(
                "Path to write the support bundle ZIP to (default: "
                "support-bundle-<timestamp>.zip in the current directory, or in "
                "your home directory when the current one is not writable)"
            ),
        )
        support_bundle.set_defaults(handler=self.get_support_bundle)

    def version(self, args) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get("api/v1/system/version"))
        return 0

    def health(self, args) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/system/health")
        deps.context.print_output(
            payload,
            [("status", "STATUS"), ("components", "COMPONENTS")],
        )
        if isinstance(payload, dict) and payload.get("status") not in (None, "ok"):
            return INVALID_STATE
        return 0

    def clock_show(self, args) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/system/clock")
        deps.context.print_output(payload)
        if isinstance(payload, dict) and payload.get("blocked"):
            return INVALID_STATE
        return 0

    def clock_confirm(self, args) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.post("api/v1/system/clock/confirm")
        if deps.context.output == "table" and isinstance(payload, dict):
            if payload.get("was_blocked"):
                print(f"Server time {payload.get('server_time')} confirmed; retention deletes expired restore points again.")
            else:
                print(f"Server time {payload.get('server_time')} confirmed; retention was not blocked.")
            return 0
        deps.context.print_output(payload)
        return 0

    def vsphere_show(self, args) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get("api/v1/system/vsphere"))
        return 0

    def vsphere_set(self, args) -> int:
        deps = CommandDependencies(args)
        changes: dict = {}
        if args.transport:
            changes["transport"] = args.transport
        if args.vddk_library is not None:
            changes["vddk_library"] = None if args.vddk_library.strip().lower() == "default" else args.vddk_library
        if args.update_all_inventories:
            changes["apply_to_existing_inventories"] = True
        if not changes:
            raise CliStructuredError(
                code="invalid_argument",
                message="Give --transport, --vddk-library or --update-all-inventories",
                exit_code=INVALID_USAGE,
            )
        deps.context.print_output(deps.context.api_client.put("api/v1/system/vsphere", json=changes))
        return 0

    def export(self, args) -> int:
        deps = CommandDependencies(args)

        password = os.environ.get(PASSWORD_ENV_VAR) or self._prompt_new_password()
        result = deps.system.export_catalog(file=args.file, password=password)

        counts = result.get("manifest", {}).get("counts", {})
        configuration_keys = result.get("manifest", {}).get("configuration_keys", [])

        print(f"Wrote encrypted export to {result['file']}")
        if counts:
            print("Included: " + _label_counts(counts))

        if configuration_keys:
            print(
                f"Included server configuration ({len(configuration_keys)} settings): "
                + ", ".join(configuration_keys)
            )

        print(
            "Keep this password safe and separate from the archive: it protects "
            "repository/inventory credentials and server configuration stored in the export. "
            "It CAN`T be recovered"
        )
        return 0

    def import_(self, args) -> int:
        deps = CommandDependencies(args)
        password = os.environ.get(PASSWORD_ENV_VAR) or _read_password(
            "Export archive password: "
        )

        try:
            result = deps.system.import_catalog(file=args.file, password=password)
        except FileNotFoundError:
            print(f"No such file: {args.file}", file=sys.stderr)
            return 1

        restored = result.get("restored", {})

        print("Import complete. Restored: " + _label_counts(restored))

        replaced_users = result.get("replaced_users") or []
        if replaced_users:
            print(
                f"User(s) {', '.join(replaced_users)} on this server were replaced by the ones from the export, "
                "and their sessions ended: log in again with the password they had on the exported server"
            )

        recovered_configuration_file = result.get("recovered_configuration_file")
        if recovered_configuration_file:
            print(
                f"Recovered server configuration written to {recovered_configuration_file}. "
                "This is NOT applied automatically: review it (storage paths and ports may "
                "differ on this host) and merge what's appropriate into "
                "/etc/default/altastor-backup-server, then restart the service"
            )

        print(
            "Next: run `abr repo verify` for each repository, then `abr restore list` to confirm restore points are visible"
        )
        return 0

    def get_support_bundle(self, args) -> int:
        deps = CommandDependencies(args)
        result = deps.system.get_support_bundle(file=args.file)
        manifest = result.get("manifest", {})

        summary = f"Wrote support bundle to {result['file']}"
        details = [
            part
            for part in (
                human_size(result.get("size_bytes")) if result.get("size_bytes") else None,
                f"{len(manifest.get('files') or [])} files" if manifest.get("files") else None,
            )
            if part
        ]
        if details:
            summary += f" ({', '.join(details)})"
        print(summary)

        for warning in manifest.get("warnings") or []:
            print(f"Warning: {warning}", file=sys.stderr)

        return 0

    def _prompt_new_password(self) -> str:
        while True:
            password = _read_password("Set export password: ")
            if len(password) < 8:
                print("Password must be at least 8 characters", file=sys.stderr)
                continue

            confirm = _read_password("Confirm export password: ")

            if password != confirm:
                print("Passwords didn`t match", file=sys.stderr)
                continue

            return password
