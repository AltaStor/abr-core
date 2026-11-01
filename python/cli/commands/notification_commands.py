# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.secret_arguments import add_secret_argument, resolve_secret
from .base import CommandDependencies

SETTINGS_PATH = "api/v1/notifications/email"
SCRIPT_PATH = "api/v1/notifications/script"
SMTP_PASSWORD_ENV_VAR = "ABR_SMTP_PASSWORD"
OAUTH_CLIENT_SECRET_ENV_VAR = "ABR_SMTP_OAUTH_CLIENT_SECRET"
OAUTH_REFRESH_TOKEN_ENV_VAR = "ABR_SMTP_OAUTH_REFRESH_TOKEN"


def _addresses(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.replace(";", ",").split(",") if item.strip()]


def _event_ids(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.split(",") if item.strip()]


def _settings_patch(args: argparse.Namespace) -> dict[str, Any]:
    """The `set` and `test` flags as a settings change; unset flags are left out."""
    oauth = {
        key: value
        for key, value in (
            ("provider", args.oauth_provider),
            ("tenant_id", args.oauth_tenant),
            ("client_id", args.oauth_client_id),
            ("grant", args.oauth_grant),
            ("token_url", args.oauth_token_url),
            ("scope", args.oauth_scope),
            (
                "client_secret",
                resolve_secret(args.oauth_client_secret, env_var=OAUTH_CLIENT_SECRET_ENV_VAR, prompt="OAuth client secret"),
            ),
            (
                "refresh_token",
                resolve_secret(args.oauth_refresh_token, env_var=OAUTH_REFRESH_TOKEN_ENV_VAR, prompt="OAuth refresh token"),
            ),
        )
        if value is not None
    }
    smtp = {
        key: value
        for key, value in (
            ("host", args.smtp_host),
            ("port", args.smtp_port),
            ("security", args.security),
            ("verify_tls", args.verify_tls),
            ("auth", args.auth),
            ("username", args.username),
            ("password", resolve_secret(args.password, env_var=SMTP_PASSWORD_ENV_VAR, prompt="SMTP password")),
            ("timeout_seconds", args.timeout),
        )
        if value is not None
    }
    if oauth:
        smtp["oauth2"] = oauth
    sender = {key: value for key, value in (("address", args.sender), ("name", args.sender_name)) if value is not None}
    patch: dict[str, Any] = {}
    if smtp:
        patch["smtp"] = smtp
    if sender:
        patch["sender"] = sender
    return patch


def _add_settings_flags(parser: argparse.ArgumentParser) -> None:
    smtp = parser.add_argument_group("SMTP server")
    smtp.add_argument("--smtp-host", help="SMTP server host name or IP address")
    smtp.add_argument("--smtp-port", type=int, help="SMTP port: usually 587 for starttls, 465 for ssl, 25 for none")
    smtp.add_argument(
        "--security",
        choices=("starttls", "ssl", "none"),
        help="starttls (TLS after connecting), ssl (TLS from the start) or none (unencrypted)",
    )
    verify = smtp.add_mutually_exclusive_group()
    verify.add_argument("--verify-tls", dest="verify_tls", action="store_const", const=True, help="Check the server certificate (the default)")
    verify.add_argument("--no-verify-tls", dest="verify_tls", action="store_const", const=False, help="Do not check the server certificate")
    smtp.add_argument("--timeout", type=int, metavar="SECONDS", help="Give up on the server after this long, 5-300 (default 30)")
    auth = parser.add_argument_group("Authentication")
    auth.add_argument("--auth", choices=("password", "oauth2", "none"), help="How to sign in to the SMTP server")
    auth.add_argument("--username", help="password: the account to sign in as; oauth2: the mailbox the token sends as")
    add_secret_argument(auth, "--password", env_var=SMTP_PASSWORD_ENV_VAR, help_text="SMTP password (an empty value clears it)")
    auth.add_argument("--oauth-provider", choices=("microsoft", "google", "custom"), help="OAuth 2.0 provider")
    auth.add_argument("--oauth-tenant", help="Microsoft: the directory (tenant) ID or domain")
    auth.add_argument("--oauth-client-id", help="The application (client) ID")
    add_secret_argument(auth, "--oauth-client-secret", env_var=OAUTH_CLIENT_SECRET_ENV_VAR, help_text="The client secret")
    auth.add_argument(
        "--oauth-grant",
        choices=("client_credentials", "refresh_token"),
        help="client_credentials (an app sends as the mailbox; Microsoft) or refresh_token (a user consented once)",
    )
    add_secret_argument(auth, "--oauth-refresh-token", env_var=OAUTH_REFRESH_TOKEN_ENV_VAR, help_text="refresh_token grant: the refresh token")
    auth.add_argument("--oauth-token-url", help="custom provider: the token endpoint (https://)")
    auth.add_argument("--oauth-scope", help="Overrides the provider's scope for SMTP")
    sender = parser.add_argument_group("Sender")
    sender.add_argument("--from", dest="sender", metavar="ADDRESS", help="The address notifications are sent from")
    sender.add_argument("--from-name", dest="sender_name", metavar="NAME", help="The sender's display name")


