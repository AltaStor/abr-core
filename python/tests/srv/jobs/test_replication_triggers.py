# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import JOBS, TASKS
from python.srv.services import abr_v41, replication_service
from python.srv.services.internal.replication import replication_session
from python.tests.srv.jobs import test_replication_job

SOURCE_JOB_ID = "job-sql-nightly"


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class _Triggers(unittest.TestCase):
    _host = test_replication_job.ReplicationJobSettingsTests._host
    _vm = test_replication_job.ReplicationJobSettingsTests._vm
    _backup_job = test_replication_job.ReplicationJobSettingsTests._backup_job
    _point = test_replication_job.ReplicationJobSettingsTests._point
    _payload = test_replication_job.ReplicationJobSettingsTests._payload

    def setUp(self) -> None:
        test_replication_job.ReplicationJobSettingsTests.setUp(self)
        self.job = abr_v41.create_replication_job(self._payload(run_after_source=True))
        self.job_id = str(self.job["id"])

    def _backup_session(self, session_id: str, status: str, *, run_id: str = "run-1", point: str | None = "rp-3") -> dict:
        task = {
            "id": session_id,
            "job_id": SOURCE_JOB_ID,
            "task_type": "backup",
            "status": status,
            "run_id": run_id,
            "resource_external_id": "vm-1",
            "created_at": _ago(1),
            "result": {"restore_point_id": point} if point and status == "completed" else {},
        }
        TASKS[session_id] = task
        return task

    def _replication_session(self, status: str, session_id: str = "rep-1") -> dict:
        task = {"id": session_id, "job_id": self.job_id, "task_type": "replication", "status": status, "created_at": _ago(2)}
        TASKS[session_id] = task
        return task

    def _stored(self) -> dict:
        return JOBS[self.job_id]


