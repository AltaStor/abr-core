# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41


IMMUTABLE = {"mode": "governance", "retention_days": 30}


class ImmutableChainDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self.tmp.cleanup()

    def _repository(self, *, repo_type: str = "s3", immutability: dict | None = None) -> None:
        record = {"id": "repo-1", "name": "Repo", "type": repo_type}
        if repo_type == "s3":
            record["s3"] = {"bucket": "bucket", "region": "us-east-1", "immutability": immutability}
        else:
            record["path"] = str(self.root)
        REPOSITORIES["repo-1"] = record

    def _point(self, point_id: str, created_at: str, *, full: bool, parent: str | None = None) -> Path:
        folder = self.root / point_id
        folder.mkdir()
        manifest = folder / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        BACKUPS[point_id] = {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "vm",
            "repository_id": "repo-1",
            "status": "success",
            "full_backup": full,
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent,
            "created_at": created_at,
            "artifact": {
                "backup_root": str(folder),
                "manifest_path": str(manifest),
                "disks": [{"abr_path": f"prefix/vm/{point_id}", "abr_backup_name": "01-vm.vhdx"}],
            },
        }
        return folder

    def _chain_id(self) -> str:
        return str(abr_v41.normalize_backup_point(BACKUPS["full-1"])["chain_id"])

    def _refuse(self, refused_paths: set[str]):
        removed: list[tuple[str, str]] = []

        def remove(repository, abr_path, name, timeout=120):
            if abr_path in refused_paths and name != "manifest":
                raise StorageToolError("deleteObject: InvalidRequest: Object is WORM protected and cannot be overwritten")
            removed.append((abr_path, name))

        return remove, removed

    def test_a_full_its_held_incremental_needs_is_kept_and_never_tried(self) -> None:
        self._repository(immutability=IMMUTABLE)
        full_folder = self._point("full-1", "2026-01-01T00:00:00Z", full=True)
        inc_folder = self._point("inc-1", "2026-01-02T00:00:00Z", full=False, parent="full-1")
        remove, removed = self._refuse({"prefix/vm/inc-1"})

        with patch.object(abr_v41, "remove_disk_tool_backup", side_effect=remove), patch.object(
            abr_v41, "_stored_backup_is_absent", return_value=False
        ):
            result = abr_v41.delete_backup_chain(self._chain_id())

        self.assertEqual(removed, [])
        self.assertIn("full-1", BACKUPS)
        self.assertIn("inc-1", BACKUPS)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["retained_restore_point_ids"], ["inc-1"])
        self.assertEqual(result["kept_for_chain_restore_point_ids"], ["full-1"])
        self.assertIn("1 older restore point(s) they need to restore are kept with them", result["message"])
        self.assertTrue((full_folder / "manifest.json").exists())
        self.assertTrue((inc_folder / "manifest.json").exists())

    def test_a_point_kept_under_object_lock_keeps_its_local_manifest(self) -> None:
        self._repository(immutability=IMMUTABLE)
        folder = self._point("full-1", "2026-01-01T00:00:00Z", full=True)
        remove, removed = self._refuse({"prefix/vm/full-1"})

        with patch.object(abr_v41, "remove_disk_tool_backup", side_effect=remove), patch.object(
            abr_v41, "_stored_backup_is_absent", return_value=False
        ):
            result = abr_v41.delete_backup_chain(self._chain_id())

        self.assertEqual(removed, [])
        self.assertIn("full-1", BACKUPS)
        self.assertEqual(result["retained_restore_point_ids"], ["full-1"])
        self.assertTrue((folder / "manifest.json").exists())

    def test_a_chain_the_storage_lets_go_is_removed_newest_first(self) -> None:
        self._repository(immutability=IMMUTABLE)
        full_folder = self._point("full-1", "2026-01-01T00:00:00Z", full=True)
        inc_folder = self._point("inc-1", "2026-01-02T00:00:00Z", full=False, parent="full-1")
        remove, removed = self._refuse(set())

        with patch.object(abr_v41, "remove_disk_tool_backup", side_effect=remove):
            result = abr_v41.delete_backup_chain(self._chain_id())

        self.assertTrue(result["deleted"])
        self.assertEqual(
            [path for path, name in removed if name != "manifest"],
            ["prefix/vm/inc-1", "prefix/vm/full-1"],
        )
        self.assertNotIn("full-1", BACKUPS)
        self.assertNotIn("inc-1", BACKUPS)
        self.assertFalse(full_folder.exists())
        self.assertFalse(inc_folder.exists())

    def test_without_immutability_a_refused_point_still_leaves_the_catalog_and_its_local_copy(self) -> None:
        self._repository(immutability=None)
        folder = self._point("full-1", "2026-01-01T00:00:00Z", full=True)
        remove, _ = self._refuse({"prefix/vm/full-1"})

        with patch.object(abr_v41, "remove_disk_tool_backup", side_effect=remove), patch.object(
            abr_v41, "_stored_backup_is_absent", return_value=False
        ):
            result = abr_v41.delete_backup_chain(self._chain_id())

        self.assertTrue(result["deleted"])
        self.assertNotIn("full-1", BACKUPS)
        self.assertFalse(folder.exists())

    def test_a_local_repository_still_wipes_its_folder_when_the_tool_fails(self) -> None:
        self._repository(repo_type="local")
        folder = self._point("full-1", "2026-01-01T00:00:00Z", full=True)
        BACKUPS["full-1"]["artifact"]["disks"][0]["abr_path"] = str(folder)
        remove, _ = self._refuse({str(folder)})

        with patch.object(abr_v41, "remove_disk_tool_backup", side_effect=remove), patch.object(
            abr_v41, "_stored_backup_is_absent", return_value=False
        ):
            removed_everything = abr_v41._delete_backup_artifacts_from_point(abr_v41.normalize_backup_point(BACKUPS["full-1"]))

        self.assertFalse(removed_everything)
        self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
