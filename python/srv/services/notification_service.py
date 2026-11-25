# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

"""The notification service: one place every component hands a notification
to, whatever it is about and however it goes out.

A component calls ``notify(event_id, subject, body)``. Each channel (e-mail,
and the messaging platforms: Telegram, Slack, Discord and ntfy) says
whether it wants the notification; for those
that do, the service queues it, and a background worker per channel delivers
it, retrying what the channel calls retryable and recording every outcome in
that channel's history and the server log. A component is never held up by a
slow mail server or chat service, and a failed delivery never fails the
operation it reports on. One channel's delays do not hold up another.

A channel is a module offering ``ID``, ``LABEL``, ``RETRY_DELAYS_SECONDS``,
``wants(notification)``, ``describe(notification)`` for the history entry, and
``send(notification, entry)``, which returns what to add to the entry or raises
``DeliveryError``. The events and what detects them live in
``notification_events`` and ``notification_detectors``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from python.srv.core.mongo_db_wrapper import load_system_state, persist_system_state
from python.srv.core.utils import utc_now

from . import email_channel
from .api_errors import api_error
from .chat_channels import PLATFORMS
from .notification_events import EVENTS_BY_ID, TEST_EVENT, DeliveryError, Notification

LOGGER = logging.getLogger(__name__)

HISTORY_SIZE = 50
QUEUE_SIZE = 500
WAITING_STATUSES = ("queued", "retrying")


class _History:
    """A channel's latest deliveries, newest first, kept in the catalog."""

    def __init__(self, state_id: str) -> None:
        self._state_id = state_id
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] | None = None

    def _loaded(self) -> deque[dict[str, Any]]:
        if self._items is None:
            try:
                stored = (load_system_state(self._state_id) or {}).get("items") or []
            except Exception as exc:
                LOGGER.warning("Could not read %s from the catalog: %s", self._state_id, exc)
                stored = []
            self._items = deque((item for item in stored if isinstance(item, dict)), maxlen=HISTORY_SIZE)
        return self._items

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            items = self._loaded()
            for index, existing in enumerate(items):
                if existing.get("id") == entry["id"]:
                    items[index] = dict(entry)
                    break
            else:
                items.appendleft(dict(entry))
            snapshot = list(items)
        try:
            persist_system_state({"id": self._state_id, "items": snapshot})
        except Exception as exc:
            LOGGER.warning("Could not record a delivery in %s: %s", self._state_id, exc)

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._loaded()]


class _Waiting:
    def __init__(self, state_id: str) -> None:
        self._state_id = state_id
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] | None = None

    def _loaded(self) -> dict[str, dict[str, Any]]:
        if self._items is None:
            try:
                stored = (load_system_state(self._state_id) or {}).get("items") or {}
            except Exception as exc:
                LOGGER.warning("Could not read %s from the catalog: %s", self._state_id, exc)
                stored = {}
            self._items = {str(key): dict(value) for key, value in stored.items() if isinstance(value, dict)} if isinstance(stored, dict) else {}
        return self._items

    def _persist(self, items: dict[str, dict[str, Any]]) -> None:
        try:
            persist_system_state({"id": self._state_id, "items": dict(items)})
        except Exception as exc:
            LOGGER.warning("Could not keep the waiting notifications in %s: %s", self._state_id, exc)

    def keep(self, entry_id: str, notification: Notification) -> None:
        with self._lock:
            items = self._loaded()
            items[entry_id] = {
                "event": notification.event,
                "subject": notification.subject,
                "body": notification.body,
                "severity": notification.severity,
                "details": dict(notification.details or {}),
            }
            self._persist(items)

    def mark(self, entry_id: str, *, started: bool, attempts: int) -> None:
        with self._lock:
            items = self._loaded()
            if entry_id in items:
                items[entry_id].update(started=started, attempts=attempts)
                self._persist(items)

    def drop(self, entry_id: str) -> None:
        with self._lock:
            items = self._loaded()
            if items.pop(entry_id, None) is not None:
                self._persist(items)

    def items(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._loaded().items()}


