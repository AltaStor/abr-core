# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41, backup_catalog

REASON = "putObjectRetention: 'prefix/job-1/vm-1/f1/01-vm.vmdk.abri': NoSuchKey: The specified key does not exist."


def _point(point_id: str, created_at: str, *, full: bool, segment: str, parent: str | None = None) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "status": "available",
            "full_backup": full,
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent,
            "segment_id": segment,
            "created_at": created_at,
            "immutability_expires_at": created_at,
            "artifact": {"disks": [{"index": 1, "abr_path": f"prefix/job-1/vm-1/{point_id}", "abr_backup_name": "01-vm.vmdk"}]},
        }
    )


def _incremental_task() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": "task-new",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": "job-1",
        "updated_at": now.isoformat(),
        "result": {
            "vm_name": "vm",
            "vm_external_id": "vm-1",
            "repository_id": "repo-1",
            "backup_mode": "incremental",
            "resolved_backup_type": "Incremental",
            "resolved_backup_reason": "StandardIncremental",
            "parent_point_id": "f1",
            "finished_at": now.isoformat(),
            "immutability_generation": {
                "lock_days": 2,
                "generation_started_at": now.isoformat(),
                "expires_at": (now + timedelta(days=2)).isoformat(),
                "starts_new_generation": True,
            },
        },
    }


class FailedLockExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        abr_v41._RETENTION_LOCK_REPORTED.clear()
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "WORM",
            "type": "s3",
            "s3": {"bucket": "b", "prefix": "prefix", "immutability": {"mode": "governance", "retention_days": 1}},
        }
        JOBS["job-1"] = {"id": "job-1", "name": "Hourly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 1, "gfs": {}}
        BACKUPS["old"] = _point("old", "2020-01-01T00:00:00+00:00", full=True, segment="old")
        BACKUPS["f1"] = _point("f1", "2020-06-01T00:00:00+00:00", full=True, segment="f1")
        BACKUPS["i1"] = _point("i1", "2020-06-01T01:00:00+00:00", full=False, segment="f1", parent="f1")
        self.locked: list[tuple[str, str]] = []
        for name, kwargs in (
            ("apply_repository_immutability", {"side_effect": self._lock}),
            ("read_storage_backup_info", {"return_value": {"immutability": "none", "retention_days": 0}}),
            ("_delete_backup_artifacts_from_point", {"return_value": True}),
            ("delete_backup", {}),
        ):
            patcher = patch.object(abr_v41, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def _lock(self, repository, abr_path, backup_name, **kwargs) -> None:
        self.locked.append((abr_path.rsplit("/", 1)[-1], backup_name))
        if abr_path.endswith("/f1") and backup_name == "01-vm.vmdk":
            raise StorageToolError(REASON)

    def _register(self) -> tuple[dict, dict | None, list[str]]:
        task = _incremental_task()
        with self.assertLogs(backup_catalog.logger, level="WARNING") as logs:
            record = backup_catalog.register_backup_from_completed_task(task)
        return task, record, logs.output

    def test_the_session_gets_the_warning_with_the_reason_and_the_point_is_returned(self) -> None:
        task, record, _logs = self._register()

        self.assertIsNotNone(record)
        self.assertEqual(record["parent_point_id"], "f1")
        warnings = abr_v41.session_to_dto(task)["warnings"]
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual((warning["restore_point_id"], warning["backup_name"]), ("f1", "01-vm.vmdk"))
        self.assertIn("NoSuchKey", warning["reason"])
        self.assertIn("Object Lock could not be extended", warning["message"])
        self.assertIn("NoSuchKey", warning["message"])
        self.assertTrue(any("NoSuchKey" in str(entry.get("message")) for entry in task["logs"] if entry.get("level") == "warn"))

    def test_the_server_log_names_the_point_and_the_reason(self) -> None:
        _task, _record, logs = self._register()

        line = next(line for line in logs if "Could not extend the Object Lock" in line)
        self.assertIn("f1", line)
        self.assertIn("01-vm.vmdk", line)
        self.assertIn("NoSuchKey", line)

    def test_the_rest_of_the_segment_is_still_extended(self) -> None:
        self._register()

        self.assertIn(("i1", "01-vm.vmdk"), self.locked)
        self.assertNotEqual(BACKUPS["i1"]["immutability_expires_at"], "2020-06-01T01:00:00+00:00")
        self.assertEqual(BACKUPS["f1"]["immutability_expires_at"], "2020-06-01T00:00:00+00:00")

    def test_the_retention_pass_still_runs_after_the_failed_extension(self) -> None:
        self._register()

        self.assertEqual([call.args[0] for call in self.delete_backup.call_args_list], ["old"])


if __name__ == "__main__":
    unittest.main()
