# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Messaging platforms: Telegram, Slack, Discord and ntfy notifications, sent
through Apprise (``apprise_runner``).

Each is a channel of the notification service with its own settings record in
the catalog's ``system_state`` (``telegram_notifications``,
``slack_notifications``, ``discord_notifications``, ``ntfy_notifications``),
its own choice of events and its own delivery history. A platform is ``added``
once its settings are first saved, and removing it puts its record back to the
defaults; the history stays. The Telegram bot token, the webhook and topic URLs
and the ntfy access token are kept encrypted with the server's credential key
and never leave the server: the API says only whether each one is set.

Telegram: a bot made with @BotFather posts to up to ten chats (a group, a
channel, a private chat). Each chat is sent to on its own, so a retry goes only
to the chats that did not get the message. Slack and Discord: a channel's
incoming webhook, whose URL is the setting. ntfy: a topic's URL on ntfy.sh or a
server of one's own, and an access token for a server with access control.

As channels of the notification service, ``TELEGRAM``, ``SLACK``, ``DISCORD``
and ``NTFY`` offer ``wants``, ``describe`` and ``send``, as ``email_channel``
does. ``PLATFORMS`` lists them by ID.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
from typing import Any
from urllib.parse import urlsplit

from python.srv.core.credential_encryption import decrypt_secret, encrypt_secret, is_encrypted
from python.srv.core.mongo_db_wrapper import load_system_state, persist_system_state
from python.srv.core.utils import utc_now

from . import apprise_runner
from .api_errors import api_error
from .apprise_runner import Target
from .notification_events import DeliveryError, Notification, event_chosen, merged_selection

LOGGER = logging.getLogger(__name__)

PRODUCT_NAME = "AltaStor Backup & Replication"
MAX_CHATS = 10
MAX_URL_LENGTH = 2048
# 123456789:AAE..., as @BotFather gives it.
BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{20,80}$")
# A chat's number (a group's or channel's is negative) or a public channel's
# @name, with a forum topic after a colon.
CHAT_ID_PATTERN = re.compile(r"^(-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})(:[0-9]{1,10})?$")
# Spaces and control characters: a URL is one line, so it cannot add another
# line to the configuration Apprise reads.
_NOT_IN_URL = re.compile(r"[\x00-\x20\x7f]")
DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
NTFY_TOPIC_PATH = re.compile(r"/[A-Za-z0-9_-]{1,64}/?")
# ntfy access tokens: tk_ and 29 letters and digits; a little room either way.
NTFY_TOKEN = re.compile(r"tk_[A-Za-z0-9]{8,64}")

_lock = threading.RLock()


def _secret(value: Any) -> str:
    """A stored secret in the clear; '' when it can't be decrypted."""
    plain = decrypt_secret(value) if value else ""
    return "" if is_encrypted(plain) else str(plain or "")


def _kind(notification: Notification) -> str:
    """Apprise's notification type, which some platforms show (a colour, an emoji)."""
    if notification.severity == "error":
        return "failure"
    if notification.severity == "warning":
        return "warning"
    return "success" if notification.event.endswith(".succeeded") else "info"


def _title(notification: Notification) -> str:
    prefix = {"error": "[AltaStor] FAILED: ", "warning": "[AltaStor] WARNING: "}.get(notification.severity, "[AltaStor] ")
    return prefix + notification.subject


def _footer() -> str:
    try:
        from . import abr_v41

        version = abr_v41.product_version()
    except Exception:
        version = "unknown version"
    return f"\n\nSent by {PRODUCT_NAME} {version} on {socket.gethostname()}."


def _refuse(problems: dict[str, str]):
    count = len(problems)
    first = next(iter(problems.items()))
    return api_error(
        422,
        "invalid_notification_settings",
        f"{first[0]}: {first[1]}" + (f" (and {count - 1} more)" if count > 1 else ""),
        fields=problems,
    )


