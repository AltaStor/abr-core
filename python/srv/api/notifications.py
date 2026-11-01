# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Request

from python.srv.services import email_channel, notification_service, rbac, script_channel
from python.srv.services.notification_events import events_dto

from ._routing import build_router, register
from .schemas import (
    EmailDeliveryList,
    EmailNotificationSettingsDTO,
    EmailNotificationSettingsRequest,
    EmailTestRequest,
    EmailTestResult,
    NotificationEventList,
    NotificationScriptContentDTO,
    NotificationScriptDTO,
    NotificationScriptRunDTO,
    NotificationScriptSettingsRequest,
    NotificationScriptUploadRequest,
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


def _script_payload(request: Request) -> dict:
    """What a script prints can hold anything, so a run's output is only for
    those who could read the script itself."""
    settings = script_channel.load_settings()
    runs = notification_service.SCRIPT.history.items()[:20]
    if not rbac.role_allows(getattr(getattr(request.state, "audit", None), "role", None), "notification.script"):
        runs = [{key: value for key, value in run.items() if key != "output"} for run in runs]
    return {**script_channel.settings_dto(settings), "events": events_dto(settings.get("events")), "runs": runs}


def get_script(request: Request) -> NotificationScriptDTO:
    """The notification script: its file (never the content), whether it runs,
    on which events, and its latest runs. `available` is false where
    ABR_NOTIFICATION_SCRIPTS=0 switches the feature off."""
    return _script_payload(request)


def update_script_settings(
    request: Request,
    payload: NotificationScriptSettingsRequest = Body(default_factory=NotificationScriptSettingsRequest),
) -> NotificationScriptDTO:
    """Change whether the script runs, on which events and how long it may
    take; fields left out keep their value. Refused as a whole, with every
    problem by field, when a value is invalid or there is no script to enable
    (422 invalid_notification_script)."""
    _settings, changed = script_channel.update_settings(
        unset_excluded(payload), updated_by=getattr(request.state, "user_name", None)
    )
    request.state.audit.details["changed"] = changed
    return _script_payload(request)


def upload_script(
    request: Request,
    payload: NotificationScriptUploadRequest = Body(default_factory=NotificationScriptUploadRequest),
) -> NotificationScriptDTO:
    """Store the script, in place of any earlier one. It runs on the appliance
    as the server's service user. It must be text of at most 256 KiB, starting
    with a #! line naming an interpreter the appliance has (422
    invalid_notification_script). The audit log records its name, size and
    SHA-256, not its content."""
    settings = script_channel.upload(unset_excluded(payload), updated_by=getattr(request.state, "user_name", None))
    file = settings["file"]
    request.state.audit.details.update(script=file["name"], size=file["size"], sha256=file["sha256"])
    return _script_payload(request)


def get_script_content(request: Request) -> NotificationScriptContentDTO:
    """The stored script, to review it; reading it is recorded in the audit log."""
    result = script_channel.content()
    request.state.audit.details.update(script=result["name"], sha256=result["sha256"])
    return result


def delete_script(request: Request) -> NotificationScriptDTO:
    """Delete the script, which also stops it running."""
    script_channel.remove(updated_by=getattr(request.state, "user_name", None))
    return _script_payload(request)


def test_script(request: Request) -> NotificationScriptRunDTO:
    """Run the script now on a sample event (`event` is `test`), whether or not
    it is switched on, and wait for it. A script that fails answers 200 with
    `status` failed, the reason in `error`, its exit code and output."""
    result = notification_service.run_script_test()
    request.state.audit.details.update(sha256=result.get("sha256"), status=result.get("status"), exit_code=result.get("exit_code"))
    return result


register(router, [
    ("/email", get_email_settings, ["GET"], "notification.view"),
    ("/email", update_email_settings, ["PUT"], "notification.edit"),
    ("/email/test", test_email, ["POST"], "notification.test"),
    ("/email/deliveries", list_deliveries, ["GET"], "notification.view"),
    ("/events", list_events, ["GET"], "notification.view"),
    ("/script", get_script, ["GET"], "notification.view"),
    ("/script", update_script_settings, ["PUT"], "notification.script"),
    ("/script/file", upload_script, ["PUT"], "notification.script"),
    ("/script/file", get_script_content, ["GET"], "notification.script"),
    ("/script/file", delete_script, ["DELETE"], "notification.script"),
    ("/script/test", test_script, ["POST"], "notification.script"),
])