def _print_settings(deps: CommandDependencies, payload: dict[str, Any]) -> None:
    if deps.context.output != "table":
        deps.context.print_output(payload)
        return
    smtp = payload.get("smtp") or {}
    oauth = smtp.get("oauth2") or {}
    sender = payload.get("sender") or {}
    auth = smtp.get("auth")
    if auth == "password":
        auth_text = f"user name and password ({smtp.get('username') or 'no user'}, password {'set' if smtp.get('password_set') else 'not set'})"
    elif auth == "oauth2":
        secret = "client secret " + ("set" if oauth.get("client_secret_set") else "not set")
        if oauth.get("grant") == "refresh_token":
            secret = "refresh token " + ("set" if oauth.get("refresh_token_set") else "not set")
        auth_text = (
            f"OAuth 2.0, {oauth.get('provider')} ({oauth.get('grant')}), mailbox {smtp.get('username') or '-'}, "
            f"client {oauth.get('client_id') or '-'}"
            + (f", tenant {oauth['tenant_id']}" if oauth.get("tenant_id") else "")
            + f", {secret}"
        )
    else:
        auth_text = "none"
    rows = [
        ("E-mail notifications", "on" if payload.get("enabled") else "off"),
        ("SMTP server", f"{smtp.get('host') or '-'}:{smtp.get('port')}"),
        ("Security", f"{smtp.get('security')}" + ("" if smtp.get("verify_tls", True) else ", certificate not verified")),
        ("Authentication", auth_text),
        ("Sender", f"{sender.get('name') or ''} <{sender.get('address') or '-'}>".strip()),
        ("Recipients", ", ".join(payload.get("recipients") or []) or "-"),
        ("Repository space threshold", f"{payload.get('repository_space_threshold_percent')}%"),
        ("Last changed", f"{payload.get('updated_at') or '-'} by {payload.get('updated_by') or '-'}"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")
    print("\nEvents:")
    for event in payload.get("events") or []:
        print(f"  [{'x' if event.get('enabled') else ' '}] {event.get('id'):<26} {event.get('name')}")
    deliveries = payload.get("deliveries") or []
    if deliveries:
        latest = deliveries[0]
        error = latest.get("error") or {}
        outcome = latest.get("status") + (f" ({error.get('code')}: {error.get('message')})" if error else "")
        print(f"\nLast delivery: {latest.get('queued_at')} {latest.get('event')} \"{latest.get('subject')}\" - {outcome}")


def _print_script(deps: CommandDependencies, payload: dict[str, Any]) -> None:
    if deps.context.output != "table":
        deps.context.print_output(payload)
        return
    file = payload.get("file") or {}
    state = "on" if payload.get("enabled") else "off"
    if not payload.get("available", True):
        state = "switched off on this appliance (ABR_NOTIFICATION_SCRIPTS=0)"
    rows = [
        ("Notification script", state),
        ("File", f"{file.get('name')} ({file.get('size')} bytes)" if file else "none uploaded"),
        ("SHA-256", file.get("sha256") or "-"),
        ("Uploaded", f"{file.get('uploaded_at') or '-'} by {file.get('uploaded_by') or '-'}"),
        ("Time limit", f"{payload.get('timeout_seconds')} s"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")
    print("\nEvents:")
    for event in payload.get("events") or []:
        print(f"  [{'x' if event.get('enabled') else ' '}] {event.get('id'):<26} {event.get('name')}")
    runs = payload.get("runs") or []
    if runs:
        print("\nLatest runs:")
    for run in runs[:10]:
        error = run.get("error") or {}
        outcome = run.get("status") + (f" ({error.get('code')}: {error.get('message')})" if error else "")
        print(f"  {run.get('queued_at')}  {run.get('event'):<26} exit {run.get('exit_code')}  {outcome}")


class NotificationCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "notification",
            help="E-mail notifications and the notification script",
            description=(
                "Notification commands: the SMTP server, how the appliance signs in to it, the sender and "
                "recipients, the events that send e-mail, a test e-mail and the delivery history, and the script "
                "the appliance can run on events (abr notification script). The web console's Settings > "
                "Notifications changes the same settings."
            ),
            formatter_class=CompactHelpFormatter,
        )
        commands = group.add_subparsers(dest="notification_command", required=True)

        show = commands.add_parser(
            "show",
            help="Show the e-mail notification settings.",
            description="Show the e-mail notification settings, the events that send e-mail and the last delivery.\n\nExamples:\n  abr notification show",
            formatter_class=CompactHelpFormatter,
        )
        show.set_defaults(handler=self.show)

        set_parser = commands.add_parser(
            "set",
            help="Change the e-mail notification settings.",
            description=(
                "Change the e-mail notification settings; options left out keep their value. The server "
                "refuses the whole change, naming every problem, when a value is invalid or when "
                "notifications are on and a setting delivery needs is missing.\n\n"
                f"Secrets are read from {SMTP_PASSWORD_ENV_VAR}, {OAUTH_CLIENT_SECRET_ENV_VAR} and "
                f"{OAUTH_REFRESH_TOKEN_ENV_VAR}, or prompted for, when their flag is given without a value.\n\n"
                "Examples:\n"
                "  abr notification set --smtp-host smtp.example.com --smtp-port 587 --security starttls \\\n"
                "      --auth password --username backup@example.com --password \\\n"
                "      --from backup@example.com --recipients ops@example.com,dba@example.com --enable\n"
                "  abr notification set --auth oauth2 --oauth-provider microsoft --oauth-tenant contoso.onmicrosoft.com \\\n"
                "      --oauth-client-id 0f1e... --oauth-client-secret --username backup@contoso.com\n"
                "  abr notification set --add-recipient oncall@example.com --remove-recipient dba@example.com\n"
                "  abr notification set --disable"
            ),
            formatter_class=CompactHelpFormatter,
        )
        state = set_parser.add_mutually_exclusive_group()
        state.add_argument("--enable", dest="enabled", action="store_const", const=True, help="Send notifications")
        state.add_argument("--disable", dest="enabled", action="store_const", const=False, help="Stop sending notifications (the settings are kept)")
        _add_settings_flags(set_parser)
        recipients = set_parser.add_argument_group("Recipients")
        recipients.add_argument("--recipients", action="append", metavar="ADDRESSES", help="The whole recipient list, comma-separated or repeated")
        recipients.add_argument("--add-recipient", action="append", metavar="ADDRESSES", help="Add these recipients")
        recipients.add_argument("--remove-recipient", action="append", metavar="ADDRESSES", help="Remove these recipients")
        set_parser.add_argument(
            "--space-threshold", type=int, metavar="PERCENT", help="repository.low_space fires at this much used space, 50-99 (default 85)"
        )
        set_parser.set_defaults(handler=self.set)

        test = commands.add_parser(
            "test",
            help="Send a test e-mail.",
            description=(
                "Send a test e-mail now and report whether the SMTP server took it, or why not (connection, "
                "TLS, sign-in, sender or recipients refused). Options test changes without saving them.\n\n"
                "Examples:\n"
                "  abr notification test\n"
                "  abr notification test --to me@example.com\n"
                "  abr notification test --smtp-port 465 --security ssl"
            ),
            formatter_class=CompactHelpFormatter,
        )
        test.add_argument("--to", action="append", metavar="ADDRESSES", help="Send to these addresses instead of the recipients")
        _add_settings_flags(test)
        test.set_defaults(handler=self.test)

        history = commands.add_parser(
            "history",
            help="List the latest e-mail deliveries.",
            description=(
                "List the latest e-mail deliveries, newest first: sent, waiting for a retry, or failed and "
                "why. Times are UTC.\n\nExamples:\n  abr notification history"
            ),
            formatter_class=CompactHelpFormatter,
        )
        history.set_defaults(handler=self.history)

        events = commands.add_parser(
            "events",
            help="The events that send e-mail",
            description="Event commands",
            formatter_class=CompactHelpFormatter,
        )
        event_commands = events.add_subparsers(dest="notification_event_command", required=True)
        events_list = event_commands.add_parser(
            "list",
            help="List the events and whether each sends e-mail.",
            description="List the events that can send e-mail, whether each does, and its default.\n\nExamples:\n  abr notification events list",
            formatter_class=CompactHelpFormatter,
        )
        events_list.set_defaults(handler=self.events_list)
        events_set = event_commands.add_parser(
            "set",
            help="Choose which events send e-mail.",
            description=(
                "Switch events on or off; the others keep their setting.\n\n"
                "Examples:\n"
                "  abr notification events set --enable job.succeeded,restore.succeeded\n"
                "  abr notification events set --disable repository.low_space"
            ),
            formatter_class=CompactHelpFormatter,
        )
        events_set.add_argument("--enable", action="append", metavar="EVENTS", help="Event IDs to switch on, comma-separated or repeated")
        events_set.add_argument("--disable", action="append", metavar="EVENTS", help="Event IDs to switch off, comma-separated or repeated")
        events_set.set_defaults(handler=self.events_set)

        script = commands.add_parser(
            "script",
            help="The script the appliance runs on events",
            description=(
                "Notification script commands. The script runs on the appliance as the server's service user, "
                "so it can do whatever that user can: upload only code you trust. See doc/Notifications.md for "
                "what it receives."
            ),
            formatter_class=CompactHelpFormatter,
        )
        script_commands = script.add_subparsers(dest="notification_script_command", required=True)
        script_show = script_commands.add_parser(
            "show",
            help="Show the script's file, settings and latest runs.",
            description="Show the uploaded script, whether it runs, on which events, and its latest runs.\n\nExamples:\n  abr notification script show",
            formatter_class=CompactHelpFormatter,
        )
        script_show.set_defaults(handler=self.script_show)
        script_upload = script_commands.add_parser(
            "upload",
            help="Upload the script, replacing any earlier one.",
            description=(
                "Upload a script of at most 256 KiB, in place of any earlier one. It must be text starting with a "
                "#! line naming an interpreter the appliance has. Uploading does not switch it on, and a "
                "replacement keeps the state of the one it replaces.\n\n"
                "Examples:\n  abr notification script upload --file ./notify-teams.sh"
            ),
            formatter_class=CompactHelpFormatter,
        )
        script_upload.add_argument("--file", required=True, metavar="PATH", help="The script file on this machine")
        script_upload.set_defaults(handler=self.script_upload)
        script_content = script_commands.add_parser(
            "content",
            help="Print the stored script.",
            description="Print the stored script, to review it. Reading it is recorded in the audit log.\n\nExamples:\n  abr notification script content > stored.sh",
            formatter_class=CompactHelpFormatter,
        )
        script_content.set_defaults(handler=self.script_content)
        script_set = script_commands.add_parser(
            "set",
            help="Change whether the script runs, on which events and for how long.",
            description=(
                "Change the script's settings; options left out keep their value. Its events are chosen apart "
                "from those that send e-mail.\n\n"
                "Examples:\n"
                "  abr notification script set --enable --timeout 120\n"
                "  abr notification script set --event-on job.succeeded --event-off repository.low_space\n"
                "  abr notification script set --disable"
            ),
            formatter_class=CompactHelpFormatter,
        )
        script_state = script_set.add_mutually_exclusive_group()
        script_state.add_argument("--enable", dest="enabled", action="store_const", const=True, help="Run the script on its events")
        script_state.add_argument("--disable", dest="enabled", action="store_const", const=False, help="Stop running it (the script is kept)")
        script_set.add_argument("--timeout", type=int, metavar="SECONDS", help="Stop a run after this long, 5-600 (default 60)")
        script_set.add_argument("--event-on", action="append", metavar="EVENTS", help="Event IDs that run the script, comma-separated or repeated")
        script_set.add_argument("--event-off", action="append", metavar="EVENTS", help="Event IDs that no longer run it, comma-separated or repeated")
        script_set.set_defaults(handler=self.script_set)
        script_remove = script_commands.add_parser(
            "remove",
            help="Delete the script.",
            description="Delete the script, which also stops it running.\n\nExamples:\n  abr notification script remove",
            formatter_class=CompactHelpFormatter,
        )
        script_remove.set_defaults(handler=self.script_remove)
        script_test = script_commands.add_parser(
            "test",
            help="Run the script now on a sample event.",
            description=(
                "Run the script now on a sample event, whether or not it is switched on, and print how it went. "
                "Exits 1 when the script fails.\n\nExamples:\n  abr notification script test"
            ),
            formatter_class=CompactHelpFormatter,
        )
        script_test.set_defaults(handler=self.script_test)

    def show(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        _print_settings(deps, deps.context.api_client.get(SETTINGS_PATH))
        return 0

    def set(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        patch = _settings_patch(args)
        if args.enabled is not None:
            patch["enabled"] = args.enabled
        if args.space_threshold is not None:
            patch["repository_space_threshold_percent"] = args.space_threshold
        if args.recipients and (args.add_recipient or args.remove_recipient):
            raise ValueError("--recipients replaces the whole list: give it alone, or use --add-recipient and --remove-recipient")
        if args.recipients:
            patch["recipients"] = _addresses(args.recipients)
        elif args.add_recipient or args.remove_recipient:
            current = list(deps.context.api_client.get(SETTINGS_PATH).get("recipients") or [])
            removed = {item.lower() for item in _addresses(args.remove_recipient)}
            added = [item for item in _addresses(args.add_recipient) if item.lower() not in {c.lower() for c in current}]
            patch["recipients"] = [item for item in [*current, *added] if item.lower() not in removed]
        if not patch:
            raise ValueError("Nothing to change: pass at least one option (abr notification set --help lists them)")
        _print_settings(deps, deps.context.api_client.put(SETTINGS_PATH, json=patch))
        return 0

    def test(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        body: dict[str, Any] = {}
        patch = _settings_patch(args)
        if patch:
            body["settings"] = patch
        if args.to:
            body["to"] = _addresses(args.to)
        result = deps.context.api_client.post(f"{SETTINGS_PATH}/test", json=body, timeout=330)
        if deps.context.output != "table":
            deps.context.print_output(result)
            return 0
        print(f"Test e-mail sent to {', '.join(result.get('recipients') or [])} in {result.get('duration_ms')} ms.")
        if result.get("refused"):
            print(f"The server refused: {', '.join(result['refused'])}")
        return 0

    def history(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"{SETTINGS_PATH}/deliveries")
        if deps.context.output == "table":
            for item in payload.get("items", []):
                error = item.get("error") or {}
                item["outcome"] = f"{error.get('code')}: {error.get('message')}" if error else ""
                item["to"] = ", ".join(item.get("recipients") or [])
        deps.context.print_output(
            payload,
            [
                ("queued_at", "QUEUED (UTC)"),
                ("event", "EVENT"),
                ("status", "STATUS"),
                ("attempts", "ATTEMPTS"),
                ("subject", "SUBJECT"),
                ("to", "TO"),
                ("outcome", "ERROR"),
            ],
        )
        return 0

    def events_list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/notifications/events")
        if deps.context.output == "table":
            for item in payload.get("items", []):
                item["state"] = "on" if item.get("enabled") else "off"
                item["default_state"] = "on" if item.get("default") else "off"
        deps.context.print_output(
            payload,
            [("id", "EVENT"), ("state", "E-MAIL"), ("default_state", "DEFAULT"), ("name", "NAME"), ("description", "WHEN")],
        )
        return 0

    def events_set(self, args: argparse.Namespace) -> int:
        enable, disable = _event_ids(args.enable), _event_ids(args.disable)
        both = sorted(set(enable) & set(disable))
        if both:
            raise ValueError(f"Both enabled and disabled: {', '.join(both)}")
        if not enable and not disable:
            raise ValueError("Nothing to change: pass --enable or --disable with event IDs (abr notification events list)")
        deps = CommandDependencies(args)
        events = {**{event_id: True for event_id in enable}, **{event_id: False for event_id in disable}}
        deps.context.api_client.put(SETTINGS_PATH, json={"events": events})
        return self.events_list(args)

    def script_show(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        _print_script(deps, deps.context.api_client.get(SCRIPT_PATH))
        return 0

    def script_upload(self, args: argparse.Namespace) -> int:
        path = Path(args.file).expanduser()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Cannot read {path} as a text file: {exc}") from exc
        deps = CommandDependencies(args)
        payload = {"name": path.name, "script_content": content}
        _print_script(deps, deps.context.api_client.put(f"{SCRIPT_PATH}/file", json=payload))
        return 0

    def script_content(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        result = deps.context.api_client.get(f"{SCRIPT_PATH}/file")
        if deps.context.output != "table":
            deps.context.print_output(result)
        else:
            print(result.get("script_content") or "", end="")
        return 0

    def script_set(self, args: argparse.Namespace) -> int:
        on, off = _event_ids(args.event_on), _event_ids(args.event_off)
        both = sorted(set(on) & set(off))
        if both:
            raise ValueError(f"Both on and off: {', '.join(both)}")
        payload: dict[str, Any] = {}
        if args.enabled is not None:
            payload["enabled"] = args.enabled
        if args.timeout is not None:
            payload["timeout_seconds"] = args.timeout
        if on or off:
            payload["events"] = {**{event_id: True for event_id in on}, **{event_id: False for event_id in off}}
        if not payload:
            raise ValueError("Nothing to change: pass --enable, --disable, --timeout, --event-on or --event-off")
        deps = CommandDependencies(args)
        _print_script(deps, deps.context.api_client.put(SCRIPT_PATH, json=payload))
        return 0

    def script_remove(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        _print_script(deps, deps.context.api_client.delete(f"{SCRIPT_PATH}/file"))
        return 0

    def script_test(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        result = deps.context.api_client.post(f"{SCRIPT_PATH}/test", json={}, timeout=660)
        failed = result.get("status") != "sent"
        if deps.context.output != "table":
            deps.context.print_output(result)
            return 1 if failed else 0
        error = result.get("error") or {}
        outcome = f"{error.get('code')}: {error.get('message')}" if failed else "succeeded"
        print(f"{result.get('script')}: {outcome} (exit code {result.get('exit_code')}, {result.get('duration_ms')} ms)")
        if result.get("output"):
            print(result["output"])
        return 1 if failed else 0
