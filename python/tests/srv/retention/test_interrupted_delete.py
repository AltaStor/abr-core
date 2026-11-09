# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.services import abr_v41


class _Crash(BaseException):
    pass


class InterruptedDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES, TASKS)])
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Local", "type": "local", "path": str(self.root)}
        JOBS["job-1"] = {"id": "job-1", "name": "Nightly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 1, "gfs": {}}
        self._point("full-1", "2020-01-01T00:00:00+00:00", None)
        self._point("inc-1", "2020-01-02T00:00:00+00:00", "full-1")
        self._point("inc-2", "2020-01-03T00:00:00+00:00", "inc-1")

    def _point(self, point_id: str, created_at: str, parent: str | None) -> None:
        folder = self.root / "vm" / point_id
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        BACKUPS[point_id] = {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "vm",
            "repository_id": "repo-1",
            "status": "success",
            "full_backup": parent is None,
            "execution_type": "ActiveFull" if parent is None else "Incremental",
            "parent_point_id": parent,
            "created_at": created_at,
            "artifact": {
                "backup_root": str(folder),
                "manifest_path": str(folder / "manifest.json"),
                "disks": [{"disk_key": 1, "abr_path": str(folder), "abr_backup_name": "01-vm.vhdx"}],
            },
        }

    def _chain_id(self) -> str:
        return str(abr_v41.normalize_backup_point(BACKUPS["full-1"])["chain_id"])

    def _crash_on(self, point_id: str):
        seen: dict[str, str] = {}

        def delete(point):
            seen[str(point["id"])] = str(BACKUPS[str(point["id"])].get("status"))
            if point["id"] == point_id:
                raise _Crash()
            return True

        return patch.object(abr_v41, "_delete_backup_artifacts_from_point", side_effect=delete), seen

    def test_a_chain_delete_marks_each_point_deleting_before_removing_its_data(self) -> None:
        crash, seen = self._crash_on("inc-1")
        with crash, self.assertRaises(_Crash):
            abr_v41.delete_backup_chain(self._chain_id())

        self.assertEqual(seen, {"inc-2": "deleting", "inc-1": "deleting"})
        self.assertNotIn("inc-2", BACKUPS)
        self.assertEqual(BACKUPS["inc-1"]["status"], "deleting")
        self.assertTrue(BACKUPS["inc-1"]["deletion_requested_at"])
        self.assertEqual(BACKUPS["full-1"]["status"], "success")

    def test_retention_marks_each_point_deleting_before_removing_its_data(self) -> None:
        crash, seen = self._crash_on("inc-1")
        with crash, self.assertRaises(_Crash):
            abr_v41.run_job_retention("job-1")

        self.assertEqual(seen.get("inc-1"), "deleting")
        self.assertEqual(BACKUPS["inc-1"]["status"], "deleting")
        self.assertEqual(BACKUPS["full-1"]["status"], "success")

    def test_a_point_a_crash_left_deleting_is_refused_for_restore(self) -> None:
        crash, _seen = self._crash_on("inc-2")
        with crash, self.assertRaises(_Crash):
            abr_v41.delete_backup_chain(self._chain_id())
        point = abr_v41.normalize_backup_point(BACKUPS["inc-2"])

        with patch.object(abr_v41, "_resolve_restore_source_point", return_value=point), self.assertRaises(abr_v41.ApiError) as caught:
            abr_v41.restore_start({"restore_point_id": "inc-2"})

        self.assertIn("Restore point is being deleted: its removal started on", caught.exception.message)
        self.assertNotIn("Object Lock", caught.exception.message)

    def test_verify_skips_a_point_waiting_for_deletion_and_checks_the_rest(self) -> None:
        crash, _seen = self._crash_on("inc-2")
        with crash, self.assertRaises(_Crash):
            abr_v41.delete_backup_chain(self._chain_id())

        with patch.object(abr_v41, "_verify_disk_artifact", return_value={"disk_key": 1, "status": "ok", "message": "ok"}) as checked:
            report = abr_v41.verify_backup_chain(self._chain_id())

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["checked_restore_points"], 2)
        self.assertEqual(sorted(call.args[0]["id"] for call in checked.call_args_list), ["full-1", "inc-1"])
        self.assertEqual(
            report["warnings"],
            [{"point_id": "inc-2", "message": "Restore point is waiting for deletion and is not offered for restore, so it was not verified"}],
        )

    def test_the_server_finishes_the_point_it_was_removing_and_keeps_the_others(self) -> None:
        crash, _seen = self._crash_on("inc-2")
        with crash, self.assertRaises(_Crash):
            abr_v41.delete_backup_chain(self._chain_id())

        with patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True):
            removed = abr_v41.retry_pending_deletions(datetime.now(timezone.utc))

        self.assertEqual(removed, 1)
        self.assertEqual(sorted(BACKUPS), ["full-1", "inc-1"])
        self.assertEqual({BACKUPS[point_id]["status"] for point_id in BACKUPS}, {"success"})

    def test_an_uninterrupted_delete_and_retention_still_remove_everything(self) -> None:
        with patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True):
            result = abr_v41.delete_backup_chain(self._chain_id())

        self.assertTrue(result["deleted"])
        self.assertEqual(list(BACKUPS), [])