def _notification(stored: dict[str, Any]) -> Notification:
    details = stored.get("details")
    return Notification(
        str(stored.get("event") or ""),
        str(stored.get("subject") or ""),
        str(stored.get("body") or ""),
        str(stored.get("severity") or "info"),
        dict(details) if isinstance(details, dict) else {},
    )


def _seconds_until(moment: Any) -> float:
    try:
        when = datetime.fromisoformat(str(moment))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - time.time())


@dataclass
class _Delivery:
    notification: Notification
    entry: dict[str, Any]


def _addressees(entry: dict[str, Any]) -> str:
    """Who a message went to, for the server log; nothing for a channel without
    recipients."""
    recipients = entry.get("recipients")
    return f" to {', '.join(recipients)}" if recipients else ""


class _Channel:
    """One channel's queue, worker and history."""

    def __init__(self, module: Any, history_state_id: str) -> None:
        self.module = module
        self.history = _History(history_state_id)
        self.waiting = _Waiting(f"{history_state_id}_waiting")
        self._queue: queue.Queue[_Delivery] = queue.Queue(maxsize=QUEUE_SIZE)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, notification: Notification) -> bool:
        if not self.module.wants(notification):
            return False
        entry = new_entry(notification, self.module.describe(notification), status="queued", attempts=0)
        try:
            self._queue.put_nowait(_Delivery(notification, entry))
        except queue.Full:
            self._give_up_on_full_queue(entry)
            LOGGER.error("%s notification '%s' dropped: %d are already waiting.", self.module.LABEL, entry["subject"], QUEUE_SIZE)
            return False
        self.waiting.keep(entry["id"], notification)
        self.history.record(entry)
        self._ensure_worker()
        return True

    def _finish(self, entry: dict[str, Any]) -> None:
        self.history.record(entry)
        self.waiting.drop(entry["id"])

    def _give_up_on_full_queue(self, entry: dict[str, Any]) -> None:
        message = "Too many notifications waiting to be delivered"
        entry.update(status="failed", error={"code": "queue_full", "message": message, "stage": "queue", "retryable": False})
        self._finish(entry)

    def _give_up_after_restart(self, entry: dict[str, Any], message: str) -> None:
        entry.update(status="failed", error={"code": "interrupted", "message": message, "stage": "queue", "retryable": False})
        entry.pop("next_attempt_at", None)
        LOGGER.error("%s notification '%s'%s was not delivered: %s.", self.module.LABEL, entry.get("subject"), _addressees(entry), message)
        self._finish(entry)

    def resume(self) -> int:
        kept = self.waiting.items()
        resumed = 0
        for entry in reversed(self.history.items()):
            if entry.get("status") not in WAITING_STATUSES:
                continue
            stored = kept.pop(str(entry.get("id")), None)
            if stored is None:
                self._give_up_after_restart(entry, "The server restarted before it was delivered, and the message was not kept to send again")
                continue
            if stored.get("started") and not self.module.REPEAT_AFTER_RESTART:
                self._give_up_after_restart(entry, "The server stopped while it ran, and it is not run again")
                continue
            entry["attempts"] = max(int(entry.get("attempts") or 0), int(stored.get("attempts") or 0))
            delay = _seconds_until(entry.get("next_attempt_at")) if entry["status"] == "retrying" else 0.0
            self._schedule(_Delivery(_notification(stored), entry), delay)
            resumed += 1
        for entry_id in kept:
            self.waiting.drop(entry_id)
        if resumed:
            self._ensure_worker()
            LOGGER.info("%s: %d notification(s) waiting before the restart are taken up again.", self.module.LABEL, resumed)
        return resumed

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                name = f"{self.module.ID}-notifications"
                self._worker = threading.Thread(target=self._work, name=name, daemon=True)
                self._worker.start()

    def _work(self) -> None:
        while True:
            delivery = self._queue.get()
            try:
                self._attempt(delivery)
            except Exception:
                LOGGER.exception("%s notification worker failed on %s", self.module.LABEL, delivery.entry.get("event"))
            finally:
                self._queue.task_done()

    def _attempt(self, delivery: _Delivery) -> None:
        entry, label = delivery.entry, self.module.LABEL
        if not self.module.wants(delivery.notification):
            message = "Switched off before it was delivered"
            entry.update(status="cancelled", error={"code": "notifications_disabled", "message": message, "stage": "queue", "retryable": False})
            entry.pop("next_attempt_at", None)
            self._finish(entry)
            return
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        self.waiting.mark(entry["id"], started=True, attempts=entry["attempts"])
        try:
            outcome = self.module.send(delivery.notification, entry)
        except DeliveryError as exc:
            entry.update(error=exc.to_dict(), **exc.extra)
            delays = self.module.RETRY_DELAYS_SECONDS
            if exc.retryable and entry["attempts"] <= len(delays):
                self._retry_later(delivery, delays[entry["attempts"] - 1], exc)
                return
            entry["status"] = "failed"
            entry.pop("next_attempt_at", None)
            LOGGER.error(
                "%s notification '%s'%s failed after %d attempt(s) (%s): %s",
                label, entry["subject"], _addressees(entry), entry["attempts"], exc.code, exc,
            )
            self._finish(entry)
            return
        entry.update(status="sent", sent_at=utc_now(), error=None, **outcome)
        entry.pop("next_attempt_at", None)
        LOGGER.info("%s notification '%s' delivered%s.", label, entry["subject"], _addressees(entry))
        self._finish(entry)

    def _retry_later(self, delivery: _Delivery, delay: int, exc: DeliveryError) -> None:
        entry = delivery.entry
        entry["status"] = "retrying"
        self.waiting.mark(entry["id"], started=False, attempts=entry["attempts"])
        entry["next_attempt_at"] = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat(timespec="seconds")
        LOGGER.warning(
            "%s notification '%s' was not delivered (attempt %d, %s): %s. Trying again in %d s.",
            self.module.LABEL, entry["subject"], entry["attempts"], exc.code, exc, delay,
        )
        self.history.record(entry)
        self._schedule(delivery, delay)

    def _schedule(self, delivery: _Delivery, delay: float) -> None:
        if delay <= 0:
            self._requeue(delivery)
            return
        timer = threading.Timer(delay, self._requeue, args=(delivery,))
        timer.daemon = True
        timer.start()

    def _requeue(self, delivery: _Delivery) -> None:
        try:
            self._queue.put_nowait(delivery)
        except queue.Full:
            self._give_up_on_full_queue(delivery.entry)


