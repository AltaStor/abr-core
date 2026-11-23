# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from ..compact_help_formatter import CompactHelpFormatter
from ..parsing.secret_arguments import add_secret_argument, resolve_secret
from .base import CommandDependencies

SETTINGS_PATH = "api/v1/notifications/email"
NOTIFICATIONS_PATH = "api/v1/notifications"
SMTP_PASSWORD_ENV_VAR = "ABR_SMTP_PASSWORD"
NTFY_ACCESS_TOKEN_ENV_VAR = "ABR_NTFY_ACCESS_TOKEN"


@dataclass(frozen=True, slots=True)
class _Platform:
    """A messaging platform as the CLI offers it."""

    name: str
    service: str
    # Where a secret flag without a value reads the secret from.
    env_var: str
    # What its main secret is called: "bot token", "webhook URL", "topic URL".
    what: str
    about: str
    # The flag that sets it.
    flag: str


PLATFORMS = {
    platform.name: platform
    for platform in (
        _Platform(
            "telegram", "Telegram", "ABR_TELEGRAM_BOT_TOKEN", "bot token",
            "A bot made with @BotFather posts to up to ten chats; add the bot to each group or channel first.",
            "--bot-token",
        ),
        _Platform(
            "slack", "Slack", "ABR_SLACK_WEBHOOK_URL", "webhook URL",
            "A Slack app's incoming webhook posts to one channel; its URL, https://hooks.slack.com/services/..., is "
            "the setting.",
            "--webhook-url",
        ),
        _Platform(
            "discord", "Discord", "ABR_DISCORD_WEBHOOK_URL", "webhook URL",
            "A Discord channel's webhook (Edit Channel > Integrations > Webhooks) posts to it; its URL, "
            "https://discord.com/api/webhooks/..., is the setting.",
            "--webhook-url",
        ),
        _Platform(
            "ntfy", "ntfy", "ABR_NTFY_TOPIC_URL", "topic URL",
            "Messages are published to a topic on ntfy.sh or on a server of your own: https://ntfy.sh/<topic> or "
            "https://<your server>/<topic>. On ntfy.sh anyone who knows the topic can read it, so pick a long, "
            "unguessable name. A server with access control also takes an access token (--access-token).",
            "--topic-url",
        ),
    )
}


def _addresses(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.replace(";", ",").split(",") if item.strip()]


def _event_ids(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.split(",") if item.strip()]


def _settings_patch(args: argparse.Namespace) -> dict[str, Any]:
    """The `set` and `test` flags as a settings change; unset flags are left out."""
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
    auth.add_argument("--auth", choices=("password", "none"), help="How to sign in to the SMTP server")
    auth.add_argument("--username", help="password: the account to sign in as")
    add_secret_argument(auth, "--password", env_var=SMTP_PASSWORD_ENV_VAR, help_text="SMTP password (an empty value clears it)")
    sender = parser.add_argument_group("Sender")
    sender.add_argument("--from", dest="sender", metavar="ADDRESS", help="The address notifications are sent from")
    sender.add_argument("--from-name", dest="sender_name", metavar="NAME", help="The sender's display name: one line, at most 100 characters")


def _print_settings(deps: CommandDependencies, payload: dict[str, Any]) -> None:
    if deps.context.output != "table":
        deps.context.print_output(payload)
        return
    smtp = payload.get("smtp") or {}
    sender = payload.get("sender") or {}
    auth = smtp.get("auth")
    if auth == "password":
        auth_text = f"user name and password ({smtp.get('username') or 'no user'}, password {'set' if smtp.get('password_set') else 'not set'})"
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


