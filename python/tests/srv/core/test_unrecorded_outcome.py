# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from concurrent.futures import Future
from unittest.mock import patch

from python.srv.core import task_pool as tp_mod
from python.srv.services.api_errors import api_error

TASK_ID = "task-1"


def _catalog_down(*_args, **_kwargs):
    raise api_error(503, "catalog_unavailable", "The catalog database is unavailable; the change was not applied.")


class UnrecordedOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = tp_mod.TaskProcessPool(worker_count=1)
        tp_mod.TASKS[TASK_ID] = {
            "id": TASK_ID,
            "task_type": "backup",
            "status": "running",
            "parameters": {"repository_id": "repo-1", "vm_name": "web01"},
            "result": {"progress": 80},
        }
        self.addCleanup(tp_mod.TASKS.pop, TASK_ID, None)
        self.discard = patch.object(tp_mod, "discard_unregistered_output", return_value=True).start()
        self.register = patch.object(tp_mod, "register_backup_from_completed_task", return_value=None).start()
        self.addCleanup(patch.stopall)

    def _finish(self, status: str = "completed", task_type: str = "backup") -> dict:
        tp_mod.TASKS[TASK_ID]["task_type"] = task_type
        future: Future = Future()
        future.set_result({"task_id": TASK_ID, "status": status, "result": {"message": "boom" if status == "failed" else None}, "updated_at": "now"})
        self.pool._futures[future] = (TASK_ID, task_type, None)
        self.pool._complete_task(future)
        return tp_mod.TASKS[TASK_ID]

    def test_a_backup_whose_point_cannot_be_recorded_fails_naming_the_catalog_and_loses_its_data(self) -> None:
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down):
            task = self._finish()

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"]["failure_kind"], "catalog_unavailable")
        self.assertEqual(
            task["result"]["message"],
            "The backup of web01 ran to the end, but its restore point could not be recorded because the catalog "
            "database is unavailable; its data was removed from the repository. Run the job again once the database "
            "is back",
        )
        self.discard.assert_called_once()
        self.assertEqual(self.discard.call_args.args[0]["id"], TASK_ID)
        self.assertEqual(self.pool._unrecorded, {TASK_ID})

    def test_nobody_sees_completed_while_the_outcome_is_being_written(self) -> None:
        seen = []

        def write_while_watching(record):
            seen.append(tp_mod.TASKS[TASK_ID]["status"])
            _catalog_down()

        self.discard.side_effect = lambda task: seen.append(tp_mod.TASKS[TASK_ID]["status"]) or True
        with patch.object(tp_mod, "persist_task", side_effect=write_while_watching):
            task = self._finish()

        self.assertEqual(task["status"], "failed")
        self.assertNotIn("completed", seen)

    def test_a_recorded_outcome_is_what_the_session_shows(self) -> None:
        with patch.object(tp_mod, "persist_task"):
            task = self._finish()

        self.assertEqual((task["status"], task["result"]["progress"]), ("completed", 100))

    def test_data_that_could_not_be_removed_is_said_to_stay(self) -> None:
        self.discard.return_value = False
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down):
            task = self._finish()

        self.assertIn("its data stays in the repository until it is removed by hand", task["result"]["message"])

    def test_the_outcome_is_recorded_once_the_catalog_answers_again(self) -> None:
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down):
            self._finish()
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down) as still_down:
            self.pool._record_unrecorded_outcomes()
        self.assertEqual(still_down.call_count, 1)
        self.assertEqual(self.pool._unrecorded, {TASK_ID})

        with patch.object(tp_mod, "persist_task") as back:
            self.pool._record_unrecorded_outcomes()

        recorded = back.call_args.args[0]
        self.assertEqual((recorded["status"], recorded["result"]["failure_kind"]), ("failed", "catalog_unavailable"))
        self.assertEqual(self.pool._unrecorded, set())

    def test_a_failure_that_is_not_the_catalog_keeps_the_old_behaviour(self) -> None:
        self.register.side_effect = RuntimeError("bug in registration")
        with patch.object(tp_mod, "persist_task"):
            task = self._finish()

        self.assertEqual(task["status"], "completed")
        self.discard.assert_not_called()
        self.assertEqual(self.pool._unrecorded, set())

    def test_a_restore_is_left_as_it_was(self) -> None:
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down):
            task = self._finish(task_type="restore")

        self.assertEqual(task["status"], "completed")
        self.discard.assert_not_called()
        self.assertEqual(self.pool._unrecorded, set())

    def test_a_recorded_point_keeps_the_backup_completed(self) -> None:
        self.register.return_value = {"id": "point-1", "chain_id": "chain-1"}
        calls = []

        def second_write_fails(record):
            calls.append(record)
            if len(calls) > 1:
                _catalog_down()

        with patch.object(tp_mod, "persist_task", side_effect=second_write_fails):
            task = self._finish()

        self.assertEqual((task["status"], task["result"]["restore_point_id"]), ("completed", "point-1"))
        self.discard.assert_not_called()
        self.assertEqual(self.pool._unrecorded, {TASK_ID})

    def test_a_backup_that_had_already_failed_keeps_its_own_failure(self) -> None:
        with patch.object(tp_mod, "persist_task", side_effect=_catalog_down):
            task = self._finish(status="failed")

        self.assertEqual((task["status"], task["result"]["message"]), ("failed", "boom"))
        self.discard.assert_not_called()
        self.assertEqual(self.pool._unrecorded, {TASK_ID})

    def test_retries_are_spaced_out(self) -> None:
        self.pool._unrecorded.add(TASK_ID)
        with patch.object(tp_mod.threading, "Thread") as thread, patch.object(tp_mod.time, "monotonic", side_effect=[100.0, 110.0, 131.0]):
            thread.return_value.is_alive.return_value = False
            self.pool._retry_unrecorded_outcomes()
            self.pool._retry_unrecorded_outcomes()
            self.pool._retry_unrecorded_outcomes()
        self.assertEqual(thread.call_count, 2)

    def test_nothing_pending_starts_nothing(self) -> None:
        with patch.object(tp_mod.threading, "Thread") as thread:
            self.pool._retry_unrecorded_outcomes()
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
