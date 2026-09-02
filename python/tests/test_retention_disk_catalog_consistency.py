# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.services import abr_v41, backup_catalog
from python.srv.services.api_errors import ApiError


def _expired_point(**overrides) -> dict:
    point = abr_v41.normalize_backup_point(
        {
            "id": "point-expired",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull",
            "created_at": "2020-01-01T00:00:00+00:00",
            "artifact": {"backup_root": "/does/not/matter"},
        }
    )
    point.update(overrides)
    return point


def _retention_job(**overrides) -> dict:
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


class ApplyRetentionKeepsCatalogInSyncWithDiskTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def test_a_pass_that_fails_to_delete_artifacts_keeps_the_catalog_entry(self) -> None:
        point = _expired_point()
        BACKUPS[point["id"]] = point
        job = _retention_job()

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=False):
            with patch("python.srv.services.abr_v41.delete_backup") as delete_backup_mock:
                abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        delete_backup_mock.assert_not_called()
        self.assertIn(point["id"], BACKUPS, "the catalog entry must survive a failed deletion so a later pass can retry")

    def test_a_pass_that_deletes_artifacts_successfully_still_removes_the_catalog_entry(self) -> None:
        point = _expired_point()
        BACKUPS[point["id"]] = point
        job = _retention_job()

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertNotIn(point["id"], BACKUPS)


def _chain_point(point_id: str, *, parent_point_id: str | None, created_at: str, full: bool) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent_point_id,
            "segment_id": "full-1",
            "created_at": created_at,
            "artifact": {"backup_root": f"/does/not/matter/{point_id}"},
        }
    )


class ApplyRetentionKeepsChainIntegrityWithinASegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def test_a_failed_incremental_keeps_its_still_needed_parent_full_too(self) -> None:
        full = _chain_point("full-1", parent_point_id=None, created_at="2020-01-01T00:00:00+00:00", full=True)
        incr = _chain_point("incr-1", parent_point_id="full-1", created_at="2020-01-02T00:00:00+00:00", full=False)
        BACKUPS[full["id"]] = full
        BACKUPS[incr["id"]] = incr
        job = _retention_job()

        def fake_delete(point: dict) -> bool:
            return point["id"] == "full-1"  # full's own artifacts would delete fine; the incremental's don't

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", side_effect=fake_delete):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertIn("incr-1", BACKUPS, "the incremental whose own deletion failed must stay")
        self.assertIn(
            "full-1",
            BACKUPS,
            "the full must not be removed while a still-cataloged incremental still depends on it, "
            "even though the full's own artifact deletion would have succeeded",
        )
        self.assertIn(BACKUPS["incr-1"]["parent_point_id"], BACKUPS, "no dangling parent_point_id")

    def test_a_fully_deletable_segment_is_still_removed_entirely(self) -> None:
        full = _chain_point("full-1", parent_point_id=None, created_at="2020-01-01T00:00:00+00:00", full=True)
        incr = _chain_point("incr-1", parent_point_id="full-1", created_at="2020-01-02T00:00:00+00:00", full=False)
        BACKUPS[full["id"]] = full
        BACKUPS[incr["id"]] = incr
        job = _retention_job()

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertNotIn("full-1", BACKUPS)
        self.assertNotIn("incr-1", BACKUPS)

    def test_the_newest_incremental_can_still_be_reclaimed_even_when_an_older_one_is_stuck(self) -> None:
        full = _chain_point("full-1", parent_point_id=None, created_at="2020-01-01T00:00:00+00:00", full=True)
        incr1 = _chain_point("incr-1", parent_point_id="full-1", created_at="2020-01-02T00:00:00+00:00", full=False)
        incr2 = _chain_point("incr-2", parent_point_id="incr-1", created_at="2020-01-03T00:00:00+00:00", full=False)
        for point in (full, incr1, incr2):
            BACKUPS[point["id"]] = point
        job = _retention_job()

        def fake_delete(point: dict) -> bool:
            return point["id"] != "incr-1"

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", side_effect=fake_delete):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertNotIn("incr-2", BACKUPS, "nothing depends on the newest point, so it can be reclaimed")
        self.assertIn("incr-1", BACKUPS, "its own deletion failed")
        self.assertIn("full-1", BACKUPS, "kept because an undeleted descendant (incr-1) still references it")
        self.assertIn(BACKUPS["incr-1"]["parent_point_id"], BACKUPS)


class DeleteBackupArtifactsTracksRealFailuresTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_path = Path(self._tmp.name)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Local", "type": "local", "path": str(self.repo_path)}

    def tearDown(self) -> None:
        REPOSITORIES.clear()

    def test_an_undeletable_backup_directory_is_reported_as_not_fully_removed(self) -> None:
        backup_root = self.repo_path / "vm-1" / "session-1"
        backup_root.mkdir(parents=True)
        (backup_root / "disk.vmdk").write_text("data", encoding="utf-8")

        point = abr_v41.normalize_backup_point(
            {
                "id": "point-locked",
                "repository_id": "repo-1",
                "vm_id": "vm-1",
                "execution_type": "ActiveFull",
                "created_at": "2020-01-01T00:00:00+00:00",
                "artifact": {"backup_root": str(backup_root)},
            }
        )

        with patch("python.srv.services.abr_v41.shutil.rmtree", side_effect=OSError("Operation not permitted")):
            all_removed = abr_v41._delete_backup_artifacts_from_point(point)

        self.assertFalse(all_removed, "a failed rmtree must not be reported as a successful deletion")
        self.assertTrue(backup_root.exists())


class RegisterBackupFromCompletedTaskRetentionFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        JOBS.clear()
        JOBS["job-1"] = _retention_job()

    def tearDown(self) -> None:
        BACKUPS.clear()
        JOBS.clear()

    def _task(self) -> dict:
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

    def test_an_operational_retention_failure_is_logged_and_does_not_fail_the_backup(self) -> None:
        with patch(
            "python.srv.services.backup_catalog.apply_retention_to_chain",
            side_effect=ApiError(503, "catalog_unavailable", "down"),
        ):
            with self.assertLogs("python.srv.services.backup_catalog", level="ERROR"):
                record = backup_catalog.register_backup_from_completed_task(self._task())

        self.assertIsNotNone(record)
        self.assertIn(record["id"], BACKUPS)

    def test_a_programming_error_in_retention_is_not_silently_swallowed(self) -> None:
        with patch(
            "python.srv.services.backup_catalog.apply_retention_to_chain",
            side_effect=TypeError("boom"),
        ):
            with self.assertRaises(TypeError):
                backup_catalog.register_backup_from_completed_task(self._task())


if __name__ == "__main__":
    unittest.main()
