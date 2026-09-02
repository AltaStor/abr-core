# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, TASKS
from python.srv.services import abr_v41, backup_catalog
from python.srv.services.api_errors import ApiError

EXPIRED_POINT_ID = "point-expired"


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


def _expired_point() -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": EXPIRED_POINT_ID,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull",
            "created_at": "2020-01-01T00:00:00+00:00",
            "artifact": {"backup_root": "/does/not/matter"},
        }
    )


def _completed_backup_task() -> dict:
    return {
        "id": "task-1",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": "job-1",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "result": {
            "vm_name": "demo-vm",
            "vm_external_id": "vm-1",
            "backup_mode": "full",
            "resolved_backup_type": "ActiveFull",
            "resolved_backup_reason": "FirstRun",
            "finished_at": "2026-01-01T00:05:00+00:00",
        },
    }


class RetentionFailureReachesTheSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        JOBS.clear()
        TASKS.clear()
        JOBS["job-1"] = _job()
        point = _expired_point()
        BACKUPS[point["id"]] = point

    def tearDown(self) -> None:
        BACKUPS.clear()
        JOBS.clear()
        TASKS.clear()

    @staticmethod
    def _run(task: dict, *, deletes: bool) -> None:
        with patch(
            "python.srv.services.abr_v41._delete_backup_artifacts_from_point",
            return_value=deletes,
        ):
            backup_catalog.register_backup_from_completed_task(task)

    @staticmethod
    def _warning_levels(session: dict) -> list[dict]:
        logs = session.get("logs") if isinstance(session.get("logs"), list) else []
        return [
            entry
            for entry in logs
            if str(entry.get("level") or "").lower() in {"warn", "warning", "error"}
        ]

    def test_the_session_names_the_point_retention_could_not_remove(self) -> None:
        task = _completed_backup_task()
        self._run(task, deletes=False)

        session = abr_v41.session_to_dto(task)
        messages = [str(item.get("message") or "") for item in session["warnings"]]
        self.assertTrue(any(EXPIRED_POINT_ID in message for message in messages), messages)
        self.assertTrue(
            any(item.get("point_id") == EXPIRED_POINT_ID for item in session["warnings"])
        )

    def test_the_session_log_carries_the_failure_too(self) -> None:
        task = _completed_backup_task()
        self._run(task, deletes=False)

        entries = self._warning_levels(abr_v41.session_to_dto(task))
        self.assertTrue(entries)
        self.assertTrue(any(EXPIRED_POINT_ID in str(entry.get("message")) for entry in entries))

    def test_the_backup_still_succeeds_and_the_point_is_still_kept(self) -> None:
        task = _completed_backup_task()
        self._run(task, deletes=False)

        session = abr_v41.session_to_dto(task)
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["errors"], [])
        self.assertIn(EXPIRED_POINT_ID, BACKUPS)

    def test_a_pass_that_deletes_cleanly_leaves_no_warning(self) -> None:
        task = _completed_backup_task()
        self._run(task, deletes=True)

        session = abr_v41.session_to_dto(task)
        self.assertEqual(session["warnings"], [])
        self.assertEqual(self._warning_levels(session), [])
        self.assertNotIn(EXPIRED_POINT_ID, BACKUPS)

    def test_nothing_expired_leaves_no_warning(self) -> None:
        JOBS["job-1"] = _job(retention_days=36500)
        task = _completed_backup_task()
        self._run(task, deletes=False)

        self.assertEqual(abr_v41.session_to_dto(task)["warnings"], [])

    def test_a_retention_pass_that_raises_is_reported_to_the_session(self) -> None:
        task = _completed_backup_task()
        with patch(
            "python.srv.services.backup_catalog.apply_retention_to_chain",
            side_effect=ApiError(503, "catalog_unavailable", "down"),
        ):
            backup_catalog.register_backup_from_completed_task(task)

        warnings = abr_v41.session_to_dto(task)["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Retention pass failed", str(warnings[0].get("message")))

    def test_a_programming_error_in_retention_is_still_not_swallowed(self) -> None:
        task = _completed_backup_task()
        with patch(
            "python.srv.services.backup_catalog.apply_retention_to_chain",
            side_effect=TypeError("boom"),
        ):
            with self.assertRaises(TypeError):
                backup_catalog.register_backup_from_completed_task(task)

    def test_the_warning_is_recorded_on_the_caller_s_task(self) -> None:
        task = _completed_backup_task()
        self._run(task, deletes=False)

        self.assertTrue(task.get("warnings"))
        self.assertTrue(task.get("logs"))


class ApplyRetentionReportsItsFailuresTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        point = _expired_point()
        BACKUPS[point["id"]] = point

    def tearDown(self) -> None:
        BACKUPS.clear()

    def test_the_points_it_could_not_delete_are_returned(self) -> None:
        with patch(
            "python.srv.services.abr_v41._delete_backup_artifacts_from_point",
            return_value=False,
        ):
            failures = abr_v41.apply_retention_to_chain(
                _job(), vm_id="vm-1", repository_id="repo-1"
            )

        self.assertEqual([item["point_id"] for item in failures], [EXPIRED_POINT_ID])
        self.assertEqual(failures[0]["job_id"], "job-1")

    def test_a_clean_pass_returns_nothing(self) -> None:
        with patch(
            "python.srv.services.abr_v41._delete_backup_artifacts_from_point",
            return_value=True,
        ):
            with patch("python.srv.services.abr_v41.delete_backup"):
                failures = abr_v41.apply_retention_to_chain(
                    _job(), vm_id="vm-1", repository_id="repo-1"
                )

        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
