# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import queue
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import SYSTEM_STATE
from python.srv.services import notification_service
from python.srv.services.notification_events import DeliveryError, Notification
from python.tests.srv.notifications.memory import in_memory

NOTIFICATION = Notification("job.failed", 'Backup job "nightly" failed', "The body\n", "error", {"job_name": "nightly"})


class FakeModule:
    ID = "fake"
    LABEL = "Fake"
    RETRY_DELAYS_SECONDS = (30, 120)
    REPEAT_AFTER_RESTART = True

    def __init__(self) -> None:
        self.enabled = True
        self.outcomes: list[Exception | None] = []
        self.sent: list[tuple[Notification, dict]] = []

    def wants(self, notification: Notification) -> bool:
        return self.enabled

    def describe(self, notification: Notification) -> dict:
        return {"subject": "[AltaStor] " + notification.subject, "recipients": ["ops@example.test"]}

    def send(self, notification: Notification, entry: dict) -> dict:
        self.sent.append((notification, dict(entry)))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {"refused": []}


def temporary() -> DeliveryError:
    return DeliveryError("smtp_error", "The SMTP server did not accept the message (421 Try again later)", stage="send", retryable=True)


class _RestartCase(unittest.TestCase):
    STATE = "fake_deliveries"

    def setUp(self) -> None:
        in_memory(self)
        self.module = FakeModule()
        self.timers: list[tuple[float, object]] = []
        timer = patch.object(notification_service.threading, "Timer", side_effect=self.fake_timer)
        timer.start()
        self.addCleanup(timer.stop)

    def fake_timer(self, delay, function, args=()):
        self.timers.append((delay, lambda: function(*args)))
        return SimpleNamespace(daemon=True, start=lambda: None)

    def channel(self) -> notification_service._Channel:
        return notification_service._Channel(self.module, self.STATE)

    def drain(self, channel: notification_service._Channel) -> None:
        while True:
            try:
                delivery = channel._queue.get_nowait()
            except queue.Empty:
                return
            channel._attempt(delivery)

    def submit(self, channel: notification_service._Channel) -> dict:
        with patch.object(channel, "_ensure_worker"):
            self.assertTrue(channel.submit(NOTIFICATION))
        return channel.history.items()[0]

    def restarted(self) -> notification_service._Channel:
        self.timers.clear()
        channel = self.channel()
        with patch.object(channel, "_ensure_worker"):
            channel.resume()
        return channel

    def only_entry(self, channel: notification_service._Channel) -> dict:
        items = channel.history.items()
        self.assertEqual(len(items), 1)
        return items[0]


class RetryAfterRestartTests(_RestartCase):
    def test_a_delivery_waiting_for_a_retry_is_sent_after_a_restart(self) -> None:
        before = self.channel()
        self.module.outcomes = [temporary()]
        self.submit(before)
        self.drain(before)
        self.assertEqual(self.only_entry(before)["status"], "retrying")

        after = self.restarted()
        self.assertEqual(len(self.timers), 1)
        self.assertGreater(self.timers[0][0], 20)
        self.timers[0][1]()
        self.drain(after)

        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["attempts"]), ("sent", 2))
        self.assertNotIn("next_attempt_at", entry)
        self.assertEqual(len(self.module.sent), 2)
        self.assertEqual(self.module.sent[1][0], NOTIFICATION)
        self.assertEqual(self.module.sent[1][1]["subject"], "[AltaStor] " + NOTIFICATION.subject)
        self.assertEqual(after.waiting.items(), {})

    def test_a_retry_whose_time_has_passed_goes_at_once(self) -> None:
        before = self.channel()
        self.module.outcomes = [temporary()]
        entry_id = self.submit(before)["id"]
        self.drain(before)
        stored = SYSTEM_STATE[self.STATE]
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        stored["items"] = [{**item, "next_attempt_at": past} if item["id"] == entry_id else item for item in stored["items"]]

        after = self.restarted()
        self.assertEqual(self.timers, [])
        self.drain(after)
        self.assertEqual(self.only_entry(after)["status"], "sent")

    def test_the_attempts_count_on_across_a_restart(self) -> None:
        before = self.channel()
        self.module.outcomes = [temporary(), temporary(), temporary()]
        self.submit(before)
        self.drain(before)

        after = self.restarted()
        self.timers[0][1]()
        self.drain(after)
        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["attempts"]), ("retrying", 2))
        self.assertGreater(self.timers[-1][0], 100)

        final = self.restarted()
        self.timers[0][1]()
        self.drain(final)
        entry = self.only_entry(final)
        self.assertEqual((entry["status"], entry["attempts"]), ("failed", 3))
        self.assertEqual(final.waiting.items(), {})

    def test_a_queued_delivery_is_sent_after_a_restart(self) -> None:
        self.submit(self.channel())
        after = self.restarted()
        self.drain(after)
        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["attempts"]), ("sent", 1))

    def test_switched_off_before_the_retry_it_is_cancelled(self) -> None:
        before = self.channel()
        self.module.outcomes = [temporary()]
        self.submit(before)
        self.drain(before)
        self.module.enabled = False

        after = self.restarted()
        self.timers[0][1]()
        self.drain(after)
        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["error"]["code"]), ("cancelled", "notifications_disabled"))
        self.assertNotIn("next_attempt_at", entry)
        self.assertEqual(len(self.module.sent), 1)
        self.assertEqual(after.waiting.items(), {})


