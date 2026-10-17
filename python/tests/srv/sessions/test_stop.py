# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import JOBS, TASKS
from python.srv.services import abr_v41


def _task(task_id: str, status: str, **extra: object) -> dict:
    return {
        "id": task_id,
        "task_type": "backup",
        "status": status,
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "parameters": {"vm_name": "vm"},
        "result": {"progress": 0, "resolved_backup_type": "ActiveFull", "resolved_backup_reason": "FirstRun"},
        "logs": [{"level": "info", "message": "Queued backup session for vm"}],
        "created_at": "2026-10-10T07:03:10+00:00",
        "updated_at": "2026-10-10T07:03:10+00:00",
        **extra,
    }


class StopQueuedSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        persist = patch.object(abr_v41, "persist_task")
        persist.start()
        self.addCleanup(persist.stop)

    def test_a_queued_session_stopped_gets_its_end_time_and_a_reason(self) -> None:
        TASKS["s1"] = _task("s1", "queued")
        before = datetime.now(timezone.utc)

        response = abr_v41.stop_session("s1")
        session = abr_v41.get_session("s1")

        self.assertEqual(response["state"], "stopped")
        self.assertEqual(session["state"], "stopped")
        self.assertEqual(session["message"], "Stopped before execution started")
        self.assertGreaterEqual(datetime.fromisoformat(session["finished_at"]), before)
        self.assertEqual(session["errors"], [{"message": "Stopped before execution started"}])
        self.assertEqual(session["resolved_backup_type"], "ActiveFull")
        self.assertEqual(session["logs"][-1]["message"], "Session stopped before execution started")

    def test_a_running_session_is_only_asked_to_stop_and_finishes_later(self) -> None:
        TASKS["s1"] = _task("s1", "running")

        response = abr_v41.stop_session("s1")
        session = abr_v41.get_session("s1")

        self.assertEqual(response["state"], "stopping")
        self.assertIsNone(session["finished_at"])
        self.assertIsNone(session["message"])

    def test_a_session_stopped_in_the_queue_before_this_fix_shows_when_and_why(self) -> None:
        TASKS["s1"] = _task(
            "s1",
            "stopped",
            updated_at="2026-10-10T07:03:12+00:00",
            logs=[
                {"level": "info", "message": "Queued backup session for vm"},
                {"level": "warn", "message": "Session stopped before execution started"},
            ],
        )

        session = abr_v41.get_session("s1")

        self.assertEqual(session["finished_at"], "2026-10-10T07:03:12+00:00")
        self.assertEqual(session["message"], "Stopped before execution started")

    def test_an_end_time_read_back_without_a_timezone_is_shown_in_utc(self) -> None:
        TASKS["s1"] = _task(
            "s1",
            "stopped",
            updated_at="2026-10-10T07:03:11.620000",
            logs=[{"level": "warn", "message": "Session stopped before execution started"}],
        )

        self.assertEqual(abr_v41.get_session("s1")["finished_at"], "2026-10-10T07:03:11.620000+00:00")

    def test_an_unfinished_session_is_not_given_an_end_time(self) -> None:
        for status in ("queued", "running", "stopping"):
            with self.subTest(status=status):
                TASKS["s1"] = _task("s1", status, updated_at="2026-10-10T07:05:00+00:00")
                self.assertIsNone(abr_v41.get_session("s1")["finished_at"])

    def test_a_recorded_end_time_and_message_are_kept(self) -> None:
        TASKS["s1"] = _task(
            "s1",
            "stopped",
            updated_at="2026-10-10T07:09:00+00:00",
            result={"progress": 40, "message": "Session stop requested", "finished_at": "2026-10-10T07:08:00+00:00"},
        )

        session = abr_v41.get_session("s1")

        self.assertEqual((session["finished_at"], session["message"]), ("2026-10-10T07:08:00+00:00", "Session stop requested"))

    def test_stopping_a_finished_session_changes_nothing(self) -> None:
        TASKS["s1"] = _task("s1", "queued")
        abr_v41.stop_session("s1")
        stamped = abr_v41.get_session("s1")["finished_at"]

        response = abr_v41.stop_session("s1")

        self.assertEqual(response["message"], "Session is already finished")
        self.assertEqual(abr_v41.get_session("s1")["finished_at"], stamped)


if __name__ == "__main__":
    unittest.main()
