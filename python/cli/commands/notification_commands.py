# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.secret_arguments import add_secret_argument, resolve_secret
from .base import CommandDependencies

SETTINGS_PATH = "api/v1/notifications/email"
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


class NotificationCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "notification",
            help="E-mail notifications",
            description=(
                "E-mail notification commands: the SMTP server, how the appliance signs in to it, the "
                "sender and recipients, the events that send e-mail, a test e-mail and the delivery history. "
                "The web console's Settings > Notifications changes the same settings."
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
