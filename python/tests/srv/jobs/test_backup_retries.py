# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.services.job_service import JobService
from python.srv.core.mongo_db_wrapper import JOBS, TASKS
from python.srv.services import abr_v41, backup_retries
from python.srv.services.api_errors import ApiError
from python.tests.srv.jobs.test_guest_consistency_scope import (
    GuestConsistencyScopeTests,
)

FINISHED = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def _failed(task_id: str, job_id: str, *, attempt: int | None = None, created: str = "2026-11-23T11:50:00+00:00", **extra) -> dict:
    parameters = {"vm_name": "db-01", "resolved_backup_type": "Incremental", "requires_task_id": "phase-0"}
    if attempt is not None:
        parameters["retry_attempt"] = attempt
    task = {
        "id": task_id,
        "job_id": job_id,
        "task_type": "backup",
        "status": "failed",
        "run_id": "run-1",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-hv",
        "hypervisor_id": "hv",
        "parameters": parameters,
        "result": {"message": "the host did not answer", "failure_kind": "runtime", "finished_at": FINISHED.isoformat()},
        "created_at": created,
        "updated_at": FINISHED.isoformat(),
    }
    task.update(extra)
    TASKS[task_id] = task
    return task


class BackupJobRetrySettingsTests(GuestConsistencyScopeTests):
    def test_a_new_backup_job_retries_three_times_ten_minutes_apart(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb")

        self.assertEqual(job["retry"], {"count": 3, "interval_minutes": 10})
        self.assertEqual((JOBS[job["id"]]["retry_count"], JOBS[job["id"]]["retry_interval_minutes"]), (3, 10))

    def test_a_job_from_before_backup_retries_does_not_retry(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb")
        JOBS[job["id"]].pop("retry_count")
        JOBS[job["id"]].pop("retry_interval_minutes")

        self.assertEqual(abr_v41.job_to_dto(JOBS[job["id"]])["retry"], {"count": 0, "interval_minutes": 10})

    def test_create_and_edit_take_the_values_and_refuse_bad_ones(self) -> None:
        job = abr_v41.create_backup_job(
            {"name": "hv2", "vms": ["hv-01"], "inventory_id": self.hyperv["id"], "repo_id": "smb", "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"}, "retry_count": 1, "retry_interval_minutes": 0}
        )
        self.assertEqual(job["retry"], {"count": 1, "interval_minutes": 0})

        edited = abr_v41.edit_job_record(job["id"], {"retry_count": 5})
        self.assertEqual(edited["retry"], {"count": 5, "interval_minutes": 0})

        for bad in ({"retry_count": 21}, {"retry_count": -1}, {"retry_interval_minutes": 1441}, {"retry_count": "x"}):
            with self.subTest(bad), self.assertRaises(ApiError) as caught:
                abr_v41.edit_job_record(job["id"], bad)
            self.assertEqual(caught.exception.status_code, 422)

    def test_an_empty_or_null_value_in_an_edit_keeps_the_stored_retries(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb")
        abr_v41.edit_job_record(job["id"], {"retry_count": 5, "retry_interval_minutes": 30})

        edited = abr_v41.edit_job_record(job["id"], {"retry_count": "", "retry_interval_minutes": ""})
        self.assertEqual(edited["retry"], {"count": 5, "interval_minutes": 30})

        with self.assertRaises(ApiError):
            abr_v41.edit_job_record(job["id"], {"retry_count": None, "retry_interval_minutes": None})
        self.assertEqual(abr_v41.job_to_dto(JOBS[job["id"]])["retry"], {"count": 5, "interval_minutes": 30})

    def test_a_vmware_job_takes_retries_too(self) -> None:
        job = self._create("vc", self.vcenter, "vc-01", "local")

        self.assertEqual(abr_v41.edit_job_record(job["id"], {"retry_count": 0})["retry"]["count"], 0)

    def test_a_copy_job_keeps_its_own_retry_defaults(self) -> None:
        self.assertEqual(backup_retries.retry_settings({}), (0, 10))


class RetryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (JOBS, TASKS)])
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Nightly",
            "job_type": "backup",
            "retry_count": 3,
            "retry_interval_minutes": 10,
            "last_run": {"id": "run-1", "trigger": "manual", "started_at": "2026-11-23T11:50:00+00:00", "session_ids": ["t1"]},
        }

    def test_a_failed_backup_is_due_after_the_interval(self) -> None:
        task = _failed("t1", "job-1")

        self.assertEqual(backup_retries.retry_due_at(task), FINISHED + timedelta(minutes=10))

    def test_what_is_not_retried(self) -> None:
        cases = {
            "the last attempt": _failed("a", "job-1", attempt=4),
            "a stopped session": _failed("b", "job-1", status="stopped"),
            "a refusal before it ran": _failed("c", "job-1", result={"failure_kind": "validation", "finished_at": FINISHED.isoformat()}),
            "one already retried": _failed("d", "job-1", retried_by="e"),
            "a copy job session": _failed("f", "job-1", parameters={"copy_job": True}),
            "a session of a job that is gone": _failed("g", "job-gone"),
        }
        for label, task in cases.items():
            with self.subTest(label):
                self.assertIsNone(backup_retries.retry_due_at(task))

    def test_a_job_with_retries_off_never_retries(self) -> None:
        JOBS["job-1"]["retry_count"] = 0

        self.assertIsNone(backup_retries.retry_due_at(_failed("t1", "job-1")))

    def test_a_newer_session_of_the_vm_makes_the_retry_unneeded(self) -> None:
        task = _failed("t1", "job-1")
        TASKS["t2"] = {"id": "t2", "job_id": "job-1", "task_type": "backup", "resource_external_id": "vm-hv", "status": "queued", "created_at": "2026-11-23T11:59:00+00:00"}

        self.assertIsNone(backup_retries.retry_due_at(task))

    def test_a_due_retry_queues_a_new_session_of_that_vm_in_the_same_run(self) -> None:
        _failed("t1", "job-1")

        self.assertEqual(backup_retries.start_due_retries(FINISHED + timedelta(minutes=9)), [])
        [retry] = backup_retries.start_due_retries(FINISHED + timedelta(minutes=10))

        self.assertEqual((retry["status"], retry["trigger"], retry["run_id"]), ("queued", "retry", "run-1"))
        self.assertEqual((retry["parameters"]["retry_attempt"], retry["parameters"]["retry_total"], retry["parameters"]["retry_of"]), (2, 4, "t1"))
        self.assertNotIn("requires_task_id", retry["parameters"])
        self.assertEqual(
            retry["logs"][0]["message"],
            "Attempt 2/4 of the backup of db-01: attempt 1 (session t1) failed: the host did not answer",
        )
        self.assertEqual(TASKS["t1"]["retried_by"], retry["id"])
        self.assertEqual(JOBS["job-1"]["last_run"]["session_ids"], ["t1", retry["id"]])
        self.assertEqual(backup_retries.start_due_retries(FINISHED + timedelta(hours=1)), [])

    def test_a_retry_of_a_session_without_transport_records_it_now(self) -> None:
        _failed("t1", "job-1")
        recorded = {"vsphere_transport": "vddk", "vddk_library": "/opt/libre/libvixDiskLib.so", "vddk_variable": "ABR_VMDK_TOOL_VDDK_LIB"}

        with patch("python.srv.services.abr_v41.session_transport_parameters", return_value=dict(recorded)):
            [retry] = backup_retries.start_due_retries(FINISHED + timedelta(minutes=10))

        self.assertEqual({key: retry["parameters"][key] for key in recorded}, recorded)

    def test_a_retry_keeps_the_transport_its_session_recorded(self) -> None:
        task = _failed("t1", "job-1")
        task["parameters"].update({"vsphere_transport": "soap", "vsphere_api_source": "job"})

        with patch("python.srv.services.abr_v41.session_transport_parameters", side_effect=AssertionError("not asked again")):
            [retry] = backup_retries.start_due_retries(FINISHED + timedelta(minutes=10))

        self.assertEqual((retry["parameters"]["vsphere_transport"], retry["parameters"]["vsphere_api_source"]), ("soap", "job"))

    def test_the_run_waits_for_its_retries_and_ends_with_the_last_attempt(self) -> None:
        _failed("t1", "job-1")

        self.assertEqual(abr_v41.job_run_summary(JOBS["job-1"])["result"], "running")

        [retry] = backup_retries.start_due_retries(FINISHED + timedelta(minutes=10))
        TASKS[retry["id"]] = {**TASKS[retry["id"]], "status": "completed", "result": {"finished_at": "2026-11-23T12:20:00+00:00"}}

        summary = abr_v41.job_run_summary(JOBS["job-1"])
        self.assertEqual((summary["result"], summary["failed"], summary["vm_count"]), ("success", 0, 1))

    def test_a_run_whose_last_attempt_failed_ends_failed(self) -> None:
        _failed("t1", "job-1", attempt=4)

        self.assertEqual(abr_v41.job_run_summary(JOBS["job-1"])["result"], "failed")


class CliRetryOptionTests(unittest.TestCase):
    def _args(self, **values) -> SimpleNamespace:
        return SimpleNamespace(**values)

    def test_job_edit_sends_retries_for_a_backup_job(self) -> None:
        payload = JobService.__new__(JobService).build_edit_payload({"job_type": "backup", "name": "j"}, self._args(retry_count=2, retry_interval_minutes=5))

        self.assertEqual((payload["retry_count"], payload["retry_interval_minutes"]), (2, 5))

    def test_job_edit_still_refuses_them_on_a_replication_job(self) -> None:
        with self.assertRaises(ValueError) as caught:
            JobService.__new__(JobService).build_edit_payload({"job_type": "replication", "name": "r"}, self._args(retry_count=2))

        self.assertIn("--retry-count apply to backup copy jobs only", str(caught.exception))


del GuestConsistencyScopeTests

if __name__ == "__main__":
    unittest.main()
