# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from python.srv.core import mongo_db_wrapper
from python.srv.db import db_manager

STORED = datetime(2026, 11, 17, 16, 11, 47, 399000, tzinfo=timezone.utc)


class ClientZoneTests(unittest.TestCase):
    def test_the_catalog_client_returns_timestamps_in_utc_with_their_zone(self) -> None:
        with patch.object(db_manager, "MongoClient") as client_class, patch.object(db_manager.Database, "_ensure_indexes"):
            db_manager.Database.connect("mongodb://catalog.example.test:27017")
        options = client_class.call_args.kwargs
        self.assertIs(options["tz_aware"], True)
        self.assertIs(options["tzinfo"], timezone.utc)


class NormalizedValueTests(unittest.TestCase):
    def test_a_timestamp_from_the_catalog_keeps_its_zone(self) -> None:
        self.assertEqual(mongo_db_wrapper._normalize_value(STORED), "2026-11-17T16:11:47.399000+00:00")

    def test_nested_timestamps_keep_their_zone(self) -> None:
        record = mongo_db_wrapper._normalize_record({"updated_at": STORED, "items": [{"at": STORED}], "meta": {"at": STORED}})
        self.assertEqual(record["items"][0]["at"], "2026-11-17T16:11:47.399000+00:00")
        self.assertEqual(record["meta"]["at"], "2026-11-17T16:11:47.399000+00:00")


class SystemStateTests(unittest.TestCase):
    def test_system_state_is_read_with_zoned_text_timestamps(self) -> None:
        database = MagicMock()
        database.get.return_value = {"id": "email_notifications", "updated_at": STORED, "sender": {"name": "Backups"}}
        with patch.object(mongo_db_wrapper, "get_database", return_value=database):
            record = mongo_db_wrapper.load_system_state("email_notifications")
        self.assertEqual(record["updated_at"], "2026-11-17T16:11:47.399000+00:00")
        self.assertEqual(record["sender"], {"name": "Backups"})

    def test_missing_system_state_is_none(self) -> None:
        database = MagicMock()
        database.get.return_value = None
        with patch.object(mongo_db_wrapper, "get_database", return_value=database):
            self.assertIsNone(mongo_db_wrapper.load_system_state("email_notifications"))


if __name__ == "__main__":
    unittest.main()