def _print_platform(deps: CommandDependencies, payload: dict[str, Any], platform: _Platform) -> None:
    """A messaging platform's settings as a table."""
    if deps.context.output != "table":
        deps.context.print_output(payload)
        return
    if not payload.get("added"):
        print(f"{platform.service} is not set up: abr notification {platform.name} set adds it.")
        return
    if platform.name == "telegram":
        token = "set" if payload.get("bot_token_set") else "not set"
        settings = [
            ("Bot token", token + (f" (bot {payload['bot_id']})" if payload.get("bot_id") else "")),
            ("Chats", ", ".join(payload.get("chat_ids") or []) or "-"),
        ]
    else:
        url = f"set ({payload.get('webhook_host')})" if payload.get("webhook_url_set") else "not set"
        settings = [(platform.what[:1].upper() + platform.what[1:], url)]
        if platform.name == "ntfy":
            settings.append(("Access token", "set" if payload.get("access_token_set") else "none"))
    apprise = payload.get("apprise") or {}
    rows = [
        ("Notifications", "on" if payload.get("enabled") else "off"),
        *settings,
        ("Apprise", apprise.get("version") or "-" if apprise.get("available") else "not installed (the apprise package)"),
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


def _chat_ids(values: list[str] | None) -> list[str]:
    return [item.strip() for value in values or [] for item in value.replace(";", ",").split(",") if item.strip()]


def _events_patch(on: list[str] | None, off: list[str] | None) -> dict[str, bool]:
    enable, disable = _event_ids(on), _event_ids(off)
    both = sorted(set(enable) & set(disable))
    if both:
        raise ValueError(f"Both on and off: {', '.join(both)}")
    return {**{event_id: True for event_id in enable}, **{event_id: False for event_id in disable}}


def _platform_patch(args: argparse.Namespace, platform: _Platform) -> dict[str, Any]:
    """The settings flags of `set` and `test` as a change; unset flags are left out."""
    patch: dict[str, Any] = {}
    if platform.name == "telegram":
        token = resolve_secret(args.bot_token, env_var=platform.env_var, prompt="Telegram bot token")
        if token is not None:
            patch["bot_token"] = token
        if args.chats:
            patch["chat_ids"] = _chat_ids(args.chats)
        return patch
    url = resolve_secret(args.webhook_url, env_var=platform.env_var, prompt=f"{platform.service} {platform.what}")
    if url is not None:
        patch["webhook_url"] = url
    if platform.name == "ntfy":
        token = resolve_secret(args.access_token, env_var=NTFY_ACCESS_TOKEN_ENV_VAR, prompt="ntfy access token")
        if token is not None:
            patch["access_token"] = token
    return patch


def _add_platform_flags(parser: argparse.ArgumentParser, platform: _Platform) -> None:
    if platform.name == "telegram":
        add_secret_argument(
            parser, "--bot-token", env_var=platform.env_var, help_text="The bot's token from @BotFather (an empty value clears it)"
        )
        parser.add_argument(
            "--chats", action="append", metavar="IDS",
            help="The whole list of chats, comma-separated or repeated: -1001234567890 or @channelname (at most 10)",
        )
    else:
        add_secret_argument(
            parser, platform.flag, env_var=platform.env_var, dest="webhook_url",
            help_text=f"The {platform.service} {platform.what} (an empty value clears it)",
        )
    if platform.name == "ntfy":
        add_secret_argument(
            parser, "--access-token", env_var=NTFY_ACCESS_TOKEN_ENV_VAR,
            help_text="An access token (tk_...) for a server with access control (an empty value clears it)",
        )


class NotificationCommands:
    def register(self, registry) -> None:
        group = registry.add_parser(
            "notification",
            help="E-mail and messaging (Telegram, Slack, Discord, ntfy) notifications",
            description=(
                "Notification commands: the SMTP server, how the appliance signs in to it, the sender and "
                "recipients, the events that send e-mail, a test e-mail and the delivery history; and the "
                "messaging platforms, Telegram, Slack, Discord and ntfy, sent through Apprise (abr "
                "notification platforms lists them; abr notification telegram|slack|discord|ntfy). The web "
                "console's Settings > Notifications changes the same settings."
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
                f"The password is read from {SMTP_PASSWORD_ENV_VAR}, or prompted for, when --password is given "
                "without a value.\n\n"
                "Examples:\n"
                "  abr notification set --smtp-host smtp.example.com --smtp-port 587 --security starttls \\\n"
                "      --auth password --username backup@example.com --password \\\n"
                "      --from backup@example.com --recipients ops@example.com,dba@example.com --enable\n"
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

        platforms = commands.add_parser(
            "platforms",
            help="List the messaging platforms and which are set up.",
            description=(
                "List the messaging platforms (Telegram, Slack, Discord, ntfy), whether each is set up "
                "and on, and its last delivery; and whether Apprise, which sends them, is installed.\n\n"
                "Examples:\n  abr notification platforms"
            ),
            formatter_class=CompactHelpFormatter,
        )
        platforms.set_defaults(handler=self.platforms)
        for platform in PLATFORMS.values():
            self._register_platform(commands, platform)

    def _register_platform(self, commands, platform: _Platform) -> None:
        name, service = platform.name, platform.service
        parser = commands.add_parser(
            name,
            help=f"{service} messages, sent through Apprise",
            description=(
                f"{service} notification commands. Messages go out through Apprise (/usr/bin/apprise, the apprise "
                f"package), with their own choice of events. {platform.about}"
            ),
            formatter_class=CompactHelpFormatter,
        )
        actions = parser.add_subparsers(dest=f"notification_{name}_command", required=True)
        secret_flag = platform.flag
        examples = {
            "show": f"  abr notification {name} show",
            "set": (
                f"  abr notification {name} set {secret_flag}"
                + (" --chats -1001234567890" if name == "telegram" else "")
                + f" --enable\n  abr notification {name} set --event-on job.succeeded --event-off repository.low_space"
            ),
            "test": f"  abr notification {name} test" + ("\n  abr notification telegram test --chats 123456789" if name == "telegram" else ""),
            "history": f"  abr notification {name} history",
            "remove": f"  abr notification {name} remove",
        }
        descriptions = {
            "show": f"Show the {service} settings, whether Apprise is installed, the events that send a message and the last delivery.",
            "set": (
                f"Change the {service} settings, which sets {service} up when it was not; options left out keep "
                "their value. The server refuses the whole change, naming every problem, when a value is invalid or "
                f"when notifications are on and a setting is missing or Apprise cannot read it. {secret_flag} without "
                f"a value reads {platform.env_var}, or prompts."
            ),
            "test": (
                f"Send a test message now and report whether {service} took it, or why not. Options test changes "
                "without saving them. Exits 1 when it was not sent."
            ),
            "history": f"List the latest {service} deliveries, newest first: sent, waiting for a retry, or failed and why. Times are UTC.",
            "remove": f"Remove {service}: its settings and {platform.what} are deleted and it sends nothing. Its delivery history stays.",
        }
        helps = {
            "show": f"Show the {service} settings.",
            "set": f"Set up or change {service}.",
            "test": f"Send a test {service} message.",
            "history": f"List the latest {service} deliveries.",
            "remove": f"Remove {service}.",
        }
        handlers = {
            "show": self.platform_show,
            "set": self.platform_set,
            "test": self.platform_test,
            "history": self.platform_history,
            "remove": self.platform_remove,
        }
        for action, handler in handlers.items():
            sub = actions.add_parser(
                action,
                help=helps[action],
                description=f"{descriptions[action]}\n\nExamples:\n{examples[action]}",
                formatter_class=CompactHelpFormatter,
            )
            if action == "set":
                state = sub.add_mutually_exclusive_group()
                state.add_argument("--enable", dest="enabled", action="store_const", const=True, help="Send messages")
                state.add_argument("--disable", dest="enabled", action="store_const", const=False, help="Stop sending messages (the settings are kept)")
            if action in ("set", "test"):
                _add_platform_flags(sub, platform)
            if action == "set":
                sub.add_argument("--event-on", action="append", metavar="EVENTS", help="Event IDs that send a message, comma-separated or repeated")
                sub.add_argument("--event-off", action="append", metavar="EVENTS", help="Event IDs that no longer send one, comma-separated or repeated")
            sub.set_defaults(handler=handler, platform=name)

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

    def platforms(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"{NOTIFICATIONS_PATH}/messaging")
        if deps.context.output != "table":
            deps.context.print_output(payload)
            return 0
        for item in payload.get("items", []):
            item["state"] = ("on" if item.get("enabled") else "off") if item.get("added") else "not set up"
            latest = (item.get("deliveries") or [{}])[0]
            item["last"] = f"{latest.get('queued_at')} {latest.get('status')}" if latest else ""
        deps.context.print_output(payload, [("id", "PLATFORM"), ("name", "NAME"), ("state", "STATE"), ("last", "LAST DELIVERY")])
        apprise = payload.get("apprise") or {}
        if not apprise.get("available"):
            print("\nApprise is not installed (the apprise package): nothing can be sent.")
        return 0

    @staticmethod
    def _platform(args: argparse.Namespace) -> tuple[str, _Platform]:
        return f"{NOTIFICATIONS_PATH}/{args.platform}", PLATFORMS[args.platform]

    def platform_show(self, args: argparse.Namespace) -> int:
        path, platform = self._platform(args)
        deps = CommandDependencies(args)
        _print_platform(deps, deps.context.api_client.get(path), platform)
        return 0

    def platform_set(self, args: argparse.Namespace) -> int:
        path, platform = self._platform(args)
        patch = _platform_patch(args, platform)
        if args.enabled is not None:
            patch["enabled"] = args.enabled
        events = _events_patch(args.event_on, args.event_off)
        if events:
            patch["events"] = events
        if not patch:
            raise ValueError(f"Nothing to change: pass at least one option (abr notification {platform.name} set --help lists them)")
        deps = CommandDependencies(args)
        _print_platform(deps, deps.context.api_client.put(path, json=patch), platform)
        return 0

    def platform_test(self, args: argparse.Namespace) -> int:
        path, platform = self._platform(args)
        patch = _platform_patch(args, platform)
        deps = CommandDependencies(args)
        result = deps.context.api_client.post(f"{path}/test", json={"settings": patch} if patch else {}, timeout=700)
        if deps.context.output != "table":
            deps.context.print_output(result)
            return 0
        where = ", ".join(result.get("delivered_to") or []) if platform.name == "telegram" else f"the {platform.what}"
        print(f"Test message sent to {where} in {result.get('duration_ms')} ms.")
        return 0

    def platform_remove(self, args: argparse.Namespace) -> int:
        path, platform = self._platform(args)
        deps = CommandDependencies(args)
        payload = deps.context.api_client.delete(path)
        if deps.context.output != "table":
            deps.context.print_output(payload)
        else:
            print(f"{platform.service} removed.")
        return 0

    def platform_history(self, args: argparse.Namespace) -> int:
        path, _platform = self._platform(args)
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get(f"{path}/deliveries")
        if deps.context.output == "table":
            for item in payload.get("items", []):
                error = item.get("error") or {}
                item["outcome"] = f"{error.get('code')}: {error.get('message')}" if error else ""
                item["to"] = ", ".join(item.get("delivered_to") or [])
        deps.context.print_output(
            payload,
            [
                ("queued_at", "QUEUED (UTC)"),
                ("event", "EVENT"),
                ("status", "STATUS"),
                ("attempts", "ATTEMPTS"),
                ("subject", "SUBJECT"),
                ("to", "SENT TO"),
                ("outcome", "ERROR"),
            ],
        )
        return 0
