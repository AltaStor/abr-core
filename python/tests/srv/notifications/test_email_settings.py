# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import SYSTEM_STATE
from python.srv.services import email_channel, notification_service
from python.srv.services.api_errors import ApiError
from python.tests.srv.notifications.test_script_channel import in_memory

INJECTED = "x\r\nBcc: evil@example.test"
SMTP = {"host": "smtp.example.test", "port": 2525, "security": "none", "auth": "none"}
COMPLETE = {"smtp": SMTP, "sender": {"address": "abr@example.test"}, "recipients": ["ops@example.test"]}


class _EmailCase(unittest.TestCase):
    def setUp(self) -> None:
        in_memory(self)

    def refused(self, call, *args, **kwargs) -> ApiError:
        with self.assertRaises(ApiError) as raised:
            call(*args, **kwargs)
        return raised.exception

    def refused_fields(self, patch_body: dict) -> dict:
        error = self.refused(email_channel.update_settings, patch_body, updated_by="admin")
        self.assertEqual((error.status_code, error.code), (422, "invalid_notification_settings"))
        return error.details["fields"]

    def store_raw(self, **overrides) -> None:
        settings = email_channel.load_settings()
        settings["smtp"].update(SMTP)
        settings["sender"].update({"address": "abr@example.test"})
        settings["recipients"] = ["ops@example.test"]
        for key, value in overrides.items():
            settings[key] = value
        SYSTEM_STATE[email_channel.STATE_ID] = settings


class SenderNameTests(_EmailCase):
    def test_a_name_with_a_line_break_is_refused_and_nothing_is_stored(self) -> None:
        fields = self.refused_fields({"sender": {"name": INJECTED}})
        self.assertEqual(list(fields), ["sender.name"])
        self.assertIn("one line", fields["sender.name"])
        self.assertNotIn(email_channel.STATE_ID, SYSTEM_STATE)

    def test_a_bare_line_feed_a_tab_and_a_nul_are_refused(self) -> None:
        for name in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x7fb"):
            with self.subTest(name=repr(name)):
                self.assertIn("sender.name", self.refused_fields({"sender": {"name": name}}))

    def test_a_line_break_at_either_end_is_trimmed_as_before(self) -> None:
        dto, _changed = email_channel.update_settings({"sender": {"name": "\r\nBackups\n"}}, updated_by="admin")
        self.assertEqual(dto["sender"]["name"], "Backups")

    def test_ordinary_names_are_kept(self) -> None:
        for name in ("AltaStor Backup & Replication", "Резервні копії", "Ops, \"night\" <team>", "n" * 100):
            with self.subTest(name=name):
                dto, _changed = email_channel.update_settings({"sender": {"name": name}}, updated_by="admin")
                self.assertEqual(dto["sender"]["name"], name)

    def test_the_length_limit_still_applies(self) -> None:
        self.assertIn("100", self.refused_fields({"sender": {"name": "n" * 101}})["sender.name"])

    def test_the_test_e_mail_refuses_such_a_name_before_sending(self) -> None:
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, {**COMPLETE, "sender": {"address": "abr@example.test", "name": INJECTED}}, ["qa@example.test"])
        self.assertEqual((error.status_code, error.code), (422, "invalid_notification_settings"))
        self.assertIn("sender.name", error.details["fields"])
        deliver.assert_not_called()

    def test_a_name_stored_before_the_check_is_named_on_the_next_change(self) -> None:
        self.store_raw(sender={"address": "abr@example.test", "name": INJECTED})
        self.assertIn("sender.name", self.refused_fields({"repository_space_threshold_percent": 90}))
        dto, _changed = email_channel.update_settings({"sender": {"name": "Backups"}}, updated_by="admin")
        self.assertEqual(dto["sender"]["name"], "Backups")


class MessageHeaderTests(_EmailCase):
    def test_a_stored_name_with_a_line_break_adds_no_header(self) -> None:
        settings = {"sender": {"address": "abr@example.test", "name": INJECTED}}
        message = email_channel.build_message(settings, ["ops@example.test"], "Subject", "Body\n")
        self.assertIsNone(message["Bcc"])
        self.assertIn("abr@example.test", message["From"])
        raw = message.as_bytes()
        self.assertNotIn(b"\nBcc:", raw)
        self.assertNotIn(b"evil@example.test\r\n", raw.split(b"\n\n", 1)[0].replace(b"From:", b""))

    def test_a_subject_with_a_line_break_stays_one_header(self) -> None:
        settings = {"sender": {"address": "abr@example.test", "name": "Backups"}}
        message = email_channel.build_message(settings, ["ops@example.test"], "Backup job \"a\"\r\nX-Evil: 1", "Body\n")
        self.assertIsNone(message["X-Evil"])
        self.assertIn("X-Evil: 1", message["Subject"])

    def test_a_notification_with_such_a_stored_name_is_sent(self) -> None:
        self.store_raw(sender={"address": "abr@example.test", "name": INJECTED})
        sent = []

        class Client:
            def send_message(self, message, **kwargs):
                sent.append((message, kwargs))
                return {}

        with patch.object(email_channel, "_connect", return_value=Client()), patch.object(email_channel, "_authenticate"), patch.object(email_channel, "_quit"):
            email_channel.deliver(email_channel.load_settings(), ["ops@example.test"], "Subject", "Body\n")
        message, kwargs = sent[0]
        self.assertEqual(kwargs["to_addrs"], ["ops@example.test"])
        self.assertIsNone(message["Bcc"])


