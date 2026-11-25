# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

from fastapi import Body, Path, Request

from python.srv.services import apprise_runner, email_channel, notification_service
from python.srv.services.api_errors import api_error
from python.srv.services.notification_events import events_dto

from ._routing import build_router, register
from .schemas import (
    EmailDeliveryList,
    EmailNotificationSettingsDTO,
    EmailNotificationSettingsRequest,
    EmailTestRequest,
    EmailTestResult,
    ChatDeliveryList,
    ChatTestResult,
    MessagingList,
    MessagingPlatformDTO,
    MessagingSettingsRequest,
    MessagingTestRequest,
    NotificationEventList,
    unset_excluded,
)

router = build_router("/notifications", tag="notifications")


def _settings_payload() -> dict:
    settings = email_channel.load_settings()
    return {
        **email_channel.settings_dto(settings),
        "events": events_dto(settings.get("events")),
        "deliveries": notification_service.EMAIL.history.items()[:20],
    }


def get_email_settings() -> EmailNotificationSettingsDTO:
    """The e-mail notification settings (secrets only as *_set flags), the
    events with whether each sends e-mail, and the latest deliveries."""
    return _settings_payload()


def update_email_settings(
    request: Request,
    payload: EmailNotificationSettingsRequest = Body(default_factory=EmailNotificationSettingsRequest),
) -> EmailNotificationSettingsDTO:
    """Change the e-mail notification settings. Fields left out keep their
    value, and so do secrets sent as null. The whole change is refused, with
    every problem by field in `error.details.fields`, when a value is invalid,
    or when notifications are on and a setting delivery needs is missing (422
    invalid_notification_settings)."""
    _settings, changed = email_channel.update_settings(
        unset_excluded(payload), updated_by=getattr(request.state, "user_name", None)
    )
    request.state.audit.details["changed"] = changed
    return _settings_payload()


def test_email(request: Request, payload: EmailTestRequest = Body(default_factory=EmailTestRequest)) -> EmailTestResult:
    """Send a test e-mail now, with the stored settings and any unsaved changes
    in `settings`, to `to` or the configured recipients; nothing is saved. A
    message the server did not take answers 502 with the reason as the error
    code (smtp_connection_failed, smtp_tls_failed, smtp_auth_failed,
    smtp_sender_refused, smtp_recipients_refused, smtp_error) and
    `details.stage`."""
    body = unset_excluded(payload)
    result = notification_service.send_test(body.get("settings"), body.get("to"))
    request.state.audit.details["recipients"] = result["recipients"]
    return result


def list_events() -> NotificationEventList:
    """The events that can send e-mail, with their defaults and whether each is on."""
    return {"items": events_dto(email_channel.load_settings().get("events"))}


def list_deliveries() -> EmailDeliveryList:
    """The latest e-mail deliveries (up to 50), newest first: notifications
    and test e-mails, sent, waiting for a retry, or failed and why."""
    return {"items": notification_service.EMAIL.history.items()}


PLATFORM = Path(description="telegram, slack, discord or ntfy")


def _channel(platform: str) -> notification_service._Channel:
    channel = notification_service.MESSAGING.get(platform)
    if channel is None:
        raise api_error(
            404,
            "notification_platform_not_found",
            f"No messaging platform {platform!r}: it is one of {', '.join(notification_service.MESSAGING)}",
        )
    return channel


def _platform_payload(channel: notification_service._Channel) -> dict:
    settings = channel.module.load_settings()
    return {
        **channel.module.settings_dto(settings),
        "events": events_dto(settings.get("events")),
        "deliveries": channel.history.items()[:20],
    }


def list_messaging() -> MessagingList:
    """Every messaging platform the appliance can notify through (Telegram,
    Slack, Discord, ntfy) with its settings, events and latest
    deliveries, and whether Apprise, which sends them, is installed. `added`
    says which ones were set up."""
    return {
        "apprise": apprise_runner.availability(),
        "items": [_platform_payload(channel) for channel in notification_service.MESSAGING.values()],
    }


def get_platform(platform: str = PLATFORM) -> MessagingPlatformDTO:
    """One messaging platform's settings (secrets only as *_set flags),
    whether Apprise is installed, the events with whether each sends a
    message, and the latest deliveries."""
    return _platform_payload(_channel(platform))


def update_platform(
    request: Request,
    platform: str = PLATFORM,
    payload: MessagingSettingsRequest = Body(default_factory=MessagingSettingsRequest),
) -> MessagingPlatformDTO:
    """Change a messaging platform's settings, which adds it when it was not.
    Telegram takes bot_token and chat_ids; Slack and Discord take webhook_url;
    ntfy takes webhook_url, the topic's URL, and access_token for a server with
    access control. Fields left out keep their value, and so does a secret sent
    as null. The whole change is refused, with every problem by field in
    `error.details.fields`, when a value is invalid, or when the platform is on
    and a setting is missing or Apprise cannot read the settings (422
    invalid_notification_settings)."""
    channel = _channel(platform)
    _settings, changed = channel.module.update_settings(
        unset_excluded(payload), updated_by=getattr(request.state, "user_name", None)
    )
    request.state.audit.details.update(platform=platform, changed=changed)
    return _platform_payload(channel)


def remove_platform(request: Request, platform: str = PLATFORM) -> MessagingPlatformDTO:
    """Remove a messaging platform: its settings and secret go, and it sends
    nothing until it is added again. Its delivery history stays."""
    channel = _channel(platform)
    channel.module.remove(updated_by=getattr(request.state, "user_name", None))
    request.state.audit.details["platform"] = platform
    return _platform_payload(channel)


def test_platform(
    request: Request,
    platform: str = PLATFORM,
    payload: MessagingTestRequest = Body(default_factory=MessagingTestRequest),
) -> ChatTestResult:
    """Send a test message now, with the stored settings and any unsaved
    changes in `settings`; nothing is saved. A message that did not go answers
    502 with the reason as the error code (connection_failed, tls_failed,
    delivery_refused, delivery_failed, timed_out, apprise_missing,
    apprise_not_started, apprise_url_refused, apprise_failed), `details.stage`,
    and `details.delivered_to`, the chats (Telegram) that did get it."""
    channel = _channel(platform)
    result = notification_service.send_chat_test(channel, unset_excluded(payload).get("settings"))
    request.state.audit.details.update(platform=platform, delivered_to=result["delivered_to"])
    return result


def list_platform_deliveries(platform: str = PLATFORM) -> ChatDeliveryList:
    """A messaging platform's latest deliveries (up to 50), newest first."""
    return {"items": _channel(platform).history.items()}


# The fixed paths come before /{platform}, which would otherwise take them.
register(router, [
    ("/email", get_email_settings, ["GET"], "notification.view"),
    ("/email", update_email_settings, ["PUT"], "notification.edit"),
    ("/email/test", test_email, ["POST"], "notification.test"),
    ("/email/deliveries", list_deliveries, ["GET"], "notification.view"),
    ("/events", list_events, ["GET"], "notification.view"),
    ("/messaging", list_messaging, ["GET"], "notification.view"),
    ("/{platform}", get_platform, ["GET"], "notification.view"),
    ("/{platform}", update_platform, ["PUT"], "notification.edit"),
    ("/{platform}", remove_platform, ["DELETE"], "notification.edit"),
    ("/{platform}/test", test_platform, ["POST"], "notification.test"),
    ("/{platform}/deliveries", list_platform_deliveries, ["GET"], "notification.view"),
])