EMAIL = _Channel(email_channel, "email_notification_deliveries")
# The messaging platforms by ID, each with its own queue, worker and history.
MESSAGING = {platform_id: _Channel(module, f"{platform_id}_notification_deliveries") for platform_id, module in PLATFORMS.items()}
TELEGRAM, SLACK, DISCORD, NTFY = (MESSAGING[platform_id] for platform_id in ("telegram", "slack", "discord", "ntfy"))
CHANNELS = (EMAIL, *MESSAGING.values())


def resume_waiting() -> None:
    for channel in CHANNELS:
        try:
            channel.resume()
        except Exception:
            LOGGER.exception("Could not take up the %s notifications waiting before the restart", channel.module.LABEL)


def new_entry(notification: Notification, described: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "event": notification.event,
        "severity": notification.severity,
        "queued_at": utc_now(),
        "error": None,
        **described,
        **fields,
    }


def notify(
    event_id: str, subject: str, body: str, *, severity: str = "info", details: dict[str, Any] | None = None
) -> bool:
    """Hand a notification about `event_id` to the service: queued for each
    channel that wants it, dropped otherwise. Never raises and never waits for
    a delivery, so a caller can report from anywhere. True when any channel
    took it. `body` is plain text, `details` the same facts for a program."""
    try:
        if event_id not in EVENTS_BY_ID:
            LOGGER.error("Notification for unknown event %r dropped: %s", event_id, subject)
            return False
        notification = Notification(event_id, subject, body, severity, details or {})
        return any([channel.submit(notification) for channel in CHANNELS])
    except Exception:
        LOGGER.exception("Could not queue the notification for %s", event_id)
        return False


