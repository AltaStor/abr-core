# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from pymongo.errors import ConnectionFailure, OperationFailure

from python.srv.db import db_manager


class _FlakyCatalog:
    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or ConnectionFailure("127.0.0.1:27017: [Errno 111] Connection refused")
        self.calls = 0

    @db_manager._retry()
    def upsert(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "stored"


class CatalogOutageRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        db_manager._outage_since = None
        self.addCleanup(setattr, db_manager, "_outage_since", None)
        sleeper = patch.object(db_manager.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_a_single_blip_on_a_healthy_catalog_is_still_retried(self) -> None:
        catalog = _FlakyCatalog(failures=1)

        self.assertEqual(catalog.upsert(), "stored")

        self.assertEqual(catalog.calls, 2)
        self.assertIsNone(db_manager._outage_since)

    def test_once_every_attempt_failed_the_next_calls_make_a_single_attempt(self) -> None:
        catalog = _FlakyCatalog(failures=100)

        with self.assertLogs("python.srv.db.db_manager", level="ERROR") as captured, self.assertRaises(ConnectionFailure):
            catalog.upsert()
        self.assertEqual(catalog.calls, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertTrue(any("single attempt" in line for line in captured.output))

        for _ in range(2):
            with self.assertRaises(ConnectionFailure):
                catalog.upsert()
        self.assertEqual(catalog.calls, 5)
        self.assertEqual(self.sleep.call_count, 2)

    def test_an_answer_ends_the_single_attempt_mode(self) -> None:
        down = _FlakyCatalog(failures=100)
        with self.assertRaises(ConnectionFailure):
            down.upsert()

        up = _FlakyCatalog(failures=0)
        with self.assertLogs("python.srv.db.db_manager", level="WARNING") as captured:
            self.assertEqual(up.upsert(), "stored")

        self.assertIsNone(db_manager._outage_since)
        self.assertTrue(any("answers again" in line for line in captured.output))
        flaky = _FlakyCatalog(failures=1)
        self.assertEqual(flaky.upsert(), "stored")
        self.assertEqual(flaky.calls, 2)

    def test_the_single_attempt_mode_ends_on_its_own_after_the_window(self) -> None:
        clock = [1000.0]
        with patch.object(db_manager.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ConnectionFailure):
                _FlakyCatalog(failures=100).upsert()

            clock[0] += db_manager.OUTAGE_SINGLE_ATTEMPT_SECONDS + 1
            catalog = _FlakyCatalog(failures=100)
            with self.assertRaises(ConnectionFailure):
                catalog.upsert()

        self.assertEqual(catalog.calls, 3)

    def test_an_error_that_is_not_a_lost_connection_is_not_retried_and_starts_no_outage(self) -> None:
        catalog = _FlakyCatalog(failures=100, error=OperationFailure("bad query"))

        with self.assertRaises(OperationFailure):
            catalog.upsert()

        self.assertEqual(catalog.calls, 1)
        self.assertIsNone(db_manager._outage_since)


if __name__ == "__main__":
    unittest.main()
