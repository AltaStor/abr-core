# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


class RetentionAcrossASyntheticFullTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        self.addCleanup(BACKUPS.clear)

    @staticmethod
    def _point(point_id: str, execution_type: str, created_at: str, parent: str | None) -> dict:
        return abr_v41.normalize_backup_point(
            {
                "id": point_id,
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "execution_type": execution_type,
                "execution_reason": "SyntheticFullWindow"
                if execution_type == "SyntheticFull"
                else "ActiveFullWindow",
                "created_at": created_at,
                "parent_point_id": parent,
                "full_backup": execution_type in {"ActiveFull", "SyntheticFull"},
                "artifact": {"backup_root": "/does/not/matter"},
            }
        )

    def test_the_merged_chain_ages_out_without_touching_the_synthetic_full(self) -> None:
        points = [
            self._point("old-full", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("old-incr", "Incremental", "2020-01-02T00:00:00+00:00", "old-full"),
            self._point("synthetic", "SyntheticFull", "2020-01-03T00:00:00+00:00", None),
            self._point("new-incr", "Incremental", "2020-01-04T00:00:00+00:00", "synthetic"),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=0)

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertNotIn("old-full", BACKUPS)
        self.assertNotIn("old-incr", BACKUPS)

    def test_a_synthetic_full_that_is_still_in_retention_keeps_its_own_increment(self) -> None:
        recent = datetime.now(timezone.utc)
        points = [
            self._point("old-full", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("synthetic", "SyntheticFull", recent.isoformat(), None),
            self._point(
                "new-incr",
                "Incremental",
                (recent + timedelta(minutes=1)).isoformat(),
                "synthetic",
            ),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=7)

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertNotIn("old-full", BACKUPS)
        self.assertIn("synthetic", BACKUPS)
        self.assertIn("new-incr", BACKUPS)

    def test_several_synthetic_cycles_keep_only_what_retention_still_needs(self) -> None:
        points = [
            self._point("full-1", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("incr-1", "Incremental", "2020-01-02T00:00:00+00:00", "full-1"),
            self._point("synth-1", "SyntheticFull", "2020-01-03T00:00:00+00:00", None),
            self._point("incr-2", "Incremental", "2020-01-04T00:00:00+00:00", "synth-1"),
            self._point("synth-2", "SyntheticFull", "2020-01-05T00:00:00+00:00", None),
            self._point("incr-3", "Incremental", "2020-01-06T00:00:00+00:00", "synth-2"),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=0)

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertEqual(BACKUPS, {}, "every segment was expired, so none may survive")

    def test_only_the_expired_cycles_go_and_the_newest_one_stays_restorable(self) -> None:
        recent = datetime.now(timezone.utc)
        points = [
            self._point("full-1", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("incr-1", "Incremental", "2020-01-02T00:00:00+00:00", "full-1"),
            self._point("synth-1", "SyntheticFull", "2020-01-03T00:00:00+00:00", None),
            self._point("incr-2", "Incremental", "2020-01-04T00:00:00+00:00", "synth-1"),
            self._point("synth-2", "SyntheticFull", recent.isoformat(), None),
            self._point(
                "incr-3", "Incremental", (recent + timedelta(minutes=1)).isoformat(), "synth-2"
            ),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=7)

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertEqual(sorted(BACKUPS), ["incr-3", "synth-2"])

    def test_what_survives_still_resolves_into_a_complete_restore_chain(self) -> None:
        from python.srv.plugins.vcenter import vcenter_restore

        recent = datetime.now(timezone.utc)
        points = [
            self._point("full-1", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("incr-1", "Incremental", "2020-01-02T00:00:00+00:00", "full-1"),
            self._point("synth-1", "SyntheticFull", recent.isoformat(), None),
            self._point(
                "incr-2", "Incremental", (recent + timedelta(minutes=1)).isoformat(), "synth-1"
            ),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=7)

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        chain = vcenter_restore._resolve_point_chain(BACKUPS["incr-2"])

        self.assertEqual([item["id"] for item in chain], ["synth-1", "incr-2"])

    def test_a_synthetic_segment_expires_exactly_like_an_active_full_one(self) -> None:
        def run(base_type: str) -> list[str]:
            BACKUPS.clear()
            recent = datetime.now(timezone.utc)
            for point in (
                self._point("old-full", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
                self._point("base", base_type, recent.isoformat(), None),
                self._point(
                    "incr", "Incremental", (recent + timedelta(minutes=1)).isoformat(), "base"
                ),
            ):
                BACKUPS[point["id"]] = point
            with patch(
                "python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True
            ):
                abr_v41.apply_retention_to_chain(
                    _retention_job(retention_days=7), vm_id="vm-1", repository_id="repo-1"
                )
            return sorted(BACKUPS)

        self.assertEqual(run("SyntheticFull"), run("ActiveFull"))

    def test_a_gfs_protected_synthetic_full_outlives_its_own_expired_increments(self) -> None:
        points = [
            self._point("synth-1", "SyntheticFull", "2020-01-01T00:00:00+00:00", None),
            self._point("incr-1", "Incremental", "2020-01-02T00:00:00+00:00", "synth-1"),
            self._point("incr-2", "Incremental", "2020-01-03T00:00:00+00:00", "synth-1"),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=0, gfs={"weekly": 4, "monthly": 0, "yearly": 0, "enabled": True})

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertEqual(sorted(BACKUPS), ["synth-1"])

    def test_a_gfs_protected_old_chain_survives_a_newer_synthetic_full(self) -> None:
        recent = datetime.now(timezone.utc)
        points = [
            self._point("old-full", "ActiveFull", "2020-01-01T00:00:00+00:00", None),
            self._point("synth-1", "SyntheticFull", recent.isoformat(), None),
        ]
        for point in points:
            BACKUPS[point["id"]] = point
        job = _retention_job(retention_days=0, gfs={"weekly": 4, "monthly": 0, "yearly": 0, "enabled": True})

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.apply_retention_to_chain(job, vm_id="vm-1", repository_id="repo-1")

        self.assertIn("old-full", BACKUPS, "a GFS keeper must survive even after a synthetic full replaced it as the base")
        self.assertIn("synth-1", BACKUPS)


if __name__ == "__main__":
    unittest.main()