class AfterSourceTests(_Triggers):
    def test_a_completed_source_backup_with_a_new_point_requests_one_run(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        stored = self._stored()
        self.assertTrue(stored["run_pending"])
        self.assertEqual(stored["pending_trigger"]["trigger"], "after-source")
        self.assertIn("backup job sql-nightly completed a run (session b-1)", stored["pending_trigger"]["detail"])

    def test_a_backup_that_failed_or_was_stopped_requests_nothing(self) -> None:
        for status in ("failed", "stopped", "canceled"):
            with self.subTest(status):
                replication_service.on_session_finished(self._backup_session(f"b-{status}", status, run_id=status))
                self.assertFalse(self._stored().get("run_pending"))

    def test_a_completed_run_without_a_new_point_requests_nothing(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed", point=None))

        self.assertFalse(self._stored().get("run_pending"))

    def test_a_two_step_run_requests_the_run_once_both_steps_completed(self) -> None:
        first = self._backup_session("b-inc", "completed")
        second = self._backup_session("b-syn", "queued")

        replication_service.on_session_finished(first)
        self.assertFalse(self._stored().get("run_pending"))

        second["status"] = "completed"
        second["result"] = {"restore_point_id": "rp-4"}
        replication_service.on_session_finished(second)
        self.assertTrue(self._stored()["run_pending"])

    def test_a_run_whose_second_step_failed_requests_nothing(self) -> None:
        first = self._backup_session("b-inc", "completed")
        self._backup_session("b-syn", "failed")

        replication_service.on_session_finished(first)

        self.assertFalse(self._stored().get("run_pending"))

    def test_jobs_that_do_not_follow_the_source_are_not_asked(self) -> None:
        cases = {
            "start after source is off": lambda: JOBS[self.job_id]["replication"].__setitem__("run_after_source", False),
            "the replication job is disabled": lambda: JOBS[self.job_id].update({"enabled": False, "status": "disabled"}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                change()
                replication_service.on_session_finished(self._backup_session("b-1", "completed"))
                self.assertFalse(self._stored().get("run_pending"))

    def test_a_backup_of_another_job_or_a_restore_requests_nothing(self) -> None:
        other = self._backup_session("b-other", "completed")
        other["job_id"] = "job-other"
        replication_service.on_session_finished(other)
        restore = self._backup_session("r-1", "completed")
        restore["task_type"] = "restore"
        replication_service.on_session_finished(restore)

        self.assertFalse(self._stored().get("run_pending"))

    def test_a_second_trigger_while_one_is_waiting_adds_nothing(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed", run_id="run-1"))
        first = dict(self._stored()["pending_trigger"])

        replication_service.on_session_finished(self._backup_session("b-2", "completed", run_id="run-2"))

        self.assertEqual(self._stored()["pending_trigger"], first)

    def test_any_run_that_starts_serves_the_request_and_says_so(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        session = replication_service.create_replication_session(abr_v41.normalize_job_record(self._stored()), trigger="manual", run_id="m-1")

        self.assertFalse(self._stored()["run_pending"])
        self.assertIsNone(self._stored()["pending_trigger"])
        self.assertTrue(any("also serves the after-source request" in str(line.get("message")) for line in session["logs"]))

    def test_saving_the_replica_keeps_a_request_that_arrived_during_the_run(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        replication_session.save_replica(self.job_id, {"vm_id": "vm-r", "point_id": "rp-3"})

        self.assertTrue(self._stored()["run_pending"])
        self.assertEqual(self._stored()["replica"]["point_id"], "rp-3")

    def test_the_job_view_shows_what_asked_for_the_next_run(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        view = abr_v41.job_to_dto(self._stored())["replication"]

        self.assertTrue(view["run_pending"])
        self.assertEqual(view["pending_trigger"]["trigger"], "after-source")
        self.assertNotIn("detail", view["pending_trigger"])


class SchedulerTests(_Triggers):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(patch.stopall)
        patch("python.srv.core.job_scheduler.persist_task", side_effect=lambda task: TASKS.__setitem__(task["id"], task)).start()
        patch(
            "python.srv.services.internal.replication.replication_session.persist_task",
            side_effect=lambda task: TASKS.__setitem__(task["id"], task),
        ).start()

    def _scheduled(self, *, due: bool) -> None:
        schedule = abr_v41.normalize_schedule_payload({"type": "periodic", "every": "1h", "enabled": True})
        JOBS[self.job_id].update(
            {
                "schedule": schedule,
                "next_incremental_run_at": _ago(1) if due else (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        )

    def _dispatch(self) -> list[dict]:
        JobScheduler(poll_interval_seconds=0.1)._dispatch_due_jobs()
        return [task for task in TASKS.values() if task.get("task_type") == "replication"]

    def test_a_due_schedule_starts_one_run_and_moves_the_next_run_on(self) -> None:
        self._scheduled(due=True)

        sessions = self._dispatch()

        self.assertEqual([task["trigger"] for task in sessions], ["schedule"])
        stored = self._stored()
        self.assertGreater(datetime.fromisoformat(stored["next_incremental_run_at"]), datetime.now(timezone.utc))
        self.assertEqual(stored["last_run"]["trigger"], "schedule")
        self.assertEqual(stored["last_session_id"], sessions[0]["id"])

    def test_a_schedule_that_is_not_due_starts_nothing(self) -> None:
        self._scheduled(due=False)

        self.assertEqual(self._dispatch(), [])

    def test_a_due_schedule_waits_for_the_running_run_and_then_starts_once(self) -> None:
        self._scheduled(due=True)
        running = self._replication_session("running")
        running["updated_at"] = _ago(0)

        self.assertEqual([task["id"] for task in self._dispatch()], ["rep-1"])

        running["status"] = "completed"
        after = [task for task in self._dispatch() if task["id"] != "rep-1"]
        again = [task for task in self._dispatch() if task["id"] != "rep-1"]
        self.assertEqual(len(after), 1)
        self.assertEqual(len(again), 1)

    def test_a_request_starts_a_run_without_a_schedule_and_is_cleared(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        sessions = self._dispatch()

        self.assertEqual([task["trigger"] for task in sessions], ["after-source"])
        self.assertFalse(self._stored()["run_pending"])
        self.assertEqual(self._stored()["last_run"]["trigger"], "after-source")

    def test_a_request_during_a_run_starts_exactly_one_run_after_it(self) -> None:
        running = self._replication_session("running")
        running["updated_at"] = _ago(0)
        for number in (1, 2, 3):
            replication_service.on_session_finished(self._backup_session(f"b-{number}", "completed", run_id=f"run-{number}"))

        self.assertEqual([task["id"] for task in self._dispatch()], ["rep-1"])
        running["status"] = "completed"
        started = [task for task in self._dispatch() if task["id"] != "rep-1"]
        self._dispatch()

        self.assertEqual(len([task for task in TASKS.values() if task.get("task_type") == "replication"]), 2)
        self.assertEqual(started[0]["trigger"], "after-source")

    def test_a_request_and_a_due_schedule_start_one_run_between_them(self) -> None:
        self._scheduled(due=True)
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        sessions = self._dispatch()
        self._dispatch()

        self.assertEqual([task["trigger"] for task in sessions], ["schedule"])
        self.assertFalse(self._stored()["run_pending"])
        self.assertGreater(datetime.fromisoformat(self._stored()["next_incremental_run_at"]), datetime.now(timezone.utc))

    def test_a_disabled_job_starts_nothing_and_drops_its_request(self) -> None:
        self._scheduled(due=True)
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))
        JOBS[self.job_id].update({"enabled": False, "status": "disabled"})

        self.assertEqual(self._dispatch(), [])
        self.assertFalse(self._stored()["run_pending"])

    def test_a_request_of_a_deleted_job_starts_nothing(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))
        JOBS.pop(self.job_id)

        self.assertEqual(self._dispatch(), [])


class ChainGuardTests(_Triggers):
    def test_a_chain_a_replication_reads_cannot_be_deleted(self) -> None:
        self._replication_session("running")
        chain = str(abr_v41.normalize_backup_point(dict(abr_v41.BACKUPS["rp-1"]))["chain_id"])

        for segment in (None, "rp-1"):
            with self.subTest(segment=segment), self.assertRaises(abr_v41.ApiError) as caught:
                abr_v41.delete_backup_chain_request(chain, segment_id=segment, wait=False)
            self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "backup_chain_in_use"))
            self.assertIn("Replication session rep-1 is reading the restore points of this backup chain", caught.exception.message)
        self.assertEqual([task["id"] for task in TASKS.values()], ["rep-1"])

    def test_retention_skips_a_chain_a_replication_reads(self) -> None:
        self._replication_session("queued")
        source = abr_v41.normalize_job_record(JOBS[SOURCE_JOB_ID])
        report = abr_v41.new_retention_report(source, "vm-1", self.repository["id"])

        failures = abr_v41.apply_retention_to_chain(source, vm_id="vm-1", repository_id=self.repository["id"], report=report)

        self.assertEqual(failures, [])
        self.assertEqual(report["removed"], [])
        self.assertIn("replication session rep-1 is reading this chain", report["skipped_reason"])

    def test_a_finished_replication_no_longer_holds_the_chain(self) -> None:
        self._replication_session("completed")

        self.assertEqual(replication_service.sessions_reading(SOURCE_JOB_ID, "vm-1"), [])

    def test_only_the_source_job_and_vm_are_held(self) -> None:
        self._replication_session("running")

        self.assertEqual([task["id"] for task in replication_service.sessions_reading(SOURCE_JOB_ID, "vm-1")], ["rep-1"])
        self.assertEqual(replication_service.sessions_reading(SOURCE_JOB_ID, "vm-2"), [])
        self.assertEqual(replication_service.sessions_reading("job-other", "vm-1"), [])


if __name__ == "__main__":
    unittest.main()
