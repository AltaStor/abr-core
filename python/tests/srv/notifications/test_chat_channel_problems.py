# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import SYSTEM_STATE
from python.srv.services import apprise_runner, chat_channels
from python.srv.services.api_errors import ApiError

CHAT_PROBLEM = "Not a chat ID: abc. Give a chat's number, such as -1001234567890, or a public channel's @name"
TOKEN = "123456789:AAEhBP0av18HGtt5hb8sMV9dUtFvBc4yWzQ"


class ChatChannelProblemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = dict(SYSTEM_STATE)
        SYSTEM_STATE.clear()
        self.addCleanup(lambda: (SYSTEM_STATE.clear(), SYSTEM_STATE.update(self.saved)))
        self.check = self.enterContext(patch.object(apprise_runner, "check", return_value=None))
        self.enterContext(patch.object(apprise_runner, "program", return_value="/usr/bin/apprise"))

    def _fields(self, channel, body: dict) -> dict:
        with self.assertRaises(ApiError) as raised:
            channel.update_settings(body, updated_by="admin")
        self.assertEqual((raised.exception.status_code, raised.exception.code), (422, "invalid_notification_settings"))
        return raised.exception.details["fields"]

    def test_a_bad_chat_is_listed_with_a_bad_token(self) -> None:
        fields = self._fields(chat_channels.TELEGRAM, {"bot_token": "bad", "chat_ids": ["abc"]})

        self.assertEqual(sorted(fields), ["bot_token", "chat_ids"])
        self.assertEqual(fields["chat_ids"], CHAT_PROBLEM)
        self.assertTrue(fields["bot_token"].startswith("Not a bot token"))

    def test_a_bad_chat_is_listed_with_an_unknown_event(self) -> None:
        fields = self._fields(chat_channels.TELEGRAM, {"chat_ids": ["abc"], "events": {"no.such": True}})

        self.assertEqual(sorted(fields), ["chat_ids", "events.no.such"])

    def test_switching_on_with_a_bad_token_also_asks_for_a_chat(self) -> None:
        fields = self._fields(chat_channels.TELEGRAM, {"enabled": True, "bot_token": "bad"})

        self.assertTrue(fields["bot_token"].startswith("Not a bot token"))
        self.assertEqual(fields["chat_ids"], "Add at least one chat ID")

    def test_a_refused_shape_keeps_its_own_message(self) -> None:
        fields = self._fields(chat_channels.TELEGRAM, {"enabled": True, "chat_ids": 5, "events": {"no.such": True}})

        self.assertEqual(fields["chat_ids"], "Must be a list of chat IDs")
        self.assertEqual(fields["bot_token"], "Enter the bot token @BotFather gave")

    def test_a_bad_enabled_value_does_not_hide_a_bad_chat(self) -> None:
        fields = self._fields(chat_channels.TELEGRAM, {"enabled": "yes", "chat_ids": ["abc"]})

        self.assertEqual(sorted(fields), ["chat_ids", "enabled"])

    def test_a_webhook_switched_on_with_a_bad_url_lists_the_event_too(self) -> None:
        fields = self._fields(chat_channels.SLACK, {"enabled": True, "webhook_url": "https://example.com/x", "events": {"no.such": True}})

        self.assertEqual(sorted(fields), ["events.no.such", "webhook_url"])

    def test_apprise_is_asked_only_when_nothing_else_is_wrong(self) -> None:
        self._fields(chat_channels.TELEGRAM, {"enabled": True, "bot_token": "bad", "chat_ids": ["-1001234567890"]})
        self.check.assert_not_called()

        chat_channels.TELEGRAM.update_settings({"enabled": True, "bot_token": TOKEN, "chat_ids": ["-1001234567890"]}, updated_by="admin")
        self.check.assert_called_once()

    def test_valid_settings_are_still_saved(self) -> None:
        dto, changed = chat_channels.TELEGRAM.update_settings({"bot_token": TOKEN, "chat_ids": ["-1001234567890", "@et_channel"]}, updated_by="admin")

        self.assertEqual(dto["chat_ids"], ["-1001234567890", "@et_channel"])
        self.assertIn("chat_ids", changed)

    def test_the_test_message_lists_every_problem_too(self) -> None:
        with self.assertRaises(ApiError) as raised:
            chat_channels.TELEGRAM.settings_for_test({"bot_token": "bad", "chat_ids": ["abc"]})

        self.assertEqual(sorted(raised.exception.details["fields"]), ["bot_token", "chat_ids"])


if __name__ == "__main__":
    unittest.main()