class LeftWaitingTests(_RestartCase):
    def test_a_delivery_left_waiting_without_its_message_ends_failed(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat(timespec="seconds")
        SYSTEM_STATE[self.STATE] = {
            "id": self.STATE,
            "items": [
                {"id": "a1", "event": "job.failed", "subject": "s", "status": "retrying", "attempts": 1, "next_attempt_at": past},
                {"id": "a2", "event": "job.failed", "subject": "s", "status": "queued", "attempts": 0},
                {"id": "a3", "event": "job.failed", "subject": "s", "status": "sent", "attempts": 1},
            ],
        }
        after = self.restarted()
        self.drain(after)
        by_id = {item["id"]: item for item in after.history.items()}
        for entry_id in ("a1", "a2"):
            self.assertEqual(by_id[entry_id]["status"], "failed")
            self.assertEqual(by_id[entry_id]["error"]["code"], "interrupted")
            self.assertNotIn("next_attempt_at", by_id[entry_id])
        self.assertEqual(by_id["a3"]["status"], "sent")
        self.assertEqual(self.module.sent, [])

    def test_kept_messages_without_a_waiting_entry_are_dropped(self) -> None:
        before = self.channel()
        self.submit(before)
        self.drain(before)
        before.waiting.keep("gone", NOTIFICATION)
        after = self.restarted()
        self.assertEqual(after.waiting.items(), {})


class NoRestartTests(_RestartCase):
    def test_a_delivery_is_kept_only_while_it_waits(self) -> None:
        channel = self.channel()
        entry_id = self.submit(channel)["id"]
        self.assertIn(entry_id, channel.waiting.items())
        self.drain(channel)
        self.assertEqual(channel.waiting.items(), {})

    def test_a_retry_without_a_restart_still_goes_out(self) -> None:
        channel = self.channel()
        self.module.outcomes = [temporary()]
        self.submit(channel)
        self.drain(channel)
        self.assertEqual(self.timers[0][0], 30)
        self.timers[0][1]()
        self.drain(channel)
        self.assertEqual(self.only_entry(channel)["status"], "sent")

    def test_the_kept_message_is_not_in_the_history(self) -> None:
        channel = self.channel()
        self.submit(channel)
        entry = self.only_entry(channel)
        self.assertNotIn("body", entry)
        self.assertNotIn("details", entry)


class InterruptedAttemptTests(_RestartCase):
    def interrupted(self, repeat: bool) -> notification_service._Channel:
        self.module.REPEAT_AFTER_RESTART = repeat
        before = self.channel()
        self.submit(before)
        delivery = before._queue.get_nowait()
        stopped = RuntimeError("the server stopped")
        self.module.outcomes = [stopped]
        with self.assertRaises(RuntimeError):
            before._attempt(delivery)
        self.module.outcomes = []
        return self.restarted()

    def test_a_delivery_cut_off_in_the_middle_is_sent_again(self) -> None:
        after = self.interrupted(repeat=True)
        self.drain(after)
        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["attempts"]), ("sent", 2))

    def test_a_channel_that_does_not_repeat_fails_one_cut_off(self) -> None:
        after = self.interrupted(repeat=False)
        self.drain(after)
        entry = self.only_entry(after)
        self.assertEqual((entry["status"], entry["error"]["code"]), ("failed", "interrupted"))
        self.assertIn("not run again", entry["error"]["message"])
        self.assertEqual(len(self.module.sent), 1)
        self.assertEqual(after.waiting.items(), {})

    def test_a_channel_that_does_not_repeat_still_sends_one_never_started(self) -> None:
        self.module.REPEAT_AFTER_RESTART = False
        self.submit(self.channel())
        after = self.restarted()
        self.drain(after)
        self.assertEqual(self.only_entry(after)["status"], "sent")

    def test_a_retry_waiting_its_turn_is_not_taken_for_one_cut_off(self) -> None:
        self.module.REPEAT_AFTER_RESTART = False
        before = self.channel()
        self.module.outcomes = [temporary()]
        self.submit(before)
        self.drain(before)
        after = self.restarted()
        self.timers[0][1]()
        self.drain(after)
        self.assertEqual(self.only_entry(after)["status"], "sent")

    def test_the_real_channels_say_whether_they_repeat(self) -> None:
        from python.srv.services import chat_channels, email_channel

        self.assertIs(email_channel.REPEAT_AFTER_RESTART, True)
        for platform in chat_channels.PLATFORMS.values():
            self.assertIs(platform.REPEAT_AFTER_RESTART, True)


class StartupTests(unittest.TestCase):
    def test_every_channel_is_taken_up_and_one_failure_does_not_stop_the_others(self) -> None:
        with ExitStack() as stack:
            email = stack.enter_context(patch.object(notification_service.EMAIL, "resume", side_effect=RuntimeError("catalog")))
            others = [
                stack.enter_context(patch.object(channel, "resume", return_value=0))
                for channel in notification_service.MESSAGING.values()
            ]
            notification_service.resume_waiting()
        email.assert_called_once()
        for other in others:
            other.assert_called_once()


class SecondsUntilTests(unittest.TestCase):
    def test_future_past_and_unreadable_times(self) -> None:
        future = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat(timespec="seconds")
        self.assertGreater(notification_service._seconds_until(future), 50)
        self.assertEqual(notification_service._seconds_until("2020-01-01T00:00:00+00:00"), 0.0)
        self.assertEqual(notification_service._seconds_until(None), 0.0)
        self.assertEqual(notification_service._seconds_until("soon"), 0.0)


if __name__ == "__main__":
    unittest.main()
