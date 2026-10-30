# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Request

from python.srv.services import email_channel, notification_service
from python.srv.services.notification_events import events_dto

from ._routing import build_router, register
from .schemas import (
    EmailDeliveryList,
    EmailNotificationSettingsDTO,
    EmailNotificationSettingsRequest,
    EmailTestRequest,
    EmailTestResult,
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
    oauth_token_failed, smtp_sender_refused, smtp_recipients_refused,
    smtp_error) and `details.stage`."""
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


register(router, [
    ("/email", get_email_settings, ["GET"], "notification.view"),
    ("/email", update_email_settings, ["PUT"], "notification.edit"),
    ("/email/test", test_email, ["POST"], "notification.test"),
    ("/email/deliveries", list_deliveries, ["GET"], "notification.view"),
    ("/events", list_events, ["GET"], "notification.view"),
])