class SmtpHostTests(_EmailCase):
    def test_host_names_and_addresses_are_accepted(self) -> None:
        long_name = ".".join(["a" * 63] * 3 + ["b" * 61])
        self.assertEqual(len(long_name), 253)
        for host in ("smtp.example.com", "mail", "192.0.2.10", "2001:db8::25", "[2001:db8::25]", "::1", long_name):
            with self.subTest(host=host):
                dto, _changed = email_channel.update_settings({"smtp": {"host": host}}, updated_by="admin")
                self.assertEqual(dto["smtp"]["host"], host)

    def test_overlong_or_malformed_hosts_are_refused(self) -> None:
        too_long = ".".join(["a" * 63] * 3 + ["b" * 62])
        for host in ("a" * 300, "abc" * 100, too_long, "a" * 64, "2001:db8::25::1", "[smtp.example.com]", "smtp example.com", "-smtp.example.com"):
            with self.subTest(host=host[:40]):
                self.assertIn("smtp.host", self.refused_fields({"smtp": {"host": host}}))


class TestRecipientsTests(_EmailCase):
    def test_to_as_text_is_refused_as_not_a_list(self) -> None:
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, COMPLETE, "qa@example.test")
        self.assertEqual((error.status_code, error.code), (422, "invalid_notification_settings"))
        self.assertEqual(error.details["fields"], {"to": "Must be a list of e-mail addresses"})
        deliver.assert_not_called()

    def test_to_with_items_that_are_not_text_is_refused_not_crashed(self) -> None:
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, COMPLETE, ["qa@example.test", 5])
        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.details["fields"], {"to": "5"})
        deliver.assert_not_called()

    def test_a_list_still_sends_to_those_addresses(self) -> None:
        with patch.object(email_channel, "deliver", return_value=[]) as deliver:
            result = notification_service.send_test(COMPLETE, [" qa@example.test ", ""])
        self.assertEqual(result["recipients"], ["qa@example.test"])
        self.assertEqual(deliver.call_args.args[1], ["qa@example.test"])

    def test_without_to_the_configured_recipients_get_it(self) -> None:
        with patch.object(email_channel, "deliver", return_value=[]):
            self.assertEqual(notification_service.send_test(COMPLETE, None)["recipients"], ["ops@example.test"])


    def test_to_is_enough_while_no_recipients_are_saved(self) -> None:
        self.store_raw(recipients=[])
        with patch.object(email_channel, "deliver", return_value=[]) as deliver:
            result = notification_service.send_test(None, ["qa@example.test"])
        self.assertEqual(result["recipients"], ["qa@example.test"])
        self.assertEqual(deliver.call_args.args[1], ["qa@example.test"])

    def test_without_to_or_saved_recipients_the_test_is_refused(self) -> None:
        self.store_raw(recipients=[])
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, None, None)
        self.assertEqual(error.details["fields"], {"recipients": "Add at least one recipient"})
        deliver.assert_not_called()

    def test_to_of_blank_addresses_counts_as_no_to(self) -> None:
        self.store_raw(recipients=[])
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, None, [" ", ""])
        self.assertEqual(error.details["fields"], {"recipients": "Add at least one recipient"})
        deliver.assert_not_called()

    def test_to_does_not_excuse_other_missing_settings(self) -> None:
        self.store_raw(recipients=[], sender={"address": "", "name": ""})
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, None, ["qa@example.test"])
        self.assertIn("sender.address", error.details["fields"])
        self.assertNotIn("recipients", error.details["fields"])
        deliver.assert_not_called()

    def test_to_does_not_excuse_bad_recipients_in_unsaved_changes(self) -> None:
        self.store_raw(recipients=[])
        with patch.object(email_channel, "deliver") as deliver:
            error = self.refused(notification_service.send_test, {"recipients": ["bad"]}, ["qa@example.test"])
        self.assertEqual(error.details["fields"], {"recipients": "Not an e-mail address: bad"})
        deliver.assert_not_called()

    def test_switching_on_still_needs_a_recipient(self) -> None:
        self.store_raw(recipients=[])
        fields = self.refused_fields({"enabled": True})
        self.assertEqual(fields.get("recipients"), "Add at least one recipient")

if __name__ == "__main__":
    unittest.main()
