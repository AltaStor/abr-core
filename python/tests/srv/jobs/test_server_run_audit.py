# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import AUDIT_RECORDS, JOBS, TASKS
from python.srv.services import abr_v41, audit_service, backup_copy, replication_service
from python.tests.srv.backup import test_verify_marks
from python.tests.srv.jobs import (
    test_backup_copy,
    test_replication_triggers,
    test_scheduler,
)


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class _Audit(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        AUDIT_RECORDS.clear()
        self.addCleanup(AUDIT_RECORDS.clear)

    def server_records(self) -> list[dict]:
        return [record for record in AUDIT_RECORDS if record.get("user") == audit_service.SYSTEM_USER]

    def assert_started_by_the_server(self, record: dict, *, function_id: str, target: dict, session_ids: list[str], trigger: str) -> None:
        self.assertEqual(
            (record["function_id"], record["result"], record["interface"], record.get("role")),
            (function_id, audit_service.ALLOWED, audit_service.SERVER_INTERFACE, None),
        )
        self.assertEqual(record["target"], target)
        self.assertEqual(record["session_id"], session_ids[0])
        self.assertEqual(record["details"]["trigger"], trigger)
        if len(session_ids) > 1:
            self.assertEqual(record["details"]["session_ids"], session_ids)
        else:
            self.assertNotIn("session_ids", record["details"])


class ScheduledBackupAuditTests(_Audit):
    _register_vm_resources = test_scheduler.JobSchedulerTests._register_vm_resources

    def setUp(self) -> None:
        super().setUp()
        test_scheduler.JobSchedulerTests.setUp(self)
        self.addCleanup(test_scheduler.JobSchedulerTests.tearDown, self)

    def due_job(self, *vm_ids: str, schedule_key: str = "schedule", next_key: str = "next_incremental_run_at") -> None:
        self._register_vm_resources(*vm_ids)
        JOBS["job-1"] = abr_v41.normalize_job_record(
            {
                "id": "job-1",
                "name": "Nightly",
                "job_type": "backup",
                "hypervisor_id": "hv-1",
                "inventory_id": "hv-1",
                "vm_ids": list(vm_ids),
                "status": "active",
                "enabled": True,
                "created_at": _ago(60),
                schedule_key: {"kind": "daily", "time": "03:00", "enabled": True},
            }
        )
        JOBS["job-1"][next_key] = _ago(1)

    def started(self) -> list[dict]:
        return sorted((task for task in TASKS.values() if task.get("job_id") == "job-1"), key=lambda task: task["created_at"])

    def test_a_scheduled_run_is_audited_as_a_job_start_by_the_server(self) -> None:
        self.due_job("vm-1")

        self.scheduler._dispatch_due_jobs()

        [task] = self.started()
        [record] = self.server_records()
        self.assert_started_by_the_server(
            record, function_id="job.start", target={"type": "job", "id": "job-1", "name": "Nightly"}, session_ids=[task["id"]], trigger="schedule"
        )
        self.assertEqual(record["details"]["schedule"], "Incremental")

    def test_a_scheduled_run_of_several_vms_names_every_session(self) -> None:
        self.due_job("vm-1", "vm-2")

        self.scheduler._dispatch_due_jobs()

        [record] = self.server_records()
        self.assertEqual(record["details"]["session_ids"], [task["id"] for task in self.started()])
        self.assertEqual(len(record["details"]["session_ids"]), 2)

    def test_an_active_full_schedule_names_the_schedule_that_fired(self) -> None:
        self.due_job("vm-1", schedule_key="active_full_schedule", next_key="active_full_next_run_at")

        self.scheduler._dispatch_due_jobs()

        [record] = self.server_records()
        self.assertEqual(record["details"]["schedule"], "ActiveFull")

    def test_a_job_that_is_not_due_leaves_no_record(self) -> None:
        self.due_job("vm-1")
        JOBS["job-1"]["next_incremental_run_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        self.scheduler._dispatch_due_jobs()

        self.assertEqual((self.started(), self.server_records()), ([], []))

    def test_a_catalog_that_refuses_the_record_does_not_stop_the_run(self) -> None:
        self.due_job("vm-1")

        with patch("python.srv.services.audit_service.append_audit_record", side_effect=RuntimeError("catalog down")):
            self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(self.started()), 1)
        self.assertGreater(datetime.fromisoformat(JOBS["job-1"]["next_incremental_run_at"]), datetime.now(timezone.utc))


class CopyRunAuditTests(_Audit, test_backup_copy._Catalog):
    _finished_backup = test_backup_copy.CopyTriggerTests._finished_backup
    _copy_sessions = test_backup_copy.CopyTriggerTests._copy_sessions

    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")

    def copy_records(self, job: dict) -> list[dict]:
        return [record for record in self.server_records() if (record.get("target") or {}).get("id") == job["id"]]

    def test_an_immediate_copy_is_audited_with_its_trigger(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        backup_copy.on_session_finished(self._finished_backup())

        [session] = self._copy_sessions(job["id"])
        [record] = self.copy_records(job)
        self.assert_started_by_the_server(
            record, function_id="job.start", target={"type": "job", "id": job["id"], "name": "Offsite copy"}, session_ids=[session["id"]], trigger="immediate"
        )

    def test_a_copy_run_after_its_job_is_audited_with_its_trigger(self) -> None:
        job = self._copy_job(run_after_job="Nightly")

        backup_copy.on_session_finished(self._finished_backup())

        [record] = self.copy_records(job)
        self.assertEqual((record["details"]["trigger"], record["session_id"]), ("after-job", self._copy_sessions(job["id"])[0]["id"]))

    def test_a_copy_that_does_not_start_leaves_no_record(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        backup_copy.on_session_finished(self._finished_backup(status="failed", result={}))

        self.assertEqual(self.copy_records(job), [])

    def test_a_manual_copy_start_is_not_recorded_again_as_the_server(self) -> None:
        job = self._copy_job()

        abr_v41.start_job_record(job["id"])

        self.assertEqual(self.copy_records(job), [])

    def test_a_scheduled_copy_is_audited_with_the_schedule_that_fired(self) -> None:
        job = self._copy_job(schedule={"type": "daily", "time": "03:00"})
        JOBS[job["id"]].update({"created_at": _ago(5), "next_incremental_run_at": _ago(5)})

        JobScheduler(poll_interval_seconds=0.1)._dispatch_due_jobs()

        [session] = self._copy_sessions(job["id"])
        [record] = self.copy_records(job)
        self.assertEqual((record["details"], record["session_id"]), ({"trigger": "schedule", "schedule": "Incremental"}, session["id"]))


class CopyWithNoSourceAuditTests(_Audit, test_backup_copy._Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")

    def test_a_scheduled_copy_with_no_source_left_is_audited_as_failed_with_the_reason(self) -> None:
        copy = self._copy_job(schedule={"type": "daily", "time": "03:00"})
        JOBS[copy["id"]].update({"created_at": _ago(5), "next_incremental_run_at": _ago(5)})
        JOBS.pop("nightly")

        JobScheduler(poll_interval_seconds=0.1)._dispatch_due_jobs()

        [record] = self.server_records()
        self.assertEqual((record["function_id"], record["result"], record["interface"]), ("job.start", audit_service.FAILED, audit_service.SERVER_INTERFACE))
        self.assertEqual(record["target"], {"type": "job", "id": copy["id"], "name": "Offsite copy"})
        self.assertNotIn("session_id", record)
        self.assertEqual(record["error"]["code"], "backup_copy_no_source")
        self.assertIn("source job Nightly was deleted", record["error"]["message"])


class ReplicationRunAuditTests(_Audit, test_replication_triggers._Triggers):
    _scheduled = test_replication_triggers.SchedulerTests._scheduled
    _dispatch = test_replication_triggers.SchedulerTests._dispatch

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(patch.stopall)
        for target in ("python.srv.core.job_scheduler.persist_task", "python.srv.services.internal.replication.replication_session.persist_task"):
            patch(target, side_effect=lambda task: TASKS.__setitem__(task["id"], task)).start()

    def replication_records(self) -> list[dict]:
        return [record for record in self.server_records() if (record.get("target") or {}).get("id") == self.job_id]

    def test_a_scheduled_replication_is_audited(self) -> None:
        self._scheduled(due=True)

        [session] = self._dispatch()

        [record] = self.replication_records()
        self.assert_started_by_the_server(
            record,
            function_id="job.start",
            target={"type": "job", "id": self.job_id, "name": self.job["name"]},
            session_ids=[session["id"]],
            trigger="schedule",
        )

    def test_a_replication_after_its_source_is_audited_with_that_trigger(self) -> None:
        replication_service.on_session_finished(self._backup_session("b-1", "completed"))

        [session] = self._dispatch()

        [record] = self.replication_records()
        self.assertEqual((record["details"]["trigger"], record["session_id"]), ("after-source", session["id"]))

    def test_a_replication_that_is_not_due_leaves_no_record(self) -> None:
        self._scheduled(due=False)

        self.assertEqual((self._dispatch(), self.replication_records()), ([], []))


class ScheduledVerifyAuditTests(_Audit, test_verify_marks._Catalog):
    def run_verify(self) -> dict:
        with patch.object(abr_v41.threading, "Thread", test_verify_marks._ImmediateThread), patch.object(
            abr_v41, "_run_verify_session", return_value={"result": "valid"}
        ):
            return abr_v41.run_job_verify("job-1")

    def test_every_session_of_a_scheduled_verify_is_audited_as_a_backup_verify(self) -> None:
        self.chain()
        other = {key: value for key, value in test_verify_marks._point("other-full", parent=None, day=7).items() if key != "chain_id"}
        self.store(abr_v41.normalize_backup_point({**other, "vm_id": "vm-2", "vm_name": "VM-2"}))

        run = self.run_verify()

        records = self.server_records()
        sessions = [TASKS[session_id] for session_id in run["session_ids"]]
        self.assertEqual(len(records), len(sessions))
        self.assertEqual(len(records), 2)
        for record, session in zip(records, sessions):
            chain_id = session["parameters"]["backup_id"]
            self.assertEqual(record["target"], audit_service.target_from_path({"backup_id": chain_id}))
            self.assertEqual((record["function_id"], record["session_id"], record["details"]["trigger"]), ("backup.verify", session["id"], "schedule"))
            self.assertEqual(record["details"]["job"], {"type": "job", "id": "job-1", "name": JOBS["job-1"]["name"]})


if __name__ == "__main__":
    unittest.main()
