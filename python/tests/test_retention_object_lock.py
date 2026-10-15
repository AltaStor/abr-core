# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, REPOSITORIES
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41

LOCKED = {"immutability": "governance", "retention_days": 1}
UNLOCKED = {"immutability": "none", "retention_days": 0}


def _job() -> dict:
    return {"id": "job-1", "name": "Hourly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 3, "gfs": {}}


def _point(point_id: str, created_at: str, *, full: bool, parent: str | None = None, expires: str | None = None) -> dict:
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
            "segment_id": "f1",
            "created_at": created_at,
            "immutability_expires_at": expires,
            "artifact": {"disks": [{"index": 1, "abr_path": f"prefix/job-1/vm-1/{point_id}", "abr_backup_name": "01-vm.vmdk"}]},
        }
    )


class RetentionUnderObjectLockTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, REPOSITORIES):
            store.clear()
            self.addCleanup(store.clear)
        abr_v41._RETENTION_LOCK_REPORTED.clear()
        self._use_repository(immutable=True)
        BACKUPS["f1"] = _point("f1", "2020-01-01T00:00:00+00:00", full=True)
        BACKUPS["i1"] = _point("i1", "2020-01-01T01:00:00+00:00", full=False, parent="f1")
        BACKUPS["i2"] = _point("i2", "2020-01-01T02:00:00+00:00", full=False, parent="i1")
        for name in ("persist_backup", "delete_backup"):
            patcher = patch.object(abr_v41, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def _use_repository(self, *, immutable: bool, kind: str = "s3") -> None:
        record = {"id": "repo-1", "name": "WORM", "type": kind, "path": "/srv/backups"}
        if kind == "s3":
            record["s3"] = {
                "bucket": "b",
                "prefix": "prefix",
                "immutability": {"mode": "governance" if immutable else "none", "retention_days": 1 if immutable else 0},
            }
        REPOSITORIES["repo-1"] = record

    def _pass(self, locks: dict, deleted: bool = True) -> tuple[list, list, list]:
        attempted: list[str] = []

        def info(repository, abr_path, backup_name, timeout=60):
            value = locks[abr_path.rsplit("/", 1)[-1]]
            if isinstance(value, Exception):
                raise value
            return value

        def delete(point):
            attempted.append(point["id"])
            return deleted

        with patch.object(abr_v41, "read_storage_backup_info", side_effect=info) as asked, patch.object(
            abr_v41, "_delete_backup_artifacts_from_point", side_effect=delete
        ):
            failures = abr_v41.apply_retention_to_chain(_job(), vm_id="vm-1", repository_id="repo-1")
        return failures, attempted, [call.args[1] for call in asked.call_args_list]

    def test_a_point_still_locked_is_kept_quietly_and_holds_the_older_ones(self) -> None:
        with self.assertLogs(abr_v41.LOGGER, level="INFO") as logs:
            first = self._pass({"i2": LOCKED, "i1": LOCKED, "f1": LOCKED})
            second = self._pass({"i2": LOCKED, "i1": LOCKED, "f1": LOCKED})

        for failures, attempted, _asked in (first, second):
            self.assertEqual(failures, [])
            self.assertEqual(attempted, [])
        kept = [line for line in logs.output if "while Object Lock holds it" in line]
        self.assertEqual(len(kept), 1)
        self.assertIn("i2", kept[0])
        self.assertIn("for 1 more day(s)", kept[0])
        self.assertFalse([line for line in logs.output if line.startswith("ERROR")])

    def test_once_the_lock_expires_the_segment_is_removed_newest_first(self) -> None:
        failures, attempted, _asked = self._pass({"i2": UNLOCKED, "i1": UNLOCKED, "f1": UNLOCKED})

        self.assertEqual(failures, [])
        self.assertEqual(attempted, ["i2", "i1", "f1"])
        self.assertEqual([call.args[0] for call in self.delete_backup.call_args_list], ["i2", "i1", "f1"])

    def test_a_point_that_cannot_be_removed_once_unlocked_is_still_a_failure(self) -> None:
        with self.assertLogs(abr_v41.LOGGER, level="ERROR"):
            failures, attempted, _asked = self._pass({"i2": UNLOCKED, "i1": UNLOCKED, "f1": UNLOCKED}, deleted=False)

        self.assertEqual([item["point_id"] for item in failures], ["i2"])
        self.assertEqual(attempted, ["i2"])

    def test_without_an_answer_from_the_tool_the_recorded_lock_decides(self) -> None:
        BACKUPS["i2"]["immutability_expires_at"] = "2999-01-01T00:00:00+00:00"
        failures, attempted, _asked = self._pass({"i2": StorageToolError("timeout"), "i1": UNLOCKED, "f1": UNLOCKED})
        self.assertEqual((failures, attempted), ([], []))

        BACKUPS["i2"]["immutability_expires_at"] = "2020-01-02T00:00:00+00:00"
        failures, attempted, _asked = self._pass({"i2": StorageToolError("timeout"), "i1": UNLOCKED, "f1": UNLOCKED})
        self.assertEqual((failures, attempted), ([], ["i2", "i1", "f1"]))

    def test_the_tool_overrides_a_recorded_lock_that_is_not_really_there(self) -> None:
        BACKUPS["i2"]["immutability_expires_at"] = "2999-01-01T00:00:00+00:00"

        failures, attempted, _asked = self._pass({"i2": UNLOCKED, "i1": UNLOCKED, "f1": UNLOCKED})

        self.assertEqual((failures, attempted), ([], ["i2", "i1", "f1"]))

    def test_a_locked_older_point_stops_the_pass_after_the_newer_ones_are_gone(self) -> None:
        failures, attempted, _asked = self._pass({"i2": UNLOCKED, "i1": LOCKED, "f1": LOCKED})

        self.assertEqual((failures, attempted), ([], ["i2"]))

    def test_a_bucket_default_lock_is_honoured_when_the_repository_sets_none(self) -> None:
        self._use_repository(immutable=False)

        failures, attempted, asked = self._pass({"i2": LOCKED, "i1": LOCKED, "f1": LOCKED})

        self.assertEqual((failures, attempted), ([], []))
        self.assertEqual(asked, ["prefix/job-1/vm-1/i2"])

    def test_a_local_repository_never_asks_about_locks(self) -> None:
        self._use_repository(immutable=False, kind="local")

        failures, attempted, asked = self._pass({})

        self.assertEqual((failures, attempted, asked), ([], ["i2", "i1", "f1"], []))


if __name__ == "__main__":
    unittest.main()
