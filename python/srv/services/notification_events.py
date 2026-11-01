# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""What the product notifies about: the events, the message a component hands
over, and how a delivery fails. Shared by the notification service and its
channels, so none of them depends on another."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

CERTIFICATE_WARNING_DAYS = 14
TEST_EVENT = "test"


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    name: str
    description: str
    default: bool


EVENTS: tuple[Event, ...] = (
    Event("job.failed", "Backup job failed", "A backup job's run finished and the backup of at least one VM failed.", True),
    Event(
        "job.warning",
        "Backup job finished with warnings",
        "A backup job's run finished, every VM was backed up, and at least one backup reported a warning.",
        True,
    ),
    Event("job.succeeded", "Backup job succeeded", "A backup job's run finished and every VM was backed up without a warning.", False),
    Event("restore.failed", "Restore failed", "A VM restore failed.", True),
    Event("restore.succeeded", "Restore finished", "A VM restore finished.", False),
    Event(
        "repository.low_space",
        "Repository running out of space",
        "After a backup, a repository's disk is filled to the threshold or beyond (local and SMB repositories; "
        "object storage has no fixed size). Sent again once it has dropped below and filled up again.",
        True,
    ),
    Event(
        "system.clock_jump",
        "Server clock jumped forward",
        "The server's clock moved further forward than expected, so retention deletes nothing until an "
        "Administrator confirms the time.",
        True,
    ),
    Event(
        "tls.certificate_expiring",
        "HTTPS certificate expiring",
        f"The HTTPS certificate an administrator installed expires within {CERTIFICATE_WARNING_DAYS} days, or has "
        "expired (checked daily; the appliance renews its own certificate by itself).",
        True,
    ),
)
EVENTS_BY_ID = {event.id: event for event in EVENTS}


@dataclass(frozen=True, slots=True)
class Notification:
    """One thing to tell people about, before any channel has shaped it:
    `body` is plain text, `details` the same facts for a program to read."""

    event: str
    subject: str
    body: str
    severity: str = "info"
    details: Mapping[str, Any] = field(default_factory=dict)


class DeliveryError(Exception):
    """A notification a channel could not deliver. `code` is stable for
    clients; `stage` says how far the attempt got; `retryable` whether trying
    again later can help; `extra` goes into the delivery history as it is."""

    def __init__(self, code: str, message: str, *, stage: str, retryable: bool, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "stage": self.stage, "retryable": self.retryable}


def event_chosen(selection: Mapping[str, Any] | None, event_id: str) -> bool:
    """Whether a channel's event selection covers `event_id`; an event it does
    not mention takes its default."""
    chosen = (selection or {}).get(event_id)
    return EVENTS_BY_ID[event_id].default if chosen is None else bool(chosen)


def events_dto(selection: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": event.id,
            "name": event.name,
            "description": event.description,
            "default": event.default,
            "enabled": event_chosen(selection, event.id),
        }
        for event in EVENTS
    ]


def merged_selection(current: Mapping[str, Any] | None, raw: Any, problems: dict[str, str]) -> dict[str, Any]:
    """`current` with the event choices in `raw` (a map, or a list of `{id,
    enabled}`) laid over it; what is wrong with them lands in `problems`."""
    if isinstance(raw, list):
        raw = {str(item.get("id")): item.get("enabled") for item in raw if isinstance(item, dict)}
    if not isinstance(raw, dict):
        problems["events"] = "Must map event IDs to true or false"
        return dict(current or {})
    events = dict(current or {})
    for event_id, enabled in raw.items():
        if event_id not in EVENTS_BY_ID:
            problems[f"events.{event_id}"] = f"Unknown event; known events: {', '.join(EVENTS_BY_ID)}"
        elif not isinstance(enabled, bool):
            problems[f"events.{event_id}"] = "Must be true or false"
        else:
            events[event_id] = enabled
    return events
