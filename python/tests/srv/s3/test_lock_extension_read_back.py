# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41, backup_catalog

NOW = datetime(2026, 11, 21, 14, 47, tzinfo=timezone.utc)
FULL_END = "2026-11-22T14:46:00+00:00"
INCREMENTAL_END = "2026-11-24T14:47:00+00:00"


def _point(point_id: str, *, full: bool, disks: int = 1) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "repository_name": "WORM",
            "status": "available",
            "full_backup": full,
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": None if full else "f1",
            "segment_id": "f1",
            "created_at": "2026-11-21T14:46:00+00:00" if full else "2026-11-21T14:46:30+00:00",
            "immutability_expires_at": FULL_END,
            "artifact": {"disks": [{"index": n, "abr_path": f"prefix/vm-1/{point_id}", "abr_backup_name": f"0{n}-vm.vmdk"} for n in range(1, disks + 1)]},
        }
    )


def held(days: int, mode: str = "governance") -> dict:
    return {"immutability": mode, "retention_days": days}


class ReadBackTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "WORM",
            "type": "s3",
            "s3": {"bucket": "b", "prefix": "prefix", "immutability": {"mode": "governance", "retention_days": 3, "retention_scope": "minimum_only"}},
        }
        BACKUPS["f1"] = _point("f1", full=True)
        self.applied: dict[tuple[str, str], dict | None] = {}
        self.read: list[tuple[str, str]] = []
        self.answers: dict[str, dict | Exception] = {}
        for name, effect in (("apply_repository_immutability", self._apply), ("read_storage_backup_info", self._read), ("_now_utc", lambda: NOW)):
            patcher = patch.object(abr_v41, name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _apply(self, repository, abr_path, backup_name, **kwargs):
        return self.applied.get((abr_path.rsplit("/", 1)[-1], backup_name), {"status": "ok"})

    def _read(self, repository, abr_path, backup_name, **kwargs):
        point = abr_path.rsplit("/", 1)[-1]
        self.read.append((point, backup_name))
        answer = self.answers.get(point, held(3))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _extend(self, until: str = INCREMENTAL_END) -> dict:
        return abr_v41.extend_segment_object_lock("f1", expires_at=until, skip_point_ids={"new"})

    def test_a_full_the_storage_still_holds_for_one_day_is_reported_and_keeps_its_real_date(self) -> None:
        self.answers["f1"] = held(1)

        report = self._extend()

        self.assertEqual(report["extended_restore_points"], [])
        self.assertEqual(len(report["failures"]), 1)
        failure = report["failures"][0]
        self.assertEqual((failure["restore_point_id"], failure["backup_name"]), ("f1", "01-vm.vmdk"))
        self.assertIn("only 1 more day(s), not the 3", failure["message"])
        self.assertEqual(BACKUPS["f1"]["immutability_expires_at"], FULL_END)

    def test_a_full_held_as_asked_is_extended_and_recorded_as_before(self) -> None:
        report = self._extend()

        self.assertEqual(report["extended_restore_points"], ["f1"])
        self.assertEqual(report["failures"], [])
        self.assertEqual(BACKUPS["f1"]["immutability_expires_at"], INCREMENTAL_END)
        self.assertEqual(self.read, [("f1", "01-vm.vmdk")])

    def test_every_disk_of_the_full_is_read_back_and_the_manifest_is_not(self) -> None:
        BACKUPS["f1"] = _point("f1", full=True, disks=2)

        self._extend()

        self.assertEqual(self.read, [("f1", "01-vm.vmdk"), ("f1", "02-vm.vmdk")])

    def test_an_incremental_extended_normally_is_not_read_back(self) -> None:
        BACKUPS["i1"] = _point("i1", full=False)

        report = self._extend()

        self.assertEqual(sorted(report["extended_restore_points"]), ["f1", "i1"])
        self.assertNotIn(("i1", "01-vm.vmdk"), self.read)

    def test_an_incremental_whose_data_object_was_not_found_is_read_back(self) -> None:
        BACKUPS["i1"] = _point("i1", full=False)
        self.applied[("i1", "01-vm.vmdk")] = {"status": "ok", "metadata_only": True}
        self.answers["i1"] = held(1)

        report = self._extend()

        self.assertEqual([failure["restore_point_id"] for failure in report["failures"]], ["i1"])
        self.assertEqual(BACKUPS["i1"]["immutability_expires_at"], FULL_END)

    def test_an_empty_disk_without_a_data_object_but_held_as_asked_is_not_reported(self) -> None:
        BACKUPS["i1"] = _point("i1", full=False)
        self.applied[("i1", "01-vm.vmdk")] = {"status": "ok", "metadata_only": True}

        report = self._extend()

        self.assertEqual(report["failures"], [])
        self.assertIn("i1", report["extended_restore_points"])

    def test_a_manifest_held_only_as_metadata_does_not_cause_a_read(self) -> None:
        BACKUPS["i1"] = _point("i1", full=False)
        self.applied[("i1", "manifest")] = {"status": "ok", "metadata_only": True}

        self._extend()

        self.assertNotIn(("i1", "01-vm.vmdk"), self.read)

    def test_a_full_that_is_no_longer_held_at_all_says_so(self) -> None:
        self.answers["f1"] = {"immutability": "none"}

        report = self._extend()

        self.assertIn("does not hold it under Object Lock any more", report["failures"][0]["message"])

    def test_a_lock_that_cannot_be_read_back_is_reported_with_the_reason(self) -> None:
        self.answers["f1"] = StorageToolError("Connection refused")

        report = self._extend()

        self.assertIn("could not be read back after the extension: Connection refused", report["failures"][0]["message"])
        self.assertEqual(BACKUPS["f1"]["immutability_expires_at"], FULL_END)

    def test_a_compliance_lock_counts_as_held(self) -> None:
        self.answers["f1"] = held(3, "compliance")

        self.assertEqual(self._extend()["failures"], [])

    def test_a_date_just_past_a_whole_day_is_not_reported_for_the_rounding(self) -> None:
        until = (NOW + timedelta(days=3, seconds=120)).isoformat()

        self.assertEqual(self._extend(until)["failures"], [])

    def test_a_whole_day_short_is_reported(self) -> None:
        self.answers["f1"] = held(2)

        report = self._extend((NOW + timedelta(days=3, hours=1)).isoformat())

        self.assertIn("only 2 more day(s), not the 4", report["failures"][0]["message"])


def _task(now: datetime) -> dict:
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
                "lock_days": 3,
                "generation_started_at": now.isoformat(),
                "expires_at": (now + timedelta(days=3)).isoformat(),
                "starts_new_generation": True,
            },
        },
    }


class SessionWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        abr_v41._RETENTION_LOCK_REPORTED.clear()
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "WORM",
            "type": "s3",
            "s3": {"bucket": "b", "prefix": "prefix", "immutability": {"mode": "governance", "retention_days": 3, "retention_scope": "minimum_only"}},
        }
        JOBS["job-1"] = {"id": "job-1", "name": "Hourly", "job_type": "backup", "repo_id": "repo-1", "repo_name": "WORM", "retention_days": 30, "gfs": {}}
        BACKUPS["f1"] = _point("f1", full=True)

    def _register(self, **patches) -> dict:
        task = _task(datetime.now(timezone.utc))
        with patch.multiple(abr_v41, **patches), self.assertLogs(backup_catalog.logger, level="WARNING"):
            backup_catalog.register_backup_from_completed_task(task)
        return task

    def test_a_full_held_shorter_warns_the_session_with_the_command_that_holds_it_again(self) -> None:
        task = self._register(
            apply_repository_immutability=lambda *args, **kwargs: {"status": "ok"},
            read_storage_backup_info=lambda *args, **kwargs: held(1),
        )

        warning = abr_v41.session_to_dto(task)["warnings"][0]
        self.assertEqual(warning["restore_point_id"], "f1")
        self.assertIn("only 1 more day(s)", warning["message"])
        self.assertIn('abr repo apply-immutability --name "WORM"', warning["message"])
        self.assertEqual(task["status"], "completed")

    def test_an_unexpected_error_of_the_extension_reaches_the_session_too(self) -> None:
        task = _task(datetime.now(timezone.utc))
        with (
            patch.object(backup_catalog, "extend_segment_object_lock", side_effect=OSError("No such file or directory: 'abr-vmdk-tool'")),
            self.assertLogs(backup_catalog.logger, level="WARNING"),
        ):
            backup_catalog.register_backup_from_completed_task(task)

        warning = abr_v41.session_to_dto(task)["warnings"][0]
        self.assertIn("could not be extended", warning["message"])
        self.assertIn("No such file or directory", warning["message"])
        self.assertIn('abr repo apply-immutability --name "WORM"', warning["message"])

    def test_a_tool_refusal_keeps_its_warning_and_now_names_the_command(self) -> None:
        def refused(*args, **kwargs):
            raise StorageToolError("putObjectRetention: 'prefix/vm-1/f1/01-vm.vmdk.abri': AccessDenied: Access Denied.")

        task = self._register(apply_repository_immutability=refused)

        warning = abr_v41.session_to_dto(task)["warnings"][0]
        self.assertIn("AccessDenied", warning["message"])
        self.assertIn("apply-immutability", warning["message"])


if __name__ == "__main__":
    unittest.main()
