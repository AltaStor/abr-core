# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


def _gfs_point(point_id: str, *, repository_id: str = "repo-1", **overrides) -> dict:
    point = abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": repository_id,
            "execution_type": "ActiveFull",
            "created_at": "2026-01-01T00:00:00+00:00",
            "gfs_marks": {"weekly": True, "monthly": False, "yearly": False},
            "artifact": {},
        }
    )
    point.update(overrides)
    return point


class DeleteBackupChainGfsOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo", "type": "local", "path": "/tmp/does-not-matter"}

    def tearDown(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()

    def test_deleting_a_gfs_protected_chain_without_force_is_still_refused(self) -> None:
        point = _gfs_point("p1")
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with self.assertRaises(ApiError) as ctx:
            abr_v41.delete_backup_chain(chain_id)

        self.assertEqual(ctx.exception.code, "gfs_protected_backup")
        self.assertIn(point["id"], BACKUPS, "the refusal must not have touched the catalog entry")

    def test_force_deletes_a_gfs_protected_chain_and_says_so(self) -> None:
        point = _gfs_point("p1")
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            result = abr_v41.delete_backup_chain(chain_id, force=True)

        self.assertTrue(result["deleted"])
        self.assertTrue(result["gfs_protection_overridden"])
        self.assertNotIn(point["id"], BACKUPS)

    def test_force_on_a_chain_with_no_gfs_marks_is_a_plain_delete(self) -> None:
        point = abr_v41.normalize_backup_point(
            {
                "id": "p-plain",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "execution_type": "ActiveFull",
                "created_at": "2026-01-01T00:00:00+00:00",
                "artifact": {},
            }
        )
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            result = abr_v41.delete_backup_chain(chain_id, force=True)

        self.assertTrue(result["deleted"])
        self.assertNotIn("gfs_protection_overridden", result)

    def test_force_still_succeeds_when_the_repository_directory_is_already_gone(self) -> None:
        point = _gfs_point("p1", artifact={"backup_root": "/no/such/directory/at/all"})
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        result = abr_v41.delete_backup_chain(chain_id, force=True)

        self.assertTrue(result["deleted"])
        self.assertNotIn(point["id"], BACKUPS)

    def test_force_does_not_bypass_the_unrelated_chain_consistency_check(self) -> None:
        point = _gfs_point("p1", execution_type="Incremental", parent_point_id=None)
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with self.assertRaises(ApiError) as ctx:
            abr_v41.delete_backup_chain(chain_id, force=True)

        self.assertEqual(ctx.exception.code, "inconsistent_backup_chain")

    def test_force_deleting_the_last_protected_chain_unblocks_repository_removal(self) -> None:
        point = _gfs_point("p1")
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with self.assertRaises(ApiError):
            abr_v41.remove_repository_record("repo-1")

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            abr_v41.delete_backup_chain(chain_id, force=True)

        removal = abr_v41.remove_repository_record("repo-1")
        self.assertTrue(removal["removed"])


class DeleteBackupApiForceParamTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo", "type": "local", "path": "/tmp/does-not-matter"}

    def tearDown(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()

    def test_the_rest_handler_threads_force_through_to_the_service_layer(self) -> None:
        from python.srv.api.backups import delete_backup

        point = _gfs_point("p1")
        BACKUPS[point["id"]] = point
        chain_id = point["chain_id"]

        with patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True):
            result = delete_backup(chain_id, segment=None, force=True)

        self.assertTrue(result["gfs_protection_overridden"])


if __name__ == "__main__":
    unittest.main()