class _AppriseChannel:
    """What the platforms have in common: the settings record, its
    validation, and sending to each target through Apprise."""

    ID = ""
    LABEL = ""
    SERVICE = ""
    STATE_ID = ""
    # A message that failed for a reason that can pass is tried again after these.
    RETRY_DELAYS_SECONDS = (30, 120)
    REPEAT_AFTER_RESTART = True
    FIELDS: tuple[str, ...] = ()

    # ------------------------------------------------- what each one adds --

    def _defaults(self) -> dict[str, Any]:
        raise NotImplementedError

    def _apply(self, out: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> None:
        raise NotImplementedError

    def _problems(self, settings: dict[str, Any], complete: bool) -> dict[str, str]:
        raise NotImplementedError

    def _targets(self, settings: dict[str, Any], kind: str = "info") -> list[Target]:
        """Where a message of `kind` goes (info, success, warning, failure)."""
        raise NotImplementedError

    def _check_target(self, settings: dict[str, Any]) -> tuple[str, Target]:
        """The field Apprise's verdict belongs to, and one target for Apprise
        to read the settings from."""
        raise NotImplementedError

    def _unreadable(self, settings: dict[str, Any], reason: str) -> str:
        raise NotImplementedError

    def _dto(self, settings: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _recipients(self, settings: dict[str, Any]) -> list[str]:
        return []

    # ------------------------------------------------------------ settings --

    def default_settings(self) -> dict[str, Any]:
        return {
            "id": self.STATE_ID,
            "added": False,
            "enabled": False,
            "events": {},
            "updated_at": None,
            "updated_by": None,
            **self._defaults(),
        }

    def load_settings(self) -> dict[str, Any]:
        """The stored settings over the defaults, secrets still encrypted."""
        try:
            stored = load_system_state(self.STATE_ID) or {}
        except Exception as exc:
            LOGGER.warning("Could not read the %s notification settings from the catalog: %s", self.LABEL, exc)
            stored = {}
        stored.pop("_id", None)
        return {**self.default_settings(), **stored}

    def settings_dto(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """The settings as the API shows them: secrets only as whether they are set."""
        current = settings or self.load_settings()
        return {
            "id": self.ID,
            "name": self.LABEL,
            "added": bool(current.get("added")),
            "enabled": bool(current.get("enabled")),
            **self._dto(current),
            "updated_at": current.get("updated_at"),
            "updated_by": current.get("updated_by"),
            "apprise": apprise_runner.availability(),
        }

    def _patched(self, current: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> dict[str, Any]:
        """`current` with `patch` laid over it. A secret left out (or null) is
        kept; an empty one clears it. Shape errors land in `problems`."""
        out = {**self.default_settings(), **current}
        if patch.get("enabled") is not None:
            if not isinstance(patch["enabled"], bool):
                problems["enabled"] = "Must be true or false"
            else:
                out["enabled"] = patch["enabled"]
        if patch.get("events") is not None:
            out["events"] = merged_selection(out.get("events"), patch["events"], problems)
        self._apply(out, patch, problems)
        return out

    def validate(self, settings: dict[str, Any], *, complete: bool) -> dict[str, str]:
        """Problems with `settings`, by field. Values are always checked;
        `complete` (switched on, or a test) also asks for every setting a
        message needs, and for Apprise to read them."""
        problems = self._problems(settings, complete)
        if complete and not problems:
            if apprise_runner.program() is None:
                problems["enabled"] = str(apprise_runner.missing_error())
            else:
                field, target = self._check_target(settings)
                try:
                    reason = apprise_runner.check(target)
                except DeliveryError as exc:
                    reason = str(exc)
                if reason:
                    problems[field] = self._unreadable(settings, reason)
        return problems

    def update_settings(self, patch: dict[str, Any], *, updated_by: str | None) -> tuple[dict[str, Any], list[str]]:
        """Save `patch` over the stored settings, or refuse the whole of it
        (422 invalid_notification_settings, with every problem by field).
        Returns the new DTO and the fields that changed, for the audit log."""
        with _lock:
            current = self.load_settings()
            problems: dict[str, str] = {}
            updated = self._patched(current, patch, problems)
            if not problems:
                problems.update(self.validate(updated, complete=bool(updated["enabled"])))
            if problems:
                raise _refuse(problems)
            changed = [key for key in self.FIELDS if updated.get(key) != current.get(key)]
            if not current.get("added"):
                changed.insert(0, "added")
            updated["added"] = True
            updated["updated_at"] = utc_now()
            updated["updated_by"] = updated_by
            persist_system_state(updated)
            return self.settings_dto(updated), changed

    def remove(self, *, updated_by: str | None) -> dict[str, Any]:
        """Take the platform off: its settings go back to the defaults, its
        secret with them, and it sends nothing. Its delivery history stays."""
        with _lock:
            cleared = self.default_settings()
            cleared.update(updated_at=utc_now(), updated_by=updated_by)
            persist_system_state(cleared)
            return self.settings_dto(cleared)

    def settings_for_test(self, patch: dict[str, Any] | None) -> dict[str, Any]:
        """The stored settings with unsaved changes from the form laid over
        them, complete enough to send, or 422 with what is missing."""
        problems: dict[str, str] = {}
        candidate = self._patched(self.load_settings(), patch or {}, problems)
        if not problems:
            problems.update(self.validate(candidate, complete=True))
        if problems:
            raise _refuse(problems)
        return candidate

    # ------------------------------------------------------------- channel --

    def wants(self, notification: Notification) -> bool:
        settings = self.load_settings()
        return bool(settings.get("enabled")) and event_chosen(settings.get("events"), notification.event)

    def describe(self, notification: Notification, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """What the delivery history shows about a message before it is sent."""
        current = settings or self.load_settings()
        described: dict[str, Any] = {"subject": _title(notification)}
        recipients = self._recipients(current)
        if recipients:
            described["recipients"] = recipients
        return described

    def send(self, notification: Notification, entry: dict[str, Any]) -> dict[str, Any]:
        """Deliver `notification` with the settings as they are now, or raise
        DeliveryError. A retry goes only to the targets that did not get it."""
        settings = self.load_settings()
        recipients = self._recipients(settings)
        if recipients:
            entry["recipients"] = recipients
        delivered = self.deliver(
            settings, entry["subject"], notification.body.rstrip() + _footer(), _kind(notification),
            already=entry.get("delivered_to") or [],
        )
        return {"delivered_to": delivered}

    def deliver(self, settings: dict[str, Any], title: str, body: str, kind: str, *, already: list[str] = ()) -> list[str]:
        """Send to every target not in `already`, or raise DeliveryError
        naming each that failed; `extra` then says which did get it."""
        delivered = list(already)
        failures: list[DeliveryError] = []
        for target in self._targets(settings, kind):
            if target.key in delivered:
                continue
            try:
                apprise_runner.send(target, service=self.SERVICE, title=title, body=body, kind=kind)
            except DeliveryError as exc:
                failures.append(exc)
                if exc.code in ("apprise_missing", "apprise_not_started"):
                    break
                continue
            delivered.append(target.key)
        if failures:
            first = failures[0]
            # Chats that failed for the same reason (no DNS, say) say it once.
            message = "; ".join(dict.fromkeys(str(failure) for failure in failures))
            if delivered:
                message += f". Sent to {', '.join(delivered)}"
            raise DeliveryError(
                first.code,
                message,
                stage=first.stage,
                retryable=any(failure.retryable for failure in failures),
                extra={"delivered_to": delivered},
            )
        return delivered

    def test_message(self) -> tuple[str, str]:
        """The subject (``describe`` adds the [AltaStor] prefix) and text of a test message."""
        return (
            "Test notification",
            f"This is a test message from {PRODUCT_NAME}. It reached you, so the appliance can send "
            f"notifications here with these settings." + _footer(),
        )


class TelegramChannel(_AppriseChannel):
    ID = "telegram"
    LABEL = "Telegram"
    SERVICE = "Telegram"
    STATE_ID = "telegram_notifications"
    FIELDS = ("enabled", "bot_token", "chat_ids", "events")

    def _defaults(self) -> dict[str, Any]:
        return {"bot_token": "", "bot_id": "", "chat_ids": []}

    def _apply(self, out: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> None:
        token = patch.get("bot_token")
        if token is not None:
            if not isinstance(token, str):
                problems["bot_token"] = "Must be text"
            else:
                token = token.strip()
                # Pasted from an API URL, it starts with "bot".
                if token[:3].lower() == "bot" and BOT_TOKEN_PATTERN.match(token[3:]):
                    token = token[3:]
                if token and not BOT_TOKEN_PATTERN.match(token):
                    problems["bot_token"] = "Not a bot token: it looks like 123456789:AAE..., as @BotFather gives it"
                else:
                    out["bot_token"] = encrypt_secret(token) if token else ""
                    out["bot_id"] = token.split(":", 1)[0] if token else ""
        chats = patch.get("chat_ids")
        if chats is not None:
            if isinstance(chats, str):
                chats = re.split(r"[,;\s]+", chats)
            if not isinstance(chats, list):
                problems["chat_ids"] = "Must be a list of chat IDs"
            else:
                seen: list[str] = []
                for item in chats:
                    chat = str(item).strip() if item is not None else ""
                    if chat and chat not in seen:
                        seen.append(chat)
                out["chat_ids"] = seen

    def _problems(self, settings: dict[str, Any], complete: bool) -> dict[str, str]:
        problems: dict[str, str] = {}
        chats = list(settings.get("chat_ids") or [])
        bad = [chat for chat in chats if not CHAT_ID_PATTERN.match(chat)]
        if bad:
            problems["chat_ids"] = (
                f"Not a chat ID: {', '.join(bad)}. Give a chat's number, such as -1001234567890, or a public channel's @name"
            )
        elif len(chats) > MAX_CHATS:
            problems["chat_ids"] = f"At most {MAX_CHATS} chats"
        elif complete and not chats:
            problems["chat_ids"] = "Add at least one chat ID"
        if complete:
            if not settings.get("bot_token"):
                problems["bot_token"] = "Enter the bot token @BotFather gave"
            elif not _secret(settings["bot_token"]):
                problems["bot_token"] = "The stored bot token cannot be decrypted with this server's credential key: enter it again"
        return problems

    def _targets(self, settings: dict[str, Any], kind: str = "info") -> list[Target]:
        token = _secret(settings.get("bot_token"))
        return [
            Target(f"tgram://{token}/{chat}?overflow=split", chat, f"chat {chat}", (token,))
            for chat in settings.get("chat_ids") or []
        ]

    def _check_target(self, settings: dict[str, Any]) -> tuple[str, Target]:
        token = _secret(settings.get("bot_token"))
        chats = "/".join(settings.get("chat_ids") or [])
        return "chat_ids", Target(f"tgram://{token}/{chats}?overflow=split", "check", "the Telegram settings", (token,))

    def _unreadable(self, settings: dict[str, Any], reason: str) -> str:
        return f"{apprise_runner.named()} cannot read the bot token and chat IDs: {reason}"

    def _dto(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "bot_token_set": bool(settings.get("bot_token")),
            "bot_id": settings.get("bot_id") or None,
            "chat_ids": list(settings.get("chat_ids") or []),
        }

    def _recipients(self, settings: dict[str, Any]) -> list[str]:
        return list(settings.get("chat_ids") or [])


class _WebhookChannel(_AppriseChannel):
    """A platform whose setting is a URL that holds its secret: Slack, Discord
    and ntfy."""

    FIELDS = ("enabled", "webhook_url", "events")
    # Added to the URL for Apprise: what keeps Apprise's own icon and footer out.
    APPRISE_OPTIONS = ""
    WHAT = "webhook URL"

    def _defaults(self) -> dict[str, Any]:
        return {"webhook_url": "", "webhook_host": ""}

    def url_problem(self, url: str) -> str | None:
        """Why `url` is not this platform's URL, or None."""
        raise NotImplementedError

    def _normalized(self, url: str) -> str:
        return url

    def _secrets(self, url: str) -> tuple[str, ...]:
        """The parts of `url` that are secret, besides the URL itself."""
        return tuple(part for part in urlsplit(url).path.split("/") if len(part) >= 16)

    def _apply(self, out: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> None:
        url = patch.get("webhook_url")
        if url is None:
            return
        if not isinstance(url, str):
            problems["webhook_url"] = "Must be text"
            return
        url = url.strip()
        problem = (_shape_problem(url) or self.url_problem(url)) if url else None
        if problem:
            problems["webhook_url"] = problem
            return
        url = self._normalized(url)
        out["webhook_url"] = encrypt_secret(url) if url else ""
        out["webhook_host"] = (urlsplit(url).hostname or "") if url else ""

    def _problems(self, settings: dict[str, Any], complete: bool) -> dict[str, str]:
        if not complete:
            return {}
        if not settings.get("webhook_url"):
            return {"webhook_url": f"Enter the {self.WHAT}"}
        if not _secret(settings["webhook_url"]):
            return {"webhook_url": f"The stored {self.WHAT} cannot be decrypted with this server's credential key: enter it again"}
        return {}

    def _apprise_url(self, url: str) -> str:
        """The URL Apprise is given: the platform's own, with the options."""
        return url + ("&" if "?" in url else "?") + self.APPRISE_OPTIONS if self.APPRISE_OPTIONS else url

    def _target(self, settings: dict[str, Any], key: str, kind: str = "info") -> Target:
        url = _secret(settings.get("webhook_url"))
        configured = self._apprise_url(url)
        return Target(configured, key, f"the {self.WHAT}", (configured, url, *self._secrets(url)))

    def _targets(self, settings: dict[str, Any], kind: str = "info") -> list[Target]:
        return [self._target(settings, "webhook", kind)]

    def _check_target(self, settings: dict[str, Any]) -> tuple[str, Target]:
        return "webhook_url", self._target(settings, "check")

    def _unreadable(self, settings: dict[str, Any], reason: str) -> str:
        return f"{apprise_runner.named()} cannot read this {self.WHAT}: {reason}"

    def _dto(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {"webhook_url_set": bool(settings.get("webhook_url")), "webhook_host": settings.get("webhook_host") or None}


class SlackChannel(_WebhookChannel):
    ID = "slack"
    LABEL = "Slack"
    SERVICE = "Slack"
    STATE_ID = "slack_notifications"
    # Apprise's own icon and footer would come with every message.
    APPRISE_OPTIONS = "image=no&footer=no"
    WHAT = "webhook URL"
    # The name messages are posted under; Apprise's own otherwise. A webhook
    # made by a Slack app posts under the app's name whatever this says.
    POSTED_AS = "AltaStor"

    def _apprise_url(self, url: str) -> str:
        """slack://AltaStor@T.../B.../.../: the form that carries a name."""
        tokens = [part for part in urlsplit(url).path.split("/") if part][1:4]
        return f"slack://{self.POSTED_AS}@{'/'.join(tokens)}/?{self.APPRISE_OPTIONS}"

    def url_problem(self, url: str) -> str | None:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "hooks.slack.com" or not re.fullmatch(r"/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+/?", parts.path):
            return (
                "Not a Slack incoming webhook URL: it looks like https://hooks.slack.com/services/T.../B.../..., as the "
                "Slack app's Incoming Webhooks page gives it"
            )
        return None


class DiscordChannel(_WebhookChannel):
    ID = "discord"
    LABEL = "Discord"
    SERVICE = "Discord"
    STATE_ID = "discord_notifications"
    # Apprise's own avatar would come from GitHub with every message, and
    # Discord takes at most 2,000 characters at a time.
    APPRISE_OPTIONS = "avatar=no&overflow=split"
    WHAT = "webhook URL"

    def url_problem(self, url: str) -> str | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in DISCORD_HOSTS or not re.fullmatch(r"/api(/v\d+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+/?", parts.path):
            return (
                "Not a Discord webhook URL: it looks like https://discord.com/api/webhooks/123.../..., as the channel's "
                "Integrations > Webhooks > Copy Webhook URL gives it"
            )
        return None

    def _normalized(self, url: str) -> str:
        """The form Apprise reads: discord.com, no API version, no test-client
        host (ptb., canary.) - the same webhook."""
        parts = urlsplit(url)
        path = re.sub(r"^/api/v\d+/", "/api/", parts.path)
        return f"https://discord.com{path}" + (f"?{parts.query}" if parts.query else "")


class NtfyChannel(_WebhookChannel):
    """ntfy (https://ntfy.sh or a server of one's own): the setting is the
    topic's URL, and, for a server with access control, an access token. On the
    public ntfy.sh, whoever knows a topic's name can read it, so the URL is kept
    as a secret."""

    ID = "ntfy"
    LABEL = "ntfy"
    SERVICE = "ntfy"
    STATE_ID = "ntfy_notifications"
    FIELDS = ("enabled", "webhook_url", "access_token", "events")
    WHAT = "topic URL"

    def _defaults(self) -> dict[str, Any]:
        return {**super()._defaults(), "access_token": ""}

    def url_problem(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.query or parts.fragment or not NTFY_TOPIC_PATH.fullmatch(parts.path):
            return (
                "Not an ntfy topic URL: it looks like https://ntfy.sh/<topic> or https://<your server>/<topic>, the "
                "topic of letters, digits, - and _ (at most 64)"
            )
        return None

    def _normalized(self, url: str) -> str:
        return url.rstrip("/")

    def _apply(self, out: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> None:
        super()._apply(out, patch, problems)
        token = patch.get("access_token")
        if token is None:
            return
        if not isinstance(token, str):
            problems["access_token"] = "Must be text"
            return
        token = token.strip()
        if token and not NTFY_TOKEN.fullmatch(token):
            problems["access_token"] = "Not an ntfy access token: it starts with tk_, as ntfy token add or the web app's Account page gives it"
            return
        out["access_token"] = encrypt_secret(token) if token else ""

    def _problems(self, settings: dict[str, Any], complete: bool) -> dict[str, str]:
        problems = super()._problems(settings, complete)
        if complete and settings.get("access_token") and not _secret(settings["access_token"]):
            problems["access_token"] = "The stored access token cannot be decrypted with this server's credential key: enter it again"
        return problems

    def _target(self, settings: dict[str, Any], key: str, kind: str = "info") -> Target:
        """ntfys://host/topic: the form Apprise reads for any ntfy server, with
        the token for a server with access control. Apprise's own icon would
        come from GitHub with every message. A failure goes at ntfy's high
        priority, which phones show more prominently."""
        url = _secret(settings.get("webhook_url"))
        token = _secret(settings.get("access_token"))
        parts = urlsplit(url)
        host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
        topic = parts.path.strip("/")
        query = "image=no" + ("&priority=high" if kind == "failure" else "") + (f"&auth=token&token={token}" if token else "")
        configured = f"ntfys://{host}/{topic}?{query}"
        secrets = (configured, url, token, topic if len(topic) >= 4 else "")
        return Target(configured, key, "the topic", secrets)

    def _dto(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {**super()._dto(settings), "access_token_set": bool(settings.get("access_token"))}


def _shape_problem(url: str) -> str | None:
    """Why `url` is not one https URL, or None."""
    if len(url) > MAX_URL_LENGTH:
        return f"At most {MAX_URL_LENGTH} characters"
    if _NOT_IN_URL.search(url):
        return "Must be one URL, without spaces or line breaks"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "Must be an https:// URL"
    return None


TELEGRAM = TelegramChannel()
SLACK = SlackChannel()
DISCORD = DiscordChannel()
NTFY = NtfyChannel()
PLATFORMS = {channel.ID: channel for channel in (TELEGRAM, SLACK, DISCORD, NTFY)}