def send_test(patch: dict[str, Any] | None, to: list[str] | None) -> dict[str, Any]:
    """Send a test e-mail now, with the stored settings and any unsaved changes
    in `patch`, to `to` or the configured recipients. Answers what happened; a
    delivery that failed is a 502 naming why (smtp_connection_failed,
    smtp_tls_failed, smtp_auth_failed, smtp_sender_refused,
    smtp_recipients_refused, smtp_error) and the stage it reached."""
    if to is not None and not isinstance(to, list):
        message = "Must be a list of e-mail addresses"
        raise api_error(422, "invalid_notification_settings", f"to: {message}", fields={"to": message})
    given = [str(address).strip() for address in to or [] if address is not None and str(address).strip()]
    settings = email_channel.settings_for_test(patch, require_recipients=not given)
    recipients = given or list(settings["recipients"])
    bad = [address for address in recipients if not email_channel.ADDRESS_PATTERN.match(address)]
    if bad:
        raise api_error(422, "invalid_notification_settings", f"Not an e-mail address: {', '.join(bad)}", fields={"to": ", ".join(bad)})
    subject, body = email_channel.test_message(settings, recipients)
    entry = new_entry(Notification(TEST_EVENT, subject, body), {"subject": subject, "recipients": recipients}, attempts=1)
    started = time.monotonic()
    try:
        refused = email_channel.deliver(settings, recipients, subject, body)
    except DeliveryError as exc:
        entry.update(status="failed", error=exc.to_dict())
        EMAIL.history.record(entry)
        LOGGER.warning("Test e-mail to %s was not sent (%s): %s", ", ".join(recipients), exc.code, exc)
        raise api_error(502, exc.code, str(exc), stage=exc.stage, retryable=exc.retryable) from exc
    entry.update(status="sent", sent_at=utc_now(), refused=refused)
    EMAIL.history.record(entry)
    LOGGER.info("Test e-mail sent to %s.", ", ".join(recipients))
    return {
        "status": "sent",
        "recipients": recipients,
        "refused": refused,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def send_chat_test(channel: _Channel, patch: dict[str, Any] | None) -> dict[str, Any]:
    """Send a test message through a messaging platform now, with the
    stored settings and any unsaved changes in `patch` (not saved). Answers
    where it went; a delivery that failed is a 502 naming why
    (connection_failed, tls_failed, delivery_refused, delivery_failed,
    timed_out, apprise_missing, apprise_not_started, apprise_url_refused,
    apprise_failed) and the stage it reached, with `delivered_to` for the
    targets that did get it."""
    module = channel.module
    settings = module.settings_for_test(patch)
    subject, body = module.test_message()
    notification = Notification(TEST_EVENT, subject, body)
    entry = new_entry(notification, module.describe(notification, settings), attempts=1)
    started = time.monotonic()
    try:
        delivered = module.deliver(settings, entry["subject"], body, "info")
    except DeliveryError as exc:
        entry.update(status="failed", error=exc.to_dict(), **exc.extra)
        channel.history.record(entry)
        LOGGER.warning("Test %s notification was not sent (%s): %s", module.LABEL, exc.code, exc)
        raise api_error(
            502, exc.code, str(exc), stage=exc.stage, retryable=exc.retryable, delivered_to=exc.extra.get("delivered_to", [])
        ) from exc
    entry.update(status="sent", sent_at=utc_now(), delivered_to=delivered)
    channel.history.record(entry)
    LOGGER.info("Test %s notification sent%s.", module.LABEL, _addressees(entry))
    return {"status": "sent", "delivered_to": delivered, "duration_ms": round((time.monotonic() - started) * 1000)}
