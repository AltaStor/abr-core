# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, TASKS
from python.srv.services import abr_v41, backup_catalog

CHAIN_ID = "backup::job-1::vm-1::repo-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(delta: timedelta) -> str:
    return (_now() + delta).isoformat()


def _point(point_id: str, created_at: str, *, full: bool, parent_point_id: str | None = None) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent_point_id,
            "segment_id": parent_point_id or point_id,
            "created_at": created_at,
            "artifact": {"backup_root": f"/does/not/matter/{point_id}"},
        }
    )


def _store(*points: dict) -> None:
    for point in points:
        BACKUPS[point["id"]] = point


def _job(**overrides) -> dict:
    job = {
        "id": "job-1",
        "name": "Nightly",
        "job_type": "backup",
        "repo_id": "repo-1",
        "retention_days": 1,
        "gfs": {},
    }
    job.update(overrides)
    return job


class ChainClockAnomalyTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def test_a_point_dated_ahead_of_the_server_is_reported(self) -> None:
        _store(_point("full-1", _stamp(timedelta(days=30)), full=True))

        anomalies = abr_v41.chain_clock_anomalies(CHAIN_ID)

        self.assertEqual([item["kind"] for item in anomalies], ["ahead_of_server_clock"])
        self.assertEqual(anomalies[0]["point_id"], "full-1")
        self.assertTrue(anomalies[0]["server_time"])

    def test_a_point_a_few_seconds_ahead_is_not_reported(self) -> None:
        _store(_point("full-1", _stamp(timedelta(seconds=30)), full=True))

        self.assertEqual(abr_v41.chain_clock_anomalies(CHAIN_ID), [])

    def test_an_incremental_older_than_its_parent_is_reported(self) -> None:
        _store(
            _point("full-1", _stamp(timedelta(days=30)), full=True),
            _point("incr-1", _stamp(-timedelta(minutes=5)), full=False, parent_point_id="full-1"),
        )

        anomalies = abr_v41.chain_clock_anomalies(CHAIN_ID)

        kinds = sorted(item["kind"] for item in anomalies)
        self.assertEqual(kinds, ["ahead_of_server_clock", "older_than_its_parent"])
        reported = next(item for item in anomalies if item["kind"] == "older_than_its_parent")
        self.assertEqual(reported["point_id"], "incr-1")
        self.assertEqual(reported["parent_point_id"], "full-1")

    def test_a_chain_written_on_a_sane_clock_reports_nothing(self) -> None:
        _store(
            _point("full-1", _stamp(-timedelta(days=2)), full=True),
            _point("incr-1", _stamp(-timedelta(days=1)), full=False, parent_point_id="full-1"),
            _point("incr-2", _stamp(-timedelta(hours=1)), full=False, parent_point_id="incr-1"),
        )

        self.assertEqual(abr_v41.chain_clock_anomalies(CHAIN_ID), [])

    def test_an_unknown_chain_reports_nothing(self) -> None:
        self.assertEqual(abr_v41.chain_clock_anomalies("backup::nope::nope::nope"), [])


class JobClockAnomalyTests(unittest.TestCase):
    @staticmethod
    def _job(last_run_at: str | None) -> dict:
        return {
            "id": "job-1",
            "name": "nightly",
            "job_type": "backup",
            "last_run_at": last_run_at,
        }

    def test_a_last_run_dated_ahead_of_the_server_is_reported(self) -> None:
        anomalies = abr_v41.job_clock_anomalies(self._job(_stamp(timedelta(days=4))))

        self.assertEqual([item["kind"] for item in anomalies], ["last_run_ahead_of_server_clock"])
        self.assertTrue(anomalies[0]["server_time"])

    def test_a_last_run_a_few_seconds_ahead_is_not_reported(self) -> None:
        self.assertEqual(abr_v41.job_clock_anomalies(self._job(_stamp(timedelta(seconds=20)))), [])

    def test_an_ordinary_last_run_reports_nothing(self) -> None:
        self.assertEqual(abr_v41.job_clock_anomalies(self._job(_stamp(timedelta(hours=-3)))), [])

    def test_a_job_that_never_ran_reports_nothing(self) -> None:
        self.assertEqual(abr_v41.job_clock_anomalies(self._job(None)), [])

    def test_the_job_dto_carries_the_anomaly(self) -> None:
        JOBS.clear()
        self.addCleanup(JOBS.clear)
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "nightly",
            "job_type": "backup",
            "inventory_id": "hv-1",
            "repo_id": "repo-1",
            "vm_ids": ["vm-1"],
            "schedule": {"type": "daily", "time": "06:00", "timezone": "UTC", "enabled": True},
            "last_run_at": _stamp(timedelta(days=4)),
        }

        dto = abr_v41.job_to_dto(abr_v41.normalize_job_record(JOBS["job-1"]))

        self.assertEqual([item["kind"] for item in dto["clock_anomalies"]], ["last_run_ahead_of_server_clock"])


class RetentionWithAPointAheadOfTheClockTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    @staticmethod
    def _run_retention() -> None:
        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(_job(), vm_id="vm-1", repository_id="repo-1")

    def test_a_full_dated_ahead_expires_with_the_backups_that_chain_onto_it(self) -> None:
        _store(
            _point("full-1", _stamp(timedelta(days=30)), full=True),
            _point("incr-1", _stamp(-timedelta(days=10)), full=False, parent_point_id="full-1"),
            _point("incr-2", _stamp(-timedelta(days=9)), full=False, parent_point_id="full-1"),
        )

        self._run_retention()

        self.assertEqual(BACKUPS, {})

    def test_a_full_dated_ahead_is_kept_while_a_backup_that_needs_it_is_not_expired(self) -> None:
        _store(
            _point("full-1", _stamp(timedelta(days=30)), full=True),
            _point("incr-1", _stamp(-timedelta(days=10)), full=False, parent_point_id="full-1"),
            _point("incr-2", _stamp(-timedelta(minutes=5)), full=False, parent_point_id="full-1"),
        )

        with self.assertLogs("python.srv.services.abr_v41", level="WARNING") as captured:
            self._run_retention()

        self.assertIn("full-1", BACKUPS)
        self.assertIn("incr-2", BACKUPS)
        self.assertTrue(
            any("full-1" in line and "chains onto it" in line for line in captured.output),
            captured.output,
        )

    def test_an_ordinary_expired_chain_is_still_removed(self) -> None:
        _store(
            _point("full-1", _stamp(-timedelta(days=10)), full=True),
            _point("incr-1", _stamp(-timedelta(days=9)), full=False, parent_point_id="full-1"),
        )

        self._run_retention()

        self.assertEqual(BACKUPS, {})

    def test_a_gfs_protected_full_dated_ahead_is_still_protected(self) -> None:
        full = _point("full-1", _stamp(timedelta(days=30)), full=True)
        full["gfs_marks"] = {"weekly": True, "monthly": False, "yearly": False}
        _store(
            full,
            _point("incr-1", _stamp(-timedelta(days=10)), full=False, parent_point_id="full-1"),
        )

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(
                _job(gfs={"enabled": True, "weekly": 1, "monthly": 0, "yearly": 0}),
                vm_id="vm-1",
                repository_id="repo-1",
            )

        self.assertIn("full-1", BACKUPS)
        self.assertNotIn("incr-1", BACKUPS)

    def test_an_ordinary_chain_with_a_fresh_incremental_is_kept(self) -> None:
        _store(
            _point("full-1", _stamp(-timedelta(days=10)), full=True),
            _point("incr-1", _stamp(-timedelta(minutes=5)), full=False, parent_point_id="full-1"),
        )

        self._run_retention()

        self.assertEqual(sorted(BACKUPS), ["full-1", "incr-1"])


def _completed_backup_task(**result_overrides) -> dict:
    result = {
        "vm_name": "demo-vm",
        "vm_external_id": "vm-1",
        "backup_mode": "incremental",
        "incremental": True,
        "resolved_backup_type": "Incremental",
        "resolved_backup_reason": "StandardIncremental",
        "finished_at": _stamp(-timedelta(minutes=1)),
    }
    result.update(result_overrides)
    return {
        "id": "task-1",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": "job-1",
        "updated_at": _stamp(-timedelta(minutes=1)),
        "result": result,
    }


class APointAheadOfTheClockReachesTheSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        JOBS.clear()
        TASKS.clear()
        JOBS["job-1"] = _job(retention_days=36500)

    def tearDown(self) -> None:
        BACKUPS.clear()
        JOBS.clear()
        TASKS.clear()

    @staticmethod
    def _register(task: dict) -> None:
        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            backup_catalog.register_backup_from_completed_task(task)

    def test_the_session_and_the_journal_both_name_the_future_dated_point(self) -> None:
        _store(_point("full-1", _stamp(timedelta(days=30)), full=True))
        task = _completed_backup_task()

        with self.assertLogs("python.srv.services.backup_catalog", level="WARNING") as captured:
            self._register(task)

        session = abr_v41.session_to_dto(task)
        messages = [str(item.get("message") or "") for item in session["warnings"]]
        self.assertTrue(any("full-1" in message and "ahead of this server" in message for message in messages), messages)
        self.assertTrue(any(item.get("kind") == "ahead_of_server_clock" for item in session["warnings"]))
        self.assertTrue(any("full-1" in line for line in captured.output), captured.output)
        self.assertEqual(session["state"], "completed")

    def test_the_session_reports_an_incremental_dated_before_its_parent(self) -> None:
        _store(_point("full-1", _stamp(timedelta(days=30)), full=True))
        task = _completed_backup_task()

        with self.assertLogs("python.srv.services.backup_catalog", level="WARNING"):
            self._register(task)

        session = abr_v41.session_to_dto(task)
        reported = [item for item in session["warnings"] if item.get("kind") == "older_than_its_parent"]
        self.assertTrue(reported, session["warnings"])
        self.assertEqual(reported[0]["parent_point_id"], "full-1")
        messages = [str(item.get("message") or "") for item in session["warnings"]]
        self.assertTrue(any("earlier than the restore point it chains onto" in message for message in messages), messages)

    def test_the_session_log_carries_a_warning_entry_as_well(self) -> None:
        _store(_point("full-1", _stamp(timedelta(days=30)), full=True))
        task = _completed_backup_task()

        with self.assertLogs("python.srv.services.backup_catalog", level="WARNING"):
            self._register(task)

        session = abr_v41.session_to_dto(task)
        entries = [
            entry
            for entry in (session.get("logs") or [])
            if str(entry.get("level") or "").lower() in {"warn", "warning"}
        ]
        self.assertTrue(any("full-1" in str(entry.get("message")) for entry in entries), entries)

    def test_a_chain_written_on_a_sane_clock_adds_no_warning(self) -> None:
        _store(_point("full-1", _stamp(-timedelta(days=1)), full=True))
        task = _completed_backup_task()

        self._register(task)

        session = abr_v41.session_to_dto(task)
        self.assertEqual(session["warnings"], [])
        self.assertEqual(session["state"], "completed")
