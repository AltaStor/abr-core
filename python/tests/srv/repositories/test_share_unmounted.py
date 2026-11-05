# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.plugins.vcenter import vcenter_backup
from python.srv.services import abr_v41, share_mount
from python.srv.services.api_errors import ApiError

UNMOUNTED = (
    "The share \\\\nas.example.test\\backups of repository Shared is not mounted at /mnt/hv: it is a folder on this "
    "appliance's own ext4 filesystem at /. Mount it there, then try again"
)


class UnmountedShareTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "hv"
        self.path.mkdir(mode=0o750)
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "Shared",
            "type": "smb",
            "path": str(self.path),
            "metadata": {"server": "nas.example.test", "share": "backups"},
        }
        BACKUPS["p1"] = {
            "id": "p1",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "vm",
            "repository_id": "repo-1",
            "status": "available",
            "full_backup": True,
            "execution_type": "ActiveFull",
            "created_at": "2026-01-01T00:00:00+00:00",
            "artifact": {"disks": [{"abr_path": str(self.path / "vm" / "t1"), "abr_backup_name": "01-disk0.vhdx"}]},
        }
        JOBS["job-1"] = {"id": "job-1", "name": "Job", "job_type": "backup", "repo_id": "repo-1", "retention_days": 1}
        unmounted = patch.object(abr_v41, "unmounted_share_message", return_value=UNMOUNTED)
        self.unmounted = unmounted.start()
        self.addCleanup(unmounted.stop)

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self._tmp.cleanup()

    def _chain_id(self) -> str:
        return str(abr_v41.normalize_backup_point(BACKUPS["p1"])["chain_id"])

    def test_the_repository_is_unavailable_with_the_cause_and_no_local_capacity(self) -> None:
        item = abr_v41.normalize_repository_record(REPOSITORIES["repo-1"])

        self.assertEqual(item["status"], "unavailable")
        self.assertEqual(item["storage"]["error"], UNMOUNTED)
        self.assertIsNone(item["capacity_bytes"])

    def test_a_mounted_share_stays_available(self) -> None:
        self.unmounted.return_value = None

        item = abr_v41.normalize_repository_record(REPOSITORIES["repo-1"])

        self.assertEqual(item["status"], "available")
        self.assertIsNotNone(item["capacity_bytes"])

    def test_a_delete_is_refused_and_the_points_stay(self) -> None:
        with patch.object(abr_v41, "_delete_backup_artifacts_from_point") as remove, self.assertRaises(ApiError) as caught:
            abr_v41.delete_backup_chain(self._chain_id())

        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "repository_share_not_mounted"))
        self.assertEqual(caught.exception.message, UNMOUNTED)
        remove.assert_not_called()
        self.assertIn("p1", BACKUPS)

    def test_retention_leaves_the_chain_and_names_the_cause(self) -> None:
        with patch.object(abr_v41, "_delete_backup_artifacts_from_point") as remove, self.assertLogs(abr_v41.LOGGER, "WARNING"):
            result = abr_v41.run_job_retention("job-1")

        chain = result["chains"][0]
        self.assertEqual((chain["removed"], chain["skipped_reason"], chain["repository_unavailable"]), ([], UNMOUNTED, UNMOUNTED))
        self.assertEqual(result["summary"]["unavailable"], 1)
        remove.assert_not_called()
        self.assertIn("p1", BACKUPS)

    def test_points_waiting_for_deletion_wait_for_the_share(self) -> None:
        BACKUPS["p1"]["status"] = "deleting"

        with patch.object(abr_v41, "_delete_backup_artifacts_from_point") as remove, self.assertLogs(abr_v41.LOGGER, "WARNING"):
            removed = abr_v41.retry_pending_deletions()

        self.assertEqual(removed, 0)
        remove.assert_not_called()
        self.assertIn("p1", BACKUPS)

    def test_a_rescan_does_not_mark_the_points_missing(self) -> None:
        with patch.object(abr_v41, "reconcile_repository_points") as reconcile, self.assertRaises(ApiError) as caught:
            abr_v41.rescan_repository("repo-1")

        self.assertEqual(caught.exception.code, "repository_share_not_mounted")
        reconcile.assert_not_called()
        self.assertEqual(BACKUPS["p1"]["status"], "available")

    def test_a_restore_is_refused(self) -> None:
        point = abr_v41.normalize_backup_point(BACKUPS["p1"])
        with patch.object(abr_v41, "_resolve_restore_source_point", return_value=point), self.assertRaises(ApiError) as caught:
            abr_v41.restore_start({"restore_point_id": "p1"})

        self.assertEqual(caught.exception.code, "repository_share_not_mounted")

    def test_a_backup_stops_before_it_makes_any_folder(self) -> None:
        with patch.object(share_mount, "unmounted_share_message", return_value=UNMOUNTED), self.assertRaises(
            share_mount.ShareNotMountedError
        ) as caught:
            vcenter_backup._resolve_backup_storage_layout(
                task={"id": "task-1", "job_id": "job-1"}, vm_name="vm", parameters={}, repository=REPOSITORIES["repo-1"]
            )

        self.assertEqual(str(caught.exception), UNMOUNTED)
        self.assertEqual(list(self.path.iterdir()), [])


class UnmountedShareMessageTests(unittest.TestCase):
    def test_the_message_names_the_share_the_folder_and_what_is_there(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mountinfo = Path(tmp) / "mountinfo"
            mountinfo.write_text("36 30 0:44 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-root rw\n", encoding="utf-8")
            repository = {"name": "Shared", "type": "smb", "path": "/mnt/hv", "metadata": {"server": "nas.example.test", "share": "backups"}}
            with patch.object(share_mount, "is_this_appliance", return_value=False), patch.object(
                Path, "resolve", lambda self, strict=False: self
            ):
                message = share_mount.unmounted_share_message(repository, mountinfo)
                mountinfo.write_text(
                    "36 30 0:44 / / rw - ext4 /dev/root rw\n37 36 0:45 / /mnt/hv rw - cifs //nas.example.test/backups rw\n",
                    encoding="utf-8",
                )
                mounted = share_mount.unmounted_share_message(repository, mountinfo)

        self.assertEqual(message, UNMOUNTED)
        self.assertIsNone(mounted)


if __name__ == "__main__":
    unittest.main()
